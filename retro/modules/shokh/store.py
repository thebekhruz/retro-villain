"""Закуп Шоха: журнал покупок под отчёт.

Деньги из кассы уходят один раз — когда бухгалтер выдаёт подотчёт
(`procurement_advance`). Записи этого модуля кассу больше не трогают: они
объясняют, на что ушла уже выданная сумма. Поэтому «на руках» здесь считается
как выдано минус записанные покупки, а бухгалтерский подотчёт уменьшается
отдельно, когда бухгалтер принимает накладную.
"""
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from retro.db import as_database
from retro.runtime import secure_directory, secure_file

# Телефонное фото редко больше пяти мегабайт; ограничение защищает базу от
# случайной загрузки видео или архива.
MAX_PHOTO_BYTES = 6 * 1024 * 1024
ALLOWED_PHOTO_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'image/heic')
UNITS = ('кг', 'шт', 'л', 'уп')
# Точки закупа: список пополняется сам из истории, но начинать с пустого экрана
# неудобно, поэтому базовые заведены сразу.
DEFAULT_POINTS = ('Базар', 'Оптовый склад', 'Магазин', 'Поставщик')


# Раньше этой даты закупа в системе не было; нижняя граница выборок «за всё время».
FIRST_DAY = date(2020, 1, 1)


class ShokhError(Exception):
    """Ошибка ввода, которую нужно показать Шоху словами."""


def pocket_position(shokh, finance, day: date) -> dict:
    """Сколько наличных у Шоха на руках — одно место для всех экранов.

    Бухгалтерский подотчёт (`reserves.shoh.balance`) = выдано − принятые
    накладные. Покупки, которые Шох записал, но бухгалтер ещё не принял, из
    подотчёта не вычтены, хотя денег на руках уже нет. Поэтому:

        на руках = подотчёт по бухгалтерии − непринятые покупки

    Считают по этой формуле и экран закупа, и кабинет учредителя. Правило про
    деньги должно жить в одном файле, иначе копии со временем разойдутся.
    """
    accounting = finance.reserves(day)['shoh']['balance']
    # Непринятым может быть и вчерашнее, поэтому смотрим всю историю до дня.
    history = shokh.purchases_between(FIRST_DAY, day)
    pending = sum((Decimal(row['total']) for row in history if row['accepted_at'] is None),
                  Decimal(0))
    return dict(accounting_balance=accounting,
                pocket=None if accounting is None else str(Decimal(accounting) - pending),
                pending=str(pending))


def _money(value, *, name='сумма'):
    try:
        amount = Decimal(str(value).replace(',', '.').strip())
    except (InvalidOperation, AttributeError):
        raise ShokhError(f'Укажите {name} числом.') from None
    if not amount.is_finite() or amount <= 0:
        raise ShokhError(f'Укажите {name} больше нуля.')
    if amount > Decimal('1000000000'):
        raise ShokhError(f'Слишком большая {name}.')
    return amount.quantize(Decimal('0.01'))


def _text(value, *, name, limit=120):
    text = (value or '').strip()
    if not text:
        raise ShokhError(f'Укажите {name}.')
    if len(text) > limit:
        raise ShokhError(f'Слишком длинное значение: {name}.')
    return text


class ShokhStore:
    def __init__(self, path):
        self.db = as_database(path)
        # .path остаётся для скриптов обслуживания и тестов
        self.path = self.db.path
        with closing(self._open()) as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS shokh_trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS shokh_purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trip_id INTEGER REFERENCES shokh_trips(id),
                    day TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    point TEXT NOT NULL,
                    item TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    total TEXT NOT NULL,
                    usual_price TEXT,
                    photo BLOB,
                    photo_type TEXT,
                    accepted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS shokh_purchases_day ON shokh_purchases(day);
                CREATE INDEX IF NOT EXISTS shokh_purchases_item ON shokh_purchases(item);
                CREATE INDEX IF NOT EXISTS shokh_trips_day ON shokh_trips(day);
            ''')

    def _open(self):
        return self.db.connect()

    # ── Поездки ────────────────────────────────────────────────────────────
    def open_trip(self, day: date, at: datetime) -> int:
        """Незакрытая поездка дня или новая. Бонус за скорость считается по ней."""
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT id FROM shokh_trips WHERE day = ? AND finished_at IS NULL '
                'ORDER BY id DESC LIMIT 1', (day.isoformat(),)).fetchone()
            if row:
                connection.commit()
                return row[0]
            cursor = connection.execute(
                'INSERT INTO shokh_trips (day, started_at) VALUES (?, ?)',
                (day.isoformat(), at.isoformat()))
            connection.commit()
            return cursor.lastrowid

    def finish_trip(self, trip_id: int, at: datetime) -> None:
        with closing(self._open()) as connection:
            connection.execute('UPDATE shokh_trips SET finished_at = ? '
                               'WHERE id = ? AND finished_at IS NULL',
                               (at.isoformat(), trip_id))
            connection.commit()

    def trip(self, trip_id: int) -> dict | None:
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT id, day, started_at, finished_at FROM shokh_trips WHERE id = ?',
                (trip_id,)).fetchone()
        return dict(id=row[0], day=row[1], started_at=row[2], finished_at=row[3]) if row else None

    def trips(self, day: date) -> list[dict]:
        with closing(self._open()) as connection:
            return [dict(id=row[0], day=row[1], started_at=row[2], finished_at=row[3])
                    for row in connection.execute(
                        'SELECT id, day, started_at, finished_at FROM shokh_trips '
                        'WHERE day = ? ORDER BY id', (day.isoformat(),))]

    # ── Покупки ────────────────────────────────────────────────────────────
    def add_purchase(self, day: date, at: datetime, *, point, item, unit, quantity, price,
                     photo=None, photo_type=None, trip_id=None) -> dict:
        point = _text(point, name='точку закупа', limit=80)
        item = _text(item, name='товар', limit=120)
        if unit not in UNITS:
            raise ShokhError('Выберите единицу измерения.')
        count = _money(quantity, name='количество')
        unit_price = _money(price, name='цену')
        if photo is not None:
            if len(photo) > MAX_PHOTO_BYTES:
                raise ShokhError('Фото больше 6 МБ — переснимите поменьше.')
            if photo_type not in ALLOWED_PHOTO_TYPES:
                raise ShokhError('Фото должно быть картинкой.')
        total = (count * unit_price).quantize(Decimal('0.01'))
        usual = self.usual_price(item, before=day)
        with closing(self._open()) as connection:
            cursor = connection.execute(
                'INSERT INTO shokh_purchases (trip_id, day, created_at, point, item, unit, '
                'quantity, price, total, usual_price, photo, photo_type) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (trip_id, day.isoformat(), at.isoformat(), point, item, unit,
                 str(count), str(unit_price), str(total),
                 str(usual) if usual is not None else None,
                 sqlite3.Binary(photo) if photo else None,
                 photo_type if photo else None))
            connection.commit()
            return self.purchase(cursor.lastrowid)

    def purchase(self, purchase_id: int) -> dict | None:
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT id, trip_id, day, created_at, point, item, unit, quantity, price, total, '
                'usual_price, photo IS NOT NULL, photo_type, accepted_at '
                'FROM shokh_purchases WHERE id = ?', (purchase_id,)).fetchone()
        return self._json(row) if row else None

    def purchases(self, day: date) -> list[dict]:
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, trip_id, day, created_at, point, item, unit, quantity, price, total, '
                'usual_price, photo IS NOT NULL, photo_type, accepted_at '
                'FROM shokh_purchases WHERE day = ? ORDER BY created_at, id',
                (day.isoformat(),)).fetchall()
        return [self._json(row) for row in rows]

    def purchases_between(self, first: date, last: date) -> list[dict]:
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, trip_id, day, created_at, point, item, unit, quantity, price, total, '
                'usual_price, photo IS NOT NULL, photo_type, accepted_at '
                'FROM shokh_purchases WHERE day >= ? AND day <= ? ORDER BY day, created_at, id',
                (first.isoformat(), last.isoformat())).fetchall()
        return [self._json(row) for row in rows]

    @staticmethod
    def _json(row) -> dict:
        usual = Decimal(row[10]) if row[10] is not None else None
        price = Decimal(row[8])
        # Дороже обычного — повод бухгалтеру проверить, а не запрет.
        above = usual is not None and price > usual
        return dict(id=row[0], trip_id=row[1], day=row[2], created_at=row[3], point=row[4],
                    item=row[5], unit=row[6], quantity=row[7], price=row[8], total=row[9],
                    usual_price=row[10], has_photo=bool(row[11]), photo_type=row[12],
                    accepted_at=row[13],
                    price_above_usual=above,
                    price_delta_percent=(str((price / usual - 1) * 100) if above and usual else None))

    def photo(self, purchase_id: int) -> tuple[bytes, str] | None:
        with closing(self._open()) as connection:
            row = connection.execute('SELECT photo, photo_type FROM shokh_purchases WHERE id = ?',
                                     (purchase_id,)).fetchone()
        if not row or row[0] is None:
            return None
        return bytes(row[0]), row[1] or 'application/octet-stream'

    def accept(self, purchase_id: int, at: datetime) -> bool:
        with closing(self._open()) as connection:
            cursor = connection.execute(
                'UPDATE shokh_purchases SET accepted_at = ? WHERE id = ? AND accepted_at IS NULL',
                (at.isoformat(), purchase_id))
            connection.commit()
            return cursor.rowcount > 0

    # ── Справочники, которые копятся сами ──────────────────────────────────
    def usual_price(self, item: str, *, before: date, window: int = 8) -> Decimal | None:
        """Медиана прошлых цен товара. Медиана, а не среднее: одна случайная
        дорогая покупка не должна поднимать «обычную» цену для всех следующих."""
        with closing(self._open()) as connection:
            prices = [Decimal(row[0]) for row in connection.execute(
                'SELECT price FROM shokh_purchases WHERE item = ? AND day < ? '
                'ORDER BY day DESC, id DESC LIMIT ?',
                (item.strip(), before.isoformat(), window))]
        if not prices:
            return None
        prices.sort()
        middle = len(prices) // 2
        if len(prices) % 2:
            return prices[middle]
        return ((prices[middle - 1] + prices[middle]) / 2).quantize(Decimal('0.01'))

    def frequent_items(self, limit: int = 12, *, today: date | None = None) -> list[dict]:
        """Частые товары с обычной ценой: экран сравнивает с ней ещё до отправки."""
        reference = today or date.today()
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT item, unit, COUNT(*) AS times, MAX(day) FROM shokh_purchases '
                'GROUP BY item ORDER BY times DESC, MAX(day) DESC LIMIT ?', (limit,)).fetchall()
        result = []
        for item, unit, times, last_day in rows:
            # Обычную цену берём на завтра от последней покупки, иначе сегодняшние
            # записи выпали бы из выборки `day < before` и цены бы не было.
            usual = self.usual_price(item, before=reference + timedelta(days=1))
            result.append(dict(item=item, unit=unit, times=times, last_day=last_day,
                               usual_price=str(usual) if usual is not None else None))
        return result

    def points(self) -> list[str]:
        with closing(self._open()) as connection:
            seen = [row[0] for row in connection.execute(
                'SELECT point, COUNT(*) AS times FROM shokh_purchases '
                'GROUP BY point ORDER BY times DESC, MAX(day) DESC')]
        return list(dict.fromkeys([*seen, *DEFAULT_POINTS]))
