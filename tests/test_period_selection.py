"""Выбор периода: проверка границ, разбор дат из iiko и своды за диапазон."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.iiko import director_rows_from_range
from retro.modules.cashier.service import DataError
from retro.modules.director.models import (
    MAX_PERIOD_DAYS, SalesRow, build_snapshot, completed_period, resolve_period,
)

TODAY = date(2026, 9, 24)


def sale(day, *, payment='UzCard', item='Плов', revenue='100000', register='Kassa-FiscalBox1'):
    return SalesRow(day, register, 'Ресторан', payment, item, 'Основное меню',
                    Decimal('1'), Decimal(revenue), Decimal('40000'), 'Олег', f'o-{day}-{item}')


def test_default_period_is_ten_closed_days_and_length_is_configurable():
    assert completed_period(TODAY) == (date(2026, 9, 14), date(2026, 9, 23))
    assert completed_period(TODAY, 3) == (date(2026, 9, 21), date(2026, 9, 23))
    assert resolve_period(TODAY, days=7) == (date(2026, 9, 17), date(2026, 9, 23))


def test_explicit_range_is_accepted_and_open_or_oversized_ranges_are_refused():
    assert resolve_period(TODAY, date(2026, 9, 1), date(2026, 9, 5)) == (date(2026, 9, 1), date(2026, 9, 5))
    with pytest.raises(DataError, match='позже'):
        resolve_period(TODAY, date(2026, 9, 5), date(2026, 9, 1))
    with pytest.raises(DataError, match='не закрыт'):
        resolve_period(TODAY, date(2026, 9, 1), TODAY)
    with pytest.raises(DataError, match='начало'):
        resolve_period(TODAY, start=date(2026, 9, 1))
    with pytest.raises(DataError, match=str(MAX_PERIOD_DAYS)):
        resolve_period(TODAY, date(2026, 1, 1), date(2026, 9, 1))


def test_snapshot_reports_payment_and_daily_totals_for_any_period_length():
    rows = [sale(date(2026, 9, 21)), sale(date(2026, 9, 21), payment='Демо', item='Чай', revenue='50000'),
            sale(date(2026, 9, 23), payment='Яндех Еда', revenue='30000')]
    snapshot = build_snapshot(rows, {}, date(2026, 9, 21), date(2026, 9, 23))

    assert snapshot.cash_total == Decimal('180000')
    # Псевдоним «Яндех Еда» приводится к одному имени, иначе в таблице
    # появляются две строки об одной и той же оплате.
    assert snapshot.payment_totals == {'UzCard': Decimal('100000'), 'Демо': Decimal('50000'),
                                       'Яндекс Еда': Decimal('30000')}
    assert [row.day for row in snapshot.daily_totals] == [date(2026, 9, 21), date(2026, 9, 23)]
    assert snapshot.daily_totals[0].revenue == Decimal('150000')
    assert snapshot.daily_totals[0].payments['Демо'] == Decimal('50000')
    # День без продаж не роняет отчёт, но остаётся видимым в ответе.
    assert snapshot.missing_days == (date(2026, 9, 22),)


def test_sales_outside_the_requested_period_are_refused():
    with pytest.raises(DataError, match='вне запрошенного периода'):
        build_snapshot([sale(date(2026, 9, 30))], {}, date(2026, 9, 21), date(2026, 9, 23))
    # Пустой период — валидный нулевой отчёт (data-integrity main: смена без
    # продаж и полностью зачтённые возвраты не должны ронять аналитику).
    empty = build_snapshot([], {}, date(2026, 9, 21), date(2026, 9, 23))
    assert empty.cash_total == Decimal('0')
    assert empty.missing_days == (date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23))


def test_dated_olap_rows_carry_the_day_of_the_sale():
    row = {'field0': {'value': '2026-09-21'}, 'field1': {'value': 'Kassa-FiscalBox1'},
           'field2': {'value': 'Ресторан'}, 'field3': {'value': 'UzCard'},
           'field4': {'value': 'Плов'}, 'field5': {'value': 'Миллий'},
           'field6': {'value': 'Олег'}, 'field7': {'value': 'order-1'},
           'field8': {'value': 2}, 'field9': {'value': 200000}, 'field10': {'value': 40000}}
    parsed = director_rows_from_range([row])

    assert parsed[0].day == date(2026, 9, 21)
    assert parsed[0].revenue == Decimal('200000')
    # ProductCostBase.ProductCost — уже суммарная себестоимость строки, а не
    # цена за единицу: во всём директорском модуле она берётся как есть.
    assert parsed[0].cost == Decimal('40000')
    with pytest.raises(DataError, match='дату продажи'):
        director_rows_from_range([{**row, 'field0': {'value': 'вчера'}}])


def test_period_endpoints_refuse_a_broken_range_before_calling_iiko(tmp_path):
    app = create_app(Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3')
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        future = (date.today() + timedelta(days=1)).isoformat()
        assert client.get('/api/director/report',
                          params={'start': future, 'end': future}).status_code == 422
        assert client.get('/api/director/report',
                          params={'start': '2026-09-05', 'end': '2026-09-01'}).status_code == 422
