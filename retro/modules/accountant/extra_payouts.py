"""Доп. выплаты сменным в «Зарплата · день» (ТЗ 09.10, Б-05).

Разовая выплата сверх клетки: временной хостес, доплата, премия за смену.
Деньги уходят обычным расходом служебной статьи «Доп. выплаты»
(accountant_movements, EXTRA_ITEM) в день выплаты: касса, журнал «Финансов
дня», остатки следующих дней, закрытие месяца и учредитель считают его как
любой расход — второй раз деньги нигде не вычитаются. В этой таблице — кому,
за какую смену, зачем и кто записал.

Почему не второе начисление: у начислений одна строка на сотрудника и смену
(UNIQUE work_day, employee_id), и клетка держит пару «начисление — выплата».
Доп. выплата за ту же смену стала бы второй выплатой этой пары — клетка
сочла бы её прежней ведомостью, а «Долги» — недоплатой или переплатой.
Отдельная запись со ссылкой на расход ничего из этого не трогает и в долги
не попадает: начисления у неё нет, она выдана сразу.

Сотрудник — из реестра (включая временных); имя, должность и группа
запоминаются на момент выплаты, поэтому запись остаётся и после того, как
человека убрали в архив.
"""

from contextlib import closing
from datetime import date
from decimal import Decimal
from uuid import uuid4

from retro.accounting_period import ACCOUNTING_START, SALARY_SHIFT_START
from retro.db import table_columns
from retro.modules.cashier.service import today_tashkent

from . import work_period
from .audit import record_audit
from .expense_catalog import EXTRA_ITEM
from .ledger import LedgerError, amount_value, ensure_open, lock_day, now_stamp, plain, required_text

# Ссылка расхода на доп. выплату: `extra:<сотрудник>:<ключ>`.
EXTRA_REFERENCE = 'extra:'
# Выдачи с начала учёта; первая смена — предыдущий день.
ENTRY_START = ACCOUNTING_START
FIRST_SHIFT = SALARY_SHIFT_START
TEMPORARY = 'temporary'

COLUMNS = ('id', 'employee_id', 'name', 'role', 'group', 'temporary', 'work_day', 'paid_day', 'amount',
           'note', 'created_at', 'created_by', 'updated_at', 'updated_by', 'movement_id')
SELECT = ('SELECT x.id, x.employee_id, x.employee_name, x.role, x.group_name, x.temporary, x.work_day, '
          'm.day, m.amount, x.note, x.created_at, x.created_by, x.updated_at, x.updated_by, x.movement_id '
          'FROM accountant_extra_payouts x JOIN accountant_movements m ON m.id = x.movement_id ')


class ExtraPayoutConfirm(LedgerError):
    """Запись похожа на ту же выдачу второй раз: записать можно только явно."""


class ExtraPayoutChanged(LedgerError):
    pass


def create_tables(connection) -> None:
    """Таблица доп. выплат; создаётся вместе с остальными таблицами бухгалтера."""
    connection.execute('''CREATE TABLE IF NOT EXISTS accountant_extra_payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        movement_id INTEGER NOT NULL UNIQUE REFERENCES accountant_movements(id),
        employee_id INTEGER NOT NULL,
        employee_name TEXT NOT NULL,
        role TEXT NOT NULL,
        group_name TEXT NOT NULL,
        temporary INTEGER NOT NULL DEFAULT 0,
        work_day TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT,
        updated_at TEXT,
        updated_by TEXT
    )''')
    connection.execute('CREATE INDEX IF NOT EXISTS accountant_extra_payouts_employee '
                       'ON accountant_extra_payouts(employee_id)')


def check_days(work_day: date, paid_day: date) -> None:
    if paid_day < ENTRY_START:
        raise LedgerError(f'Доп. выплаты вводятся с {ACCOUNTING_START:%d.%m.%Y} — с начала рабочего учёта.')
    if paid_day > today_tashkent():
        raise LedgerError('Нельзя записать выплату будущим днём.')
    if work_day > paid_day:
        raise LedgerError('Смена не может быть позже дня выплаты.')
    if work_day < FIRST_SHIFT:
        raise LedgerError(f'Смена — не раньше {FIRST_SHIFT:%d.%m.%Y}: с неё начинается ручная ведомость.')


def _item(row) -> dict:
    item = dict(zip(COLUMNS, row))
    item['temporary'] = bool(item['temporary'])
    item['amount'] = plain(Decimal(item['amount']))
    return item


def read(connection, payout_id: int) -> dict | None:
    row = connection.execute(SELECT + 'WHERE x.id = ?', (payout_id,)).fetchone()
    return _item(row) if row else None


def between(connection, first: date, last: date, *, by_shift=False) -> list[dict]:
    """Ведомость по сменам или финансовый отчёт по фактической дате выплаты."""
    column = 'x.work_day' if by_shift else 'm.day'
    items = [_item(row) for row in connection.execute(
        SELECT + f'WHERE {column} >= ? AND {column} <= ? ORDER BY m.day, x.id',
        (first.isoformat(), last.isoformat()))]
    known = work_period.periods(connection, {item['employee_id'] for item in items if item['temporary']})
    for item in items:
        found = known.get(item['employee_id'])
        item['work_period'] = work_period.label(found[1], found[2]) if item['temporary'] and found else None
    return items


def temporary_ids(connection) -> set[int]:
    """Кто в реестре сейчас временный. Признак (employment_type) ставит
    бухгалтер в «Сотрудниках» (T-434); в базе без этого поля временных просто нет."""
    if 'employment_type' not in table_columns(connection, 'accountant_employees'):
        return set()
    return {row[0] for row in connection.execute(
        'SELECT id FROM accountant_employees WHERE employment_type = ?', (TEMPORARY,))}


def guard_movement(connection, movement_id: int) -> None:
    """Расход доп. выплаты правят только в «Зарплата · день»: там же кому и за
    какую смену. Правка из журнала разошлась бы с этой записью."""
    if connection.execute('SELECT 1 FROM accountant_extra_payouts WHERE movement_id = ?',
                          (movement_id,)).fetchone():
        raise LedgerError('Это доп. выплата сотруднику: измените или удалите её в «Зарплата · день».')


def _employee(connection, employee_id: int) -> dict:
    temporary = 'employment_type' in table_columns(connection, 'accountant_employees')
    row = connection.execute(
        'SELECT name, role, group_name' + (', employment_type' if temporary else '')
        + ' FROM accountant_employees WHERE id = ?', (employee_id,)).fetchone()
    if row is None:
        raise LedgerError('Сотрудник не найден или находится в архиве.')
    return dict(name=row[0], role=row[1], group=row[2], temporary=temporary and row[3] == TEMPORARY)


def _description(name: str, work_day: date, note: str) -> str:
    text = f'Доп. выплата · {name} · смена {work_day.strftime("%d.%m")} · {note}'
    return text if len(text) <= 160 else text[:159] + '…'


def _money(value: Decimal) -> str:
    return f'{value:,.2f}'.replace(',', ' ').removesuffix('.00')


def _warnings(connection, employee_id: int, name: str, work_day: date, paid_day: date,
              value: Decimal) -> list[str]:
    """Почему это может быть та же выдача второй раз: выплата в клетке за этот
    день выплаты или за эту смену, либо такая же доп. выплата уже записана."""
    texts = []
    cells = connection.execute(
        'SELECT a.work_day, p.paid_day, p.amount FROM accountant_salary_payments p '
        'JOIN accountant_accruals a ON a.id = p.accrual_id '
        'WHERE a.employee_id = ? AND (p.paid_day = ? OR a.work_day = ?) ORDER BY p.paid_day, p.id',
        (employee_id, paid_day.isoformat(), work_day.isoformat())).fetchall()
    paid = [row for row in cells if Decimal(row[2]) > 0]
    if paid:
        listed = ', '.join(f'{row[1][8:10]}.{row[1][5:7]} — {_money(Decimal(row[2]))} сум '
                           f'(смена {row[0][8:10]}.{row[0][5:7]})' for row in paid)
        texts.append(f'У сотрудника «{name}» уже отмечена выплата в клетке: {listed}. '
                     'Доп. выплата — отдельные деньги сверх клетки; если это та же выдача, '
                     'второй раз её не записывайте.')
    same = [row for row in connection.execute(
        SELECT + 'WHERE x.employee_id = ? AND x.work_day = ? AND m.day = ?',
        (employee_id, work_day.isoformat(), paid_day.isoformat())) if Decimal(row[8]) == value]
    if same:
        texts.append(f'Такая доп. выплата уже записана: {_money(value)} сум, выплата '
                     f'{paid_day.strftime("%d.%m")} за смену {work_day.strftime("%d.%m")}.')
    return texts


def add(finance, *, employee_id: int, work_day: date, paid_day: date, amount, note: str,
        confirm: bool = False, by: str | None = None, cashier_amount=None) -> dict:
    """Записать доп. выплату: расход в день выплаты и запись о ней — вместе.

    Касса и закрытый месяц проверяются так же, как у клетки. Похоже на повтор
    (выплата в клетке за этот день или смену, такая же доп. выплата) — без
    `confirm` не пишем ничего и объясняем почему (ExtraPayoutConfirm)."""
    check_days(work_day, paid_day)
    value = amount_value(amount)
    note = required_text(note, 'назначение выплаты')
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            # Тот же порядок блокировок, что у клетки: смена, затем выплата.
            for day in sorted({work_day, paid_day}):
                lock_day(connection, day)
                ensure_open(connection, day)
            person = _employee(connection, employee_id)
            # Временный — только за смены своего периода (T-434).
            work_period.lock_employee(connection, employee_id)
            outside = work_period.guard(connection, employee_id, work_day, 'extra')
            if outside:
                raise LedgerError(outside)
            texts = _warnings(connection, employee_id, person['name'], work_day, paid_day, value)
            if texts and not confirm:
                raise ExtraPayoutConfirm(' '.join(texts))
            finance._require_cash(connection, paid_day, value, cashier_amount)
            now = now_stamp()
            movement_id = connection.execute(
                'INSERT INTO accountant_movements '
                '(day, kind, description, amount, item_code, reference, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (paid_day.isoformat(), 'other_expense', _description(person['name'], work_day, note),
                 plain(value), EXTRA_ITEM, f'{EXTRA_REFERENCE}{employee_id}:{uuid4().hex}', now)).lastrowid
            record_audit(connection, 'movement', movement_id, 'create', None,
                         finance._row_dict(connection, 'accountant_movements', movement_id))
            payout_id = connection.execute(
                'INSERT INTO accountant_extra_payouts '
                '(movement_id, employee_id, employee_name, role, group_name, temporary, work_day, note, '
                'created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (movement_id, employee_id, person['name'], person['role'], person['group'],
                 int(person['temporary']), work_day.isoformat(), note, now, by)).lastrowid
            item = read(connection, payout_id)
            record_audit(connection, 'extra_payout', payout_id, 'create', None, item)
            finance._check_cash_balances(connection, paid_day)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return item


def update(finance, payout_id: int, *, amount, work_day: date, note: str, expected_amount=None,
           by: str | None = None, cashier_amount=None) -> dict:
    """Изменить сумму, смену или назначение. Сотрудник и день выплаты не
    меняются: другая дата — другой день кассы, такую запись удаляют и
    записывают заново. `expected_amount` — сумма, которую видел экран."""
    value = amount_value(amount)
    note = required_text(note, 'назначение выплаты')
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            before = read(connection, payout_id)
            if before is None:
                raise LedgerError('Доп. выплата не найдена.')
            paid_day = date.fromisoformat(before['paid_day'])
            old_work = date.fromisoformat(before['work_day'])
            check_days(work_day, paid_day)
            for day in sorted({old_work, work_day, paid_day}):
                lock_day(connection, day)
                ensure_open(connection, day)
            work_period.lock_employee(connection, before['employee_id'])
            outside = work_period.guard(connection, before['employee_id'], work_day, 'extra')
            if outside:
                raise LedgerError(outside)
            current = Decimal(before['amount'])
            if expected_amount is not None and amount_value(expected_amount, allow_zero=True) != current:
                raise ExtraPayoutChanged('Сумма уже изменилась. Обновите страницу и повторите.')
            if value == current and work_day == old_work and note == before['note']:
                connection.rollback()
                return dict(before, changed=False)
            # Больше денег — только из кассы этого дня; меньше — возврат в кассу.
            if value > current:
                finance._require_cash(connection, paid_day, value - current, cashier_amount)
            movement_before = finance._row_dict(connection, 'accountant_movements', before['movement_id'])
            connection.execute('UPDATE accountant_movements SET amount = ?, description = ? WHERE id = ?',
                               (plain(value), _description(before['name'], work_day, note), before['movement_id']))
            connection.execute('UPDATE accountant_extra_payouts SET work_day = ?, note = ?, updated_at = ?, '
                               'updated_by = ? WHERE id = ?',
                               (work_day.isoformat(), note, now_stamp(), by, payout_id))
            record_audit(connection, 'movement', before['movement_id'], 'update', movement_before,
                         finance._row_dict(connection, 'accountant_movements', before['movement_id']))
            after = read(connection, payout_id)
            record_audit(connection, 'extra_payout', payout_id, 'update', before, after)
            finance._check_cash_balances(connection, paid_day)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return dict(after, changed=True)


def delete(finance, payout_id: int, *, by: str | None = None) -> dict:
    """Удалить доп. выплату вместе с её расходом: деньги возвращаются в кассу
    дня выплаты. В аудите остаётся запись целиком и кто удалил."""
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            before = read(connection, payout_id)
            if before is None:
                raise LedgerError('Доп. выплата не найдена.')
            for day in sorted({date.fromisoformat(before['work_day']), date.fromisoformat(before['paid_day'])}):
                lock_day(connection, day)
                ensure_open(connection, day)
            movement_before = finance._row_dict(connection, 'accountant_movements', before['movement_id'])
            connection.execute('DELETE FROM accountant_extra_payouts WHERE id = ?', (payout_id,))
            connection.execute('DELETE FROM accountant_movements WHERE id = ?', (before['movement_id'],))
            record_audit(connection, 'movement', before['movement_id'], 'delete', movement_before, None)
            # После удаления записи нет — в «после» только кто и когда удалил.
            record_audit(connection, 'extra_payout', payout_id, 'delete', before,
                         dict(deleted_by=by, deleted_at=now_stamp()))
            finance._check_cash_balances(connection, date.fromisoformat(before['paid_day']))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return before
