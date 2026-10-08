"""ТЗ «Выручка, оплаты и предоплаты» (08.10.2026): кассы iiko, зачёт предоплат, реестр."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from retro.modules.cashier.pay_groups import (build_payment_groups, flat_payment_rows, full_totals, pay_leaves,
                                              redeemed_orders)
from retro.modules.cashier.prepayment_registry import PrepaymentRegistry
from retro.modules.cashier.routes import group_advances
from retro.modules.cashier.service import DataError, Payment, Prepayment, Snapshot, TZ

DAY = date(2026, 10, 7)


def node(value, amount, children=None, level=0, sum_index=2):
    row = {f'field{level}': {'value': value}, f'field{sum_index}': {'value': amount}}
    if children is not None:
        row['children'] = children
    return row


# Ответ iiko за 07.10 (форма как в живом отчёте «группа → тип»).
PAID = [node('Оплата наличными', 27869000, [
            node('Демо', 24464000, level=1), node('Наличные (Инкасса QR)', 3405000, level=1)]),
        node('Банковские карты', 29134000, [
            node('Click/Payme Безналичный перевод', 926000, level=1), node('UzCard', 15429500, level=1),
            node('Xumo', 4801500, level=1), node('Я Rahmat', 6447000, level=1),
            node('Яндех Еда', 1530000, level=1)])]
# Зачтённые при закрытии заказов предоплаты: заказ → группа → тип.
REDEEMED = [
    {'field0': {'value': 70551}, 'field3': {'value': 4000000}, 'children': [
        {'field1': {'value': 'Банковские карты'}, 'field3': {'value': 4000000}, 'children': [
            {'field2': {'value': 'Click/Payme Безналичный перевод'}, 'field3': {'value': 3949000}},
            {'field2': {'value': 'Xumo'}, 'field3': {'value': 51000}}]}]},
    {'field0': {'value': 70657}, 'field3': {'value': 1000000}, 'children': [
        {'field1': {'value': 'Оплата наличными'}, 'field3': {'value': 1000000}, 'children': [
            {'field2': {'value': 'Демо'}, 'field3': {'value': 1000000}}]}]}]
DICTIONARY = [node('Банковские карты', 0, [node('Uzum', 0, level=1), node('Единый QR', 0, level=1)])]


def test_groups_come_from_iiko_with_cash_first_and_zero_types_from_the_dictionary():
    groups = build_payment_groups(pay_leaves(PAID, 2), pay_leaves(REDEEMED, 3), pay_leaves(DICTIONARY, 2))
    assert [group.name for group in groups] == ['Оплата наличными', 'Банковские карты']
    cash, cards = groups
    assert [(t.name, t.paid, t.redeemed) for t in cash.types] == [
        ('Демо', 24464000, 1000000), ('Наличные (Инкасса QR)', 3405000, 0)]
    names = [t.name for t in cards.types]
    assert names == sorted(names, key=str.casefold)
    assert 'Яндекс Еда' in names and 'Яндех Еда' not in names      # справочное написание
    assert {'Uzum', 'Единый QR'} <= set(names)                       # нулевые — из справочника
    assert cash.total + cards.total == Decimal(57003000 + 5000000)


def test_new_type_and_new_group_appear_by_themselves():
    paid = pay_leaves(PAID + [node('Агрегаторы', 500000, [node('Uzum Tezkor', 500000, level=1)])], 2)
    groups = build_payment_groups(paid, [])
    assert [group.name for group in groups] == ['Оплата наличными', 'Агрегаторы', 'Банковские карты']
    assert groups[1].types[0].name == 'Uzum Tezkor'


def test_flat_rows_match_the_classic_payments_report():
    rows = flat_payment_rows(pay_leaves(PAID, 2))
    assert {row['field0']['value']: row['field1']['value'] for row in rows}['Яндекс Еда'] == 1530000
    assert sum(row['field1']['value'] for row in rows) == 57003000


def test_redeemed_orders_keep_amount_and_methods_per_order():
    orders = redeemed_orders(pay_leaves(REDEEMED, 3))
    assert [(o.order_number, o.amount, o.methods) for o in orders] == [
        ('70551', 4000000, ('Click/Payme Безналичный перевод', 'Xumo')), ('70657', 1000000, ('Демо',))]


def test_money_without_type_or_group_is_rejected():
    with pytest.raises(DataError):
        build_payment_groups(pay_leaves([node('(без оплаты)', 10, [node('(без оплаты)', 10, level=1)])], 2), [])
    with pytest.raises(DataError):
        pay_leaves([{'field0': {'value': 'Оплата наличными'}, 'field2': {'value': 5}, 'children': []}], 2)


def test_full_totals_read_one_day():
    assert full_totals(DAY, [{'field0': {'value': '2026-10-07'}, 'field1': {'value': 101},
                              'field2': {'value': 63503000}}]) == (101, 63503000)
    with pytest.raises(DataError):
        full_totals(DAY, [{'field0': {'value': '2026-10-06'}, 'field1': {'value': 1}, 'field2': {'value': 1}}])


def test_real_cash_is_full_bills_minus_redeemed_prepayments():
    """Пример ТЗ 3.3: 06.10 аванс, 09.10 счёт 70 → зачтено −50 → реальная касса 20."""
    groups = build_payment_groups(
        [('Оплата наличными', 'Демо', Decimal(20_000_000))],
        [('1', 'Оплата наличными', 'Демо', Decimal(50_000_000))])
    snapshot = Snapshot('s', date(2026, 10, 9), Decimal(20_000_000), 5,
                        (Payment('Демо', Decimal(20_000_000)),), datetime.now(TZ),
                        payment_groups=groups, redeemed_orders=redeemed_orders(
                            [('1', 'Оплата наличными', 'Демо', Decimal(50_000_000))]),
                        full_total=Decimal(70_000_000), full_receipt_count=5)
    data = snapshot.json()
    assert data['full_total'] == '70000000'
    assert data['redeemed_total'] == '50000000'
    assert data['real_cash'] == '20000000'
    assert data['groups_match'] is True
    # Передача кассира по-прежнему считает только оплаты «Демо» за день: аванс не вдвое.
    assert data['revenue'] == '20000000'


def test_registry_keeps_iiko_facts_and_cashier_notes_apart(tmp_path):
    registry = PrepaymentRegistry(tmp_path / 'cashier.sqlite3')
    registry.record_received([('58983', date(2026, 10, 6), '2026-10-06T17:19:45+05:00', Decimal(54_000_000))])
    note = registry.annotate('58983', guest='Иванов', phone='+998 90 000 00 00', event_day=date(2026, 10, 9),
                             method='Наличные', by='cashier')
    assert note['status'] == 'pending' and note['event_day'] == '2026-10-09'
    # Повторное чтение iiko не стирает то, что ввёл кассир.
    registry.record_received([('58983', date(2026, 10, 6), '2026-10-06T17:19:45+05:00', Decimal(54_000_000))])
    registry.record_credited(date(2026, 10, 9), [('58983', Decimal(54_000_000), ('Демо',))])
    row = registry.rows(['58983'])[0]
    assert (row['guest'], row['status'], row['credited_day'], row['amount']) == (
        'Иванов', 'credited', '2026-10-09', '54000000')
    assert registry.rows_between(date(2026, 10, 9), date(2026, 10, 9))[0]['order_number'] == '58983'
    refund = registry.annotate('58983', guest='Иванов', refunded=True)
    assert refund['status'] == 'refund'
    with pytest.raises(DataError):
        registry.annotate('58983', guest='x' * 121)


def test_registry_shows_iiko_method_until_the_cashier_writes_one(tmp_path):
    registry = PrepaymentRegistry(tmp_path / 'cashier.sqlite3')
    registry.record_credited(DAY, [('70551', Decimal(4_000_000), ('Click/Payme Безналичный перевод', 'Xumo'))])
    assert registry.rows(['70551'])[0]['method_shown'] == 'Click/Payme Безналичный перевод, Xumo'
    assert registry.missing_received(['70551']) == ['70551']


def test_advances_of_one_order_are_one_prepayment():
    entries = [Prepayment('a', '2026-10-06T17:37:28+05:00', Decimal(278000), order_number='70551'),
               Prepayment('b', '2026-10-06T17:38:58+05:00', Decimal(2671000), order_number='70551'),
               Prepayment('c', '2026-10-06T11:57:28+05:00', Decimal(1000000), order_number='70700')]
    grouped = {order: (day, at, amount) for order, day, at, amount in group_advances(entries)}
    assert grouped['70551'] == (date(2026, 10, 6), '2026-10-06T17:37:28+05:00', Decimal(2949000))
    assert grouped['70700'][2] == Decimal(1000000)


def registry_client(tmp_path):
    from fastapi.testclient import TestClient
    from legacy_app import create_app
    from retro.config import Settings
    return TestClient(create_app(Settings(data_dir=tmp_path)), client=('127.0.0.1', 50000))


def test_registry_api_demo_edit_and_period_rules(tmp_path):
    with registry_client(tmp_path) as client:
        demo = client.get('/api/cashier/prepayments?date=2026-10-07&demo=true').json()
        assert [row['status'] for row in demo['credited']] == ['credited']
        assert demo['received_total'] == '3000000'
        period = client.get('/api/cashier/prepayments?start=2026-10-02&end=2026-10-20&demo=true').json()
        assert len(period['rows']) == 2
        saved = client.put('/api/cashier/prepayments/58983', json={
            'guest': 'Иванов', 'phone': '+998', 'event_day': '2026-10-09', 'method': 'Наличные'})
        assert saved.status_code == 200
        assert (saved.json()['guest'], saved.json()['status']) == ('Иванов', 'pending')
        assert client.put('/api/cashier/prepayments/58983', json={'guest': 'x' * 121}).status_code == 422
        # Период: обе границы, не задом наперёд, не длиннее двух месяцев, не из будущего.
        for query in ('start=2026-10-05', 'start=2026-10-09&end=2026-10-05', 'start=2026-10-02&end=2026-12-20',
                      'start=2099-01-01&end=2099-01-02'):
            assert client.get('/api/cashier/prepayments?' + query).status_code == 422, query
