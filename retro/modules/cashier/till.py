"""Деньги кассы мимо iiko: передача бухгалтеру, выдачи Шоху и доллары в сейф.

Экран 5a. Все три пишутся в базу бухгалтера (FinanceStore): это один источник
истины для его денег, подотчёта Шоха и сейфа, своих копий у кассира нет.

* «Выдать Шоху» из кассы. Наличные уходят из ящика кассира, поэтому к передаче
  бухгалтеру остаётся меньше ровно на эту сумму: выдача вычитается вместе с
  расходами кассы. Деньги бухгалтера второй раз не трогаем — выдачи лежат в
  своей таблице, а не среди движений его кассы (accountant_movements), поэтому
  ни в остаток бухгалтера, ни в его «Выдано Шоху на закуп» не попадают.
  Подотчёт Шоха (резерв `shoh`) считает их приходом, как выдачу бухгалтера.
* Доллары «сразу в сейф» — приход резерва `usd` (реальные доллары). В передачу
  сумов не входят.
* Передача. Сумма считается на сервере по той же формуле, что у бухгалтера
  (cash_to_finance), из того снимка iiko, что открыт у кассира, и пишется в
  accountant_handover_days датой следующего дня — приход у бухгалтера.
"""

from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from retro.accounting_period import accounting_range_start
from retro.db import table_exists
from retro.modules.accountant.handover_dates import receipt_day
from retro.logging_config import log_safe_failure
from retro.modules.accountant.audit import record_audit
from retro.modules.accountant.ledger import LedgerError, amount_value, ensure_open, local_timestamp, now_stamp

from .expenses import cash_to_finance

CASHIER = 'cashier'
# Кассир переписывает только свою передачу или автоматическую (расчёт iiko вне
# ручного режима). Приход, который записал или исправил бухгалтер, — его.
CASHIER_MAY_REPLACE = ('cashier', 'auto')
USD_LIMIT = Decimal('1000000000')


def create_tables(connection) -> None:
    """Таблицы кассы в базе бухгалтера. Их создаёт FinanceStore при старте:
    резервы читают их в той же транзакции, что и свои записи."""
    connection.execute('''CREATE TABLE IF NOT EXISTS cashier_shokh_gives (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,
        amount TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''')
    connection.execute('CREATE INDEX IF NOT EXISTS cashier_shokh_gives_day ON cashier_shokh_gives(day)')
    # legacy = 1 — дневная сумма старого поля «Доллары в кассе», перенесённая
    # одним взносом. Кассир её видит, но в резерв `usd` она не входит.
    connection.execute('''CREATE TABLE IF NOT EXISTS cashier_usd_deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,
        amount TEXT NOT NULL,
        created_at TEXT,
        legacy INTEGER NOT NULL DEFAULT 0
    )''')
    connection.execute('CREATE INDEX IF NOT EXISTS cashier_usd_deposits_day ON cashier_usd_deposits(day)')
    # Какие дни старого поля уже перенесены. Отдельно от взносов: удалённый
    # кассиром перенесённый взнос не должен воскресать при следующем старте.
    connection.execute('''CREATE TABLE IF NOT EXISTS cashier_usd_legacy (
        day TEXT PRIMARY KEY,
        amount TEXT NOT NULL,
        migrated_at TEXT NOT NULL
    )''')


def reserve_rows(connection, account: str) -> list[dict]:
    """Приход резервов из кассы — для reserves._entries.

    Выдача Шоху из кассы — приход подотчёта `shoh`, доллары кассира — приход
    сейфа `usd`. Перенесённые дневные суммы старого поля в резерв не входят:
    раньше бухгалтер вёл сейф отдельно, и задним числом его остатки не меняем."""
    if account == 'shoh':
        rows = connection.execute('SELECT day, amount, created_at FROM cashier_shokh_gives')
        note = 'Шоху из кассы'
    elif account == 'usd':
        rows = connection.execute(
            'SELECT day, amount, created_at FROM cashier_usd_deposits WHERE legacy = 0')
        note = 'Кассир · в сейф'
    else:
        return []
    return [dict(id=None, day=day, kind='deposit', amount=amount, note=note,
                 source=CASHIER, created_at=local_timestamp(created_at))
            for day, amount, created_at in rows]


def _balance_through(connection, account: str, day: date) -> Decimal | None:
    from retro.modules.accountant.reserves import _balance, _entries
    return _balance(_entries(connection, account, day.isoformat()))


def _stays_non_negative(connection, account: str, message: str) -> None:
    """Удаление прихода не должно увести резерв в минус ни в один записанный день."""
    from retro.modules.accountant.reserves import _balance, _entries
    rows = _entries(connection, account)
    for cutoff in {row['day'] for row in rows}:
        balance = _balance([row for row in rows if row['day'] <= cutoff])
        if balance is not None and balance < 0:
            raise LedgerError(message)


# ── Выдачи Шоху из кассы ─────────────────────────────────────────────────

def _give_json(row: dict) -> dict:
    return dict(id=row['id'], day=row['day'], amount=row['amount'],
                created_at=local_timestamp(row['created_at']))


def shokh_gives(finance, first: date, last: date | None = None) -> list[dict]:
    last = last or first
    first = accounting_range_start(first, last)
    with closing(finance._open()) as connection:
        rows = connection.execute(
            'SELECT id, day, amount, created_at FROM cashier_shokh_gives '
            'WHERE day >= ? AND day <= ? ORDER BY day, id',
            (first.isoformat(), last.isoformat())).fetchall()
    return [_give_json(dict(id=row[0], day=row[1], amount=row[2], created_at=row[3]))
            for row in rows]


def shokh_total(gives) -> Decimal:
    return sum((Decimal(row['amount']) for row in gives), Decimal(0))


def give_shokh(finance, day: date, amount) -> dict:
    value = amount_value(amount)
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            ensure_open(connection, day)
            give_id = connection.execute(
                'INSERT INTO cashier_shokh_gives (day, amount, created_at) VALUES (?, ?, ?)',
                (day.isoformat(), str(value), now_stamp())).lastrowid
            after = finance._row_dict(connection, 'cashier_shokh_gives', give_id)
            record_audit(connection, 'cashier_shokh_give', give_id, 'create', None, after)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _give_json(after)


def delete_shokh_give(finance, give_id: int, day: date) -> bool:
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            ensure_open(connection, day)
            before = finance._row_dict(connection, 'cashier_shokh_gives', give_id)
            if before is None or before['day'] != day.isoformat():
                connection.rollback()
                return False
            connection.execute('DELETE FROM cashier_shokh_gives WHERE id = ?', (give_id,))
            _stays_non_negative(connection, 'shoh',
                                'Шох уже отчитался этими деньгами: принятые покупки больше, '
                                'чем у него останется. Удалить выдачу нельзя.')
            record_audit(connection, 'cashier_shokh_give', give_id, 'delete', before, None)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise


# ── Доллары в сейф ───────────────────────────────────────────────────────

def usd_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value).replace(' ', '').replace(' ', '').replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise LedgerError('Укажите сумму в долларах.') from None
    if (not amount.is_finite() or amount <= 0 or amount > USD_LIMIT
            or amount.as_tuple().exponent < -2):
        raise LedgerError('Доллары: от 0,01 до 1 млрд, не больше двух знаков после запятой.')
    return amount


def _deposit_json(row: dict) -> dict:
    return dict(id=row['id'], day=row['day'], amount=row['amount'],
                created_at=local_timestamp(row['created_at']), legacy=bool(row['legacy']))


def usd_deposits(finance, day: date) -> list[dict]:
    with closing(finance._open()) as connection:
        rows = connection.execute(
            'SELECT id, day, amount, created_at, legacy FROM cashier_usd_deposits '
            'WHERE day = ? ORDER BY id', (day.isoformat(),)).fetchall()
    return [_deposit_json(dict(id=row[0], day=row[1], amount=row[2], created_at=row[3],
                               legacy=row[4])) for row in rows]


def usd_day(finance, day: date) -> dict:
    """Доллары кассира за день и сколько всего в сейфе на конец дня.

    «Всего в сейфе» — остаток резерва `usd` у бухгалтера; без начального
    пересчёта сейфа он неизвестен (None), а не ноль."""
    deposits = usd_deposits(finance, day)
    with closing(finance._open()) as connection:
        safe = _balance_through(connection, 'usd', day)
    return dict(date=day.isoformat(), deposits=deposits,
                total=str(sum((Decimal(row['amount']) for row in deposits), Decimal(0))),
                safe_balance=str(safe) if safe is not None else None)


def usd_balance(finance, day: date) -> dict:
    """Прежний ответ «доллары за день» ({date, amount}) — теперь сумма взносов
    дня, None без взносов. Нужен старым читателям (инструменты чата)."""
    result = usd_day(finance, day)
    return dict(result, amount=result['total'] if result['deposits'] else None)


def add_usd_deposit(finance, day: date, amount) -> dict:
    value = usd_amount(amount)
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            ensure_open(connection, day)
            deposit_id = connection.execute(
                'INSERT INTO cashier_usd_deposits (day, amount, created_at, legacy) VALUES (?, ?, ?, 0)',
                (day.isoformat(), str(value), now_stamp())).lastrowid
            after = finance._row_dict(connection, 'cashier_usd_deposits', deposit_id)
            record_audit(connection, 'cashier_usd_deposit', deposit_id, 'create', None, after)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _deposit_json(after)


def delete_usd_deposit(finance, deposit_id: int, day: date) -> bool:
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            ensure_open(connection, day)
            before = finance._row_dict(connection, 'cashier_usd_deposits', deposit_id)
            if before is None or before['day'] != day.isoformat():
                connection.rollback()
                return False
            connection.execute('DELETE FROM cashier_usd_deposits WHERE id = ?', (deposit_id,))
            _stays_non_negative(connection, 'usd',
                                'Эти доллары уже выданы из сейфа — удалить взнос нельзя.')
            record_audit(connection, 'cashier_usd_deposit', deposit_id, 'delete', before, None)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise


def migrate_legacy_usd(usd_rates, finance) -> int:
    """Старое поле «Доллары в кассе» хранило одну сумму на день
    (cashier_usd_balances в базе кассира). Каждая такая сумма становится одним
    взносом этого дня. Повторный запуск ничего не дублирует: перенесённые дни
    помечены в cashier_usd_legacy. Сами старые строки не трогаем."""
    with closing(usd_rates._open()) as connection:
        if not table_exists(connection, 'cashier_usd_balances'):
            return 0
        rows = connection.execute(
            'SELECT day, amount FROM cashier_usd_balances ORDER BY day').fetchall()
    if not rows:
        return 0
    moved = 0
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            done = {row[0] for row in connection.execute('SELECT day FROM cashier_usd_legacy')}
            for day, raw in rows:
                if day in done:
                    continue
                try:
                    value = Decimal(str(raw))
                    date.fromisoformat(day)
                except (InvalidOperation, ValueError, TypeError):
                    continue  # нечитаемую строку не переносим и не помечаем
                if not value.is_finite() or value < 0:
                    continue
                claimed = connection.execute(
                    'INSERT INTO cashier_usd_legacy (day, amount, migrated_at) VALUES (?, ?, ?) '
                    'ON CONFLICT(day) DO NOTHING', (day, str(value), now_stamp()))
                if claimed.rowcount != 1:
                    continue
                if value > 0:
                    connection.execute(
                        'INSERT INTO cashier_usd_deposits (day, amount, created_at, legacy) '
                        'VALUES (?, ?, NULL, 1)', (day, str(value)))
                moved += 1
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return moved


def migrate_legacy_usd_safely(usd_rates, finance) -> None:
    """Перенос при старте: ошибка не должна мешать панели открыться."""
    try:
        migrate_legacy_usd(usd_rates, finance)
    except Exception as error:  # noqa: BLE001 — журнал без тела ошибки
        log_safe_failure('cashier-till', error, operation='migrate_legacy_usd')


# ── Передача ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TillTotals:
    """Что ушло из кассы и что пришло в неё помимо iiko за день."""
    expenses: Decimal   # расходы кассира (его база)
    shokh: Decimal      # выдано Шоху из кассы (база бухгалтера)
    receipts: Decimal   # прочие поступления наличными

    @property
    def cash_out(self) -> Decimal:
        return self.expenses + self.shokh


def till_totals(state, day: date) -> TillTotals:
    """Одно место, где собираются слагаемые передачи кассира.

    Им пользуются все, кто считает «к передаче»: кассир, бухгалтер без ручного
    режима, проверка передачи у учредителя, выгрузка кассы."""
    expenses = state.expenses.list(day)
    receipts = state.expenses.list_receipts(day)
    return TillTotals(sum((item.amount for item in expenses), Decimal(0)),
                      shokh_total(shokh_gives(state.accountant_finance, day)),
                      sum((item.amount for item in receipts), Decimal(0)))


def expected_handover(snapshot, totals: TillTotals) -> Decimal:
    return cash_to_finance(snapshot, totals.cash_out, totals.receipts)


def till_summary(state, day: date, snapshot) -> dict:
    """Готовые числа карточки «К передаче бухгалтеру» и «Касса за день» (5a).

    Экран их не пересчитывает (Функционал §1): формула одна — здесь и в
    cash_to_finance. К передаче = «Демо» + предоплаты наличными + прочие
    поступления − расходы наличными − выдано Шоху из кассы. Доллары и открытые
    счета в неё не входят. В демо ручных операций нет."""
    totals = TillTotals(Decimal(0), Decimal(0), Decimal(0)) if snapshot.demo else till_totals(state, day)
    demo_cash = next((p.amount for p in snapshot.payments if p.name == 'Демо'), Decimal(0))
    register = snapshot.register_received_total
    if register is not None:
        base = register
    elif snapshot.new_prepayment is not None:
        base = snapshot.revenue + snapshot.new_prepayment
    else:
        # Предоплаты неизвестны — неизвестен и весь приход. Экран покажет
        # «требует проверки», а не продажи, выданные за полный приход.
        base = None
    handover = expected_handover(snapshot, totals)
    return dict(date=day.isoformat(), snapshot_id=snapshot.id,
                demo_cash=str(demo_cash),
                cash_prepayment=str(snapshot.cash_prepayment) if snapshot.cash_prepayment is not None else None,
                receipts=str(totals.receipts), expenses=str(totals.expenses),
                shokh=str(totals.shokh), cash_out=str(totals.cash_out),
                handover=str(handover) if handover is not None else None,
                prepayment_issue=snapshot.prepayment_issue,
                sales=str(snapshot.revenue),
                total_inflow=str(base + totals.receipts) if base is not None else None)


def expected_from_saved(state, day: date):
    """«Ожидается» для бухгалтера: расчёт по последнему снимку iiko, который уже
    есть на сервере (кэш страницы кассира или архив дня), — без похода в iiko.
    Это подсказка, а не приход: остаток бухгалтера по ней не считается."""
    try:
        snapshot = state.cache.latest_for_day(day)
    except RuntimeError:
        # Зовут из рабочего потока, а кэш в этот миг пополняет страница кассира.
        snapshot = None
    days = getattr(state, 'cashier_days', None)
    if snapshot is None and days is not None:
        snapshot = days.archive.get(day)
    if snapshot is None or snapshot.demo:
        return None, None
    expected = expected_handover(snapshot, till_totals(state, day))
    # Предоплаты неизвестны — сверять не с чем, как и без расчёта вообще.
    return (expected, snapshot.fetched_at) if expected is not None else (None, None)


def cashier_active(state, day: date) -> bool:
    """Работал ли кассир в панели в этот день: свой расход (не авто-строка
    «Зарплата кассира»), поступление, выдача Шоху из кассы, доллары в сейф
    (не перенесённые со старого поля) или «Передать» — хоть раз, даже если
    потом отменил. Только тогда расчёт кассы знает её реальные расходы, и
    ручной приход бухгалтера есть с чем сверять."""
    if any(not item.automatic for item in state.expenses.list(day)) or state.expenses.list_receipts(day):
        return True
    finance = state.accountant_finance
    with closing(finance._open()) as connection:
        if connection.execute('SELECT 1 FROM cashier_shokh_gives WHERE day = ? LIMIT 1',
                              (day.isoformat(),)).fetchone():
            return True
        if connection.execute('SELECT 1 FROM cashier_usd_deposits WHERE day = ? AND legacy = 0 LIMIT 1',
                              (day.isoformat(),)).fetchone():
            return True
        if connection.execute("SELECT 1 FROM accountant_handover_days WHERE day = ? AND source = 'cashier'",
                              (receipt_day(day).isoformat(),)).fetchone():
            return True
    # Передача, которую потом подтвердили, исправили или отменили: след — в журнале.
    migrations = finance.audit_entries(entity_type='finance_migration', entity_id='handover_receipt_day_v1')
    cutoff = migrations[-1]['id'] if migrations else 0
    entries = [entry for entry in finance.audit_entries(
        entity_type='handover', entity_id=receipt_day(day).isoformat()) if entry['id'] > cutoff]
    # Audit is append-only: pre-migration records still use the shift date.
    entries += [entry for entry in finance.audit_entries(
        entity_type='handover', entity_id=day.isoformat()) if entry['id'] < cutoff]
    return any((entry['before'] or {}).get('source') == CASHIER or (entry['after'] or {}).get('source') == CASHIER
               for entry in entries)


def handover_check(state, day: date, expected: Decimal | None = None) -> dict | None:
    """Передача дня со сверкой к ТЕКУЩЕМУ расчёту кассы: одна недостача для
    2a (тост, карточка, «Проверки»), кассира, учредителя и Excel.
    `expected` — уже посчитанный расчёт (учредитель берёт его из свежего
    снимка iiko той же формулой); без него — по снимку на сервере. Ручной
    приход бухгалтера сверяется, только если кассир в этот день работал в
    панели (cashier_active)."""
    if expected is None:
        expected, _ = expected_from_saved(state, day)
    return state.accountant_finance.handover_state(receipt_day(day), current_expected=expected,
                                                   cashier_active=cashier_active(state, day))


class HandoverChanged(LedgerError):
    """Сумма на сервере разошлась с той, что видел кассир."""

    def __init__(self, amount: Decimal):
        self.amount = amount
        super().__init__('Сумма к передаче изменилась. Проверьте её и нажмите «Передать» ещё раз.')


class NothingToHandOver(LedgerError):
    """Расходы больше наличных: отрицательную передачу записать нельзя."""


def hand_over(state, day: date, snapshot, *, expected: Decimal | None = None) -> dict:
    """Записать передачу кассира: сумма с сервера, а не из браузера."""
    amount = expected_handover(snapshot, till_totals(state, day))
    if expected is not None and abs(expected - amount) >= Decimal('0.01'):
        raise HandoverChanged(amount)
    if amount < 0:
        raise NothingToHandOver('Расходы больше наличных в кассе — передавать нечего. '
                                'Проверьте расходы и выдачи Шоху.')
    finance = state.accountant_finance
    finance.record_handover(receipt_day(day), amount, source=CASHIER, replace_sources=CASHIER_MAY_REPLACE)
    return finance.handover_state(receipt_day(day), current_expected=amount, cashier_active=True)
