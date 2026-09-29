"""T-391: смену начисляют по людям.

Сотрудник без ставки не запирает всю смену. Без привязки Hikvision можно платить: кому
сумму можно посчитать, тому начисляется и выдаётся сразу; остальные ждут с
причиной и начисляются, как только их разблокировали. День закрыт целиком,
когда начислено всем. На SQLite и на Postgres (RETRO_TEST_POSTGRES_URL).
"""

import threading
from datetime import timedelta
from decimal import Decimal

from test_accountant_design_parity import (  # noqa: F401 — any_db — фикстура
    BEFORE, DAY, any_db, cash, client, day_json, manual_person, mark, month_json)

from retro.modules.accountant.payroll import draft_payroll
from retro.modules.cashier.service import today_tashkent

NEXT = DAY + timedelta(days=1)


def shift(c):
    """Смена DAY: Акмаль (вручную, ставка есть), Лола (вручную, без ставки),
    Жасур (Hikvision без привязки, ставка есть)."""
    roster = c.app.state.accountant_roster
    ready = manual_person(c, 'Акмаль', 'охрана', '150000', 'Охрана')
    no_rate = manual_person(c, 'Лола', 'техперсонал', None, 'Уборка')
    unlinked = roster.add(name='Жасур', role='официант', rate='180000', group_name='Обслуживание зала')
    return ready, no_rate, unlinked


def confirm(c, day=DAY, **body):
    response = c.post('/api/accountant/payroll/confirm',
                      json={'date': day.isoformat(), 'approver': 'Любовь', **body})
    assert response.status_code == 200, response.text
    return response.json()


def rows(c, day=DAY):
    return {row['employee_id']: row for row in day_json(c, day)['employees']}


def patch_employee(c, person, **body):
    current = next(item for item in c.app.state.accountant_roster.list() if item.id == person.id)
    payload = {'rate': str(current.rate) if current.rate is not None else None, 'reason': 'Смена', **body}
    return c.patch(f'/api/accountant/employees/{person.id}', json=payload)


def test_partial_confirm_pays_the_ready_ones_and_names_the_blockers(any_db):
    c = any_db
    ready, no_rate, unlinked = shift(c)
    result = confirm(c)
    assert result['accrued'] == [ready.id, unlinked.id]
    assert result['blockers'] == [
        {'employee_id': no_rate.id, 'name': 'Лола', 'reason': 'missing_rate'}]
    assert (result['confirmed'], result['partial']) == (False, True)

    day = day_json(c)
    shown = {row['employee_id']: row for row in day['employees']}
    assert (shown[ready.id]['accrued'], shown[ready.id]['blocker']) == (True, None)
    assert shown[ready.id]['accrual_id'] is not None
    assert (shown[no_rate.id]['accrued'], shown[no_rate.id]['blocker']) == (False, 'missing_rate')
    assert (shown[unlinked.id]['accrued'], shown[unlinked.id]['blocker']) == (True, None)
    assert shown[unlinked.id]['status'] == 'unlinked'
    assert shown[unlinked.id]['hikvision_registered'] is False
    assert (day['ledger']['payroll_confirmed'], day['ledger']['payroll_partial']) == (False, True)
    assert (day['payroll']['accrued_count'], day['payroll']['blocked_count']) == (2, 1)
    staff = c.get('/api/accountant/staff', params={'date': DAY.isoformat()}).json()
    assert (staff['payroll_partial'], staff['accrued_count'], staff['blocked_count']) == (True, 2, 1)

    # Начисленному выдают сразу, не дожидаясь остальных.
    cash(c, NEXT, handover='1000000', opening='0')
    paid = c.post('/api/accountant/salary-payments', json={
        'accrual_id': shown[ready.id]['accrual_id'], 'date': NEXT.isoformat(), 'amount': '150000'})
    assert paid.status_code == 201, paid.text
    # Человеку без Hikvision тоже выдаётся зарплата при выключенном check_mode.
    assert c.app.state.settings.check_mode is False
    paid_unlinked = c.post('/api/accountant/salary-payments', json={
        'accrual_id': shown[unlinked.id]['accrual_id'], 'date': NEXT.isoformat(), 'amount': '180000'})
    assert paid_unlinked.status_code == 201, paid_unlinked.text
    assert day_json(c, NEXT)['ledger']['cash_balance'] == '670000'


def test_unblocked_people_are_accrued_by_the_next_confirm_until_the_day_closes(any_db):
    c = any_db
    ready, no_rate, unlinked = shift(c)
    confirm(c)
    # Ставку задали впервые — она действует и на ждущий день.
    assert patch_employee(c, no_rate, rate='130000').status_code == 200
    second = confirm(c)
    assert second['accrued'] == [no_rate.id] and second['confirmed'] is True
    assert second['blockers'] == []
    assert rows(c)[no_rate.id]['payable'] == '130000'
    assert rows(c)[unlinked.id]['status'] == 'unlinked'
    day = day_json(c)
    assert day['ledger']['payroll_confirmed'] is True and day['ledger']['payroll_partial'] is False
    assert sorted(item['amount'] for item in c.app.state.accountant_finance.accruals(DAY)
                  if item['work_day'] == DAY.isoformat()) == ['130000', '150000', '180000']
    assert confirm(c)['already_confirmed'] is True


def test_unlinked_person_needs_no_exception_and_closed_day_stays_closed(any_db):
    c = any_db
    ready, no_rate, unlinked = shift(c)
    confirm(c)
    assert rows(c)[unlinked.id]['accrued'] is True
    assert confirm(c)['accrued'] == []
    # Закрыли день целиком — новый человек, заведённый позже, в него уже не попадёт:
    # ни исключением, ни отметкой, ни повторным подтверждением.
    patch_employee(c, no_rate, rate='130000')
    assert confirm(c)['confirmed'] is True
    newcomer = c.app.state.accountant_roster.add(name='Бахром', role='бармен', rate='200000', group_name='Бар')
    late = c.post('/api/accountant/exceptions', json={
        'date': DAY.isoformat(), 'employee_id': newcomer.id, 'reason': 'x', 'approver': 'Любовь'})
    assert late.status_code == 409 and 'уже подтверждено' in late.json()['detail']
    assert confirm(c)['already_confirmed'] is True
    assert newcomer.id not in c.app.state.accountant_finance.accrued_employees(DAY)


def test_marks_lock_per_person_not_per_day(any_db):
    c = any_db
    ready, no_rate, unlinked = shift(c)
    confirm(c)
    # Начисленному отметку не поменять…
    locked = mark(c, ready.id, DAY, False)
    assert locked.status_code == 409 and 'уже подтверждено' in locked.json()['detail']
    # …а ждущему — можно, день ещё открыт.
    assert mark(c, no_rate.id, DAY, False).status_code == 200


def test_marks_recheck_covers_only_the_rows_being_accrued(any_db):
    c = any_db
    ready, no_rate, unlinked = shift(c)
    finance, roster, attendance = (c.app.state.accountant_finance, c.app.state.accountant_roster,
                                   c.app.state.attendance)
    people = roster.list(DAY)
    snapshot = attendance.snapshot(DAY, people)
    computed = draft_payroll(DAY, people, set(), snapshot.rows)
    # Отметку поменяли у того, кого сейчас не начисляют, — начислению Акмаля это не мешает.
    assert finance.mark_manual_attendance(no_rate.id, DAY, False, 'Любовь')
    result = finance.confirm_payroll(DAY, computed, 'Любовь', marks=snapshot.marks, employee_ids=[ready.id])
    assert result.accrued == [ready.id]


def test_employee_ids_limit_who_is_accrued_now(any_db):
    c = any_db
    ready, no_rate, unlinked = shift(c)
    other = manual_person(c, 'Дилноза', 'хостес', '200000', 'Встреча гостей')
    result = confirm(c, employee_ids=[other.id])
    assert result['accrued'] == [other.id] and result['blockers'] == []
    assert rows(c)[ready.id]['accrued'] is False
    assert confirm(c)['accrued'] == [ready.id, unlinked.id]


def test_concurrent_confirms_never_accrue_twice(any_db):
    c = any_db
    people = [manual_person(c, f'Сотрудник {index}', 'охрана', '100000', 'Охрана') for index in range(6)]
    finance, roster = c.app.state.accountant_finance, c.app.state.accountant_roster
    staff = roster.list(DAY)
    snapshot = c.app.state.attendance.snapshot(DAY, staff)
    computed = draft_payroll(DAY, staff, set(), snapshot.rows)
    errors, results = [], []

    def run():
        try:
            result = finance.confirm_payroll(DAY, computed, 'Любовь', marks=snapshot.marks)
            results.append(dict(accrued=result.accrued))
        except Exception as error:  # проверяем в основном потоке
            errors.append(error)

    workers = [threading.Thread(target=run) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    assert errors == []
    accruals = [item for item in c.app.state.accountant_finance.accruals(DAY) if item['work_day'] == DAY.isoformat()]
    assert sorted(item['employee_id'] for item in accruals) == sorted(person.id for person in people)
    assert sum(len(result.get('accrued', [])) for result in results) == len(people)
    assert c.app.state.accountant_finance.is_payroll_confirmed(DAY)


def test_a_first_rate_applies_to_waiting_days_but_a_new_rate_only_from_today(any_db):
    c = any_db
    roster = c.app.state.accountant_roster
    today = today_tashkent()
    no_rate = roster.add(name='Лола', role='техперсонал', rate=None, group_name='Уборка')
    rated = manual_person(c, 'Акмаль', 'охрана', '150000', 'Охрана')
    assert patch_employee(c, no_rate, rate='130000').status_code == 200
    rate = lambda person, day: next(item.rate for item in roster.list(day) if item.id == person.id)
    assert rate(no_rate, BEFORE) == rate(no_rate, today) == Decimal('130000')
    assert patch_employee(c, rated, rate='170000').status_code == 200
    assert (rate(rated, BEFORE), rate(rated, today)) == (Decimal('150000'), Decimal('170000'))


def test_accrued_days_keep_their_amount_when_the_rate_is_reset(any_db):
    c = any_db
    person = manual_person(c, 'Акмаль', 'охрана', '150000', 'Охрана')
    other = manual_person(c, 'Лола', 'техперсонал', None, 'Уборка')
    confirm(c)  # Акмаль начислен, Лола ждёт ставку — день открыт
    assert patch_employee(c, person, rate=None).status_code == 200
    assert patch_employee(c, person, rate='210000').status_code == 200
    # Сняли ручную отметку: по живым данным он теперь «нет привязки», но начисленное
    # не плывёт — в строке сумма начисления, и блокером он не считается.
    assert patch_employee(c, person, manual_attendance=False).status_code == 200
    row = rows(c)[person.id]
    assert (row['payable'], row['accrued'], row['blocker']) == ('150000', True, None)
    assert rows(c)[other.id]['blocker'] == 'missing_rate'
    accrual, = [item for item in c.app.state.accountant_finance.accruals(DAY) if item['work_day'] == DAY.isoformat()]
    assert accrual['amount'] == '150000'


def test_month_sheet_marks_partial_and_closed_days(any_db):
    c = any_db
    ready, no_rate, unlinked = shift(c)
    confirm(c)
    month = month_json(c)
    assert DAY.isoformat() in month['partial_days'] and DAY.isoformat() not in month['confirmed_days']
    people = {person['employee_id']: person for person in month['shift']}
    assert DAY.isoformat() in people[ready.id]['cells'] and no_rate.id not in people
    patch_employee(c, no_rate, rate='130000')
    confirm(c)
    month = month_json(c)
    assert DAY.isoformat() in month['confirmed_days'] and DAY.isoformat() not in month['partial_days']


def test_accountant_role_can_switch_an_unlinked_person_to_manual(tmp_path):
    users = {'buh': ('pw', 'accountant'), 'kassa': ('pw', 'cashier')}
    with client(tmp_path, dashboard_panel_users=users) as c:
        person = c.app.state.accountant_roster.add(name='Жасур', role='официант', rate='180000',
                                                   group_name='Обслуживание зала')
        body = {'rate': '180000', 'reason': 'Нет в Hikvision', 'manual_attendance': True}
        c.post('/api/session', json={'username': 'kassa', 'password': 'pw'})
        assert c.patch(f'/api/accountant/employees/{person.id}', json=body).status_code == 403
        c.post('/api/session/logout')
        c.post('/api/session', json={'username': 'buh', 'password': 'pw'})
        switched = c.patch(f'/api/accountant/employees/{person.id}', json=body)
        assert switched.status_code == 200
        assert switched.json()['employee']['manual_since'] == today_tashkent().isoformat()
        assert mark(c, person.id, DAY, True).status_code == 200
