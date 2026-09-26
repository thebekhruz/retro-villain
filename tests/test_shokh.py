"""Закуп Шоха: журнал покупок, фото, опыт и приёмка бухгалтером."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings, parse_dashboard_panel_users
from retro.modules.cashier.service import TZ
from retro.modules.shokh.gamification import (level_for, purchase_xp, quests, streak,
                                              total_xp, trip_bonus, week_marks)
from retro.modules.shokh.store import ShokhError, ShokhStore

DAY = date(2026, 9, 16)
AT = datetime(2026, 9, 16, 9, 30, tzinfo=TZ)
# Однопиксельный PNG: настоящая картинка, но без лишних байтов в тесте.
PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4'
    '890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082')


def client(tmp_path):
    app = create_app(Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
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


# ── Опыт, уровни, серия ───────────────────────────────────────────────────

def test_experience_rewards_a_photo_and_a_fair_price(tmp_path):
    plain = dict(has_photo=False, usual_price=None, price_above_usual=False)
    assert purchase_xp(plain)['total'] == 30
    assert purchase_xp(dict(plain, has_photo=True))['total'] == 40
    # Премия за цену — только когда есть с чем сравнивать.
    assert purchase_xp(dict(plain, usual_price='5000'))['total'] == 40
    assert purchase_xp(dict(plain, usual_price='5000', price_above_usual=True))['total'] == 30
    assert purchase_xp(dict(plain, has_photo=True, usual_price='5000'))['total'] == 50


def test_fast_trip_earns_the_bonus_and_a_slow_one_does_not():
    quick = dict(started_at='2026-09-16T09:00:00+05:00', finished_at='2026-09-16T09:12:00+05:00')
    slow = dict(started_at='2026-09-16T09:00:00+05:00', finished_at='2026-09-16T09:40:00+05:00')
    open_trip = dict(started_at='2026-09-16T09:00:00+05:00', finished_at=None)
    assert trip_bonus(quick) == 50
    assert trip_bonus(slow) == 0
    # Незакрытая поездка бонус ещё не заработала.
    assert trip_bonus(open_trip) == 0


def test_level_grows_with_experience_and_reports_what_is_left():
    assert level_for(0)['level'] == 1
    assert level_for(0)['title'] == 'Новичок закупа'
    second = level_for(400)
    assert second['level'] == 2
    assert second['to_next'] == 500
    assert 0 < second['progress'] < 1
    top = level_for(99999)
    assert top['next_at'] is None
    assert top['to_next'] == 0
    assert top['progress'] == 1.0


def test_streak_counts_back_from_today_or_yesterday():
    days = {date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)}
    assert streak(days, date(2026, 9, 16)) == 3
    # Утром закупа ещё не было — серия не должна обнуляться до конца дня.
    assert streak(days, date(2026, 9, 17)) == 3
    # А через день без закупа серия действительно прервалась.
    assert streak(days, date(2026, 9, 18)) == 0
    assert streak(set(), date(2026, 9, 16)) == 0


def test_week_marks_cover_seven_days_and_point_at_today():
    marks = week_marks({date(2026, 9, 16)}, date(2026, 9, 16))
    assert len(marks) == 7
    assert marks[-1]['today'] is True and marks[-1]['active'] is True
    assert sum(mark['active'] for mark in marks) == 1


def test_quests_track_purchases_photos_and_speed():
    rows = [dict(has_photo=True), dict(has_photo=True), dict(has_photo=False)]
    trips = [dict(started_at='2026-09-16T09:00:00+05:00', finished_at='2026-09-16T09:10:00+05:00')]
    by_key = {item['key']: item for item in quests(rows, trips)}
    assert by_key['three']['complete'] is True
    assert by_key['photos']['done'] == 2 and by_key['photos']['complete'] is False
    assert by_key['fast']['complete'] is True
    # Пустой день не выдаёт «все с фото» за отсутствие покупок.
    assert {item['key']: item['complete'] for item in quests([], [])}['photos'] is False


def test_total_experience_adds_trip_bonuses_to_purchases():
    rows = [dict(has_photo=True, usual_price=None, price_above_usual=False)]
    trips = [dict(started_at='2026-09-16T09:00:00+05:00', finished_at='2026-09-16T09:05:00+05:00')]
    assert total_xp(rows, trips) == 40 + 50


# ── Деньги: подотчёт и приёмка ────────────────────────────────────────────

def advance(c, day, amount):
    """Бухгалтер выдаёт подотчёт: начальный остаток плюс выдача наличными."""
    assert c.post('/api/accountant/reserves', json={
        'date': day.isoformat(), 'account': 'shoh', 'kind': 'opening',
        'amount': amount, 'note': 'Пересчёт подотчёта'}).status_code == 201


def test_pocket_is_the_advance_less_what_is_recorded_but_not_yet_accepted(tmp_path):
    with client(tmp_path) as c:
        advance(c, DAY, '900000')
        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        assert home['accounting_balance'] == '900000'
        assert home['pocket'] == '900000'

        row = purchase(c).json()
        # Записанная, но не принятая покупка уже вынута из кармана.
        assert row['accounting_balance'] == '900000'
        assert row['pending'] == '108000.00'
        assert row['pocket'] == '792000.00'


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


def test_without_an_advance_the_pocket_is_unknown_rather_than_zero(tmp_path):
    with client(tmp_path) as c:
        purchase(c)
        home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
        # Подотчёт не заведён — придумывать остаток нельзя.
        assert home['accounting_balance'] is None
        assert home['pocket'] is None
        assert home['pending'] == '108000.00'


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

        assert shokh_home['pocket'] == '772000.00'
        # Кабинет округляет до копеек той же money(), поэтому сравниваем числом.
        assert Decimal(cabinet['shokh']['pocket']) == Decimal(shokh_home['pocket'])
        # И обе цифры сходятся с подотчётом минус непринятое.
        assert (Decimal(shokh_home['accounting_balance']) - Decimal(shokh_home['pending'])
                == Decimal(shokh_home['pocket']))


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
