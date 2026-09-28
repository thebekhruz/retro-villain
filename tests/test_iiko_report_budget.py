"""Запросы к iiko — дефицитный ресурс, и тратить их надо считанно.

Замеры 28 сентября: iiko деградирует от одновременности, а не ускоряется
(шестнадцать параллельных отчётов растянули кассовый день с 2,4 с до 17 с),
частый опрос готовности удваивал число запросов и время, а сырые отчёты
директора за месяц весят около 46 MB и в кэш по весу не помещаются вовсе.
Отсюда три правила, которые проверяются здесь: окно отчёта выбирается по его
тяжести, закрытый период живёт в кэше дольше открытого, а потолок ожидания
считается по часам, а не по числу попыток."""

import asyncio
import json
from collections import Counter
from datetime import date, timedelta

import httpx
import pytest

from retro.config import Settings
from retro.integrations import iiko as iiko_module
from retro.integrations.iiko import (
    DIRECTOR_RANGE_MAX_DAYS, FOUNDER_OLAP_MAX_DAYS, IIKO_POLL_BUDGET, IikoClient,
    OLAP_TTL_CLOSED, OLAP_TTL_OPEN, olap_ttl,
)
from retro.modules.cashier.service import DataError

TODAY = date(2026, 9, 28)
END = TODAY - timedelta(days=1)


def collecting_transport(windows):
    """Отвечает пустым отчётом и записывает, каким окном его спросили."""

    async def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'synthetic'})
        body = json.loads(request.content)
        if request.url.path == '/api/olap/init':
            windows.append((len(body['groupFields']),
                            body['filters'][0]['dateFrom'], body['filters'][0]['dateTo']))
            return httpx.Response(200, json={'fetchId': 'synthetic'})
        return httpx.Response(200, json={'result': {'rows': []}})

    return httpx.MockTransport(handler)


def test_director_asks_payments_by_month_and_costs_by_ten_days():
    """Пять измерений iiko считает месяцем; резать их вместе с тяжёлыми незачем."""
    windows = []
    source = IikoClient(Settings(login='test', password='test', store_id=1),
                        transport=collecting_transport(windows), poll_delay=0)
    start = END - timedelta(days=29)
    asyncio.run(source.load_director_report(TODAY, start=start, end=END))
    asyncio.run(source.close())

    # Два отчёта на окно различаются только фильтром, поэтому считаем все.
    heavy = [window for window in windows if window[0] > 5]
    payments = [window for window in windows if window[0] == 5]
    # Тяжёлые отчёты остаются в десятидневном окне: на них iiko отдаёт 500.
    assert len(heavy) == 2 * len(range(0, 30, DIRECTOR_RANGE_MAX_DAYS))
    for _, first, last in heavy:
        span = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
        assert span <= DIRECTOR_RANGE_MAX_DAYS
    # Оплаты берутся месячным окном — два отчёта на весь период, а не шесть.
    assert len(payments) == 2
    for _, first, last in payments:
        assert date.fromisoformat(first) == start and date.fromisoformat(last) == END
        assert (END - start).days + 1 <= FOUNDER_OLAP_MAX_DAYS
    # Итого месяц директора стоит восьми отчётов вместо двенадцати.
    assert len(windows) == 8


def test_closed_period_reports_outlive_the_open_one():
    """Закрытый день iiko сам не пересчитывает — держать его минуту незачем."""
    assert olap_ttl(END, today=TODAY) == OLAP_TTL_CLOSED
    assert olap_ttl(TODAY, today=TODAY) == OLAP_TTL_OPEN
    assert olap_ttl(TODAY + timedelta(days=1), today=TODAY) == OLAP_TTL_OPEN
    assert OLAP_TTL_CLOSED > OLAP_TTL_OPEN


def test_slow_report_gets_the_whole_budget_instead_of_fifteen_tries():
    """В логах прода встречаются честные 23 с; пятнадцать попыток их обрывали."""
    clock, polls = [0.0], Counter()

    async def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'synthetic'})
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': 'synthetic'})
        polls['fetch'] += 1
        # Отчёт готов на двадцатой секунде по часам стенда.
        if clock[0] >= 20:
            return httpx.Response(200, json={'result': {'rows': []}})
        return httpx.Response(400, text='data not found')

    async def scenario(monkeypatch_sleep):
        source = IikoClient(Settings(login='test', password='test', store_id=1),
                            transport=httpx.MockTransport(handler), poll_delay=1)
        try:
            async with source._client() as client:
                return await source._olap(client, END, ['PayTypes'], ['DishDiscountSumInt'])
        finally:
            await source.close()

    async def fake_sleep(seconds):
        clock[0] += seconds

    async def run():
        original = asyncio.sleep
        asyncio.sleep = fake_sleep
        monotonic = iiko_module.monotonic
        iiko_module.monotonic = lambda: clock[0]
        try:
            return await scenario(fake_sleep)
        finally:
            asyncio.sleep = original
            iiko_module.monotonic = monotonic

    assert asyncio.run(run()) == []
    # Двадцать секунд ожидания укладываются в бюджет и не обрываются на 15-й.
    assert polls['fetch'] > 15 and clock[0] <= IIKO_POLL_BUDGET


def test_report_that_never_arrives_still_stops_inside_the_budget():
    clock = [0.0]

    async def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'synthetic'})
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': 'synthetic'})
        return httpx.Response(400, text='data not found')

    async def run():
        original = asyncio.sleep

        async def fake_sleep(seconds):
            clock[0] += seconds

        asyncio.sleep = fake_sleep
        monotonic = iiko_module.monotonic
        iiko_module.monotonic = lambda: clock[0]
        source = IikoClient(Settings(login='test', password='test', store_id=1),
                            transport=httpx.MockTransport(handler), poll_delay=1)
        try:
            async with source._client() as client:
                with pytest.raises(DataError, match='ещё не подготовил отчёт'):
                    await source._olap(client, END, ['PayTypes'], ['DishDiscountSumInt'])
        finally:
            asyncio.sleep = original
            iiko_module.monotonic = monotonic
            await source.close()

    asyncio.run(run())
    assert clock[0] <= IIKO_POLL_BUDGET
