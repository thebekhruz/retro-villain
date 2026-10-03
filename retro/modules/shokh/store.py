"""Закуп Шоха: журнал покупок под отчёт.

Деньги из кассы уходят один раз — когда бухгалтер выдаёт подотчёт
(`procurement_advance`). Записи этого модуля кассу больше не трогают: они
объясняют, на что ушла уже выданная сумма. Поэтому «на руках» здесь считается
как выдано минус записанные покупки, а бухгалтерский подотчёт уменьшается
отдельно, когда бухгалтер принимает накладную.
"""
import json
import sqlite3
from collections import Counter
from contextlib import closing, nullcontext
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from retro.db import as_database, table_columns
from retro.runtime import secure_directory, secure_file

# Телефонное фото редко больше пяти мегабайт; ограничение защищает базу от
# случайной загрузки видео или архива.
MAX_PHOTO_BYTES = 6 * 1024 * 1024
ALLOWED_PHOTO_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif')
# Единицы для товара не из справочника iiko (§3a: кг, шт, л, пучок). У товаров
# iiko единица приходит из их карточки.
UNITS = ('кг', 'шт', 'л', 'уп', 'пучок')
# Точки закупа — заведения, для которых Шох закупает (макет 3a, «Функционал»
# §3a): всегда эти четыре и в этом порядке, чтобы рука находила их на том же
# месте. Точки из прошлых покупок добавляются после них.
DEFAULT_POINTS = ('Школа MU', 'Школа YA', 'RETRO', 'ШЕФ Базаар')
# «Цена выше обычной больше чем на 10%» — внимание бухгалтеру (§4). До +10% —
# «в норме»: рыночная цена гуляет, и каждую копейку сверху проверять незачем.
ABOVE_USUAL = Decimal('1.10')


# Раньше этой даты закупа в системе не было; нижняя граница выборок «за всё время».
FIRST_DAY = date(2020, 1, 1)


class ShokhError(Exception):
    """Ошибка ввода, которую нужно показать Шоху словами."""


def cash_amount(total) -> Decimal:
    """Сколько наличных ушло за покупку — целые сумы, половина вверх.

    Тийинов в наличных нет. Накладная iiko хранит точный итог (3 × 33 333,33 =
    99 999,99), а из кармана Шоха ушло 100 000. Все денежные цифры — на руках,
    потрачено, подотчёт, резервы, касса, учредитель — считают по этой сумме.
    Единственное место правила: колонка `cash_total` заполняется только отсюда.
    """
    return Decimal(total).quantize(Decimal(1), rounding=ROUND_HALF_UP).quantize(Decimal('0.01'))


def _median(prices: list[Decimal]) -> Decimal | None:
    """Медиана, а не среднее: одна случайная дорогая покупка не должна поднимать
    «обычную» цену для всех следующих."""
    if not prices:
        return None
    prices = sorted(prices)
    middle = len(prices) // 2
    if len(prices) % 2:
        return prices[middle]
    return ((prices[middle - 1] + prices[middle]) / 2).quantize(Decimal('0.01'))


# Колонки покупки в порядке, который читает ShokhStore._json.
PURCHASE_COLUMNS = ('id, trip_id, day, created_at, point, item, unit, quantity, price, total, '
                    'usual_price, photo IS NOT NULL, photo_type, accepted_at, cash_total, off_catalog, '
                    'iiko_refs')


def pocket_position(shokh, finance, day: date) -> dict:
    """Сколько наличных у Шоха на руках — одно место для всех экранов.

    С ТЗ 02.10 (п. 5) Шох сам ничего не вносит: остаток ведёт бухгалтер на
    странице «Баланс Шохруха» — выделено − расходы по счёт-фактуре. Поэтому
    на руках = подотчёт по бухгалтерии (`reserves.shoh.balance`); покупки,
    записанные когда-то с телефона и не принятые, в него больше не входят —
    иначе расход, внесённый бухгалтером по счёт-фактуре, посчитался бы дважды.
    Счёт без начального остатка начинается с нуля (reserves._entries).

    Считают по этой формуле экран бухгалтера, кабинет учредителя и Шох.
    Правило про деньги должно жить в одном файле, иначе копии разойдутся.
    """
    reserve = finance.reserves(day)['shoh']
    accounting = reserve['balance']
    pocket = None if accounting is None else Decimal(accounting)
    given = sum((Decimal(row['amount']) for row in reserve['entries'] if row['kind'] == 'deposit'),
                Decimal(0))
    spent = sum((Decimal(row['amount']) for row in reserve['entries'] if row['kind'] == 'withdrawal'),
                Decimal(0))
    start = None if pocket is None else pocket - given + spent
    # «Отчитались за X%» = потрачено / (на начало + выдано).
    base = None if pocket is None else start + given
    reported = (None if base is None else 0 if base <= 0 else
                int((spent * 100 / base).quantize(Decimal(1), rounding=ROUND_HALF_UP)))
    return dict(accounting_balance=accounting,
                pocket=None if pocket is None else str(pocket),
                pending='0', day_start=None if start is None else str(start),
                given_today=str(given), spent_day=str(spent), reported_percent=reported)


def _money(value, *, name='сумма', quantum='0.01'):
    try:
        amount = Decimal(str(value).replace(',', '.').strip())
    except (InvalidOperation, AttributeError):
        raise ShokhError(f'Укажите {name} числом.') from None
    if not amount.is_finite() or amount <= 0:
        raise ShokhError(f'Укажите {name} больше нуля.')
    if amount > Decimal('1000000000'):
        raise ShokhError(f'Слишком большая {name}.')
    rounded = amount.quantize(Decimal(quantum), rounding=ROUND_HALF_UP)
    if rounded <= 0:
        raise ShokhError(f'Слишком маленькая {name}.')
    return rounded


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
            # cash_total — наличные за покупку в целых сумах (cash_amount);
            # off_catalog — товара нет в справочнике iiko, накладную проводит
            # бухгалтер руками; client_key — ключ повторной отправки такой
            # покупки; iiko_refs — поставщик, склад и товар iiko (JSON).
            columns = table_columns(connection, 'shokh_purchases')
            for column, kind in (('cash_total', 'TEXT'), ('off_catalog', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('client_key', 'TEXT'), ('iiko_refs', 'TEXT')):
                if column not in columns:
                    connection.execute(f'ALTER TABLE shokh_purchases ADD COLUMN {column} {kind}')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS shokh_purchases_client_key '
                               'ON shokh_purchases(client_key)')
            # Старые покупки: наличные — тот же итог, округлённый до сума.
            for purchase_id, total in connection.execute(
                    'SELECT id, total FROM shokh_purchases WHERE cash_total IS NULL').fetchall():
                connection.execute('UPDATE shokh_purchases SET cash_total = ? WHERE id = ?',
                                   (str(cash_amount(total)), purchase_id))
            connection.commit()

    def _open(self):
        return self.db.connect()

    # ── Поездки ────────────────────────────────────────────────────────────
    def open_trip(self, day: date, at: datetime) -> int:
        """Незакрытая поездка дня или новая: время закупа считается по ней."""
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

    def cancel_trip(self, trip_id: int) -> bool:
        """Закрыли закуп, ничего не купив: поездки не было. Покупки удалить так
        нельзя — поездку с ними только завершают."""
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute(
                'DELETE FROM shokh_trips WHERE id = ? AND finished_at IS NULL AND NOT EXISTS '
                '(SELECT 1 FROM shokh_purchases WHERE trip_id = ?)', (trip_id, trip_id))
            connection.commit()
            return cursor.rowcount > 0

    def trip(self, trip_id: int) -> dict | None:
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT id, day, started_at, finished_at FROM shokh_trips WHERE id = ?',
                (trip_id,)).fetchone()
        return dict(id=row[0], day=row[1], started_at=row[2], finished_at=row[3]) if row else None

    def trips(self, day: date) -> list[dict]:
        return self.trips_between(day, day)

    def trips_between(self, first: date, last: date) -> list[dict]:
        with closing(self._open()) as connection:
            return [dict(id=row[0], day=row[1], started_at=row[2], finished_at=row[3])
                    for row in connection.execute(
                        'SELECT id, day, started_at, finished_at FROM shokh_trips '
                        'WHERE day >= ? AND day <= ? ORDER BY id', (first.isoformat(), last.isoformat()))]

    # ── Покупки ────────────────────────────────────────────────────────────
    def add_purchase(self, day: date, at: datetime, *, point, item, unit, quantity, price,
                     photo=None, photo_type=None, trip_id=None, _connection=None, iiko_unit=False,
                     off_catalog=False, client_key=None, refs=None) -> dict:
        point = _text(point, name='точку закупа', limit=80)
        item = _text(item, name='товар', limit=120)
        if not iiko_unit and unit not in UNITS:
            raise ShokhError('Выберите единицу измерения.')
        # Товар не из iiko вводят тем же экраном, что и товар iiko: до тысячных.
        count = _money(quantity, name='количество',
                       quantum='0.001' if iiko_unit or off_catalog else '0.01')
        unit_price = _money(price, name='цену')
        if off_catalog and (count * unit_price) > Decimal('1000000000'):
            raise ShokhError('Слишком большая сумма покупки.')
        if photo is not None:
            if len(photo) > MAX_PHOTO_BYTES:
                raise ShokhError('Фото больше 6 МБ — переснимите поменьше.')
            if photo_type not in ALLOWED_PHOTO_TYPES:
                raise ShokhError('Фото должно быть картинкой.')
        total = (count * unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        usual = self.usual_price(item, before=day)
        with (nullcontext(_connection) if _connection else closing(self._open())) as connection:
            if client_key is not None:
                # Ответ на прошлую отправку потерялся, и Шох нажал «Повторить»:
                # та же покупка, а не вторая.
                found = connection.execute('SELECT id, item, quantity, price FROM shokh_purchases '
                                           'WHERE client_key = ?', (client_key,)).fetchone()
                if found:
                    if (found[1], Decimal(found[2]), Decimal(found[3])) != (item, count, unit_price):
                        raise ShokhError('Этот ключ уже использован для другой покупки.')
                    return self.purchase(found[0])
            if trip_id is not None:
                trip = connection.execute('SELECT day, finished_at FROM shokh_trips WHERE id=?', (trip_id,)).fetchone()
                if not trip or trip[0] != day.isoformat() or trip[1] is not None:
                    raise ShokhError('Закуп уже закрыт или относится к другому дню. Начните новый.')
            cursor = connection.execute(
                'INSERT INTO shokh_purchases (trip_id, day, created_at, point, item, unit, '
                'quantity, price, total, usual_price, photo, photo_type, cash_total, off_catalog, '
                'client_key, iiko_refs) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (trip_id, day.isoformat(), at.isoformat(), point, item, unit,
                 str(count), str(unit_price), str(total),
                 str(usual) if usual is not None else None,
                 sqlite3.Binary(photo) if photo else None,
                 photo_type if photo else None, str(cash_amount(total)), 1 if off_catalog else 0,
                 client_key, json.dumps(refs, ensure_ascii=False) if refs else None))
            if _connection is not None:
                return cursor.lastrowid
            connection.commit()
            return self.purchase(cursor.lastrowid)

    def purchase(self, purchase_id: int) -> dict | None:
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT ' + PURCHASE_COLUMNS + ' '
                'FROM shokh_purchases WHERE id = ?', (purchase_id,)).fetchone()
        return self._json(row) if row else None

    def purchases(self, day: date) -> list[dict]:
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT ' + PURCHASE_COLUMNS + ' '
                'FROM shokh_purchases WHERE day = ? ORDER BY created_at, id',
                (day.isoformat(),)).fetchall()
        return [self._json(row) for row in rows]

    def purchases_between(self, first: date, last: date) -> list[dict]:
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT ' + PURCHASE_COLUMNS + ' '
                'FROM shokh_purchases WHERE day >= ? AND day <= ? ORDER BY day, created_at, id',
                (first.isoformat(), last.isoformat())).fetchall()
        return [self._json(row) for row in rows]

    def purchase_by_key(self, client_key: str) -> dict | None:
        with closing(self._open()) as connection:
            row = connection.execute('SELECT id FROM shokh_purchases WHERE client_key = ?',
                                     (client_key,)).fetchone()
        return self.purchase(row[0]) if row else None

    @staticmethod
    def _json(row) -> dict:
        usual = Decimal(row[10]) if row[10] is not None else None
        price = Decimal(row[8])
        # Дороже обычного больше чем на 10% — повод бухгалтеру проверить, а не запрет.
        above = usual is not None and price > usual * ABOVE_USUAL
        try:
            refs = json.loads(row[16]) if row[16] else {}
        except ValueError:
            refs = {}
        # total — наличные (целые сумы): его складывают все экраны денег.
        # invoice_total — точный итог накладной iiko, с тийинами.
        return dict(id=row[0], trip_id=row[1], day=row[2], created_at=row[3], point=row[4],
                    item=row[5], unit=row[6], quantity=row[7], price=row[8],
                    total=row[14] if row[14] is not None else str(cash_amount(row[9])),
                    invoice_total=row[9], off_catalog=bool(row[15]),
                    supplier=refs.get('supplier'), storage=refs.get('storage'),
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

    def accept_with_finance(self, purchase_id, day, at, finance):
        from retro.modules.accountant.reserves import add_reserve_entry
        with closing(self._open()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            claimed = c.execute('UPDATE shokh_purchases SET accepted_at=? '
                                'WHERE id=? AND accepted_at IS NULL', (at.isoformat(), purchase_id))
            if not claimed.rowcount:
                return False
            # Из подотчёта уходят наличные — целые сумы, а не итог накладной с тийинами.
            row = c.execute('SELECT day,cash_total,item,point FROM shokh_purchases WHERE id=?', (purchase_id,)).fetchone()
            if day.isoformat() < row[0]:
                raise ShokhError('Нельзя принять покупку раньше даты закупа.')
            add_reserve_entry(finance, day, 'shoh', 'withdrawal', row[1],
                              f'Закуп: {row[2]} · {row[3]}', None, existing_connection=c)
        return True

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
        return _median(prices)

    def frequent_items(self, limit: int = 12, *, today: date | None = None) -> list[dict]:
        """Частые товары с обычной ценой: экран сравнивает с ней ещё до отправки."""
        reference = today or date.today()
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT item, unit, COUNT(*) AS times, MAX(day) FROM shokh_purchases '
                'GROUP BY item, unit ORDER BY times DESC, MAX(day) DESC, item, unit LIMIT ?', (limit,)).fetchall()
        result = []
        for item, unit, times, last_day in rows:
            # Обычную цену берём на завтра от последней покупки, иначе сегодняшние
            # записи выпали бы из выборки `day < before` и цены бы не было.
            usual = self.usual_price(item, before=reference + timedelta(days=1))
            result.append(dict(item=item, unit=unit, times=times, last_day=last_day,
                               usual_price=str(usual) if usual is not None else None))
        return result

    def item_history(self, *, days: int = 365, limit: int = 400, today: date | None = None) -> list[dict]:
        """Что и где Шох покупал: для «Часто покупаете» и обычной цены.

        Товар узнаём по id номенклатуры iiko из накладной — название в iiko
        могут поправить, а id останется. Покупки без id (до связи с iiko и
        товары не из справочника) узнаём по названию. `points` — сколько раз
        товар брали на каждой точке: на экране точки её товары идут первыми.
        """
        reference = today or date.today()
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT point, item, unit, price, day, iiko_refs, off_catalog FROM shokh_purchases '
                'WHERE day >= ? ORDER BY day DESC, id DESC',
                ((reference - timedelta(days=days)).isoformat(),)).fetchall()
        groups = {}
        for point, item, unit, price, day, refs, off_catalog in rows:
            try:
                product = (json.loads(refs) if refs else {}).get('product_id')
            except (ValueError, AttributeError):
                product = None
            key = ('id', product) if product else ('name', item.strip().casefold())
            group = groups.get(key)
            if group is None:
                # Строки идут от новых к старым: название и единица — последние.
                group = groups[key] = dict(product_id=product, item=item, unit=unit, times=0,
                                           points=Counter(), last_day=day, prices=[],
                                           off_catalog=bool(off_catalog))
            group['times'] += 1
            group['points'][point] += 1
            if len(group['prices']) < 8:
                group['prices'].append(Decimal(price))
        # Чаще — выше; при равенстве — что брали недавно, потом по алфавиту.
        result = sorted(groups.values(), key=lambda g: g['item'])
        result.sort(key=lambda g: g['last_day'], reverse=True)
        result.sort(key=lambda g: -g['times'])
        return [dict(product_id=g['product_id'], item=g['item'], unit=g['unit'], times=g['times'],
                     points=dict(g['points']), last_day=g['last_day'], off_catalog=g['off_catalog'],
                     usual_price=str(_median(g['prices']))) for g in result[:limit]]

    def point_defaults(self, limit: int = 300) -> dict:
        """Поставщик и склад последней покупки по каждой точке: нажал точку — и
        сразу к товару (§3a), без повторного выбора из справочников iiko."""
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT point, iiko_refs FROM shokh_purchases WHERE iiko_refs IS NOT NULL '
                'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        result = {}
        for point, refs in rows:
            if point in result:
                continue
            try:
                data = json.loads(refs)
            except ValueError:
                continue
            if data.get('supplier_id') and data.get('storage_id'):
                result[point] = dict(supplier_id=data['supplier_id'], storage_id=data['storage_id'])
        return result

    def points(self) -> list[str]:
        with closing(self._open()) as connection:
            seen = [row[0] for row in connection.execute(
                'SELECT point, COUNT(*) AS times FROM shokh_purchases '
                'GROUP BY point ORDER BY times DESC, MAX(day) DESC')]
        return list(dict.fromkeys([*DEFAULT_POINTS, *seen]))
