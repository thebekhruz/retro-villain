"""T-428: три даты зарплаты, выходной бухгалтера и согласованность итогов (ТЗ 09.10, Б-01–Б-03).

Смена — день, когда человек работал; выплата — день, когда деньги ушли из
кассы; ввод — когда бухгалтер это записал. Смена 08.10 → выплата 09.10 →
расход в «Финансах дня» 09.10 ровно один раз: в журнале, на дэшборде, в итогах
дня, в ведомости, у учредителя. Поздний ввод дату выплаты не двигает.

Всё — через API, как ходит экран, с ручной передачей кассы, как на проде, и на
обеих базах: SQLite и Postgres (RETRO_TEST_POSTGRES_URL), как в CI.
"""

import os
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import Settings
from retro.integrations.hikvision import HikvisionEvent
from retro.modules.accountant.hikvision import AttendanceStore
from retro.modules.cashier.service import TZ, today_tashkent

POSTGRES_URL = os.getenv('RETRO_TEST_POSTGRES_URL', '')
DATABASES = ['sqlite', pytest.param('postgres', marks=pytest.mark.skipif(
    not POSTGRES_URL, reason='RETRO_TEST_POSTGRES_URL не задан'))]


def oct_(day: int) -> date:
    return date(2026, 10, day)


@contextmanager
def postgres_url():
    """Своя схема на тест: таблицы других тестов не мешают."""
    import psycopg
    from psycopg import sql
    schema = 'payout_' + uuid4().hex
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(POSTGRES_URL)
    query = dict(parse_qsl(parts.query), options='-csearch_path=' + schema)
    try:
        yield urlunsplit(parts._replace(query=urlencode(query)))
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture(params=DATABASES)
def c(request, tmp_path, monkeypatch):
    """Панель бухгалтера с ручной передачей кассы. «Сегодня» двигает тест: c.clock."""
    clock = {'today': oct_(10)}
    for module in ('routes', 'salary_day'):
        monkeypatch.setattr(f'retro.modules.accountant.{module}.today_tashkent', lambda: clock['today'])
    # Реестр заведён 1 октября: версии сотрудников действуют на все смены месяца.
    monkeypatch.setattr('retro.modules.accountant.roster.today_tashkent', lambda: oct_(1))

    def opened(app):
        client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))
        client.clock = clock
        return client

    if request.param == 'postgres':
        with postgres_url() as url:
            app = create_app(Settings(database_url=url, data_dir=tmp_path, manual_handover_only=True))
            with opened(app) as client:
                yield client
        return
    app = create_app(Settings(data_dir=tmp_path, manual_handover_only=True),
                     expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     director_db_path=tmp_path / 'director.sqlite3',
                     founder_db_path=tmp_path / 'founder.sqlite3')
    with opened(app) as client:
        yield client


def cash_days(c, first, last, amount='5000000'):
    """Сумма от кассира, вписанная бухгалтером, за каждый день — входит в остаток сразу."""
    day = first
    while day <= last:
        c.app.state.accountant_finance.record_handover(day, Decimal(amount))
        day += timedelta(days=1)


def seed(c, *, cash_through=oct_(10), opening='20000000', handover='5000000'):
    """Контрольные примеры ТЗ: Ихтиер пришёл 07.10 в 09:38 и 08.10 в 09:31,
    Жахонгир 08.10 в 10:58 (опоздал), Сельвины не было, Карамат — временная."""
    cash_days(c, oct_(2), cash_through, handover)
    c.app.state.accountant_finance.set_cash_opening(oct_(2), opening, 'Пересчёт кассы')
    roster = c.app.state.accountant_roster
    people = dict(
        ikhtiyor=roster.add(name='Баходиров Ихтиер', role='Менеджер', rate='360000', group_name='Управление'),
        jakhongir=roster.add(name='Каримов Жахонгир', role='Менеджер', rate='360000', group_name='Управление'),
        selvina=roster.add(name='Абдулганиева Сельвина', role='Хостес', rate='360000',
                           group_name='Встреча гостей'),
        karamat=roster.add(name='Карамат', role='Хостес · временная', rate='150000',
                           group_name='Встреча гостей'))
    entries = [('ikhtiyor', oct_(7), 9, 38), ('ikhtiyor', oct_(8), 9, 31), ('jakhongir', oct_(8), 10, 58)]
    for index, (who, day, hour, minute) in enumerate(entries):
        c.app.state.attendance_store.ingest(HikvisionEvent(
            'retro-main-entry', f'e-{index}', str(100 + index),
            datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)), people[who].id)
    return people


def cell(c, paid_day, person, amount, expected='0'):
    return c.put('/api/accountant/salary-day/cell', json={
        'date': paid_day.isoformat(), 'employee_id': person.id,
        'amount': str(amount), 'expected_amount': str(expected)})


def day_json(c, day):
    response = c.get('/api/accountant/day', params={'date': day.isoformat()})
    assert response.status_code == 200, response.text
    return response.json()


def sheet(c):
    response = c.get('/api/accountant/salary-day/month', params={'month': '2026-10'})
    assert response.status_code == 200, response.text
    return response.json()


def table(c, name):
    with closing(c.app.state.accountant_finance._open()) as connection:
        return connection.execute(f'SELECT * FROM {name} ORDER BY id').fetchall()


def spent_on(c, day) -> Decimal:
    """Сменные за день выплаты — одним числом, если оно сходится везде.

    Журнал «Финансов дня» (строки выплат), дэшборд (раскладка дня), итог дня
    (cash_flow), выданное по ведомости «Зарплата · день» за этот столбец,
    «Зарплата · месяц» (выдано из кассы за день) и учредитель (расходы и
    движения денег за период) обязаны показать одно и то же.
    """
    data = day_json(c, day)
    ledger = data['ledger']
    finance = c.app.state.accountant_finance
    month = c.get('/api/accountant/payroll/month', params={'month': '2026-10'}).json()
    views = dict(
        journal=sum((Decimal(m['amount']) for m in ledger['movements'] if m['type'] == 'salary_payment'),
                    Decimal(0)),
        dashboard=Decimal(ledger['day_flow']['salary']),
        day_total=Decimal(ledger['cash_flow']['salary_paid']),
        summary=Decimal(ledger['salary_paid_on_day']),
        sheet=sum((Decimal(person['cells'][day.isoformat()]['amount']) for person in sheet(c)['people']),
                  Decimal(0)),
        payroll_month=Decimal(month['paid_per_day'].get(day.isoformat(), '0')),
        founder=finance.expense_totals_between(day, day)['salary'],
        founder_flows=sum((Decimal(row['amount']) for row in finance.cash_flows_between(day, day)
                           if row['type'] == 'salary_payment'), Decimal(0)))
    assert len(set(views.values())) == 1, views
    return views['journal']


def shift_lines(c, day):
    """Строки выплат дня: (имя, день смены, сумма) — то, из чего журнал собирает «Сменные за …»."""
    return sorted((m['description'].split(' · ')[1], m['description'].rsplit(' за ', 1)[1], m['amount'])
                  for m in day_json(c, day)['ledger']['movements'] if m['type'] == 'salary_payment')


def flow(c, day):
    return day_json(c, day)['ledger']['day_flow']


# ── Б-01: смена, выплата, ввод ─────────────────────────────────────────────

def test_shift_0810_paid_0910_is_spent_on_0910_once_and_late_entry_keeps_the_payout_day(c):
    people = seed(c)
    c.clock['today'] = oct_(9)
    first = cell(c, oct_(9), people['ikhtiyor'], 360000)
    assert first.status_code == 200, first.text
    assert (first.json()['date'], first.json()['work_day']) == ('2026-10-09', '2026-10-08')
    assert cell(c, oct_(9), people['jakhongir'], 360000).status_code == 200

    assert spent_on(c, oct_(9)) == Decimal('720000')
    assert spent_on(c, oct_(8)) == 0
    assert shift_lines(c, oct_(9)) == [('Баходиров Ихтиер', '2026-10-08', '360000'),
                                       ('Каримов Жахонгир', '2026-10-08', '360000')]
    # Начисление — за смену 08.10, выплата — 09.10.
    accruals = {row[3]: row[1] for row in table(c, 'accountant_accruals')}
    assert accruals == {'Баходиров Ихтиер': '2026-10-08', 'Каримов Жахонгир': '2026-10-08'}

    # 10.10 бухгалтер дописывает выдачу 09.10 Карамат: выплата остаётся 09.10.
    c.clock['today'] = oct_(10)
    late = cell(c, oct_(9), people['karamat'], 150000)
    assert late.status_code == 200, late.text
    assert (late.json()['date'], late.json()['work_day']) == ('2026-10-09', '2026-10-08')
    assert spent_on(c, oct_(9)) == Decimal('870000')
    assert spent_on(c, oct_(10)) == 0
    assert spent_on(c, oct_(8)) == 0
    assert {row[2] for row in table(c, 'accountant_salary_payments')} == {'2026-10-09'}
    # Остаток 08.10 зарплата 09.10 не трогает, а 09.10 уменьшает ровно на неё.
    assert Decimal(flow(c, oct_(8))['closing']) - Decimal(flow(c, oct_(9))['closing']) \
        == Decimal('870000') - Decimal('5000000')
    sheet_cells = {person['name']: person['cells'] for person in sheet(c)['people']}
    assert sheet_cells['Карамат']['2026-10-09']['amount'] == '150000'
    assert sheet_cells['Карамат']['2026-10-10']['amount'] == '0'
    assert sheet_cells['Карамат']['2026-10-08']['amount'] == '0'


# ── Б-03: повтор, правка, отмена ───────────────────────────────────────────

def test_resave_edit_and_cancel_keep_every_total_in_step(c):
    people = seed(c)
    person = people['ikhtiyor']
    closing_before = Decimal(flow(c, oct_(9))['closing'])
    assert cell(c, oct_(9), person, 360000).json()['changed'] is True
    # Ответ потерялся, экран повторил тот же запрос — второй выплаты нет.
    again = cell(c, oct_(9), person, 360000, expected='0')
    assert again.status_code == 200 and again.json()['changed'] is False
    assert cell(c, oct_(9), person, 360000, expected='360000').json()['changed'] is False
    assert len(table(c, 'accountant_salary_payments')) == len(table(c, 'accountant_accruals')) == 1
    assert spent_on(c, oct_(9)) == Decimal('360000')

    assert cell(c, oct_(9), person, 300000, expected='360000').status_code == 200
    assert spent_on(c, oct_(9)) == Decimal('300000')
    assert Decimal(flow(c, oct_(9))['closing']) == closing_before - Decimal('300000')
    assert len(table(c, 'accountant_salary_payments')) == 1

    # Чужая (устаревшая) ожидаемая сумма — отказ, ничего не меняется.
    stale = cell(c, oct_(9), person, 100000, expected='360000')
    assert stale.status_code == 409
    assert spent_on(c, oct_(9)) == Decimal('300000')

    assert cell(c, oct_(9), person, 0, expected='300000').status_code == 200
    assert spent_on(c, oct_(9)) == 0
    assert table(c, 'accountant_salary_payments') == table(c, 'accountant_accruals') == []
    assert Decimal(flow(c, oct_(9))['closing']) == closing_before
    assert day_json(c, oct_(9))['ledger']['salary_debt'] == '0'
    actions = [entry['action'] for entry in c.app.state.accountant_finance.audit_entries(
        entity_type='salary_payment')]
    assert actions == ['create', 'update', 'delete']


def test_correcting_a_past_payout_moves_the_following_openings(c):
    people = seed(c, opening='1000000', handover='0')
    person = people['ikhtiyor']
    assert cell(c, oct_(9), person, 360000).status_code == 200
    assert Decimal(flow(c, oct_(10))['opening']) == Decimal(flow(c, oct_(9))['closing']) == Decimal('640000')
    spend = c.post('/api/accountant/expenses', json={'date': '2026-10-10', 'item_code': 'admin_other',
                                                      'note': 'Ремонт', 'amount': '600000'})
    assert spend.status_code == 201, spend.text
    report = c.post('/api/accountant/day-report', json={'date': '2026-10-10'})
    assert report.status_code == 200, report.text
    assert day_json(c, oct_(10))['day_report']['changed'] is False

    # Выдали 09.10 меньше, чем записали: остатки 09.10 и 10.10 растут на разницу,
    # сданный отчёт 10.10 помечается «изменён после сдачи».
    assert cell(c, oct_(9), person, 200000, expected='360000').status_code == 200
    assert Decimal(flow(c, oct_(9))['closing']) == Decimal('800000')
    assert Decimal(flow(c, oct_(10))['opening']) == Decimal('800000')
    assert Decimal(flow(c, oct_(10))['closing']) == Decimal('200000')
    assert day_json(c, oct_(10))['day_report']['changed'] is True

    # Больше, чем осталось бы в последующие дни, — отказ целиком: 10.10 ушёл бы в минус.
    refused = cell(c, oct_(9), person, 500000, expected='200000')
    assert refused.status_code == 422 and 'последующие дни' in refused.json()['detail']
    assert spent_on(c, oct_(9)) == Decimal('200000')
    assert Decimal(flow(c, oct_(10))['closing']) == Decimal('200000')


# ── Б-03: частичные и дополнительные выплаты сходятся везде ────────────────

def test_partial_and_extra_payouts_agree_in_sheet_journal_and_dashboard(c):
    people = seed(c)
    c.clock['today'] = oct_(8)
    # Смена 07.10 подтверждена по-старому (начисление по проходам), выдают частями.
    confirmed = c.post('/api/accountant/payroll/confirm', json={'date': '2026-10-07', 'approver': 'Бухгалтер'})
    assert confirmed.status_code == 200, confirmed.text
    accrual = {row[3]: row[0] for row in table(c, 'accountant_accruals')}
    first_part = c.post('/api/accountant/salary-payments', json={
        'accrual_id': accrual['Каримов Жахонгир'], 'date': '2026-10-08', 'amount': '200000'})
    assert first_part.status_code == 201, first_part.text
    # Ихтиеру 08.10 — галочкой в ведомости: прежнее начисление становится ручной парой.
    assert cell(c, oct_(8), people['ikhtiyor'], 360000).status_code == 200

    c.clock['today'] = oct_(9)
    rest = c.post('/api/accountant/salary-payments', json={
        'accrual_id': accrual['Каримов Жахонгир'], 'date': '2026-10-09', 'amount': '160000'})
    assert rest.status_code == 201, rest.text
    # Другая сумма за смену 08.10 и оклад частями — в тот же день.
    assert cell(c, oct_(9), people['karamat'], 100000).status_code == 200
    monthly = c.post('/api/accountant/monthly-employees', json={
        'name': 'Шеф-повар', 'role': 'шеф', 'salary': '9000000'})
    assert monthly.status_code == 201, monthly.text
    paid = c.post('/api/accountant/monthly-payments', json={
        'date': '2026-10-09', 'employee_id': monthly.json()['employee']['id'], 'amount': '1000000'})
    assert paid.status_code == 201, paid.text

    assert spent_on(c, oct_(8)) == Decimal('560000')
    assert spent_on(c, oct_(9)) == Decimal('260000')
    assert shift_lines(c, oct_(9)) == [('Карамат', '2026-10-08', '100000'),
                                       ('Каримов Жахонгир', '2026-10-07', '160000')]
    data = day_json(c, oct_(9))
    ledger = data['ledger']
    assert Decimal(ledger['day_flow']['monthly']) == Decimal('1000000')
    assert sum((Decimal(m['amount']) for m in ledger['movements']
                if m['type'] == 'other_expense' and m['item_code'] == 'salary_monthly'), Decimal(0)) \
        == Decimal('1000000')
    # Всё списание дня — одно число в журнале и на дэшборде.
    assert Decimal(ledger['cash_flow']['salary_paid']) + Decimal(ledger['cash_flow']['other_outflows']) \
        == Decimal(ledger['day_flow']['outflows']) == Decimal('1260000')
    # Долг: смена 07.10 Сельвины и Карамат начислена и не выдана; Жахонгиру выдано целиком.
    assert Decimal(ledger['salary_debt']) == Decimal('510000')
    # В ведомости выдача Жахонгиру 09.10 за смену 07.10 видна в столбце 09.10, но
    # клетка заперта: смена 08.10 за ней не та, правка задвоила бы деньги.
    cells = {person['name']: person['cells'] for person in sheet(c)['people']}
    assert cells['Каримов Жахонгир']['2026-10-09'] == dict(
        amount='160000', work_day='2026-10-08', editable=False, rate='360000',
        attendance=dict(status='late', time='10:58', source='late'))
    assert cells['Каримов Жахонгир']['2026-10-08']['editable'] is False


# ── Б-02: выходной бухгалтера ──────────────────────────────────────────────

def test_day_off_cash_is_confirmed_separately_and_both_days_are_processed_later(c):
    people = seed(c, cash_through=oct_(8))
    finance = c.app.state.accountant_finance
    # 09.10 у бухгалтера выходной: кассир передал кассы за 08.10 и за 09.10 кнопкой,
    # бухгалтер их не подтверждал и ничего не вносил.
    for day in (oct_(9), oct_(10)):
        finance.record_handover(day, Decimal('5000000'), source='cashier', replace_sources=('cashier', 'auto'))
    c.clock['today'] = oct_(10)
    gap = day_json(c, oct_(9))['ledger']
    assert gap['cash_flow']['handover_status'] == 'pending' and gap['salary_paid_on_day'] == '0'

    # 10.10: подтверждает кассу дня и сразу работает с 10.10 — 09.10 не закрыт.
    confirm = c.post('/api/accountant/handover/confirm', json={'date': '2026-10-10', 'amount': '5000000'})
    assert confirm.status_code == 200, confirm.text
    assert cell(c, oct_(10), people['ikhtiyor'], 360000).status_code == 200
    report = c.post('/api/accountant/day-report', json={'date': '2026-10-10'})
    assert report.status_code == 200, report.text
    # Пробел — не удаление и не готовый расчёт: столбец 09.10 открыт и пуст,
    # касса 09.10 всё ещё ждёт подтверждения.
    cells = {person['name']: person['cells'] for person in sheet(c)['people']}
    assert all(cells[name]['2026-10-09'] == dict(cells[name]['2026-10-09'], amount='0', editable=True)
               for name in cells)
    assert day_json(c, oct_(9))['ledger']['cash_flow']['handover_status'] == 'pending'

    # Потом — 09.10: подтверждает кассу и вносит выдачу 09.10 за смену 08.10.
    confirm = c.post('/api/accountant/handover/confirm', json={'date': '2026-10-09', 'amount': '5000000'})
    assert confirm.status_code == 200, confirm.text
    assert cell(c, oct_(9), people['ikhtiyor'], 360000).status_code == 200
    assert cell(c, oct_(9), people['jakhongir'], 300000).status_code == 200

    assert spent_on(c, oct_(9)) == Decimal('660000')
    assert spent_on(c, oct_(10)) == Decimal('360000')
    assert shift_lines(c, oct_(9)) == [('Баходиров Ихтиер', '2026-10-08', '360000'),
                                       ('Каримов Жахонгир', '2026-10-08', '300000')]
    assert shift_lines(c, oct_(10)) == [('Баходиров Ихтиер', '2026-10-09', '360000')]
    nine, ten = flow(c, oct_(9)), flow(c, oct_(10))
    assert Decimal(ten['opening']) == Decimal(nine['closing'])
    assert Decimal(nine['closing']) == Decimal(nine['opening']) + Decimal('5000000') - Decimal('660000')
    assert Decimal(ten['closing']) == Decimal(ten['opening']) + Decimal('5000000') - Decimal('360000')
    # Ничего не потерялось: три выплаты, у каждой — запись в истории.
    assert len(table(c, 'accountant_salary_payments')) == 3
    assert [entry['action'] for entry in finance.audit_entries(entity_type='salary_payment')] == ['create'] * 3
    # Отчёт 10.10 сдан до разбора 09.10 — его начало сдвинулось, и это видно.
    assert day_json(c, oct_(10))['day_report']['changed'] is True


def test_a_day_without_any_cash_row_stops_only_until_its_cash_is_entered(c):
    people = seed(c, cash_through=oct_(8))
    c.app.state.accountant_finance.record_handover(oct_(10), Decimal('5000000'))
    c.clock['today'] = oct_(10)
    # За 09.10 ни передачи, ни записи: остаток через этот день не переносится.
    refused = cell(c, oct_(10), people['ikhtiyor'], 360000)
    assert refused.status_code == 422 and '2026-10-09' in refused.json()['detail']
    assert table(c, 'accountant_salary_payments') == []
    # Достаточно вписать кассу 09.10 — операции 09.10 разбирать не нужно.
    added = c.post('/api/accountant/handover', json={'date': '2026-10-09', 'amount': '5000000',
                                                     'note': 'Касса за 08.10'})
    assert added.status_code == 201, added.text
    assert cell(c, oct_(10), people['ikhtiyor'], 360000).status_code == 200
    assert spent_on(c, oct_(10)) == Decimal('360000')
    assert spent_on(c, oct_(9)) == 0


# ── C-02: столбец сегодняшней выплаты без кассы дня ────────────────────────

def test_todays_payout_without_todays_cash_names_the_cash_to_enter(c):
    """Утро 09.10: кассу за 08.10 ещё не вписали. Выдача за смену 08.10 не
    записывается — и отказ говорит, какую кассу вписать и где, а не «обновите
    отчёт» (в ручном режиме обновлять нечего). Клетка остаётся пустой, в
    финансах 09.10 — 0: это и есть «вносили — а там ноль»."""
    people = seed(c, cash_through=oct_(8))
    c.clock['today'] = oct_(9)
    refused = cell(c, oct_(9), people['ikhtiyor'], 360000)
    assert refused.status_code == 409
    detail = refused.json()['detail']
    assert '09.10' in detail and '08.10' in detail and '«Финансах дня»' in detail
    assert table(c, 'accountant_salary_payments') == []
    assert spent_on(c, oct_(9)) == 0
    # Вчерашний столбец открыт, но это выплата 08.10 за смену 07.10 — не смена 08.10.
    yesterday = cell(c, oct_(8), people['ikhtiyor'], 360000)
    assert yesterday.json()['work_day'] == '2026-10-07'
    assert spent_on(c, oct_(8)) == Decimal('360000') and spent_on(c, oct_(9)) == 0
    assert cell(c, oct_(8), people['ikhtiyor'], 0, expected='360000').status_code == 200

    # Касса 09.10 вписана — та же выдача ложится в 09.10.
    added = c.post('/api/accountant/handover', json={'date': '2026-10-09', 'amount': '5000000',
                                                     'note': 'Касса за 08.10'})
    assert added.status_code == 201, added.text
    assert cell(c, oct_(9), people['ikhtiyor'], 360000).status_code == 200
    assert spent_on(c, oct_(9)) == Decimal('360000') and spent_on(c, oct_(8)) == 0


def test_payout_day_follows_tashkent_midnight_not_the_server_clock():
    """Сервер на Railway живёт в UTC: 08.10 с 19:00 UTC в Ташкенте уже 09.10,
    и столбец «сегодня» в ведомости — 09.10."""
    assert today_tashkent(datetime(2026, 10, 8, 18, 59, tzinfo=timezone.utc)) == oct_(8)
    assert today_tashkent(datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)) == oct_(9)
    assert today_tashkent(datetime(2026, 10, 8, 23, 50, tzinfo=TZ)) == oct_(8)


def test_late_entry_after_midnight_belongs_to_the_new_shift(tmp_path):
    """Проход в 00:20 по Ташкенту (19:20 UTC накануне) — смена нового дня."""
    store = AttendanceStore(tmp_path / 'accountant.sqlite3')
    store.ingest(HikvisionEvent('retro-main-entry', 's-1', '7', datetime(2026, 10, 8, 19, 20, tzinfo=timezone.utc)), 1)
    assert set(store.first_entries(oct_(9))) == {1} and store.first_entries(oct_(8)) == {}
