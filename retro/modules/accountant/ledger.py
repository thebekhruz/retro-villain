"""Demo finance ledger: earned wages, actual payments, and available cash."""

import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from .payroll import PayrollRow, attendance_after_payment
from .expense_catalog import CASH_DIVIDENDS_ITEM, EXTRA_ITEM, EXTRA_SALARY_CODES, ITEMS
from .audit import audit_entries as read_audit_entries, record_audit
from retro.db import PostgresConnection, as_database, table_columns
from retro.request_reads import call as cached_call, once
from retro.modules.cashier.service import TZ
from retro.runtime import secure_directory, secure_file
from retro.accounting_period import ACCOUNTING_START, accounting_range_start, cash_opening_table, period_start


# Базары по умолчанию для расходов Шоха (ТЗ 02.10, п. 5); остальные добавляет бухгалтер.
DEFAULT_BAZAARS = ('Алайский базар', 'Food City', 'Базар Шеф')

# Ссылка выплаты оклада на сотрудника реестра окладов: `monthly:<id>:<ключ>`.
MONTHLY_REFERENCE = 'monthly:'
MONTHLY_ITEM = 'salary_monthly'
# Доп. выплату пишут только с сотрудником и сменой — общим расходом её не завести.
EXTRA_ONLY = 'Доп. выплату записывают в «Зарплата · день»: с сотрудником и датой смены.'


class LedgerError(ValueError):
    pass


class PayrollConfirmation:
    """Итог подтверждения смены: кому начислено сейчас и кто ещё ждёт.

    Истинно, если что-то изменилось (кому-то начислено или день закрыт)."""

    def __init__(self, *, accrued=(), blockers=(), confirmed=False, already_confirmed=False,
                 accrued_before=()):
        self.accrued = list(accrued)
        self.blockers = list(blockers)
        self.confirmed = confirmed
        self.already_confirmed = already_confirmed
        self.accrued_before = list(accrued_before)

    def __bool__(self):
        return bool(self.accrued) or (self.confirmed and not self.already_confirmed)


def now_stamp() -> str:
    """Момент записи с поясом Ташкента: не зависит от пояса сервера."""
    return datetime.now(TZ).isoformat()


def local_timestamp(value) -> str | None:
    """Время записи по Ташкенту — для подписи «08:40» на экране.

    Старые строки писались `datetime.now().isoformat()` без пояса, то есть во
    времени сервера (UTC на Railway, Ташкент на машине разработчика). Такое
    значение читаем как время сервера и переводим в Ташкент; новые записи
    хранятся уже с поясом. Нечитаемое значение — не повод ронять журнал.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment.astimezone(TZ).isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        return None


def is_monthly_link(reference) -> bool:
    return isinstance(reference, str) and reference.startswith(MONTHLY_REFERENCE)


# Ключ блокировки дня в Postgres: «RETR» в старших битах, день — в младших.
DAY_LOCK_BASE = 0x52455452 << 32


def lock_day(connection, day: date) -> None:
    """Подтверждение смены и отметки «был / не был» за день — строго по одному.

    Вызывается сразу после начала транзакции. В SQLite это уже делает
    BEGIN IMMEDIATE (одна пишущая транзакция на файл); в Postgres транзакции
    идут параллельно, поэтому обе операции берут одну блокировку на день —
    она снимается сама на COMMIT или ROLLBACK.
    """
    if isinstance(connection, PostgresConnection):
        connection.execute('SELECT pg_advisory_xact_lock(CAST(? AS BIGINT))',
                           (DAY_LOCK_BASE + day.toordinal(),))


def plain(value: Decimal) -> str:
    """Сумма строкой без хвостовых нулей: 300000.00 → «300000», 0.50 → «0.5»."""
    return format(value.normalize(), 'f') if value else '0'


def amount_value(value, *, allow_zero=False) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise LedgerError('Укажите корректную сумму.') from None
    if (not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero)
            or amount > Decimal('1000000000000') or amount.as_tuple().exponent < -2):
        raise LedgerError('Сумма должна быть положительной, не более 1 трлн сум.')
    return amount


def required_text(value: str, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ''
    if not text or len(text) > 160:
        raise LedgerError(f'Укажите {label} (до 160 символов).')
    return text


def counted_handover(amount, source, confirmed_at) -> str:
    """Сколько из прихода кассира за день входит в остаток бухгалтера.

    Деньги есть у бухгалтера, когда он их подтвердил («Получено от кассира»)
    или сам записал приход вручную (`source` 'accountant'; у старых строк
    источника нет — они тоже ручные). Передача кассира кнопкой и расчёт iiko до
    подтверждения — это «ожидается»: денег в руках ещё нет, и в остаток они не
    входят (ТЗ 02.10, п. 1 и «Получено от кассира»). Строка при этом остаётся в
    цепочке дней — день не «пропущен», просто его приход пока ноль.
    """
    return amount if confirmed_at is not None or source in (None, 'accountant') else '0'


def closure_row(connection):
    """Последний закрытый месяц: (месяц, последний день, кем, когда) или None.

    За один безопасный запрос читается один раз; внутри записи кэша нет, и
    транзакция видит собственные изменения (`ensure_open`). Ключ без
    соединения: база бухгалтера в запросе одна, а соединение у каждого
    вызова своё."""
    return cached_call(('closure_row',), lambda: connection.execute(
        'SELECT month, last_day, closed_by, closed_at FROM accountant_month_closures '
        'ORDER BY last_day DESC LIMIT 1').fetchone())


def flow_json(flow: dict | None) -> dict | None:
    """Раскладка дня (CashBook.flow) для ответа API: суммы строками."""
    if flow is None:
        return None
    result = {}
    for key, value in flow.items():
        if isinstance(value, Decimal):
            result[key] = plain(value) if value else '0'
        elif isinstance(value, dict):
            result[key] = {k: (plain(v) if isinstance(v, Decimal) and v else '0' if isinstance(v, Decimal) else v)
                           for k, v in value.items()}
        else:
            result[key] = value
    return result


def ensure_open(connection, day: date) -> None:
    """Закрытый месяц только для чтения: ни добавить, ни изменить, ни удалить.

    Закрытие месяца закрывает и все дни раньше него. Проверка идёт внутри
    транзакции записи — тем же соединением, что и сама запись."""
    row = closure_row(connection)
    if row is not None and day.isoformat() <= row[1]:
        closed = date.fromisoformat(row[1])
        raise LedgerError(f'Месяц закрыт: дни по {closed.strftime("%d.%m.%Y")} только для чтения.')


class CashBook:
    """Всё, из чего складывается остаток бухгалтера, прочитанное один раз.

    Строки берутся по `through` включительно, а `position(day)` отбирает из
    них нужные дню так же, как раньше это делали запросы с `day <= ?`.
    Остаток за любой день до `through` поэтому считается без базы: месячная
    ведомость читает её одним набором запросов, а не заново на каждый день.

    Приход кассира входит в остаток только подтверждённым (`counted_handover`).
    Точка отсчёта — начальный остаток бухгалтера или конец последнего
    закрытого месяца: остаток на его последний день становится началом первого
    числа следующего, и правки внутри закрытого месяца его уже не сдвигают.
    """

    CASH_KINDS = ('other_expense', 'procurement_advance', 'other_receipt')

    def __init__(self, connection, through: date):
        last = through.isoformat()
        self.recorded = {}
        self.handovers = []
        for day, amount, source, confirmed_at in connection.execute(
                'SELECT day, amount, source, confirmed_at FROM accountant_handover_days '
                'WHERE day <= ? ORDER BY day', (last,)).fetchall():
            self.recorded[day] = dict(amount=amount, source=source or 'accountant',
                                      confirmed=confirmed_at is not None)
            self.handovers.append((day, counted_handover(amount, source, confirmed_at)))
        anchor = connection.execute('SELECT day,amount FROM accountant_cash_opening WHERE id=1').fetchone()
        self.anchor = anchor
        self.working_anchor = connection.execute(
            'SELECT day,amount FROM accountant_working_cash_opening WHERE id=1').fetchone()
        # Точки отсчёта: (день, остаток на начало этого дня, откуда).
        self.anchors = [(anchor[0], anchor[1], 'opening')] if anchor and anchor[0] < ACCOUNTING_START.isoformat() else []
        if self.working_anchor:
            self.anchors.append((*self.working_anchor, 'opening'))
        for month, last_day, balance in connection.execute(
                'SELECT month, last_day, closing_balance FROM accountant_month_closures '
                'ORDER BY last_day').fetchall():
            following = date.fromisoformat(last_day) + timedelta(days=1)
            self.anchors.append((following.isoformat(), balance, 'closure:' + month))
        self.anchors.sort()
        self.movements = connection.execute(
            "SELECT day, kind, amount, item_code FROM accountant_movements WHERE day <= ? "
            "AND kind IN ('other_expense','procurement_advance','other_receipt')", (last,)).fetchall()
        self.salaries = connection.execute(
            'SELECT paid_day, amount FROM accountant_salary_payments WHERE paid_day <= ?', (last,)).fetchall()
        self.transfers = connection.execute(
            "SELECT day, amount FROM accountant_reserves WHERE kind='transfer' AND day <= ?", (last,)).fetchall()
        self.through = last

    def anchor_for(self, today: str):
        """Последняя точка отсчёта не позже дня: (день, остаток, откуда) или None."""
        found = None
        for item in self.anchors:
            if period_start(date.fromisoformat(today)).isoformat() <= item[0] <= today:
                found = item
        return found

    def position(self, day: date, start_day: date | None = None, *, current_amount=None,
                 tolerate_gaps: bool = False):
        today = day.isoformat()
        if today > self.through:
            raise ValueError('CashBook прочитана только по ' + self.through)
        rows = [row for row in self.handovers if row[0] <= today]
        if current_amount is not None:
            rows = sorted([row for row in rows if row[0] != today] + [(today, str(current_amount))])
        if start_day is not None:
            rows = [row for row in rows if row[0] >= start_day.isoformat()]
        boundary = period_start(day)
        if day >= ACCOUNTING_START:
            tolerate_gaps = False
        rows = [row for row in rows if row[0] >= boundary.isoformat()]
        if day >= ACCOUNTING_START and self.working_anchor is None:
            return None, None, ACCOUNTING_START.isoformat(), ACCOUNTING_START.isoformat()
        anchor = self.anchor_for(today)
        if anchor:
            rows = [row for row in rows if row[0] >= anchor[0]]
        if not rows and anchor is None:
            return None, None, today, None
        first = date.fromisoformat(anchor[0] if anchor else rows[0][0])
        expected = first
        for recorded, _ in rows:
            if date.fromisoformat(recorded) != expected and not tolerate_gaps:
                return None, None, expected.isoformat(), first.isoformat()
            expected = date.fromordinal(date.fromisoformat(recorded).toordinal() + 1)
        if expected < day and not tolerate_gaps:
            return None, None, expected.isoformat(), first.isoformat()
        start = first.isoformat()
        opening = Decimal(anchor[1]) if anchor else Decimal(0)
        opening += sum((Decimal(value) for recorded, value in rows if recorded < today), Decimal(0))
        for cutoff, kind, amount, _ in self.movements:
            if start <= cutoff < today:
                opening += Decimal(amount) if kind == 'other_receipt' else -Decimal(amount)
        for cutoff, amount in self.salaries:
            if start <= cutoff < today:
                opening -= Decimal(amount)
        for cutoff, amount in self.transfers:
            if start <= cutoff < today:
                opening -= Decimal(amount)
        if not rows or rows[-1][0] != today:
            return opening, None, today, start
        receipts = sum((Decimal(amount) for cutoff, kind, amount, _ in self.movements
                        if cutoff == today and kind == 'other_receipt'), Decimal(0))
        return (opening, opening + Decimal(rows[-1][1]) + receipts - self.outflows(day), None, start)

    def outflows(self, day: date) -> Decimal:
        """То же, что FinanceStore._daily_outflows, но из прочитанных строк."""
        today = day.isoformat()
        spent = sum((Decimal(amount) for cutoff, kind, amount, _ in self.movements
                     if cutoff == today and kind in ('other_expense', 'procurement_advance')), Decimal(0))
        salaries = sum((Decimal(amount) for cutoff, amount in self.salaries if cutoff == today), Decimal(0))
        transfers = sum((Decimal(amount) for cutoff, amount in self.transfers if cutoff == today), Decimal(0))
        return spent + salaries + transfers

    def flow(self, day: date, start_day: date | None = None, *, tolerate_gaps: bool = False) -> dict:
        """Из чего сложился день: начало → приход → расходы по видам → конец.

        Одна раскладка для расшифровки «На начало дня», сверки месяца по
        дням, сданного отчёта дня и закрытия месяца. Сумма строк сходится с
        `position`: конец = начало + приход в остатке + прочие поступления −
        все расходы.

        `salary` — выплаты сменным по начислениям и строки журнала «Доп.
        зарплата и временный персонал» (ТЗ 09.10, Б-08): это зарплата, а не
        прочий расход. `other_dividends` — часть `other`, дивиденды прямо из
        кассы: экран показывает их своей строкой, а в `other` они остаются,
        чтобы сданные раньше отчёты дня сверялись с теми же суммами."""
        from .reserves import is_monthly_salary  # reserves импортирует ledger
        opening, closing, missing, first = self.position(day, start_day, tolerate_gaps=tolerate_gaps)
        today = day.isoformat()
        recorded = self.recorded.get(today)
        counted = next((Decimal(value) for recorded_day, value in self.handovers
                        if recorded_day == today), None)
        if recorded is None:
            status = 'none'
        elif recorded['confirmed']:
            status = 'confirmed'
        elif recorded['source'] == 'accountant':
            status = 'accountant'
        else:
            status = 'pending'

        def moved(test):
            return sum((Decimal(amount) for cutoff, kind, amount, code in self.movements
                        if cutoff == today and test(kind, code)), Decimal(0))

        shoh = moved(lambda kind, code: kind == 'procurement_advance'
                     or (kind == 'other_expense' and code == 'proc_shoh'))
        monthly = moved(lambda kind, code: kind == 'other_expense' and is_monthly_salary(code))
        # Доп. зарплата из журнала и доп. выплаты из «Зарплата · день» — в «Сменным»
        # (строка «Зарплаты» дэшборда), а не в прочих.
        extra = moved(lambda kind, code: kind == 'other_expense' and code in EXTRA_SALARY_CODES)
        other = moved(lambda kind, code: kind == 'other_expense' and code != 'proc_shoh'
                      and code not in EXTRA_SALARY_CODES and not is_monthly_salary(code))
        salary = extra + sum((Decimal(amount) for cutoff, amount in self.salaries if cutoff == today), Decimal(0))
        transfers = sum((Decimal(amount) for cutoff, amount in self.transfers if cutoff == today), Decimal(0))
        anchor = self.anchor_for(today)
        return dict(day=today, opening=opening,
                    handover=Decimal(recorded['amount']) if recorded else None,
                    handover_counted=counted if counted is not None else Decimal(0),
                    handover_status=status,
                    receipts=moved(lambda kind, code: kind == 'other_receipt'),
                    salary=salary, monthly=monthly, shoh=shoh, other=other, transfers=transfers,
                    other_dividends=moved(lambda kind, code: kind == 'other_expense'
                                          and code == CASH_DIVIDENDS_ITEM),
                    outflows=salary + monthly + shoh + other + transfers,
                    closing=closing, missing=missing, first_day=first,
                    # День начинается с точки отсчёта: начальный остаток или
                    # конец закрытого месяца — тогда расшифровка говорит об этом.
                    anchor=(dict(kind=anchor[2], amount=Decimal(anchor[1]))
                            if anchor and anchor[0] == today else None))


class FinanceStore:
    def __init__(self, path: Path, *, allow_negative_cash: bool = False, migrate_handovers: bool = True):
        self.db = as_database(path)
        # .path остаётся для скриптов обслуживания и тестов
        self.path = self.db.path
        # Режим проверки: остаток разрешено уводить в минус. Обычно отрицательный
        # остаток отменяет операцию — это единственная защита от выдачи денег,
        # которых в кассе нет.
        self.allow_negative_cash = allow_negative_cash
        self._initialize(migrate_handovers=migrate_handovers)

    def _open(self):
        return self.db.connect()

    def _initialize(self, *, migrate_handovers=True):
        with closing(self._open()) as connection, connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS accountant_exceptions (
                    employee_id INTEGER PRIMARY KEY,
                    day TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_payroll_days (
                    day TEXT PRIMARY KEY,
                    approver TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_accruals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_day TEXT NOT NULL,
                    employee_id INTEGER NOT NULL,
                    employee_name TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    attendance_status TEXT NOT NULL,
                    rate TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    UNIQUE(work_day, employee_id)
                );
                CREATE TABLE IF NOT EXISTS accountant_salary_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accrual_id INTEGER NOT NULL REFERENCES accountant_accruals(id),
                    paid_day TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    item_code TEXT,
                    reference TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(kind, reference)
                );
                CREATE TABLE IF NOT EXISTS accountant_reserves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL,
                    amount TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_monthly_plans (
                    month TEXT PRIMARY KEY, amount TEXT NOT NULL, note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_data_migrations (
                    name TEXT PRIMARY KEY, applied INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_handover_days (
                    day TEXT PRIMARY KEY, amount TEXT NOT NULL, checked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_cash_opening (
                    id INTEGER PRIMARY KEY CHECK(id=1), day TEXT NOT NULL,
                    amount TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_working_cash_opening (
                    id INTEGER PRIMARY KEY CHECK(id=1), day TEXT NOT NULL,
                    amount TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_debts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                    item_code TEXT NOT NULL, description TEXT NOT NULL,
                    total_amount TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_debt_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    debt_id INTEGER NOT NULL REFERENCES accountant_debts(id),
                    day TEXT NOT NULL, amount TEXT NOT NULL,
                    movement_id INTEGER NOT NULL REFERENCES accountant_movements(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accountant_finance_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    changed_at TEXT NOT NULL
                );
                -- Перечисление поставщику со счёта: наличные кассы и подотчёт
                -- Шоха оно не меняет, поэтому живёт отдельно от движений денег.
                CREATE TABLE IF NOT EXISTS accountant_supplier_transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    supplier TEXT NOT NULL,
                    item TEXT NOT NULL,
                    point TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                -- Сданный бухгалтером отчёт дня: снимок «начало → приход →
                -- расход → конец» на момент сдачи (closing.py).
                CREATE TABLE IF NOT EXISTS accountant_day_reports (
                    day TEXT PRIMARY KEY,
                    submitted_at TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                );
                -- Закрытый месяц: дни по last_day только для чтения, а
                -- closing_balance — начало первого числа следующего месяца.
                CREATE TABLE IF NOT EXISTS accountant_month_closures (
                    month TEXT PRIMARY KEY,
                    last_day TEXT NOT NULL,
                    closing_balance TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    closed_by TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                );
                -- Базары, где Шох тратит подотчёт: список пополняет бухгалтер.
                CREATE TABLE IF NOT EXISTS accountant_bazaars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            ''')
            # Уже подтверждённый остаток ровно на 2 октября можно использовать.
            # Сентябрьский остаток остаётся в архивной таблице без изменений.
            connection.execute('INSERT INTO accountant_working_cash_opening '
                               'SELECT * FROM accountant_cash_opening WHERE day=? '
                               'ON CONFLICT(id) DO NOTHING', (ACCOUNTING_START.isoformat(),))
            columns = table_columns(connection, 'accountant_movements')
            if 'item_code' not in columns:
                connection.execute('ALTER TABLE accountant_movements ADD COLUMN item_code TEXT')
            connection.execute('CREATE INDEX IF NOT EXISTS accountant_salary_accrual_day ON accountant_salary_payments(accrual_id, paid_day)')
            connection.execute('CREATE INDEX IF NOT EXISTS accountant_salary_paid_day ON accountant_salary_payments(paid_day)')
            connection.execute('CREATE INDEX IF NOT EXISTS accountant_movements_day ON accountant_movements(day)')
            connection.execute('CREATE INDEX IF NOT EXISTS accountant_supplier_transfers_day '
                               'ON accountant_supplier_transfers(day)')
            # Резервы читает почти каждый экран бухгалтера и учредителя: счёт с
            # датой (подотчёт, сейф, доллары) и переводы в сейф по дню.
            connection.execute('CREATE INDEX IF NOT EXISTS accountant_reserves_account_day '
                               'ON accountant_reserves(account, day)')
            connection.execute('CREATE INDEX IF NOT EXISTS accountant_reserves_kind_day '
                               'ON accountant_reserves(kind, day)')
            # Расходы Шоха ищутся по статье и виду движения, а не по дню.
            connection.execute('CREATE INDEX IF NOT EXISTS accountant_movements_kind_item '
                               'ON accountant_movements(kind, item_code)')
            # Касса кассира (5a) живёт в этой же базе: выдачи Шоху из кассы и
            # доллары в сейф читают подотчёт и сейф — см. modules/cashier/till.py.
            from retro.modules.cashier.till import create_tables
            create_tables(connection)
            # Доп. выплаты «Зарплата · день» (ТЗ 09.10, Б-05): кому и за какую
            # смену; сами деньги — расход в accountant_movements.
            from .extra_payouts import create_tables as create_extra_tables
            create_extra_tables(connection)
            # Кто записал приход: 'cashier' — кнопка кассира, 'accountant' — ручная
            # запись бухгалтера, 'auto' — расчёт iiko. Старые строки — ручные.
            if 'source' not in table_columns(connection, 'accountant_handover_days'):
                connection.execute('ALTER TABLE accountant_handover_days ADD COLUMN source TEXT')
            # Подтверждение бухгалтера: сколько реально получено (amount), каким
            # был расчёт в момент подтверждения (expected_amount), кто и когда.
            # После подтверждения кассир передачу не отменит и не перепишет.
            handover_columns = table_columns(connection, 'accountant_handover_days')
            for column in ('expected_amount', 'confirmed_at', 'confirmed_by'):
                if column not in handover_columns:
                    connection.execute(f'ALTER TABLE accountant_handover_days ADD COLUMN {column} TEXT')
            # Расход Шоха по счёт-фактуре: на каком базаре (страница «Баланс Шохруха»).
            if 'place' not in table_columns(connection, 'accountant_reserves'):
                connection.execute('ALTER TABLE accountant_reserves ADD COLUMN place TEXT')

        from .handover_dates import migrate_handover_dates
        if migrate_handovers:
            with closing(self._open()) as connection:
                migrate_handover_dates(connection)

    def reserves(self, day: date):
        from .reserves import reserve_summary
        return reserve_summary(self, day)

    @once
    def reserve_entries(self, account: str) -> list[dict]:
        """Все записи одного резерва за всё время, одним чтением."""
        from .reserves import _entries
        with closing(self._open()) as connection:
            return _entries(connection, account)

    @once
    def expense_totals_between(self, start: date, end: date):
        """Paid expenses recorded by accounting, excluding moves of our own cash.

        The founder subtracts these from iiko's net profit, which already
        contains the cost of goods that passed through iiko stock. The rule for
        procurement, so goods are counted exactly once:

        * Cash handed to Шох (`procurement_advance`) is not an expense: it only
          moves our money to him. What he buys becomes an iiko incoming invoice
          and reaches the profit as iiko cost of goods; accepting his purchase
          is a reserve entry. Neither is subtracted here.
        * Procurement the accountant pays a supplier directly — «Закуп» items
          paid in cash (`other_expense`) — is a paid expense: no iiko invoice is
          created for it, so it is subtracted.
        * A supplier bank transfer is exactly that direct procurement, paid by
          transfer instead of cash: no invoice is created, so it is subtracted
          the same way and is reported inside `other`, like the «Закуп» items.
          (If such goods were ever posted to iiko by hand, both the transfer and
          the matching «Закуп» cash item would double count — same rule.)
        """
        if start > end:
            raise ValueError('expense range start must not exceed end')
        bounds = (accounting_range_start(start, end).isoformat(), end.isoformat())
        with closing(self._open()) as connection:
            movements = sum((Decimal(row[0]) for row in connection.execute(
                "SELECT amount FROM accountant_movements WHERE day >= ? AND day <= ? "
                "AND kind = 'other_expense'", bounds)), Decimal(0))
            transfers = sum((Decimal(row[0]) for row in connection.execute(
                'SELECT amount FROM accountant_supplier_transfers WHERE day >= ? AND day <= ?',
                bounds)), Decimal(0))
            salaries = sum((Decimal(row[0]) for row in connection.execute(
                'SELECT amount FROM accountant_salary_payments '
                'WHERE paid_day >= ? AND paid_day <= ?', bounds)), Decimal(0))
        other = movements + transfers
        return {'other': other, 'salary': salaries, 'total': other + salaries}

    @once
    def cash_flows_between(self, start: date, end: date) -> list[dict]:
        """Все движения денег бухгалтера за период одной выборкой — для недели
        и месяца учредителя. Зарплатные выплаты идут со своей группой, по ней
        видно, сменная это выплата или оклад; перевод в сейф — отдельной строкой,
        потому что только он и есть отложенные дивиденды."""
        if start > end:
            raise ValueError('cash flow range start must not exceed end')
        bounds = (accounting_range_start(start, end).isoformat(), end.isoformat())
        with closing(self._open()) as connection:
            rows = [dict(day=row[0], type=row[1], item_code=row[2], amount=row[3],
                         description=row[4])
                    for row in connection.execute(
                        'SELECT day, kind, item_code, amount, description FROM accountant_movements '
                        "WHERE day >= ? AND day <= ? AND kind IN "
                        "('other_expense','procurement_advance','other_receipt','cashier_transfer') "
                        'ORDER BY day, id', bounds)]
            rows += [dict(day=row[0], type='salary_payment', item_code=None, amount=row[1],
                          group=row[2])
                     for row in connection.execute(
                         'SELECT p.paid_day, p.amount, a.group_name FROM accountant_salary_payments p '
                         'JOIN accountant_accruals a ON a.id = p.accrual_id '
                         'WHERE p.paid_day >= ? AND p.paid_day <= ? ORDER BY p.paid_day, p.id', bounds)]
            rows += [dict(day=row[0], type='reserve_transfer', item_code=None, amount=row[1])
                     for row in connection.execute(
                         "SELECT day, amount FROM accountant_reserves WHERE kind = 'transfer' "
                         "AND account = 'dividends' AND day >= ? AND day <= ? ORDER BY day, id", bounds)]
            handovers = {row[0]: row[1] for row in connection.execute(
                'SELECT day, amount FROM accountant_handover_days WHERE day >= ? AND day <= ?', bounds)}
        rows += [dict(day=day, type='handover', item_code=None, amount=amount)
                 for day, amount in handovers.items()]
        return sorted(rows, key=lambda row: row['day'])

    def reserve_entry(self, day, account, kind, amount, note, *, cashier_amount=None):
        from .reserves import add_reserve_entry
        return add_reserve_entry(self, day, account, kind, amount, note, cashier_amount)

    def set_monthly_plan(self, day, amount, note):
        from .reserves import set_monthly_plan
        return set_monthly_plan(self, day, amount, note)

    def record_handover(self, day: date, amount: Decimal, *, add=False, create_only=False,
                        source='accountant', replace_sources=None):
        """Приход от кассира за день получения (смена предыдущего дня).

        `source` — кто записал: 'cashier' (кнопка кассира), 'accountant' (ручная
        запись и исправления), 'auto' (расчёт iiko вне ручного режима). Кассир
        передаёт `replace_sources`, и запись бухгалтера его кнопка не перепишет.
        Та же сумма повторно ничего не меняет: время записи остаётся временем
        передачи («получено 21:40»), а не последней выплатой бухгалтера."""
        amount = amount_value(amount, allow_zero=True)
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            ensure_open(connection, day)
            row = connection.execute(
                'SELECT day, amount, checked_at, source FROM accountant_handover_days WHERE day = ?',
                (day.isoformat(),)).fetchone()
            before = dict(day=row[0], amount=row[1], checked_at=row[2], source=row[3]) if row else None
            if row and create_only:
                raise LedgerError('Приход за этот день уже записан. Используйте исправление дневного итога.')
            if row and not add and Decimal(row[1]) == amount:
                return
            confirmed = row is not None and connection.execute(
                'SELECT confirmed_at FROM accountant_handover_days WHERE day = ?',
                (day.isoformat(),)).fetchone()[0] is not None
            if confirmed and source == 'auto':
                return
            if confirmed and replace_sources is not None:
                raise LedgerError('Бухгалтер уже подтвердил получение кассы — передачу не изменить.')
            if row and replace_sources is not None and row[3] not in replace_sources:
                raise LedgerError('Приход за этот день уже записал бухгалтер. Исправить его может только он.')
            if row and add:
                amount = amount_value(Decimal(row[1]) + amount, allow_zero=True)
            connection.execute('INSERT INTO accountant_handover_days (day, amount, checked_at, source) '
                               'VALUES (?, ?, ?, ?) '
                               'ON CONFLICT(day) DO UPDATE SET amount=excluded.amount, '
                               'checked_at=excluded.checked_at, source=excluded.source',
                               (day.isoformat(), str(amount), datetime.now().isoformat(), source))
            # Остаток меняется, только если меняется приход, который в него входит:
            # неподтверждённая передача кассира ничего не двигает и проверять нечего.
            stamp = 'confirmed' if confirmed else None
            was = counted_handover(row[1], row[3], stamp) if row else '0'
            if Decimal(was) != Decimal(counted_handover(str(amount), source, stamp)):
                self._check_known_future_balances(connection, day)
            row = connection.execute(
                'SELECT day, amount, checked_at, source FROM accountant_handover_days WHERE day = ?',
                (day.isoformat(),)).fetchone()
            after = dict(day=row[0], amount=row[1], checked_at=row[2], source=row[3])
            if before is None or before['amount'] != after['amount']:
                record_audit(connection, 'handover', day.isoformat(),
                             'create' if before is None else 'update', before, after)

    def handover_state(self, day: date, current_expected=None, *, cashier_active=None) -> dict | None:
        """Приход кассира за день для экранов: сумма, когда записан и кем.

        Недостача считается здесь и только здесь — одно число для тоста 2a,
        карточки «Деньги на расходы», «Проверок», учредителя и Excel:
        shortfall = текущий расчёт кассира − полученное (не меньше 0).
        `current_expected` — текущий расчёт (iiko + расходы кассы и выдачи Шоху,
        cashier.till.expected_from_saved); без него — расчёт на момент
        подтверждения. Сверяется полученное бухгалтером (`checked`):
        подтверждённая передача — всегда, его собственная ручная запись
        прихода — только если кассир в этот день работал в панели
        (`cashier_active`, cashier.till.cashier_active). Без этого расчёт iiko
        не знает реальных расходов кассы, и «недостача» была бы ложной (прод
        ведёт приход вручную, модуль кассира только внедряется). Передача
        кассира до подтверждения и авто-расчёт iiko — сами расчёт, их не сверяем.
        Касса изменилась после подтверждения (`expected_changed`) — расчёт
        сейчас не тот, что был при подтверждении: бухгалтер подтверждает снова."""
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT amount, checked_at, source, expected_amount, confirmed_at, confirmed_by '
                'FROM accountant_handover_days WHERE day = ?',
                (day.isoformat(),)).fetchone()
        if row is None:
            return None
        confirmed = row[4] is not None
        source = row[2] or 'accountant'
        stored = Decimal(row[3]) if confirmed and row[3] is not None else None
        current = Decimal(str(current_expected)) if current_expected is not None else None
        checked = confirmed or (source == 'accountant' and bool(cashier_active))
        calculation = (current if current is not None else stored) if checked else None
        shortfall = max(Decimal(0), calculation - Decimal(row[0])) if calculation is not None else None
        changed = (confirmed and stored is not None and current is not None
                   and abs(current - stored) >= Decimal('0.01'))
        return dict(amount=row[0], handed_at=local_timestamp(row[1]), source=source,
                    confirmed_at=local_timestamp(row[4]) if confirmed else None,
                    confirmed_by=row[5] if confirmed else None,
                    # Расчёт, записанный при подтверждении (у кассира — «после
                    # подтверждения сумма изменилась на …»).
                    expected_amount=row[3] if confirmed else None,
                    checked=checked,
                    # Работал ли кассир в панели в этот день (None — не проверяли).
                    cashier_active=cashier_active,
                    # Расчёт, с которым сверено полученное, и недостача к нему.
                    calculation=plain(calculation) if calculation is not None else None,
                    shortfall=plain(shortfall) if shortfall is not None else None,
                    expected_changed=bool(changed))

    def confirm_handover(self, day: date, received, expected, approver: str) -> dict:
        """Бухгалтер подтверждает, сколько наличных от кассира реально получил.

        Расчёт — то, что кассир передал кнопкой (или расчёт iiko, если кнопки
        ещё не было); при повторном подтверждении он не меняется. Полученная
        сумма становится приходом дня: остаток считается от неё. Разницу видно
        как недостачу («От кассира получено меньше расчёта»), а не обнуляют.
        После подтверждения кассир передачу не отменит и не перепишет."""
        received = amount_value(received, allow_zero=True)
        approver = required_text(approver, 'кто подтвердил')
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            ensure_open(connection, day)
            columns = ('day', 'amount', 'checked_at', 'source', 'expected_amount', 'confirmed_at', 'confirmed_by')
            row = connection.execute(
                f'SELECT {", ".join(columns)} FROM accountant_handover_days WHERE day = ?',
                (day.isoformat(),)).fetchone()
            before = dict(zip(columns, row)) if row else None
            if expected is not None:
                # Текущий расчёт кассы — тот, что в «Проверках» и у учредителя.
                # Повторное подтверждение после правок кассира берёт уже новый.
                calculation = str(Decimal(str(expected)))
            elif before and before['confirmed_at'] is not None:
                calculation = before['expected_amount']
            elif before and before['source'] in ('cashier', 'auto'):
                calculation = before['amount']
            else:
                # Своя ручная запись бухгалтера без расчёта iiko — сверять не с чем.
                calculation = None
            if calculation is None:
                calculation = str(received)
            now = datetime.now().isoformat()
            connection.execute(
                'INSERT INTO accountant_handover_days '
                '(day, amount, checked_at, source, expected_amount, confirmed_at, confirmed_by) '
                'VALUES (?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(day) DO UPDATE SET amount=excluded.amount, '
                'expected_amount=excluded.expected_amount, confirmed_at=excluded.confirmed_at, '
                'confirmed_by=excluded.confirmed_by',
                (day.isoformat(), str(received), now, 'accountant', str(calculation), now, approver))
            row = connection.execute(
                f'SELECT {", ".join(columns)} FROM accountant_handover_days WHERE day = ?',
                (day.isoformat(),)).fetchone()
            record_audit(connection, 'handover', day.isoformat(), 'confirm', before, dict(zip(columns, row)))
        return self.handover_state(day, current_expected=expected)

    @once
    def handover_for_day(self, day: date) -> Decimal | None:
        with closing(self._open()) as connection:
            row = connection.execute('SELECT amount FROM accountant_handover_days WHERE day = ?',
                                     (day.isoformat(),)).fetchone()
        return Decimal(row[0]) if row else None

    def delete_handover(self, day: date, *, only_source=None):
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            ensure_open(connection, day)
            if only_source is not None:
                # Кассир отменяет только свою передачу: ручную запись бухгалтера — нет.
                owner = connection.execute('SELECT source FROM accountant_handover_days WHERE day = ?',
                                           (day.isoformat(),)).fetchone()
                if owner is None:
                    return
                if owner[0] != only_source:
                    raise LedgerError('Приход за этот день записал бухгалтер. Отменить его может только он.')
                confirmed = connection.execute(
                    'SELECT confirmed_at FROM accountant_handover_days WHERE day = ?',
                    (day.isoformat(),)).fetchone()[0]
                if confirmed is not None:
                    raise LedgerError('Бухгалтер уже подтвердил получение кассы — отменить передачу нельзя.')
            anchor = connection.execute(f'SELECT day FROM {cash_opening_table(day)} WHERE id=1').fetchone()
            # Неподтверждённую передачу кассир отменяет, пока бухгалтер не потратил
            # эти деньги: операции дня сами по себе не мешают (зарплату выдают
            # утром, а кассу передают вечером). Проверка — остаток без этой
            # передачи не уходит в минус ни в этот, ни в следующие дни.
            # Неподтверждённая передача кассира в остаток не входит (counted_handover),
            # поэтому её отмена остатков не меняет — проверять нечего.
            dependent = None if only_source is not None else connection.execute(
                'SELECT day FROM accountant_handover_days WHERE day > ? '
                'UNION SELECT day FROM accountant_movements WHERE day >= ? '
                'UNION SELECT paid_day FROM accountant_salary_payments WHERE paid_day >= ? '
                'UNION SELECT day FROM accountant_reserves WHERE day >= ?',
                (day.isoformat(),) * 4).fetchone()
            if (anchor and anchor[0] == day.isoformat()) or dependent:
                raise LedgerError('Приход используется в остатках. Исправьте сумму вместо удаления.')
            row = connection.execute(
                'SELECT day, amount, checked_at FROM accountant_handover_days WHERE day = ?',
                (day.isoformat(),)).fetchone()
            before = dict(day=row[0], amount=row[1], checked_at=row[2]) if row else None
            connection.execute('DELETE FROM accountant_handover_days WHERE day = ?', (day.isoformat(),))
            if before is not None:
                record_audit(connection, 'handover', day.isoformat(), 'delete', before, None)

    @once
    def audit_entries(self, *, entity_type=None, entity_id=None):
        with closing(self._open()) as connection:
            return read_audit_entries(
                connection, entity_type=entity_type, entity_id=entity_id)

    @staticmethod
    def _row_dict(connection, table: str, row_id: int):
        cursor = connection.execute(f'SELECT * FROM {table} WHERE id = ?', (row_id,))
        row = cursor.fetchone()
        return dict(zip((column[0] for column in cursor.description), row)) if row else None

    def delete_operation(self, operation_type: str, operation_id: int, day: date):
        tables = {
            'movement': 'accountant_movements',
            'salary_payment': 'accountant_salary_payments',
            'debt_payment': 'accountant_debt_payments',
            'reserve_transfer': 'accountant_reserves',
            'debt': 'accountant_debts',
        }
        table = tables.get(operation_type)
        if table is None:
            raise LedgerError('Эту операцию нельзя удалить здесь.')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                ensure_open(connection, day)
                before = self._row_dict(connection, table, operation_id)
                if before is None:
                    raise LedgerError('Операция не найдена.')
                stored_day = before['paid_day'] if operation_type == 'salary_payment' else before['day']
                if stored_day != day.isoformat():
                    raise LedgerError('Нельзя изменить дату операции.')
                if operation_type == 'salary_payment':
                    self._guard_manual_salary_payment(connection, before['accrual_id'])
                if operation_type == 'reserve_transfer':
                    if before['kind'] != 'transfer':
                        raise LedgerError('Эту резервную операцию нельзя удалить здесь.')
                    connection.execute('DELETE FROM accountant_reserves WHERE id = ?', (operation_id,))
                    from .reserves import _entries, first_negative_day
                    if first_negative_day(_entries(connection, before['account'])):
                        raise LedgerError('Удаление делает остаток отрицательным в последующие дни.')
                elif operation_type == 'debt':
                    # Ошибочно записанный долг удаляется целиком — вместе с оплатами
                    # того же дня. Оплату в другой день сначала удаляют в том дне:
                    # иначе молча поменялся бы остаток чужого дня.
                    payments = connection.execute(
                        'SELECT id, day, movement_id FROM accountant_debt_payments WHERE debt_id = ?',
                        (operation_id,)).fetchall()
                    if any(payment[1] != before['day'] for payment in payments):
                        raise LedgerError('По долгу есть оплаты в другие дни — сначала удалите их там.')
                    for payment_id, _, movement_id in payments:
                        for table_name, row_id in (('accountant_debt_payments', payment_id),
                                                   ('accountant_movements', movement_id)):
                            removed = self._row_dict(connection, table_name, row_id)
                            connection.execute(f'DELETE FROM {table_name} WHERE id = ?', (row_id,))
                            record_audit(connection, 'debt_payment' if table_name.endswith('payments')
                                         else 'movement', row_id, 'delete', removed, None)
                    connection.execute('DELETE FROM accountant_debts WHERE id = ?', (operation_id,))
                elif operation_type == 'debt_payment':
                    connection.execute('DELETE FROM accountant_debt_payments WHERE id = ?', (operation_id,))
                    connection.execute('DELETE FROM accountant_movements WHERE id = ?',
                                       (before['movement_id'],))
                elif operation_type == 'movement':
                    from .extra_payouts import guard_movement
                    guard_movement(connection, operation_id)
                    linked = connection.execute(
                        'SELECT id FROM accountant_debt_payments WHERE movement_id = ?',
                        (operation_id,)).fetchone()
                    if linked:
                        connection.execute('DELETE FROM accountant_debt_payments WHERE id = ?', (linked[0],))
                    connection.execute('DELETE FROM accountant_movements WHERE id = ?', (operation_id,))
                    self._check_cash_balances(connection, day)
                else:
                    connection.execute('DELETE FROM accountant_salary_payments WHERE id = ?', (operation_id,))
                self._check_reserve_balances(connection)
                record_audit(connection, operation_type, operation_id, 'delete', before, None)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def set_cash_opening(self, day: date, amount, note):
        if day >= ACCOUNTING_START and day != ACCOUNTING_START:
            raise LedgerError('Начальный остаток нужно указать на 02.10.2026 — первый день учёта.')
        table = cash_opening_table(day)
        value = amount_value(amount, allow_zero=True)
        note = required_text(note, 'основание начального остатка')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                ensure_open(connection, day)
                handover = connection.execute('SELECT 1 FROM accountant_handover_days WHERE day = ?',
                                              (day.isoformat(),)).fetchone()
                if handover is None:
                    raise LedgerError('Сначала запишите передачу кассы за первый день учёта.')
                inserted = connection.execute(
                    f'INSERT INTO {table} VALUES (1,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                    (day.isoformat(), str(value), note, datetime.now().isoformat()))
                if inserted.rowcount != 1:
                    raise LedgerError('Начальный остаток денег уже указан.')
                after = self._row_dict(connection, table, 1)
                record_audit(connection, 'cash_opening', day.isoformat(), 'create', None, after)
                self._check_known_future_balances(connection, day)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @once
    def accounting_start(self, through: date = ACCOUNTING_START) -> date | None:
        """Рабочий учёт — со 2 октября; архив — со своей первой записи."""
        if through >= ACCOUNTING_START:
            return ACCOUNTING_START
        with closing(self._open()) as connection:
            first = connection.execute('SELECT MIN(day) FROM accountant_handover_days').fetchone()[0]
            anchor = connection.execute('SELECT day FROM accountant_cash_opening WHERE id=1').fetchone()
        days = [value for value in (first, anchor[0] if anchor else None) if value]
        return date.fromisoformat(min(days)) if days else None

    # Запрос зависит только от того, какая это таблица остатков, а не от дня:
    # неделя учредителя спрашивала одно и то же семь раз.
    @once(key=lambda day=ACCOUNTING_START: cash_opening_table(day))
    def cash_opening(self, day: date = ACCOUNTING_START):
        with closing(self._open()) as connection:
            row = connection.execute(f'SELECT day,amount,note FROM {cash_opening_table(day)} WHERE id=1').fetchone()
        return dict(day=row[0], amount=row[1], note=row[2]) if row else None

    def cash_book(self, connection, through: date) -> CashBook:
        """Книга остатков за один безопасный запрос читается один раз.

        Строки книги берутся по `through` включительно, а `position`/`flow`
        отбирают из них нужный день, поэтому книга, прочитанная по более
        поздний день, обслуживает и более ранние. Неделя учредителя просит её
        по последний день недели до сборки дней (`warm_cash_book`) — и семь
        книг, каждая из которых читает всю историю, становятся одной.

        Вне безопасного метода кэша нет, и книга читается заново: запись
        должна видеть собственную незавершённую транзакцию.
        """
        holder = cached_call(('cash_book', id(self)), lambda: {'book': None})
        book = holder['book']
        if book is None or book.through < through.isoformat():
            holder['book'] = book = CashBook(connection, through)
        return book

    def warm_cash_book(self, through: date) -> None:
        """Прочитать книгу остатков по `through` заранее — до сборки дней."""
        with closing(self._open()) as connection:
            self.cash_book(connection, through)

    def cash_position(self, connection, day: date, start_day: date | None = None, *, current_amount=None,
                      tolerate_gaps: bool = False):
        """Carry verified daily handovers forward; never silently fill an unobserved day.

        `tolerate_gaps` — только для режима проверки (ACCOUNTANT_CHECK_MODE):
        пропущенный день считается нулевым приходом вместо отказа. Вне режима
        дыра в цепочке обязана останавливать операцию: перенесённый через неё
        остаток был бы выдумкой, а не деньгами.
        """
        return CashBook(connection, day).position(day, start_day, current_amount=current_amount,
                                                  tolerate_gaps=tolerate_gaps)

    def available_cash(self, connection, day: date, cashier_amount: Decimal | None):
        if day >= ACCOUNTING_START and not connection.execute(
                'SELECT 1 FROM accountant_working_cash_opening WHERE id=1').fetchone():
            raise LedgerError('Укажите подтверждённый начальный остаток на 02.10.2026.')
        has_handovers = connection.execute('SELECT 1 FROM accountant_handover_days LIMIT 1').fetchone()
        if cashier_amount is None and not has_handovers and day < ACCOUNTING_START:
            return self._cash_balance(connection, day)
        if cashier_amount is not None and not connection.execute(
                'SELECT 1 FROM accountant_handover_days WHERE day=?', (day.isoformat(),)).fetchone():
            # Расчёт, а не полученные деньги: в остаток войдёт после подтверждения.
            connection.execute('INSERT INTO accountant_handover_days (day, amount, checked_at, source) '
                               'VALUES (?,?,?,?)',
                               (day.isoformat(), str(cashier_amount), datetime.now().isoformat(), 'auto'))
            record_audit(connection, 'handover', day.isoformat(), 'create', None,
                         dict(day=day.isoformat(), amount=str(cashier_amount), source='cash_operation'))
        opening, closing, missing, _ = self.cash_position(
            connection, day, tolerate_gaps=self.allow_negative_cash)
        if missing:
            raise LedgerError(f'Для переноса остатка загрузите данные кассира за {missing}.')
        return closing

    def _require_cash(self, connection, day: date, value: Decimal, cashier_amount) -> None:
        """Хватает ли наличных на операцию за день.

        В режиме проверки не спрашиваем, но `available_cash` всё равно зовём:
        у неё есть побочный эффект — она записывает приход за день, без него
        остаток дня остался бы неизвестным.
        """
        available = self.available_cash(connection, day, cashier_amount)
        if not self.allow_negative_cash and available < value:
            raise LedgerError('На выбранный день недостаточно денег от кассира.')

    def _check_cash_balances(self, connection, day):
        if self.allow_negative_cash:
            return
        if connection.execute('SELECT 1 FROM accountant_handover_days LIMIT 1').fetchone():
            self._check_known_future_balances(connection, day)
        else:
            self._check_future_balances(connection, day)

    @staticmethod
    def _check_reserve_balances(connection):
        from .reserves import _entries, first_negative_day
        for account in ('shoh', 'dividends', 'usd'):
            if first_negative_day(_entries(connection, account)):
                raise LedgerError('Операция делает остаток подотчёта или резерва отрицательным.')

    def _check_known_future_balances(self, connection, day: date):
        if self.allow_negative_cash:
            return
        for (recorded,) in connection.execute(
                'SELECT day FROM accountant_handover_days WHERE day>=? ORDER BY day',
                (day.isoformat(),)):
            _, balance, missing, _ = self.cash_position(connection, date.fromisoformat(recorded))
            if missing is None and balance < 0:
                raise LedgerError('Операция делает остаток отрицательным в последующие дни.')

    def debt_summary(self, day: date):
        from .debts import debt_summary
        return debt_summary(self, day)

    def record_debt(self, day, item_code, note, total, paid, *, cashier_amount=None):
        from .debts import record_debt
        return record_debt(self, day, item_code, note, total, paid, cashier_amount)

    def pay_debt(self, debt_id, day, amount, *, cashier_amount=None):
        from .debts import pay_debt
        return pay_debt(self, debt_id, day, amount, cashier_amount)

    @once
    def exceptions_for_day(self, day: date) -> set[int]:
        with closing(self._open()) as connection:
            rows = connection.execute('SELECT employee_id FROM accountant_exceptions WHERE day = ?',
                                      (day.isoformat(),)).fetchall()
        return {row[0] for row in rows}

    @once
    def is_payroll_confirmed(self, day: date) -> bool:
        """Смена дня закрыта: начислено всем сотрудникам."""
        with closing(self._open()) as connection:
            return connection.execute('SELECT 1 FROM accountant_payroll_days WHERE day = ?',
                                      (day.isoformat(),)).fetchone() is not None

    @once
    def paid_employees(self, day: date) -> set[int]:
        """Кому выдали деньги за эту смену (дата выдачи может быть другой).

        Читаем сохранённые выплаты, поэтому отмена ошибочной выдачи убирает
        и подтверждение присутствия; старые выплаты работают без миграции.
        """
        with closing(self._open()) as connection:
            return {row[0] for row in connection.execute(
                'SELECT a.employee_id, p.amount FROM accountant_accruals a '
                'JOIN accountant_salary_payments p ON p.accrual_id = a.id '
                'WHERE a.work_day = ?', (day.isoformat(),)) if Decimal(row[1]) > 0}

    @once
    def day_accruals(self, day: date) -> dict[int, dict]:
        """Начисления смены дня одним запросом: {сотрудник: {id, rate, amount}}."""
        with closing(self._open()) as connection:
            return {row[0]: dict(id=row[1], rate=row[2], amount=row[3]) for row in connection.execute(
                'SELECT employee_id, id, rate, amount FROM accountant_accruals WHERE work_day = ?',
                (day.isoformat(),))}

    @once
    def accrued_employees(self, day: date) -> dict[int, int]:
        """Кому смена дня уже начислена: {сотрудник: id начисления}."""
        return {employee_id: item['id'] for employee_id, item in self.day_accruals(day).items()}

    @staticmethod
    def _employee_closed(connection, employee_id: int, day: date) -> bool:
        """День сотрудника закрыт: ему уже начислено или вся смена подтверждена.

        Подтверждённая целиком смена закрыта и для людей, добавленных в реестр
        позже: новый сотрудник действует «с начала времён» и иначе попал бы в
        прошлую закрытую смену."""
        return bool(connection.execute(
            'SELECT 1 FROM accountant_accruals WHERE work_day = ? AND employee_id = ? '
            'UNION SELECT 1 FROM accountant_payroll_days WHERE day = ?',
            (day.isoformat(), employee_id, day.isoformat())).fetchone())

    def grant_exception(self, employee_id: int, day: date, reason: str, approver: str):
        reason = required_text(reason, 'причину исключения')
        approver = required_text(approver, 'имя подтвердившего')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                lock_day(connection, day)
                ensure_open(connection, day)
                if self._employee_closed(connection, employee_id, day):
                    raise LedgerError('Начисление этому сотруднику за день уже подтверждено.')
                if connection.execute('SELECT 1 FROM accountant_exceptions WHERE employee_id = ?',
                                      (employee_id,)).fetchone():
                    raise LedgerError('Однодневное исключение уже использовано для этого сотрудника.')
                connection.execute('INSERT INTO accountant_exceptions '
                                   '(employee_id, day, reason, approver, created_at) VALUES (?, ?, ?, ?, ?)',
                                   (employee_id, day.isoformat(), reason, approver, datetime.now().isoformat()))
                record_audit(connection, 'exception', employee_id, 'create', None,
                             dict(employee_id=employee_id, day=day.isoformat(), reason=reason,
                                  approver=approver))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def confirm_payroll(self, day: date, rows: list[PayrollRow], approver: str, *,
                        marks: dict[int, bool] | None = None,
                        employee_ids=None) -> 'PayrollConfirmation':
        """Начислить смену дня — по людям.

        `rows` — вся смена дня (весь реестр на этот день). Начисляется каждый,
        чью сумму можно посчитать и кому ещё не начислено; остальные ждут и
        возвращаются в `blockers` с причиной (нет ставки, нет привязки
        Hikvision, данные неполные). Повторный вызов начисляет тех, кого с тех
        пор разблокировали. `employee_ids` ограничивает, кого начислять сейчас.
        День считается подтверждённым целиком (строка в accountant_payroll_days)
        только когда начислено всем сотрудникам смены; после этого смена
        закрыта.

        `marks` — явные отметки «был / не был», по которым посчитаны `rows`.
        Для начисляемых строк они сверяются со свежими внутри транзакции:
        отметка, поставленная после расчёта, не должна потеряться.
        """
        from .payroll import blocker_reason
        approver = required_text(approver, 'имя подтвердившего')
        if not rows:
            raise LedgerError('Нельзя подтвердить пустую смену: в реестре на этот день нет сотрудников.')
        if len({row.employee_id for row in rows}) != len(rows):
            raise LedgerError('В начислении повторяется сотрудник.')
        wanted = None if employee_ids is None else {int(value) for value in employee_ids}
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                lock_day(connection, day)
                ensure_open(connection, day)
                from .salary_day import MANUAL_STATUS
                if connection.execute(
                        'SELECT 1 FROM accountant_accruals WHERE work_day=? AND attendance_status=?',
                        (day.isoformat(), MANUAL_STATUS)).fetchone():
                    raise LedgerError('За смену уже введена ручная зарплата. Используйте таблицу «Зарплата · день».')
                if connection.execute('SELECT 1 FROM accountant_payroll_days WHERE day = ?',
                                      (day.isoformat(),)).fetchone():
                    connection.rollback()
                    return PayrollConfirmation(already_confirmed=True, confirmed=True)
                if connection.execute(
                        "SELECT 1 FROM accountant_movements WHERE day=? AND kind='other_expense' "
                        "AND item_code IN ('salary_cashier','salary_staff','salary_technical','salary_carryover')",
                        (day.isoformat(),)).fetchone():
                    raise LedgerError('За день уже записана зарплата без сотрудника. Сверьте ручные выплаты перед начислением.')
                already = {row[0] for row in connection.execute(
                    'SELECT employee_id FROM accountant_accruals WHERE work_day = ?', (day.isoformat(),))}
                pending = [row for row in rows if row.employee_id not in already
                           and (wanted is None or row.employee_id in wanted)]
                ready = [row for row in pending if blocker_reason(row) is None]
                blockers = [dict(employee_id=row.employee_id, name=row.name, reason=blocker_reason(row))
                            for row in pending if blocker_reason(row) is not None]
                if marks is not None and ready:
                    from .hikvision import read_manual_marks
                    fresh = read_manual_marks(connection, day)
                    if any(fresh.get(row.employee_id) != marks.get(row.employee_id) for row in ready):
                        raise LedgerError('Отметки «был / не был» за этот день уже изменились. '
                                          'Обновите смену и подтвердите снова.')
                for row in ready:
                    amount_value(row.payable, allow_zero=True)
                    amount_value(row.rate, allow_zero=True)
                accrued = []
                for row in ready:
                    cursor = connection.execute(
                        'INSERT INTO accountant_accruals '
                        '(work_day, employee_id, employee_name, group_name, attendance_status, rate, amount) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (day.isoformat(), row.employee_id, row.name, row.group_name,
                         row.status, str(row.rate), str(row.payable)))
                    record_audit(connection, 'accrual', cursor.lastrowid, 'create', None,
                                 self._row_dict(connection, 'accountant_accruals', cursor.lastrowid))
                    accrued.append(row.employee_id)
                complete = all(row.employee_id in already or row.employee_id in accrued for row in rows)
                if complete:
                    connection.execute('INSERT INTO accountant_payroll_days (day, approver, confirmed_at) '
                                       'VALUES (?, ?, ?)', (day.isoformat(), approver, datetime.now().isoformat()))
                    record_audit(connection, 'payroll_day', day.isoformat(), 'create', None,
                                 dict(day=day.isoformat(), approver=approver))
                connection.commit()
                return PayrollConfirmation(accrued=accrued, blockers=blockers, confirmed=complete,
                                           accrued_before=sorted(already))
            except Exception:
                connection.rollback()
                raise

    def mark_manual_attendance(self, employee_id: int, day: date, present: bool,
                               approver: str) -> bool:
        """Отметка «был / не был» за день — одной транзакцией с проверкой.

        Отметку меняют, пока этому сотруднику за день ничего не начислено (и
        смена не закрыта целиком). Проверка и запись идут на одном соединении
        под той же блокировкой дня, что и начисление, поэтому отметка не
        проскочит в начисленный день. Каждая смена отметки — запись аудита с
        «до» и «после». Повтор той же отметки ничего не пишет.
        Возвращает, изменилось ли что-нибудь.
        """
        from .hikvision import read_manual_mark, write_manual_mark
        approver = required_text(approver, 'кто отметил')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                lock_day(connection, day)
                ensure_open(connection, day)
                if self._employee_closed(connection, employee_id, day):
                    raise LedgerError('Начисление этому сотруднику за день уже подтверждено — '
                                      'отметку не изменить.')
                before = read_manual_mark(connection, employee_id, day)
                if before is not None and before['present'] == bool(present):
                    connection.rollback()
                    return False
                write_manual_mark(connection, employee_id, day, bool(present), approver)
                after = read_manual_mark(connection, employee_id, day)
                record_audit(connection, 'manual_attendance', f'{day.isoformat()}:{employee_id}',
                             'create' if before is None else 'update', before, after)
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def set_salary_day_cell(self, paid_day, employee_id, amount, expected_amount, *, cashier_amount=None):
        from .salary_day import set_cell
        return set_cell(self, paid_day, employee_id, amount, expected_amount, cashier_amount=cashier_amount)

    def salary_day_month(self, first, last, attendance=None):
        from .salary_day import month_data
        return month_data(self, first, last, attendance)

    def add_extra_payout(self, **values):
        from .extra_payouts import add
        return add(self, **values)

    def update_extra_payout(self, payout_id, **values):
        from .extra_payouts import update
        return update(self, payout_id, **values)

    def delete_extra_payout(self, payout_id, *, by=None):
        from .extra_payouts import delete
        return delete(self, payout_id, by=by)

    def extra_payout(self, payout_id):
        from .extra_payouts import read
        with closing(self._open()) as connection:
            return read(connection, payout_id)

    @once
    def extra_payouts(self, first: date, last: date) -> list[dict]:
        """Доп. выплаты по дню выплаты за период (журнал дня, ведомость)."""
        from .extra_payouts import between
        with closing(self._open()) as connection:
            return between(connection, first, last)

    def payroll_month(self, first: date, last: date) -> dict:
        """Shift accruals and their payments for a whole month, in one pass.

        The monthly sheet needs a cell per employee and day. Asking for each day
        separately meant thirty round trips over the same two tables, so the
        range is read once and grouped here.
        """
        first = accounting_range_start(first, last)
        from .reserves import is_monthly_salary  # reserves импортирует ledger
        with closing(self._open()) as connection:
            accruals = connection.execute(
                'SELECT id, work_day, employee_id, employee_name, group_name, '
                'attendance_status, rate, amount FROM accountant_accruals '
                'WHERE work_day >= ? AND work_day <= ? ORDER BY employee_id, work_day',
                (first.isoformat(), last.isoformat())).fetchall()
            payments = defaultdict(Decimal)
            # Сами выплаты по начислению: по ним ячейку «✓» можно отменить.
            payment_rows = defaultdict(list)
            for payment_id, accrual_id, paid_day, amount in connection.execute(
                    'SELECT p.id, p.accrual_id, p.paid_day, p.amount FROM accountant_salary_payments p '
                    'JOIN accountant_accruals a ON a.id = p.accrual_id '
                    'WHERE a.work_day >= ? AND a.work_day <= ? ORDER BY p.id',
                    (first.isoformat(), last.isoformat())):
                payments[accrual_id] += Decimal(amount)
                payment_rows[accrual_id].append(dict(id=payment_id, day=paid_day, amount=str(amount)))
            # «Выдано из кассы за день» — по дню выдачи, какая бы смена ни была:
            # 1-го числа выдают смену последнего дня прошлого месяца, и эти
            # деньги ушли из кассы этого месяца.
            paid_per_day = defaultdict(Decimal)
            for paid_day, amount in connection.execute(
                    'SELECT paid_day, amount FROM accountant_salary_payments '
                    'WHERE paid_day >= ? AND paid_day <= ?', (first.isoformat(), last.isoformat())):
                paid_per_day[paid_day] += Decimal(amount)
            # Оклады за месяц: расход по зарплате, не привязанный к начислению.
            # Здесь общая сумма — вместе с записями без сотрудника; разбивку по
            # людям даёт monthly_payments (только выплаты со ссылкой на человека).
            monthly_paid = sum((Decimal(row[0]) for row in connection.execute(
                'SELECT amount, item_code FROM accountant_movements '
                "WHERE day >= ? AND day <= ? AND kind = 'other_expense'",
                (first.isoformat(), last.isoformat())) if is_monthly_salary(row[1])), Decimal(0))
            closed = {row[0] for row in connection.execute(
                'SELECT day FROM accountant_payroll_days WHERE day >= ? AND day <= ?',
                (first.isoformat(), last.isoformat()))}
        # Строка — сотрудник (по id: у однофамильцев разные строки). Ставка в
        # ячейке — та, что действовала в день смены: «сумма ≠ ставке» сверяют
        # с ней; у строки — ставка последней смены месяца.
        people = {}
        for row in accruals:
            person = people.setdefault(row[2], dict(employee_id=row[2], cells={}))
            person.update(name=row[3], group=row[4], rate=str(row[6]))
            paid = payments[row[0]]
            person['cells'][row[1]] = dict(
                accrual_id=row[0], status=attendance_after_payment(row[5], paid), rate=str(row[6]), amount=str(row[7]),
                paid=str(paid), debt=str(Decimal(row[7]) - paid), payments=payment_rows[row[0]])
        for person in people.values():
            cells = person['cells'].values()
            person['accrued'] = str(sum((Decimal(c['amount']) for c in cells), Decimal(0)))
            person['paid'] = str(sum((Decimal(c['paid']) for c in cells), Decimal(0)))
            person['debt'] = str(sum((Decimal(c['debt']) for c in cells), Decimal(0)))
        # Смену начисляют по людям: у начисленных — ячейки, остальные ждут.
        # partial_days — начислено не всем; такие дни ещё не закрыты.
        accrued_days = {row[1] for row in accruals}
        return dict(shift=sorted(people.values(), key=lambda item: item['name']),
                    paid_per_day={day: str(amount) for day, amount in paid_per_day.items()},
                    monthly_paid=str(monthly_paid),
                    confirmed_days=sorted(closed),
                    partial_days=sorted(accrued_days - closed))

    def negative_cash_days(self, first: date, last: date, start_day: date | None = None) -> list[dict]:
        """Дни периода, когда остаток бухгалтера на конец дня ушёл в минус.

        Тот же расчёт, что и «Остаток» в «Финансах дня» (cash_position); день
        без данных кассира пропускается — «нет данных» не значит «минус».
        """
        first = accounting_range_start(first, last)
        result = []
        with closing(self._open()) as connection:
            # Одна книга на весь период: раньше каждый день месяца читал
            # базу заново — девять запросов на день, 270 на ведомость.
            book = CashBook(connection, last)
            day = first
            while day <= last:
                _, remaining, _, _ = book.position(day, start_day)
                if remaining is not None and remaining < 0:
                    result.append(dict(day=day.isoformat(), balance=str(remaining)))
                day += timedelta(days=1)
        return result

    @once
    def accruals(self, day: date) -> list[dict]:
        with closing(self._open()) as connection:
            rows = connection.execute('SELECT id, work_day, employee_id, employee_name, group_name, '
                                      'attendance_status, rate, amount FROM accountant_accruals '
                                      'WHERE work_day >= ? AND work_day <= ? ORDER BY work_day, id', (period_start(day).isoformat(), day.isoformat())).fetchall()
            payments = defaultdict(Decimal)
            for accrual_id, amount in connection.execute(
                    'SELECT p.accrual_id, p.amount FROM accountant_salary_payments p '
                    'JOIN accountant_accruals a ON a.id = p.accrual_id '
                    'WHERE p.paid_day <= ? AND a.work_day <= ?', (day.isoformat(), day.isoformat())):
                payments[accrual_id] += Decimal(amount)
            result = []
            for row in rows:
                paid = payments[row[0]]
                result.append(dict(id=row[0], work_day=row[1], employee_id=row[2],
                                   name=row[3], group=row[4], status=attendance_after_payment(row[5], paid), rate=str(row[6]),
                                   amount=str(row[7]), paid=str(paid),
                                   debt=str(Decimal(row[7]) - paid)))
        return result

    @staticmethod
    def _cash_balance(connection, day: date) -> Decimal:
        """Остаток бухгалтера на конец дня — три выборки за период учёта.

        Сводка дня считает его дважды (на день и на предыдущий), а неделя
        учредителя — за семь дней подряд, то есть каждый день по два раза.
        За один безопасный запрос каждый день считается однажды; внутри
        записи кэша нет, и проверки остатка видят свою транзакцию. Ключ без
        соединения — по той же причине, что и у `closure_row`."""
        return cached_call(('cash_balance', day),
                           lambda: FinanceStore._cash_balance_now(connection, day))

    @staticmethod
    def _cash_balance_now(connection, day: date) -> Decimal:
        movements = connection.execute('SELECT kind, amount FROM accountant_movements WHERE day >= ? AND day <= ?',
                                       (period_start(day).isoformat(), day.isoformat())).fetchall()
        balance = sum((Decimal(amount) if kind in ('opening', 'cashier_transfer', 'other_receipt') else -Decimal(amount)
                       for kind, amount in movements), Decimal(0))
        payments = connection.execute('SELECT amount FROM accountant_salary_payments WHERE paid_day >= ? AND paid_day <= ?',
                                      (period_start(day).isoformat(), day.isoformat())).fetchall()
        transfers = sum((Decimal(row[0]) for row in connection.execute(
            "SELECT amount FROM accountant_reserves WHERE kind = 'transfer' AND day >= ? AND day <= ?",
            (period_start(day).isoformat(), day.isoformat()))), Decimal(0))
        return balance - sum((Decimal(row[0]) for row in payments), Decimal(0)) - transfers

    @classmethod
    def _check_future_balances(cls, connection, day: date):
        dates = connection.execute(
            'SELECT day FROM accountant_movements WHERE day >= ? '
            'UNION SELECT paid_day FROM accountant_salary_payments WHERE paid_day >= ?',
            (day.isoformat(), day.isoformat())).fetchall()
        if any(cls._cash_balance(connection, date.fromisoformat(row[0])) < 0 for row in dates):
            raise LedgerError('Операция делает остаток отрицательным в последующие дни.')

    @staticmethod
    def _daily_outflows(connection, day: date) -> Decimal:
        spent = sum((Decimal(row[0]) for row in connection.execute(
            "SELECT amount FROM accountant_movements WHERE day = ? AND kind IN ('other_expense', 'procurement_advance')",
            (day.isoformat(),))), Decimal(0))
        salaries = sum((Decimal(row[0]) for row in connection.execute(
            'SELECT amount FROM accountant_salary_payments WHERE paid_day = ?',
            (day.isoformat(),))), Decimal(0))
        transfers = sum((Decimal(row[0]) for row in connection.execute(
            "SELECT amount FROM accountant_reserves WHERE kind = 'transfer' AND day = ?",
            (day.isoformat(),))), Decimal(0))
        return spent + salaries + transfers

    @staticmethod
    def _guard_manual_salary_expense(connection, day, item_code):
        if item_code not in {'salary_cashier', 'salary_staff', 'salary_technical', 'salary_carryover'}:
            return
        from .salary_day import MANUAL_STATUS
        lock_day(connection, day)
        if connection.execute(
                'SELECT 1 FROM accountant_salary_payments p '
                'JOIN accountant_accruals a ON a.id=p.accrual_id '
                'WHERE p.paid_day=? AND a.attendance_status=?',
                (day.isoformat(), MANUAL_STATUS)).fetchone():
            raise LedgerError('За день уже введена зарплата по сотрудникам. '
                              'Измените её в таблице «Зарплата · день», чтобы не задвоить общий расход.')

    @staticmethod
    def _validate_salary_expense(connection, day, item_code):
        if item_code not in {'salary_cashier', 'salary_staff', 'salary_technical', 'salary_carryover'}:
            return
        FinanceStore._guard_manual_salary_expense(connection, day, item_code)
        earned = sum((Decimal(r[0]) for r in connection.execute(
            'SELECT amount FROM accountant_accruals WHERE work_day>=? AND work_day<=?',
            (period_start(day).isoformat(), day.isoformat()))), Decimal(0))
        paid = sum((Decimal(r[0]) for r in connection.execute(
            'SELECT amount FROM accountant_salary_payments WHERE paid_day>=? AND paid_day<=?',
            (period_start(day).isoformat(), day.isoformat()))), Decimal(0))
        if earned > paid:
            raise LedgerError('Выберите начисление сотрудника в форме выплаты зарплаты; общий расход не погашает долг.')

    def _movement(self, day: date, kind: str, description: str, amount,
                  *, reference: str | None = None, item_code: str | None = None,
                  allow_zero=False, cashier_amount: Decimal | None = None):
        value = amount_value(amount, allow_zero=allow_zero)
        description = required_text(description, 'назначение')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                ensure_open(connection, day)
                if kind == 'other_expense':
                    self._validate_salary_expense(connection, day, item_code)
                if kind not in ('opening', 'cashier_transfer', 'other_receipt'):
                    self._require_cash(connection, day, value, cashier_amount)
                cursor = connection.execute('INSERT INTO accountant_movements '
                                            '(day, kind, description, amount, item_code, reference, created_at) '
                                            'VALUES (?, ?, ?, ?, ?, ?, ?)',
                                            (day.isoformat(), kind, description, str(value), item_code, reference,
                                             now_stamp()))
                record_audit(connection, 'movement', cursor.lastrowid, 'create', None,
                             self._row_dict(connection, 'accountant_movements', cursor.lastrowid))
                self._check_cash_balances(connection, day)
                connection.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                connection.rollback()
                raise LedgerError('Эта передача уже подтверждена.') from None
            except Exception:
                connection.rollback()
                raise

    def add_opening(self, day: date, amount, note: str):
        return self._movement(day, 'opening', note, amount, reference='initial', allow_zero=True)

    def confirm_transfer(self, cashier_day: date, received_day: date, amount):
        if received_day < cashier_day:
            raise LedgerError('Дата получения не может быть раньше кассового дня.')
        return self._movement(received_day, 'cashier_transfer',
                              f'Касса за {cashier_day.isoformat()}', amount,
                              reference=cashier_day.isoformat(), allow_zero=True)

    def add_expense(self, day: date, item_code: str, note: str, amount,
                    *, cashier_amount: Decimal | None = None):
        if item_code not in ITEMS or ITEMS[item_code][0] == 'income':
            raise LedgerError('Выберите наименование затрат из справочника.')
        if item_code == EXTRA_ITEM:
            raise LedgerError(EXTRA_ONLY)
        note = note.strip() if isinstance(note, str) else ''
        if len(note) > 160:
            raise LedgerError('Пояснение должно быть не длиннее 160 символов.')
        label = ITEMS[item_code][1]
        description = f'{label} · {note}' if note else label
        return self._movement(day, 'other_expense', description, amount, item_code=item_code,
                              cashier_amount=cashier_amount)

    def add_income(self, day: date, item_code: str, note: str, amount):
        if item_code != 'income_other':
            raise LedgerError('Для этого типа используйте отдельную операцию кассы.')
        note = note.strip() if isinstance(note, str) else ''
        if len(note) > 160:
            raise LedgerError('Пояснение должно быть не длиннее 160 символов.')
        description = f'Прочие поступления · {note}' if note else 'Прочие поступления'
        return self._movement(day, 'other_receipt', description, amount, item_code=item_code,
                              allow_zero=True)

    def update_movement(self, movement_id, day: date, item_code: str, note: str, amount):
        value = amount_value(amount, allow_zero=True)
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                ensure_open(connection, day)
                row = connection.execute(
                    'SELECT kind, amount, item_code, day, reference FROM accountant_movements WHERE id = ?',
                    (movement_id,)).fetchone()
                monthly = row is not None and row[0] == 'other_expense' and is_monthly_link(row[4])
                if not monthly:
                    note = required_text(note, 'назначение')
                if row is None or row[0] not in ('other_expense', 'other_receipt'):
                    raise LedgerError('Эту операцию нельзя изменить здесь.')
                kind, old_amount, old_code, stored_day, _ = row
                if stored_day != day.isoformat():
                    raise LedgerError('Нельзя изменить дату операции.')
                from .extra_payouts import guard_movement
                guard_movement(connection, movement_id)
                if item_code == EXTRA_ITEM:
                    raise LedgerError(EXTRA_ONLY)
                before = self._row_dict(connection, 'accountant_movements', movement_id)
                if monthly:
                    # Выплата оклада привязана к человеку ссылкой. Другая статья
                    # тихо вычеркнула бы её из выданного сотруднику — и оклад
                    # выдали бы второй раз. Подпись с именем тоже не правится:
                    # она обязана совпадать со ссылкой. Меняется только сумма.
                    if item_code != MONTHLY_ITEM:
                        raise LedgerError('Выплата оклада привязана к сотруднику: статью не поменять. '
                                          'Удалите выплату и запишите расход заново.')
                    if value == 0:
                        raise LedgerError('Выплата оклада должна быть больше нуля. '
                                          'Чтобы отменить выплату, удалите её.')
                    description = before['description']
                elif kind == 'other_receipt':
                    if item_code != 'income_other':
                        raise LedgerError('Выберите тип прочего поступления.')
                    description = f'Прочие поступления · {note}' if note else 'Прочие поступления'
                else:
                    if item_code not in ITEMS or ITEMS[item_code][0] == 'income':
                        raise LedgerError('Выберите наименование затрат из справочника.')
                    description = f'{ITEMS[item_code][1]} · {note}' if note else ITEMS[item_code][1]
                if kind == 'other_expense':
                    self._guard_manual_salary_expense(connection, day, item_code)
                    if item_code != old_code:
                        self._validate_salary_expense(connection, day, item_code)
                linked = connection.execute(
                    'SELECT debt_id FROM accountant_debt_payments WHERE movement_id = ?',
                    (movement_id,)).fetchone()
                if linked:
                    debt_before = self._row_dict(
                        connection, 'accountant_debt_payments',
                        connection.execute(
                            'SELECT id FROM accountant_debt_payments WHERE movement_id = ?',
                            (movement_id,)).fetchone()[0])
                    debt_total = connection.execute(
                        'SELECT total_amount FROM accountant_debts WHERE id = ?', (linked[0],)).fetchone()[0]
                    other_paid = sum((Decimal(item[0]) for item in connection.execute(
                        'SELECT amount FROM accountant_debt_payments WHERE debt_id = ? AND movement_id != ?',
                        (linked[0], movement_id))), Decimal(0))
                    if value > Decimal(debt_total) - other_paid:
                        raise LedgerError('Выплата превышает оставшийся долг.')
                    connection.execute('UPDATE accountant_debt_payments SET amount = ? WHERE movement_id = ?',
                                       (str(value), movement_id))
                    debt_after = self._row_dict(
                        connection, 'accountant_debt_payments', debt_before['id'])
                    record_audit(connection, 'debt_payment', debt_before['id'], 'update',
                                 debt_before, debt_after)
                connection.execute('UPDATE accountant_movements SET description=?, amount=?, item_code=? '
                                   'WHERE id=?', (description, str(value), item_code, movement_id))
                after = self._row_dict(connection, 'accountant_movements', movement_id)
                self._check_reserve_balances(connection)
                record_audit(connection, 'movement', movement_id, 'update', before, after)
                handover = connection.execute(
                    'SELECT amount FROM accountant_handover_days WHERE day = ?', (day.isoformat(),)).fetchone()
                cashier_amount = Decimal(handover[0]) if handover else None
                available = self.available_cash(connection, day, cashier_amount)
                if kind == 'other_expense' and available < 0:
                    raise LedgerError('Операция делает остаток отрицательным.')
                self._check_cash_balances(connection, day)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def update_salary_payment(self, payment_id, day: date, amount):
        value = amount_value(amount)
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                ensure_open(connection, day)
                row = connection.execute(
                    'SELECT a.amount, p.amount, p.accrual_id, p.paid_day '
                    'FROM accountant_salary_payments p '
                    'JOIN accountant_accruals a ON a.id = p.accrual_id WHERE p.id = ?',
                    (payment_id,)).fetchone()
                if row is None:
                    raise LedgerError('Выплата не найдена.')
                if row[3] != day.isoformat():
                    raise LedgerError('Нельзя изменить дату операции.')
                self._guard_manual_salary_payment(connection, row[2])
                before = self._row_dict(connection, 'accountant_salary_payments', payment_id)
                paid_elsewhere = sum((Decimal(item[0]) for item in connection.execute(
                    'SELECT amount FROM accountant_salary_payments WHERE accrual_id = ? AND id != ?',
                    (row[2], payment_id))), Decimal(0))
                if value > Decimal(row[0]) - paid_elsewhere:
                    raise LedgerError('Выплата превышает начисленную сумму.')
                connection.execute('UPDATE accountant_salary_payments SET amount = ? WHERE id = ?',
                                   (str(value), payment_id))
                after = self._row_dict(connection, 'accountant_salary_payments', payment_id)
                record_audit(connection, 'salary_payment', payment_id, 'update', before, after)
                handover = connection.execute(
                    'SELECT amount FROM accountant_handover_days WHERE day = ?', (day.isoformat(),)).fetchone()
                cashier_amount = Decimal(handover[0]) if handover else None
                if self.available_cash(connection, day, cashier_amount) < 0:
                    raise LedgerError('Операция делает остаток отрицательным.')
                self._check_cash_balances(connection, day)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def give_procurement(self, day: date, recipient: str, purpose: str, amount,
                         *, cashier_amount: Decimal | None = None):
        recipient = required_text(recipient, 'получателя')
        purpose = required_text(purpose, 'назначение закупки')
        return self._movement(day, 'procurement_advance', f'{recipient}: {purpose}', amount,
                              cashier_amount=cashier_amount)

    def pay_monthly(self, employee_id: int, name: str, day: date, amount,
                    *, cashier_amount: Decimal | None = None):
        """Часть оклада конкретному сотруднику.

        Деньги уходят обычным расходом «Месячная заработная плата», а ссылка на
        человека лежит в reference: так касса, журнал и удаление строки
        работают как у любого расхода, а ведомость раскладывает оклад по дням.
        """
        label = ITEMS[MONTHLY_ITEM][1]
        name = ' '.join(str(name or '').split()) or f'сотрудник №{employee_id}'
        # Имя в реестре бывает до 160 знаков, как и вся подпись расхода: длинное
        # имя укорачиваем, человека всё равно определяет ссылка.
        room = 160 - len(label) - len(' · ')
        if len(name) > room:
            name = name[:room - 1] + '…'
        return self._movement(day, 'other_expense', f'{label} · {name}', amount,
                              item_code=MONTHLY_ITEM,
                              reference=f'{MONTHLY_REFERENCE}{employee_id}:{uuid4().hex}',
                              cashier_amount=cashier_amount)

    @once
    def monthly_payments(self, first: date, last: date) -> list[dict]:
        """Выплаты окладов с привязкой к человеку за период.

        Окладом считается только то, что и сейчас числится статьёй «Месячная
        заработная плата»: ссылка на сотрудника у другой статьи — уже не его
        зарплата, и в выданное ему она не входит.
        """
        first = accounting_range_start(first, last)
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, day, amount, reference, created_at FROM accountant_movements '
                "WHERE kind = 'other_expense' AND item_code = ? AND reference LIKE ? "
                'AND day >= ? AND day <= ? ORDER BY day, id',
                (MONTHLY_ITEM, MONTHLY_REFERENCE + '%', first.isoformat(), last.isoformat())).fetchall()
        result = []
        for movement_id, day, amount, reference, created_at in rows:
            owner = reference[len(MONTHLY_REFERENCE):].split(':')[0]
            if not owner.isdigit():
                continue
            result.append(dict(id=movement_id, day=day, amount=amount, employee_id=int(owner),
                               created_at=local_timestamp(created_at)))
        return result

    # ── Перечисления поставщикам ────────────────────────────────────────────
    # Оплата поставщику со счёта, мимо кассы. Наличные кассы и подотчёт Шоха
    # она не меняет — поэтому лежит в своей таблице, а не среди движений денег,
    # которые читают остатки. Запись нужна, чтобы Шох не заплатил тому же
    # поставщику ещё и наличными и чтобы закуп дня был виден целиком.

    def add_supplier_transfer(self, day: date, supplier, item, point, amount) -> int:
        supplier = required_text(supplier, 'поставщика')
        item = item.strip() if isinstance(item, str) else ''
        if len(item) > 160:
            raise LedgerError('«За что» — не длиннее 160 символов.')
        point = required_text(point, 'точку закупа')
        value = amount_value(amount)
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                ensure_open(connection, day)
                transfer_id = connection.execute(
                    'INSERT INTO accountant_supplier_transfers '
                    '(day, supplier, item, point, amount, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                    (day.isoformat(), supplier, item or '—', point, str(value), now_stamp())).lastrowid
                record_audit(connection, 'supplier_transfer', transfer_id, 'create', None,
                             self._row_dict(connection, 'accountant_supplier_transfers', transfer_id))
                connection.commit()
                return transfer_id
            except Exception:
                connection.rollback()
                raise

    def delete_supplier_transfer(self, transfer_id: int, day: date) -> None:
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                ensure_open(connection, day)
                before = self._row_dict(connection, 'accountant_supplier_transfers', transfer_id)
                if before is None:
                    raise LedgerError('Перечисление не найдено.')
                if before['day'] != day.isoformat():
                    raise LedgerError('Нельзя изменить дату операции.')
                connection.execute('DELETE FROM accountant_supplier_transfers WHERE id = ?', (transfer_id,))
                record_audit(connection, 'supplier_transfer', transfer_id, 'delete', before, None)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @once
    def supplier_transfers(self, first: date, last: date | None = None) -> list[dict]:
        """Перечисления за день или за период — в порядке записи."""
        last = last or first
        first = accounting_range_start(first, last)
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, day, supplier, item, point, amount, created_at '
                'FROM accountant_supplier_transfers WHERE day >= ? AND day <= ? ORDER BY day, id',
                (first.isoformat(), last.isoformat())).fetchall()
        return [dict(id=row[0], day=row[1], supplier=row[2], item=row[3], point=row[4],
                     amount=row[5], created_at=local_timestamp(row[6])) for row in rows]

    @staticmethod
    def _guard_manual_salary_payment(connection, accrual_id):
        from .salary_day import MANUAL_STATUS
        row = connection.execute('SELECT attendance_status FROM accountant_accruals WHERE id=?',
                                 (accrual_id,)).fetchone()
        if row and row[0] == MANUAL_STATUS:
            raise LedgerError('Эта выплата ведётся в таблице «Зарплата · день». Измените сумму в её ячейке.')

    def pay_salary(self, accrual_id: int, paid_day: date, amount,
                   *, cashier_amount: Decimal | None = None):
        value = amount_value(amount)
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                ensure_open(connection, paid_day)
                accrual = connection.execute('SELECT work_day, amount FROM accountant_accruals WHERE id = ?',
                                             (accrual_id,)).fetchone()
                if accrual is None:
                    raise LedgerError('Начисление не найдено.')
                self._guard_manual_salary_payment(connection, accrual_id)
                if accrual[0] < period_start(paid_day).isoformat():
                    raise LedgerError('Начисление относится к архиву до 02.10.2026.')
                if paid_day.isoformat() < accrual[0]:
                    raise LedgerError('Выплата не может быть раньше рабочего дня.')
                already_paid = sum((Decimal(row[0]) for row in connection.execute(
                    'SELECT amount FROM accountant_salary_payments WHERE accrual_id = ?', (accrual_id,))),
                    Decimal(0))
                if value > Decimal(accrual[1]) - already_paid:
                    raise LedgerError('Выплата превышает оставшийся долг сотруднику.')
                self._require_cash(connection, paid_day, value, cashier_amount)
                cursor = connection.execute('INSERT INTO accountant_salary_payments '
                                            '(accrual_id, paid_day, amount, created_at) VALUES (?, ?, ?, ?)',
                                            (accrual_id, paid_day.isoformat(), str(value), now_stamp()))
                record_audit(connection, 'salary_payment', cursor.lastrowid, 'create', None,
                             self._row_dict(connection, 'accountant_salary_payments', cursor.lastrowid))
                self._check_cash_balances(connection, paid_day)
                connection.commit()
                return cursor.lastrowid
            except Exception:
                connection.rollback()
                raise

    def summary(self, day: date) -> dict:
        with closing(self._open()) as connection:
            day_accruals = connection.execute(
                'SELECT amount FROM accountant_accruals WHERE work_day = ?', (day.isoformat(),)).fetchall()
            accrued_on_day = sum((Decimal(row[0]) for row in day_accruals), Decimal(0))
            paid_on_day = sum((Decimal(row[0]) for row in connection.execute(
                'SELECT amount FROM accountant_salary_payments WHERE paid_day = ?', (day.isoformat(),))), Decimal(0))
            movements = [dict(id=row[0], type=row[1], operation='movement', description=row[2], amount=row[3],
                              day=row[4], item_code=row[5], created_at=local_timestamp(row[6]))
                         for row in connection.execute('SELECT id, kind, description, amount, day, item_code, '
                                                       'created_at FROM accountant_movements WHERE day = ? ORDER BY id',
                                                       (day.isoformat(),))]
            for row in connection.execute(
                                 'SELECT p.id, a.employee_name, a.work_day, p.amount, a.group_name, p.created_at '
                                 'FROM accountant_salary_payments p '
                                 'JOIN accountant_accruals a ON a.id = p.accrual_id '
                                 'WHERE p.paid_day = ? ORDER BY p.id', (day.isoformat(),)):
                code = ('salary_technical' if row[4] == 'Уборка' else
                        'salary_cashier' if row[4] == 'Касса' else 'salary_staff')
                label = ITEMS[code][1]
                movements.append(dict(id=row[0], type='salary_payment', operation='salary_payment',
                                      description=f'{label} · {row[1]} · за {row[2]}',
                                      amount=row[3], day=day.isoformat(), item_code=code,
                                      created_at=local_timestamp(row[5])))
            balance = self._cash_balance(connection, day)
            prior_balance = self._cash_balance(connection, date.fromordinal(day.toordinal() - 1))
            confirmed = connection.execute('SELECT approver, confirmed_at FROM accountant_payroll_days '
                                           'WHERE day = ?', (day.isoformat(),)).fetchone()
        accruals = self.accruals(day)
        debt = sum((Decimal(item['debt']) for item in accruals), Decimal(0))
        opening_today = sum((Decimal(item['amount']) for item in movements
                             if item['type'] == 'opening'), Decimal(0))
        received = sum((Decimal(item['amount']) for item in movements
                        if item['type'] == 'cashier_transfer'), Decimal(0))
        other_outflows = sum((Decimal(item['amount']) for item in movements
                              if item['type'] in ('other_expense', 'procurement_advance')), Decimal(0))
        cash_flow = dict(opening_balance=str(prior_balance + opening_today),
                         received_from_cashier=str(received),
                         salary_paid=str(paid_on_day),
                         other_outflows=str(other_outflows),
                         closing_balance=str(balance))
        salary_categories = {}
        for item in movements:
            if item['type'] == 'salary_payment':
                label = ITEMS[item['item_code']][1]
                salary_categories[label] = salary_categories.get(label, Decimal(0)) + Decimal(item['amount'])
        return dict(accrued_on_day=accrued_on_day, salary_paid_on_day=paid_on_day,
                    salary_debt=debt, cash_balance=balance, movements=movements,
                    cash_flow=cash_flow,
                    salary_categories={name: str(amount) for name, amount in salary_categories.items()},
                    accruals=accruals, payroll_confirmed=bool(confirmed),
                    # Смену начисляют по людям: часть уже начислена, день не закрыт.
                    payroll_partial=bool(day_accruals) and not confirmed,
                    payroll_accrued_count=len(day_accruals),
                    payroll_approver=confirmed[0] if confirmed else None)

    def daily_summary(self, day: date, cashier_amount: Decimal | None, *, carry_history: bool = True,
                      carry_start: date | None = None) -> dict:
        """Verified carried cash plus today's handover less actual cash outflows."""
        result = self.summary(day)
        movements = [item for item in result['movements']
                     if item['type'] in ('other_expense', 'other_receipt', 'procurement_advance', 'salary_payment')]
        with closing(self._open()) as connection:
            for entry_id, note, amount, created_at in connection.execute(
                    "SELECT id, note, amount, created_at FROM accountant_reserves "
                    "WHERE kind = 'transfer' AND day = ?", (day.isoformat(),)):
                movements.append(dict(id=entry_id, type='reserve_transfer', operation='reserve_transfer', description=note,
                                      amount=amount, day=day.isoformat(), item_code=None,
                                      created_at=local_timestamp(created_at)))
        # Строки «Касса за день» и «Остаток на начало дня» — расчёт, а не запись:
        # момента записи у них нет, поэтому created_at пустой.
        if cashier_amount is not None:
            movements.insert(0, dict(id=None, type='auto_cashier',
                                     description=f'Касса за {(day - timedelta(days=1)).strftime("%d.%m.%Y")}',
                                     amount=str(cashier_amount), day=day.isoformat(), item_code=None,
                                     created_at=None))
        other_receipts = sum((Decimal(item['amount']) for item in movements
                              if item['type'] == 'other_receipt'), Decimal(0))
        other = sum((Decimal(item['amount']) for item in movements
                     if item['type'] in ('other_expense', 'procurement_advance', 'reserve_transfer')), Decimal(0))
        paid = result['salary_paid_on_day']
        result['salary_unallocated_on_day'] = sum(
            (Decimal(item['amount']) for item in movements
             if item['type'] == 'other_expense'
             and ITEMS.get(item['item_code'], (None,))[0] == 'salary'), Decimal(0))
        result['salary_recorded_on_day'] = paid + result['salary_unallocated_on_day']
        # Приход дня берётся из записи передачи и входит в остаток только
        # подтверждённым (counted_handover): «ожидается от кассира» — подсказка
        # рядом с остатком, а не деньги в нём.
        with closing(self._open()) as connection:
            book = self.cash_book(connection, day)
            flow = book.flow(day, carry_start, tolerate_gaps=self.allow_negative_cash)
            # «На начало дня» = конец вчерашнего: расшифровка — вчерашний день целиком.
            previous = (book.flow(day - timedelta(days=1), carry_start, tolerate_gaps=self.allow_negative_cash)
                        if flow['anchor'] is None and day != ACCOUNTING_START else None)
        opening, remaining, missing, first_day = (flow['opening'], flow['closing'],
                                                  flow['missing'], flow['first_day'])
        if opening is not None and opening > 0:
            movements.insert(0, dict(id=None, type='opening',
                                     description='Остаток на начало дня',
                                     amount=str(opening), day=day.isoformat(), item_code=None,
                                     created_at=None))
        debts = self.debt_summary(day)
        result.update(debts)
        result['cash_opening'] = self.cash_opening(day)
        result['accounting_start'] = ACCOUNTING_START.isoformat()
        result['archived'] = day < ACCOUNTING_START
        result['movements'] = movements
        result['cash_balance'] = remaining
        result['cash_flow'] = dict(opening_balance=str(opening) if opening is not None else None,
                                   received_from_cashier=str(cashier_amount) if cashier_amount is not None else None,
                                   other_receipts=str(other_receipts),
                                   salary_paid=str(paid), other_outflows=str(other),
                                   closing_balance=str(remaining) if remaining is not None else None,
                                   missing_day=missing, first_day=first_day,
                                   received_counted=str(flow['handover_counted']),
                                   handover_status=flow['handover_status'])
        result['day_flow'] = flow_json(flow)
        result['opening_breakdown'] = dict(
            anchor=flow_json(flow)['anchor'], previous=flow_json(previous) if previous else None)
        return result

    def day_flow(self, day: date, carry_start: date | None = None) -> dict:
        with closing(self._open()) as connection:
            return CashBook(connection, day).flow(day, carry_start, tolerate_gaps=self.allow_negative_cash)

    def reconciliation(self, first: date, last: date, carry_start: date | None = None) -> list[dict]:
        """Сверка по дням: начало → приход → расход → конец (ТЗ 02.10, п. 1.4)."""
        first = accounting_range_start(first, last)
        with closing(self._open()) as connection:
            book = CashBook(connection, last)
            days = []
            day = first
            while day <= last:
                days.append(book.flow(day, carry_start, tolerate_gaps=self.allow_negative_cash))
                day += timedelta(days=1)
        return days
