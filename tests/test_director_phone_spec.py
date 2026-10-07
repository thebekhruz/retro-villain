"""Телефон директора (6a) по «Функционал.md»: месяц в строке сотрудника,
официанты Retro с чеками и сменами, группы блюд для среза «Слабые» и
проверки бухгалтера, которые директор читает, но не пишет."""

from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import Settings
from retro.modules.cashier.service import today_tashkent
from retro.modules.director.models import SalesRow, build_snapshot


def client(tmp_path, **settings):
    app = create_app(Settings(**settings), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     director_db_path=tmp_path / 'director.sqlite3')
    return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))


def test_team_rows_carry_month_shifts_paid_and_salary_left(tmp_path, monkeypatch):
    with client(tmp_path) as c:
        shift = c.post('/api/director/team', json={
            'type': 'shift', 'name': 'Жасур', 'role': 'официант', 'amount': '180000'}).json()['employee']
        salaried = c.post('/api/director/team', json={
            'type': 'monthly', 'name': 'Шоира', 'role': 'бухгалтер', 'amount': '6000000'}).json()['employee']
        finance = c.app.state.accountant_finance
        today = today_tashkent()
        first = today.replace(day=1)
        seen = {}

        def payroll_month(start, end):
            seen['range'] = (start, end)
            # Две смены пришёл, одну не пришёл; выдано за одну.
            return dict(shift=[dict(employee_id=shift['id'], name='Жасур', paid='180000', cells={
                '1': dict(status='on_time'), '2': dict(status='manual_present'),
                '3': dict(status='missing')})])

        monkeypatch.setattr(finance, 'payroll_month', payroll_month)
        monkeypatch.setattr(finance, 'monthly_payments', lambda start, end: [
            dict(id=1, day=start.isoformat(), amount='4000000', employee_id=salaried['id'], created_at=None),
            dict(id=2, day=end.isoformat(), amount='2500000', employee_id=salaried['id'], created_at=None)])
        team = c.get('/api/director/team', params={'date': today.isoformat()}).json()

    assert seen['range'] == (first, today)
    row = next(item for item in team['shift'] if item['employee_id'] == shift['id'])
    assert row['month_shifts'] == 2 and row['month_paid'] == '180000'
    person = next(item for item in team['monthly'] if item['id'] == salaried['id'])
    # Выдано больше оклада: остаток отрицательный — это переплата, не ноль.
    assert person['month_paid'] == '6500000' and person['month_left'] == '-500000'
    assert team['month'] == today.strftime('%Y-%m')


def sale(*, item='Плов', category='Основное меню', register='Kassa-FiscalBox1', section='Ресторан',
         revenue='200000', waiter='Олег', order_id='o-1', day=date(2026, 9, 8)):
    return SalesRow(day, register, section, 'Наличные', item, category,
                    Decimal('1'), Decimal(revenue), Decimal('80000'), waiter, order_id)


def test_snapshot_exposes_item_groups_and_retro_waiter_checks_shifts():
    start = date(2026, 9, 8)
    rows = [
        sale(order_id='a', day=start), sale(order_id='a', item='Чай', category='Напитки', day=start),
        sale(order_id='b', day=start + timedelta(days=1)),
        # Счёт Шефа без выручки — не чек.
        sale(order_id='c', revenue='0', day=start + timedelta(days=1)),
        # Oxbridge в официанты Retro не входит.
        sale(order_id='d', register='GL-Kassa-Oksbrich', section='Зал', waiter='Алина', day=start),
    ]
    data = build_snapshot(rows, {}, start, start + timedelta(days=1)).json()
    assert data['item_groups'] == {'Плов': 'Основное меню', 'Чай': 'Напитки'}
    assert set(data['waiter_retro']) == {'Олег'}
    oleg = data['waiter_retro']['Олег']
    assert oleg['checks'] == 2 and oleg['shifts'] == 2
    assert oleg['revenue'] == '600000.00' and oleg['average_check'] == '300000.00'


def test_director_reads_accountant_checks_data_but_cannot_write(tmp_path):
    users = {'dir': ('password', 'director')}
    with client(tmp_path, dashboard_panel_users=users) as c:
        c.post('/api/session', json={'username': 'dir', 'password': 'password'})
        today = today_tashkent().isoformat()
        staff = c.get('/api/director/accounting/staff', params={'date': today})
        buys = c.get('/api/director/accounting/purchases', params={'date': today})
        assert staff.status_code == 200 and 'employees' in staff.json()
        assert buys.status_code == 200 and buys.json()['purchases'] == []
        # Сам модуль бухгалтера директору закрыт: писать туда он не может.
        assert c.get('/api/accountant/staff', params={'date': today}).status_code == 403
