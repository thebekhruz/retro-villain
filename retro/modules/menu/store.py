"""Меню в нашей базе: позиции, журнал прогонов и срок следующей синхронизации.

Почему справочник хранится у нас, а не читается из iiko на каждый вопрос: iiko
— дефицитный ресурс (см. комментарии в `integrations/iiko.py`), а меню меняется
раз в недели, не в минуты. Поэтому недельный снимок лежит здесь, а панель
читает его из базы.

Исчезнувшую из iiko позицию не удаляем, а помечаем `missing_since`: по ней
остаются продажи в отчётах, и «блюдо пропало из номенклатуры» — это факт,
который надо видеть, а не потерянная строка.
"""
from contextlib import closing
from decimal import Decimal, InvalidOperation

from retro.db import as_database

SCHEMA = '''
    CREATE TABLE IF NOT EXISTS menu_items (
        product_id TEXT PRIMARY KEY,
        code TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '',
        group_id TEXT NOT NULL DEFAULT '',
        group_path TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        unit TEXT NOT NULL DEFAULT '',
        price TEXT,
        in_menu INTEGER,
        deleted INTEGER NOT NULL DEFAULT 0,
        search TEXT NOT NULL DEFAULT '',
        group_search TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        synced_at TEXT NOT NULL,
        missing_since TEXT
    );
    CREATE INDEX IF NOT EXISTS menu_items_group ON menu_items(group_path, name);
    CREATE TABLE IF NOT EXISTS menu_sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        items INTEGER NOT NULL DEFAULT 0,
        added INTEGER NOT NULL DEFAULT 0,
        changed INTEGER NOT NULL DEFAULT 0,
        missing INTEGER NOT NULL DEFAULT 0,
        returned INTEGER NOT NULL DEFAULT 0,
        priced INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        error TEXT
    );
    CREATE TABLE IF NOT EXISTS menu_sync_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        next_run_at TEXT NOT NULL,
        last_success_at TEXT
    );
'''

VISIBLE = ('code', 'name', 'kind', 'group_id', 'group_path', 'category', 'unit',
           'price', 'in_menu', 'deleted')
# `search` и `group_search` — те же название, код и путь, свёрнутые в нижний
# регистр при записи. Иначе поиск «вино» не находит «Вино»: LOWER() в SQLite
# знает только латиницу, а меню у нас русское.
FIELDS = (*VISIBLE, 'search', 'group_search')
ROW = ('product_id', *VISIBLE, 'first_seen_at', 'updated_at', 'synced_at', 'missing_since')
# Журнал прогонов: недели, а не минуты, поэтому хранить много незачем.
RUNS_KEPT = 60


class MenuError(RuntimeError):
    """Ошибка данных справочника, которую нужно показать словами."""


def money_text(value):
    """Деньги в базе лежат текстом; больше двух знаков — это уже не сум."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise MenuError('Некорректная цена позиции меню.') from None
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise MenuError('Некорректная цена позиции меню.')
    return str(amount)


def flag(value):
    """`in_menu`: 1 — в меню, 0 — нет, NULL — iiko не сказал."""
    return None if value is None else int(bool(value))


class MenuStore:
    def __init__(self, path):
        self.db = as_database(path)
        # .path остаётся для скриптов обслуживания и тестов
        self.path = self.db.path
        with closing(self._open()) as connection:
            connection.executescript(SCHEMA)

    def _open(self):
        return self.db.connect()

    # ── Позиции ────────────────────────────────────────────────────────────

    def save(self, items, *, at):
        """Снимок номенклатуры → база. Одна транзакция, без удалений.

        Возвращает, что именно изменилось: это единственный способ увидеть, что
        недельный прогон действительно что-то принёс, а не записал то же самое.
        """
        incoming = {}
        for item in items:
            product_id = str(item['product_id'])
            if not product_id:
                raise MenuError('Позиция меню без идентификатора iiko.')
            if product_id in incoming:
                raise MenuError('iiko вернул две карточки с одним идентификатором.')
            if not str(item.get('name', '')).strip():
                raise MenuError('Позиция меню без названия.')
            code, name = str(item.get('code', '')), str(item['name'])
            group_path = str(item.get('group_path', ''))
            incoming[product_id] = (
                code, name, str(item.get('kind', '')),
                str(item.get('group_id', '')), group_path,
                str(item.get('category', '')), str(item.get('unit', '')),
                money_text(item.get('price')), flag(item.get('in_menu')),
                int(bool(item.get('deleted'))),
                f'{name} {code}'.casefold(), group_path.casefold())
        price_at = FIELDS.index('price')
        counts = dict(items=len(incoming), added=0, changed=0, missing=0, returned=0,
                      priced=sum(1 for row in incoming.values() if row[price_at] is not None))
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                stored = {row[0]: (tuple(row[1:1 + len(FIELDS)]), row[-1])
                          for row in connection.execute(
                              'SELECT product_id, ' + ', '.join(FIELDS)
                              + ', missing_since FROM menu_items')}
                for product_id, values in incoming.items():
                    existing = stored.get(product_id)
                    if existing is None:
                        counts['added'] += 1
                        connection.execute(
                            'INSERT INTO menu_items (product_id, ' + ', '.join(FIELDS)
                            + ', first_seen_at, updated_at, synced_at) VALUES ('
                            + ','.join('?' for _ in range(len(FIELDS) + 4)) + ')',
                            (product_id, *values, at, at, at))
                        continue
                    previous, missing_since = existing
                    changed = previous != values
                    counts['changed'] += int(changed)
                    counts['returned'] += int(missing_since is not None)
                    # `updated_at` двигаем только когда карточка правда
                    # изменилась: иначе «когда позиция менялась последний раз»
                    # превратится в «когда был прогон», и увидеть, что цена
                    # поехала, станет невозможно. Когда прогон был — synced_at.
                    sets = ', '.join(f'{name}=?' for name in FIELDS)
                    connection.execute(
                        'UPDATE menu_items SET ' + sets + (', updated_at=?' if changed else '')
                        + ', synced_at=?, missing_since=NULL WHERE product_id=?',
                        (*values, *((at,) if changed else ()), at, product_id))
                gone = [product_id for product_id, (_, missing_since) in stored.items()
                        if product_id not in incoming and missing_since is None]
                counts['missing'] = len(gone)
                for product_id in gone:
                    connection.execute('UPDATE menu_items SET missing_since=?, synced_at=? '
                                       'WHERE product_id=?', (at, at, product_id))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return counts

    def items(self, *, scope='menu', include_missing=False, query='', group='', limit=None):
        """Сохранённое меню. `scope='all'` — вся номенклатура, включая склад."""
        if scope not in ('menu', 'all'):
            raise MenuError('Неизвестный срез меню.')
        where, params = [], []
        if scope == 'menu':
            # Меню — это то, что продают: блюда, модификаторы и услуги целиком,
            # а из товаров только те, что iiko отметил в меню или с ценой
            # (ингредиенты склада тоже товары, и в меню их быть не должно).
            where.append("(kind <> 'GOODS' OR in_menu = 1 OR (in_menu IS NULL AND price IS NOT NULL))")
            where.append('deleted = 0')
        if not include_missing:
            where.append('missing_since IS NULL')
        if query:
            where.append('search LIKE ?')
            params.append(f'%{query.casefold()}%')
        if group:
            where.append('group_search LIKE ?')
            params.append(f'%{group.casefold()}%')
        sql = ('SELECT ' + ', '.join(ROW) + ' FROM menu_items'
               + (' WHERE ' + ' AND '.join(where) if where else '')
               + ' ORDER BY group_path, name')
        if limit is not None:
            # Потолок ответа: вся номенклатура — это и складские товары тоже,
            # а отдавать их одним куском никому не нужно. Сколько всего — в
            # `count()`, поэтому обрезание видно, а не выглядит как «меню кончилось».
            sql += ' LIMIT ?'
            params.append(max(1, int(limit)))
        with closing(self._open()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    @staticmethod
    def _item(row):
        item = dict(zip(ROW, row))
        item['in_menu'] = None if item['in_menu'] is None else bool(item['in_menu'])
        item['deleted'] = bool(item['deleted'])
        return item

    def count(self):
        """Сколько позиций лежит в базе — для сводки без выгрузки всего меню."""
        with closing(self._open()) as connection:
            total, missing = connection.execute(
                'SELECT COUNT(*), COUNT(missing_since) FROM menu_items').fetchone()
        return dict(total=int(total or 0), missing=int(missing or 0))

    # ── Расписание и журнал ────────────────────────────────────────────────

    def state(self):
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT next_run_at, last_success_at FROM menu_sync_state WHERE id = 1').fetchone()
        return dict(next_run_at=row[0], last_success_at=row[1]) if row else None

    def claim(self, now, interval, *, force=False):
        """Занять прогон: первый — сразу, дальше не чаще одного раза в `interval`.

        Срок живёт в базе, а не в памяти процесса: иначе каждый выкат заново
        дёргал бы iiko, а расписание сбрасывалось бы на день релиза. Здесь же
        лежит защита от двух одновременных прогонов — срок сдвигается вперёд до
        того, как начнётся чтение.
        """
        planned = (now + interval).isoformat()
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute(
                    'SELECT next_run_at FROM menu_sync_state WHERE id = 1').fetchone()
                if row is None:
                    # База пустая — значит меню ещё ни разу не грузили: первый
                    # прогон идёт сейчас, следующий через неделю от сегодня.
                    connection.execute(
                        'INSERT INTO menu_sync_state (id, next_run_at) VALUES (1, ?)', (planned,))
                    connection.commit()
                    return True
                if not force and now.isoformat() < row[0]:
                    connection.rollback()
                    return False
                connection.execute('UPDATE menu_sync_state SET next_run_at=? WHERE id = 1', (planned,))
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def reschedule(self, at):
        """Сдвинуть следующий прогон — после неудачи на короткую повторную попытку."""
        with closing(self._open()) as connection:
            connection.execute(
                'INSERT INTO menu_sync_state (id, next_run_at) VALUES (1, ?) '
                'ON CONFLICT(id) DO UPDATE SET next_run_at=excluded.next_run_at', (at,))
            connection.commit()

    def start_run(self, at):
        with closing(self._open()) as connection:
            run_id = self.db.insert_returning_id(
                connection, "INSERT INTO menu_sync_runs (started_at, status) VALUES (?, 'running')",
                (at,))
            connection.commit()
        return run_id

    def finish_run(self, run_id, status, *, at, counts=None, note='', error=None):
        if status not in ('ok', 'failed'):
            raise MenuError('Неизвестный итог прогона меню.')
        counts = counts or {}
        with closing(self._open()) as connection:
            connection.execute(
                'UPDATE menu_sync_runs SET finished_at=?, status=?, items=?, added=?, changed=?, '
                'missing=?, returned=?, priced=?, note=?, error=? WHERE id=?',
                (at, status, counts.get('items', 0), counts.get('added', 0),
                 counts.get('changed', 0), counts.get('missing', 0), counts.get('returned', 0),
                 counts.get('priced', 0), note[:500], error[:500] if error else None, run_id))
            if status == 'ok':
                connection.execute('UPDATE menu_sync_state SET last_success_at=? WHERE id = 1', (at,))
            keep = [row[0] for row in connection.execute(
                'SELECT id FROM menu_sync_runs ORDER BY started_at DESC, id DESC LIMIT ?', (RUNS_KEPT,))]
            if keep:
                connection.execute('DELETE FROM menu_sync_runs WHERE id NOT IN ('
                                   + ','.join('?' for _ in keep) + ')', keep)
            connection.commit()

    def runs(self, limit=5):
        columns = ('id', 'started_at', 'finished_at', 'status', 'items', 'added', 'changed',
                   'missing', 'returned', 'priced', 'note', 'error')
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT ' + ', '.join(columns) + ' FROM menu_sync_runs '
                'ORDER BY started_at DESC, id DESC LIMIT ?', (max(1, int(limit)),)).fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def status(self):
        """Состояние справочника одним ответом: сколько, когда и чем закончилось."""
        state = self.state() or dict(next_run_at=None, last_success_at=None)
        runs = self.runs(1)
        return dict(count=self.count(), last_run=runs[0] if runs else None,
                    synced_at=state['last_success_at'], next_run_at=state['next_run_at'])
