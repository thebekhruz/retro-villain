"""Закуп Шоха: журнал покупок, фото, время закупа и приёмка бухгалтером."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import Settings, parse_dashboard_panel_users
from retro.modules.cashier.service import TZ
from retro.modules.shokh.trips import trip_minutes
from retro.modules.shokh.store import ShokhError, ShokhStore

DAY = date(2026, 9, 16)
AT = datetime(2026, 9, 16, 9, 30, tzinfo=TZ)
# Однопиксельный PNG: настоящая картинка, но без лишних байтов в тесте.
PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4'
    '890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082')


def client(tmp_path):
    app = create_app(Settings(shokh_module=True), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3')
    return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))


def store(tmp_path):
    return ShokhStore(tmp_path / 'accountant.sqlite3')


def purchase(client_, **extra):
    payload = dict(point='Базар', item='Помидоры', unit='кг', quantity='12', price='9000',
                   date=DAY.isoformat())
    payload.update(extra)
    return client_.post('/api/shokh/purchase', data=payload)


# ── Журнал ────────────────────────────────────────────────────────────────

def test_purchase_multiplies_quantity_by_price_and_keeps_the_unit(tmp_path):
    with client(tmp_path) as c:
        row = purchase(c).json()['purchase']
        assert row['total'] == '108000.00'
        assert row['unit'] == 'кг'
        assert row['has_photo'] is False


@pytest.mark.parametrize('field,value', [
    ('quantity', '0'), ('quantity', '-3'), ('quantity', 'много'),
    ('price', '0'), ('price', 'дорого'), ('item', '   '), ('point', ''),
])
def test_purchase_refuses_nonsense_input(tmp_path, field, value):
    with client(tmp_path) as c:
        assert purchase(c, **{field: value}).status_code == 422


def test_unknown_unit_is_refused(tmp_path):
    with client(tmp_path) as c:
        assert purchase(c, unit='мешок').status_code == 422


# ── Фото ──────────────────────────────────────────────────────────────────

def test_photo_is_stored_and_served_back(tmp_path):
    with client(tmp_path) as c:
        response = c.post('/api/shokh/purchase',
                          data=dict(point='Базар', item='Лук', unit='кг',
                                    quantity='5', price='4000', date=DAY.isoformat()),
                          files={'photo': ('shot.png', PNG, 'image/png')})
        assert response.status_code == 201
        row = response.json()['purchase']
        assert row['has_photo'] is True
        photo = c.get(f'/api/shokh/photo/{row["id"]}')
        assert photo.status_code == 200
        assert photo.content == PNG
        assert photo.headers['content-type'].startswith('image/png')
        # Фото — часть финансового документа, в общий кеш его отдавать нельзя.
        assert 'no-store' in photo.headers['cache-control']


def test_missing_photo_is_a_clean_404_not_an_empty_body(tmp_path):
    with client(tmp_path) as c:
        row = purchase(c).json()['purchase']
        assert c.get(f'/api/shokh/photo/{row["id"]}').status_code == 404
        assert c.get('/api/shokh/photo/9999').status_code == 404


def test_store_refuses_a_file_that_is_not_an_image_or_is_too_big(tmp_path):
    shokh = store(tmp_path)
    with pytest.raises(ShokhError):
        shokh.add_purchase(DAY, AT, point='Базар', item='Лук', unit='кг', quantity='1',
                           price='100', photo=b'%PDF-1.4', photo_type='application/pdf')
    with pytest.raises(ShokhError):
        shokh.add_purchase(DAY, AT, point='Базар', item='Лук', unit='кг', quantity='1',
                           price='100', photo=b'x' * (6 * 1024 * 1024 + 1), photo_type='image/png')


# ── Обычная цена ──────────────────────────────────────────────────────────

def test_usual_price_is_the_median_so_one_costly_trip_does_not_move_it(tmp_path):
    shokh = store(tmp_path)
    for offset, price in ((5, '9000'), (4, '9500'), (3, '9200'), (2, '40000')):
        shokh.add_purchase(DAY - timedelta(days=offset), AT, point='Базар', item='Помидоры',
                           unit='кг', quantity='1', price=price)
    # Медиана 9000/9200/9500/40000 — между 9200 и 9500, а не среднее с выбросом.
    assert shokh.usual_price('Помидоры', before=DAY) == Decimal('9350.00')


def test_price_above_the_usual_one_is_flagged_for_the_accountant(tmp_path):
    shokh = store(tmp_path)
    for offset in (3, 2):
        shokh.add_purchase(DAY - timedelta(days=offset), AT, point='Базар', item='Лук',
                           unit='кг', quantity='1', price='5000')
    cheap = shokh.add_purchase(DAY, AT, point='Базар', item='Лук', unit='кг',
                               quantity='1', price='4800')
    dear = shokh.add_purchase(DAY, AT, point='Базар', item='Лук', unit='кг',
                              quantity='1', price='7500')
    assert cheap['price_above_usual'] is False
    assert dear['price_above_usual'] is True
    assert dear['usual_price'] == '5000.00'


def test_the_first_ever_purchase_of_an_item_is_not_suspicious(tmp_path):
    shokh = store(tmp_path)
    row = shokh.add_purchase(DAY, AT, point='Базар', item='Айва', unit='кг',
                             quantity='2', price='30000')
    assert row['usual_price'] is None
    assert row['price_above_usual'] is False


# ── Время закупа ──────────────────────────────────────────────────────────

def test_trip_minutes_count_only_a_finished_trip():
    done = dict(started_at='2026-09-16T09:00:00+05:00', finished_at='2026-09-16T09:12:00+05:00')
    assert trip_minutes(done) == 12
    assert trip_minutes(dict(done, finished_at=None)) is None


def test_purchase_and_trip_answers_carry_no_experience(tmp_path):
    """Опыт, уровни, задания и серию дней убрали: в ответах их быть не должно."""
    with client(tmp_path) as c:
        trip = c.post('/api/shokh/trip', params={'date': DAY.isoformat()}).json()
        row = purchase(c, trip_id=str(trip['trip_id'])).json()
        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        done = c.post(f"/api/shokh/trip/{trip['trip_id']}/finish").json()
    gamified = {'xp', 'level', 'quests', 'streak', 'week', 'fast_trip_minutes'}
    assert not gamified & set(row)
    assert not gamified & set(home)
    assert not gamified & set(done)
    assert 'bonus' not in done['trip']
    assert done['spent'] == '108000.00'


# ── Деньги: подотчёт и приёмка ────────────────────────────────────────────

def advance(c, day, amount):
    """Бухгалтер выдаёт подотчёт: начальный остаток плюс выдача наличными."""
    assert c.post('/api/accountant/reserves', json={
        'date': day.isoformat(), 'account': 'shoh', 'kind': 'opening',
        'amount': amount, 'note': 'Пересчёт подотчёта'}).status_code == 201


def shoh_expense(c, day, amount, place='Алайский базар'):
    response = c.post('/api/accountant/shoh/expenses', json={
        'date': day.isoformat(), 'place': place, 'amount': amount, 'note': 'Счёт-фактура №1'})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_pocket_is_allocated_less_invoice_expenses_entered_by_the_accountant(tmp_path):
    """ТЗ 02.10, п. 5: остаток Шоха = выделено − расходы по счёт-фактуре, всё
    вносит бухгалтер. Покупка с телефона остаток не меняет."""
    with client(tmp_path) as c:
        advance(c, DAY, '900000')
        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        assert home['accounting_balance'] == '900000'
        assert home['pocket'] == '900000'

        row = purchase(c).json()
        assert row['accounting_balance'] == '900000'
        assert row['pocket'] == '900000'
        shoh_expense(c, DAY, '108000')
        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        assert Decimal(home['pocket']) == Decimal('792000')


def test_accepting_a_purchase_moves_it_out_of_pending_without_touching_the_till(tmp_path):
    with client(tmp_path) as c:
        advance(c, DAY, '900000')
        row = purchase(c).json()['purchase']

        before = c.get('/api/accountant/day', params={'date': DAY.isoformat()}).json()
        accepted = c.post(f'/api/accountant/shokh/purchases/{row["id"]}/accept',
                          json={'date': DAY.isoformat()})
        assert accepted.status_code == 200
        assert accepted.json()['purchase']['accepted_at'] is not None

        after = c.get('/api/accountant/day', params={'date': DAY.isoformat()}).json()
        # Подотчёт уменьшился на сумму накладной.
        assert (Decimal(before['reserves']['shoh']['balance'])
                - Decimal(after['reserves']['shoh']['balance'])) == Decimal('108000.00')
        # Касса второй раз не списывается: наличные ушли при выдаче подотчёта.
        assert before['ledger']['cash_balance'] == after['ledger']['cash_balance']

        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        assert home['pending'] == '0'
        assert home['pocket'] == home['accounting_balance']


def test_a_purchase_cannot_be_accepted_twice(tmp_path):
    with client(tmp_path) as c:
        advance(c, DAY, '900000')
        row = purchase(c).json()['purchase']
        body = {'date': DAY.isoformat()}
        assert c.post(f'/api/accountant/shokh/purchases/{row["id"]}/accept', json=body).status_code == 200
        assert c.post(f'/api/accountant/shokh/purchases/{row["id"]}/accept', json=body).status_code == 409
        assert c.post('/api/accountant/shokh/purchases/4242/accept', json=body).status_code == 404


def test_without_an_advance_the_pocket_starts_at_zero(tmp_path):
    """Начальный остаток Шоха не обязателен: счёт начинается с нуля, и расход
    больше выделенного не записать."""
    with client(tmp_path) as c:
        purchase(c)
        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        assert home['accounting_balance'] == '0'
        assert home['pocket'] == '0'
        refused = c.post('/api/accountant/shoh/expenses', json={
            'date': DAY.isoformat(), 'place': 'Food City', 'amount': '1000'})
        assert refused.status_code == 422 and 'больше' in refused.json()['detail']


def test_the_accountant_sees_shokh_purchases_for_the_day(tmp_path):
    with client(tmp_path) as c:
        purchase(c)
        listed = c.get('/api/accountant/shokh/purchases', params={'date': DAY.isoformat()})
        assert listed.status_code == 200
        assert [row['item'] for row in listed.json()['purchases']] == ['Помидоры']


# ── Доступ ────────────────────────────────────────────────────────────────

def test_shokh_is_an_optional_role_so_existing_deployments_keep_working():
    base = 'a:1:cashier;b:2:accountant;c:3:director;d:4:founder'
    # Уже настроенные развёртывания не знают про закуп и должны проходить.
    assert parse_dashboard_panel_users(base)['a'] == ('1', 'cashier')
    with_shokh = parse_dashboard_panel_users(base + ';e:5:shokh')
    assert with_shokh['e'] == ('5', 'shokh')


def test_the_purchase_module_belongs_to_the_shokh_role():
    from retro.app import ROLE_PATHS, panel_for_path
    assert panel_for_path('/shokh') == 'shokh'
    assert panel_for_path('/api/shokh/home') == 'shokh'
    assert panel_for_path('/api/shokh/photo/1') == 'shokh'
    assert ROLE_PATHS['shokh'] == '/shokh'


def test_both_screens_report_the_same_pocket_from_one_formula(tmp_path):
    """«На руках у Шоха» считается в одном месте — store.pocket_position.

    Раньше формула была скопирована в кабинет учредителя, и копии могли
    разойтись. Тест держит их вместе: экран закупа и кабинет обязаны называть
    одну и ту же сумму.
    """
    with client(tmp_path) as c:
        advance(c, DAY, '900000')
        purchase(c)
        purchase(c, item='Лук', quantity='5', price='4000')

        shokh_home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        cabinet = c.get('/api/founder/spending', params={'date': DAY.isoformat()}).json()

        shoh_expense(c, DAY, '128000')
        shokh_home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        cabinet = c.get('/api/founder/spending', params={'date': DAY.isoformat()}).json()
        assert Decimal(shokh_home['pocket']) == Decimal('772000')
        # Кабинет округляет до копеек той же money(), поэтому сравниваем числом.
        assert Decimal(cabinet['shokh']['pocket']) == Decimal(shokh_home['pocket'])
        assert Decimal(shokh_home['accounting_balance']) == Decimal(shokh_home['pocket'])


def test_pocket_formula_lives_in_one_place_only():
    """Копии формулы в кабинете быть не должно — иначе тест выше однажды солжёт."""
    from pathlib import Path
    cabinet = Path('retro/modules/founder/cabinet.py').read_text(encoding='utf-8')
    assert 'pocket_position' in cabinet
    # Признак вернувшейся копии: самостоятельный пересчёт непринятых покупок.
    assert "accepted_at'] is None" not in cabinet


def test_repeating_employee_creation_with_one_key_does_not_add_a_second_person(tmp_path):
    """Потерянный ответ не должен рождать второго сотрудника со ставкой.

    Дубль в реестре начислился бы на следующем подтверждении смены как
    отдельный человек, поэтому создание идёт под ключом операции.
    """
    from uuid import uuid4
    with client(tmp_path) as c:
        before = len(c.get('/api/accountant/staff', params={'date': DAY.isoformat()}).json()['employees'])
        body = {'name': 'Шерзод Усманов', 'role': 'повар', 'rate': '260000', 'group': 'Кухня'}
        key = {'Idempotency-Key': str(uuid4())}

        first = c.post('/api/accountant/employees', json=body, headers=key)
        assert first.status_code == 201
        # Клиент не увидел ответа и повторил запрос тем же ключом.
        again = c.post('/api/accountant/employees', json=body, headers=key)
        assert again.status_code == 201
        assert again.json() == first.json(), 'повтор обязан вернуть тот же ответ, а не создать нового'

        staff = c.get('/api/accountant/staff', params={'date': DAY.isoformat()}).json()['employees']
        assert len(staff) == before + 1
        assert sum(row['name'] == 'Шерзод Усманов' for row in staff) == 1


def test_two_real_namesakes_can_both_be_added(tmp_path):
    """Однофамильцы — не дубли: под своим ключом добавляется каждый.

    Поэтому уникальность имени в базе не вводим: она запретила бы законный
    случай, а от повтора защищает ключ операции.
    """
    from uuid import uuid4
    with client(tmp_path) as c:
        body = {'name': 'Жасур Алиев', 'role': 'официант', 'rate': '200000',
                'group': 'Обслуживание зала'}
        assert c.post('/api/accountant/employees', json=body,
                      headers={'Idempotency-Key': str(uuid4())}).status_code == 201
        assert c.post('/api/accountant/employees', json=body,
                      headers={'Idempotency-Key': str(uuid4())}).status_code == 201
        staff = c.get('/api/accountant/staff', params={'date': DAY.isoformat()}).json()['employees']
        assert sum(row['name'] == 'Жасур Алиев' for row in staff) == 2


def test_registry_creation_is_guarded_by_the_request_journal():
    """Пути реестра должны быть в списке сторожа, иначе ключ игнорируется."""
    from retro.financial_requests import PATHS
    assert '/api/accountant/employees' in PATHS
    assert '/api/accountant/monthly-employees' in PATHS
    assert '/api/director/team' in PATHS


def test_reopened_trip_keeps_its_real_start_for_the_timer(tmp_path):
    """Закуп, брошенный на середине, продолжается: клиент получает то же время
    начала, и таймер на экране совпадает с итогом закупа."""
    with client(tmp_path) as c:
        first = c.post('/api/shokh/trip', params={'date': DAY.isoformat()}).json()
        again = c.post('/api/shokh/trip', params={'date': DAY.isoformat()}).json()
    assert again['trip_id'] == first['trip_id']
    assert again['started_at'] == first['started_at']
    assert first['started_at'].endswith('+05:00')


# ── «Функционал» §3.6 и §3a ───────────────────────────────────────────────

def test_day_position_follows_the_spec_formula_and_survives_acceptance(tmp_path):
    """На руках = на начало + выдано сегодня − расходы за день; «Отчитались за
    X%» = потрачено / (на начало + выдано). Расходы вносит бухгалтер по
    счёт-фактуре (ТЗ 02.10)."""
    yesterday = DAY - timedelta(days=1)
    with client(tmp_path) as c:
        advance(c, yesterday, '900000')
        shoh_expense(c, yesterday, '100000')
        assert c.post('/api/cashier/shokh', json={'date': DAY.isoformat(), 'amount': '500000'}).status_code == 201
        shoh_expense(c, DAY, '300000')
        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        assert Decimal(home['day_start']) == Decimal('800000')
        assert Decimal(home['given_today']) == Decimal('500000')
        assert Decimal(home['spent_day']) == Decimal('300000')
        assert Decimal(home['pocket']) == Decimal('1000000')
        assert home['reported_percent'] == 23  # 300 000 / 1 300 000


def test_without_an_advance_the_share_is_zero(tmp_path):
    with client(tmp_path) as c:
        purchase(c)
        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
    assert home['reported_percent'] == 0 and Decimal(home['day_start']) == 0


def test_up_to_ten_percent_above_the_usual_price_is_normal(tmp_path):
    shokh = store(tmp_path)
    for offset in (3, 2):
        shokh.add_purchase(DAY - timedelta(days=offset), AT, point='RETRO', item='Лук',
                           unit='кг', quantity='1', price='5000')
    normal = shokh.add_purchase(DAY, AT, point='RETRO', item='Лук', unit='кг', quantity='1', price='5500')
    above = shokh.add_purchase(DAY, AT, point='RETRO', item='Лук', unit='кг', quantity='1', price='5501')
    assert normal['price_above_usual'] is False
    assert above['price_above_usual'] is True


def test_the_four_venues_come_first_in_a_fixed_order(tmp_path):
    shokh = store(tmp_path)
    shokh.add_purchase(DAY, AT, point='Оптовый склад', item='Лук', unit='кг', quantity='1', price='1')
    assert shokh.points()[:5] == ['Школа MU', 'Школа YA', 'RETRO', 'ШЕФ Базаар', 'Оптовый склад']


def test_closing_an_empty_trip_cancels_it_and_a_trip_with_purchases_finishes(tmp_path):
    with client(tmp_path) as c:
        empty = c.post('/api/shokh/trip', params={'date': DAY.isoformat()}).json()
        assert c.post(f"/api/shokh/trip/{empty['trip_id']}/close").json() == {'cancelled': True}
        # Отменённый закуп не продолжается: следующий начинается заново.
        fresh = c.post('/api/shokh/trip', params={'date': DAY.isoformat()}).json()
        assert fresh['trip_id'] != empty['trip_id']
        purchase(c, trip_id=str(fresh['trip_id']))
        closed = c.post(f"/api/shokh/trip/{fresh['trip_id']}/close").json()
        assert closed['cancelled'] is False
        assert closed['trip']['finished_at'] is not None and len(closed['purchases']) == 1
        assert c.post('/api/shokh/trip/4242/close').status_code == 404
