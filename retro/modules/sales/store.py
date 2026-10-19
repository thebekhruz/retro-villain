"""История продаж в нашей базе: строки чеков, чеки, состояние дня и журнал прогонов.

День хранится целиком и целиком же заменяется одной транзакцией: iiko отдаёт
день отчётом, а не потоком событий, поэтому «правка», «отмена» и «удалённый
чек» для нас — просто другая версия дня. Версию узнаём по хэшу ответа
(`revision`): тот же хэш — строки не трогаем, двигаем только время проверки.

Деньги и количество лежат текстом Decimal, как во всей панели: суммы по
предоплате приходят долями (344 531 000,28), и float здесь неуместен.
Время события (`opened_at`, `closed_at`) и время получения (`fetched_at`) —
ISO с поясом Ташкента.
"""
import hashlib
import json
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal

from retro.db import as_database
from retro.modules.cashier.service import DataError, TZ

# Меняется, когда меняется разбор отчёта iiko: тогда старая история не
# смешивается с новой, а дозаливается заново.
CALCULATION_VERSION = 'sales-2026-10-17-v1'
# Журнал прогонов: сегодняшний день пишет строку раз в десять минут.
RUNS_KEPT_DAYS = 45
# Почасовые контрольные снимки дня (T-507) живут долго: это след «сколько было
# на 13:00» на случай спора или восстановления, а строк в них — не больше 24 в день.
SNAPSHOTS_KEPT_DAYS = 400

SCHEMA = '''
    CREATE TABLE IF NOT EXISTS sales_lines (
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        line_key TEXT NOT NULL,
        order_id TEXT NOT NULL,
        order_num TEXT NOT NULL DEFAULT '',
        opened_at TEXT,
        closed_at TEXT,
        register TEXT NOT NULL DEFAULT '',
        section TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL,
        waiter TEXT NOT NULL DEFAULT '',
        dish_id TEXT NOT NULL DEFAULT '',
        dish_name TEXT NOT NULL DEFAULT '',
        dish_group TEXT NOT NULL DEFAULT '',
        dish_type TEXT NOT NULL DEFAULT '',
        storned INTEGER NOT NULL DEFAULT 0,
        quantity TEXT NOT NULL,
        gross TEXT NOT NULL,
        revenue TEXT NOT NULL,
        discount TEXT NOT NULL,
        cost TEXT,
        returned TEXT NOT NULL DEFAULT '0',
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source, day, line_key)
    );
    CREATE TABLE IF NOT EXISTS sales_orders (
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        order_id TEXT NOT NULL,
        order_num TEXT NOT NULL DEFAULT '',
        opened_at TEXT,
        closed_at TEXT,
        register TEXT NOT NULL DEFAULT '',
        section TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL,
        lines INTEGER NOT NULL,
        quantity TEXT NOT NULL,
        gross TEXT NOT NULL,
        revenue TEXT NOT NULL,
        discount TEXT NOT NULL,
        cost TEXT,
        paid TEXT,
        prepaid TEXT,
        other TEXT,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source, day, order_id)
    );
    CREATE TABLE IF NOT EXISTS sales_days (
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        revision TEXT NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0,
        lines INTEGER NOT NULL,
        orders INTEGER NOT NULL,
        revenue TEXT NOT NULL,
        iiko_revenue TEXT,
        status TEXT NOT NULL,
        reconciliation TEXT NOT NULL,
        revisions INTEGER NOT NULL DEFAULT 1,
        changed_at TEXT NOT NULL,
        PRIMARY KEY (source, day)
    );
    CREATE TABLE IF NOT EXISTS sales_sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        reason TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        lines INTEGER,
        orders INTEGER,
        revenue TEXT,
        previous_revenue TEXT,
        revision TEXT,
        error TEXT
    );
    CREATE INDEX IF NOT EXISTS sales_sync_runs_day ON sales_sync_runs(source, day);
    CREATE TABLE IF NOT EXISTS sales_hourly_snapshots (
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        hour INTEGER NOT NULL,
        fetched_at TEXT NOT NULL,
        revision TEXT NOT NULL,
        lines INTEGER NOT NULL,
        orders INTEGER NOT NULL,
        receipts INTEGER NOT NULL,
        quantity TEXT NOT NULL,
        gross TEXT NOT NULL,
        revenue TEXT NOT NULL,
        iiko_revenue TEXT,
        iiko_orders INTEGER,
        iiko_quantity TEXT,
        difference TEXT,
        last_closed_at TEXT,
        status TEXT NOT NULL,
        by_direction TEXT NOT NULL,
        differences TEXT NOT NULL,
        PRIMARY KEY (source, day, hour)
    );
    CREATE TABLE IF NOT EXISTS sales_day_leases (
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        owner TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (source, day)
    )
'''

LINE_COLUMNS = ('line_key', 'order_id', 'order_num', 'opened_at', 'closed_at', 'register', 'section',
                'direction', 'waiter', 'dish_id', 'dish_name', 'dish_group', 'dish_type', 'storned',
                'quantity', 'gross', 'revenue', 'discount', 'cost', 'returned')
ORDER_COLUMNS = ('order_id', 'order_num', 'opened_at', 'closed_at', 'register', 'section', 'direction',
                 'lines', 'quantity', 'gross', 'revenue', 'discount', 'cost', 'paid', 'prepaid', 'other')
DAY_COLUMNS = ('day', 'fetched_at', 'revision', 'closed', 'lines', 'orders', 'revenue', 'iiko_revenue',
               'status', 'reconciliation', 'revisions', 'changed_at')
MONEY = frozenset({'quantity', 'gross', 'revenue', 'discount', 'cost', 'returned', 'paid', 'prepaid',
                   'other', 'iiko_revenue', 'iiko_quantity', 'difference'})
SNAPSHOT_COLUMNS = ('day', 'hour', 'fetched_at', 'revision', 'lines', 'orders', 'receipts', 'quantity', 'gross',
                    'revenue', 'iiko_revenue', 'iiko_orders', 'iiko_quantity', 'difference', 'last_closed_at',
                    'status', 'by_direction', 'differences')
RUN_COLUMNS = ('id', 'day', 'reason', 'started_at', 'finished_at', 'status', 'lines', 'orders',
               'revenue', 'previous_revenue', 'revision', 'error')


def stored(value):
    """Число → текст базы; `None` остаётся «неизвестно»."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    return str(value) if isinstance(value, Decimal) else value


def decoded(columns, row):
    item = dict(zip(columns, row))
    for key in MONEY & item.keys():
        if item[key] is not None:
            item[key] = Decimal(item[key])
    if 'storned' in item:
        item['storned'] = bool(item['storned'])
    return item


class SalesStore:
    def __init__(self, database, settings):
        self.db = as_database(database)
        # Источник без учётных данных: другой ресторан или другой разбор
        # отчёта не должны отдать чужую историю.
        scope = [settings.base_url, settings.store_id, CALCULATION_VERSION]
        self.source = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
        self.snapshots_kept_days = getattr(settings, 'sales_snapshots_kept_days', SNAPSHOTS_KEPT_DAYS)
        with closing(self.db.connect()) as connection:
            connection.executescript(SCHEMA)

    # ── День ───────────────────────────────────────────────────────────────

    def _day(self, row):
        item = decoded(DAY_COLUMNS, row)
        item['day'] = date.fromisoformat(item['day'])
        item['fetched_at'] = datetime.fromisoformat(item['fetched_at'])
        item['closed'] = bool(item['closed'])
        item['reconciliation'] = json.loads(item['reconciliation'])
        return item

    def state(self, day):
        with closing(self.db.connect()) as connection:
            row = connection.execute(
                'SELECT ' + ', '.join(DAY_COLUMNS) + ' FROM sales_days WHERE source=? AND day=?',
                (self.source, day.isoformat())).fetchone()
        return self._day(row) if row else None

    def states(self, start, end):
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                'SELECT ' + ', '.join(DAY_COLUMNS) + ' FROM sales_days WHERE source=? AND day>=? AND day<=? '
                'ORDER BY day', (self.source, start.isoformat(), end.isoformat())).fetchall()
        return {item['day']: item for item in map(self._day, rows)}

    def save_day(self, day, lines, orders, *, fetched_at, revision, closed, revenue, iiko_revenue,
                 status, reconciliation):
        """День целиком → база одной транзакцией. Возвращает, что произошло.

        Тот же `revision` — строки остаются как есть (их `fetched_at` — когда эта
        версия пришла впервые), обновляется только время проверки дня. Другой —
        строки и чеки дня заменяются, счётчик версий растёт. Более старый ответ
        (медленный прогон, закончившийся позже свежего) не перезаписывает новый.
        """
        if fetched_at.tzinfo is None:
            raise DataError('Время получения продаж должно быть с часовым поясом.')
        at = fetched_at.astimezone(TZ).isoformat()
        key = (self.source, day.isoformat())
        with closing(self.db.connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                previous = connection.execute(
                    'SELECT fetched_at, revision, revenue, revisions FROM sales_days WHERE source=? AND day=?',
                    key).fetchone()
                if previous is not None and previous[0] > at:
                    connection.rollback()
                    return dict(saved=False, changed=False, previous_revenue=Decimal(previous[2]))
                changed = previous is None or previous[1] != revision
                if changed:
                    connection.execute('DELETE FROM sales_lines WHERE source=? AND day=?', key)
                    connection.execute('DELETE FROM sales_orders WHERE source=? AND day=?', key)
                    if lines:
                        connection.executemany(
                            'INSERT INTO sales_lines (source, day, ' + ', '.join(LINE_COLUMNS)
                            + ', fetched_at) VALUES (' + ','.join('?' for _ in range(len(LINE_COLUMNS) + 3))
                            + ')', [(*key, *(stored(line[name]) for name in LINE_COLUMNS), at) for line in lines])
                    if orders:
                        connection.executemany(
                            'INSERT INTO sales_orders (source, day, ' + ', '.join(ORDER_COLUMNS)
                            + ', fetched_at) VALUES (' + ','.join('?' for _ in range(len(ORDER_COLUMNS) + 3))
                            + ')', [(*key, *(stored(order[name]) for name in ORDER_COLUMNS), at)
                                    for order in orders])
                revisions = (previous[3] if previous else 0) + int(changed)
                values = (at, revision, int(closed), len(lines), len(orders), str(revenue),
                          stored(iiko_revenue), status, json.dumps(reconciliation, ensure_ascii=False),
                          revisions)
                connection.execute(
                    'INSERT INTO sales_days (source, day, fetched_at, revision, closed, lines, orders, revenue, '
                    'iiko_revenue, status, reconciliation, revisions, changed_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, day) DO UPDATE SET '
                    'fetched_at=excluded.fetched_at, revision=excluded.revision, closed=excluded.closed, '
                    'lines=excluded.lines, orders=excluded.orders, revenue=excluded.revenue, '
                    'iiko_revenue=excluded.iiko_revenue, status=excluded.status, '
                    'reconciliation=excluded.reconciliation, revisions=excluded.revisions'
                    + (', changed_at=excluded.changed_at' if changed else ''),
                    (*key, *values, at))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return dict(saved=True, changed=changed, revisions=revisions,
                    previous_revenue=Decimal(previous[2]) if previous else None)

    def lines(self, start, end, columns=LINE_COLUMNS):
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                'SELECT day, fetched_at, ' + ', '.join(columns) + ' FROM sales_lines '
                'WHERE source=? AND day>=? AND day<=? ORDER BY day, closed_at, order_id, line_key',
                (self.source, start.isoformat(), end.isoformat())).fetchall()
        result = []
        for row in rows:
            item = decoded(('day', 'fetched_at', *columns), row)
            item['day'] = date.fromisoformat(item['day'])
            result.append(item)
        return result

    def orders(self, start, end):
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                'SELECT day, fetched_at, ' + ', '.join(ORDER_COLUMNS) + ' FROM sales_orders '
                'WHERE source=? AND day>=? AND day<=? ORDER BY day, closed_at, order_id',
                (self.source, start.isoformat(), end.isoformat())).fetchall()
        result = []
        for row in rows:
            item = decoded(('day', 'fetched_at', *ORDER_COLUMNS), row)
            item['day'] = date.fromisoformat(item['day'])
            result.append(item)
        return result

    def coverage(self):
        with closing(self.db.connect()) as connection:
            first, last, days = connection.execute(
                'SELECT MIN(day), MAX(day), COUNT(*) FROM sales_days WHERE source=?',
                (self.source,)).fetchone()
        return dict(first_day=first, last_day=last, days=int(days or 0))

    # ── Аренда дня: два процесса не грузят один день одновременно ──────────

    def acquire(self, day, owner, now, seconds=300):
        with closing(self.db.connect()) as connection, connection:
            result = connection.execute(
                'INSERT INTO sales_day_leases (source, day, owner, expires_at) VALUES (?,?,?,?) '
                'ON CONFLICT(source, day) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at '
                'WHERE sales_day_leases.expires_at <= ?',
                (self.source, day.isoformat(), owner, (now + timedelta(seconds=seconds)).isoformat(),
                 now.isoformat()))
            return result.rowcount == 1

    def release(self, day, owner):
        with closing(self.db.connect()) as connection, connection:
            connection.execute('DELETE FROM sales_day_leases WHERE source=? AND day=? AND owner=?',
                               (self.source, day.isoformat(), owner))

    # ── Журнал прогонов ────────────────────────────────────────────────────

    def start_run(self, day, reason, at):
        with closing(self.db.connect()) as connection:
            run_id = self.db.insert_returning_id(
                connection, "INSERT INTO sales_sync_runs (source, day, reason, started_at, status) "
                "VALUES (?, ?, ?, ?, 'running')", (self.source, day.isoformat(), reason, at.isoformat()))
            connection.commit()
        return run_id

    def finish_run(self, run_id, status, *, at, lines=None, orders=None, revenue=None,
                   previous_revenue=None, revision=None, error=None):
        if status not in ('ok', 'unchanged', 'failed', 'skipped'):
            raise DataError('Неизвестный итог загрузки продаж.')
        with closing(self.db.connect()) as connection, connection:
            connection.execute(
                'UPDATE sales_sync_runs SET finished_at=?, status=?, lines=?, orders=?, revenue=?, '
                'previous_revenue=?, revision=?, error=? WHERE id=?',
                (at.isoformat(), status, lines, orders, stored(revenue), stored(previous_revenue),
                 revision, error[:500] if error else None, run_id))
            connection.execute('DELETE FROM sales_sync_runs WHERE source=? AND started_at < ?',
                               (self.source, (at - timedelta(days=RUNS_KEPT_DAYS)).isoformat()))

    def runs(self, limit=20, *, day=None, status=None):
        where, params = ['source=?'], [self.source]
        if day is not None:
            where.append('day=?')
            params.append(day.isoformat())
        if status is not None:
            where.append('status=?')
            params.append(status)
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                'SELECT ' + ', '.join(RUN_COLUMNS) + ' FROM sales_sync_runs WHERE ' + ' AND '.join(where)
                + ' ORDER BY started_at DESC, id DESC LIMIT ?', (*params, max(1, int(limit)))).fetchall()
        return [dict(zip(RUN_COLUMNS, row)) for row in rows]

    # ── Почасовые контрольные снимки (T-507) ───────────────────────────────

    def save_snapshot(self, day, hour, *, fetched_at, revision, lines, orders, receipts, quantity, gross,
                      revenue, iiko_revenue, iiko_orders, iiko_quantity, last_closed_at, status, by_direction,
                      differences):
        """Итог дня «на этот час» — не больше одной строки на (день, час).

        Первая загрузка часа пишет снимок, следующие в тот же час ничего не
        меняют (`INSERT OR IGNORE`): снимок — след того, что было на тот момент,
        а не ещё одна копия текущего дня. Возвращает, записан ли снимок.
        Заодно удаляет снимки старше `snapshots_kept_days`; строки, чеки и
        состояние дня это не трогает.
        """
        if fetched_at.tzinfo is None:
            raise DataError('Время снимка продаж должно быть с часовым поясом.')
        if not 0 <= hour <= 23:
            raise DataError('Час снимка — от 0 до 23.')
        difference = None if iiko_revenue is None else revenue - iiko_revenue
        values = (self.source, day.isoformat(), hour, fetched_at.astimezone(TZ).isoformat(), revision, lines,
                  orders, receipts, str(quantity), str(gross), str(revenue), stored(iiko_revenue), iiko_orders,
                  stored(iiko_quantity), stored(difference), last_closed_at, status,
                  json.dumps(by_direction, ensure_ascii=False, sort_keys=True),
                  json.dumps(differences, ensure_ascii=False))
        with closing(self.db.connect()) as connection, connection:
            result = connection.execute(
                'INSERT OR IGNORE INTO sales_hourly_snapshots (source, ' + ', '.join(SNAPSHOT_COLUMNS)
                + ') VALUES (' + ','.join('?' for _ in values) + ')', values)
            inserted = result.rowcount == 1
            connection.execute('DELETE FROM sales_hourly_snapshots WHERE source=? AND day < ?',
                               (self.source, (day - timedelta(days=self.snapshots_kept_days)).isoformat()))
        return inserted

    def snapshots(self, day):
        """Снимки дня по часам, от раннего к позднему."""
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                'SELECT ' + ', '.join(SNAPSHOT_COLUMNS) + ' FROM sales_hourly_snapshots '
                'WHERE source=? AND day=? ORDER BY hour', (self.source, day.isoformat())).fetchall()
        result = []
        for row in rows:
            item = dict(zip(SNAPSHOT_COLUMNS, row))
            item['by_direction'] = json.loads(item['by_direction'])
            item['differences'] = json.loads(item['differences'])
            result.append(item)
        return result
