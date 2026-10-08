"""Маршрут до iiko теряет пакеты — один сбой не должен гасить экран.

Прод 27–28 сентября: 17 транспортных отказов (9 ConnectTimeout, 8 ReadTimeout)
на 40 успешных отчётов. Кассир тянет пять запросов разом через gather_reads,
поэтому единственная потеря обнуляла всю страницу."""

import asyncio
import json
from datetime import date

import httpx
import pytest

from retro.config import IIKO_ORIGIN, Settings
from retro.integrations import iiko as iiko_module
from retro.integrations.iiko import (
    IIKO_CONNECT_ATTEMPTS, IIKO_READ_ATTEMPTS, IIKO_RETRY_PAUSE, IikoClient,
)

# Снято до автопатча пауз — бюджет считаем по боевому значению, не по нулю.
REAL_RETRY_PAUSE = IIKO_RETRY_PAUSE
from retro.modules.cashier.service import DataError


DAY = date(2026, 9, 15)


@pytest.fixture(autouse=True)
def instant_retries(monkeypatch):
    """Пауза между попытками проверяется арифметикой бюджета, не ожиданием."""
    monkeypatch.setattr(iiko_module, 'IIKO_RETRY_PAUSE', 0)


def row(**fields):
    return {f'field{index}': {'value': value} for index, value in fields.items()}


def cashier_handler(failures):
    """Отвечает как живой iiko, но выдаёт заготовленные сбои по пути запроса."""
    calls = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        queued = failures.get(path)
        if queued:
            raise queued.pop(0)
        if path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        if path == '/api/cash/shift/list_period':
            return httpx.Response(200, json={'shifts': [{'id': 'shift-1',
                'openDate': '2026-09-15T11:00:00', 'cashRegNumber': 1,
                'payOrders': 31824000, 'salesCash': 20000000,
                'salesCard': 11824000, 'salesCredit': 0}]})
        body = json.loads(request.content)
        groups = body['groupFields']
        if path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': groups[0]})
        if groups == ['CashRegisterName', 'RestaurantSection']:
            rows = [
                {**row(**{'0': 'GL-Kassa-Oksbrich', '2': 3970000}),
                 'children': [row(**{'1': 'Зал', '2': 3970000})]},
                {**row(**{'0': 'Kassa-FiscalBox1', '2': 54314000}),
                 'children': [row(**{'1': 'Ресторан', '2': 32124000}),
                              row(**{'1': 'Бехруз (Свадьба)', '2': 22190000})]},
            ]
        elif groups == ['OpenDate.Typed']:
            rows = [row(**{'0': DAY.isoformat(), '1': 75, '2': 31824000})]
        elif groups == ['PayTypes']:
            rows = [row(**{'0': 'Демо', '1': 20000000}),
                    row(**{'0': 'UzCard', '1': 11824000})]
        elif groups == ['PayTypes.Group', 'PayTypes']:
            rows = [{**row(**{'0': 'Оплата наличными', '2': 20000000}),
                     'children': [row(**{'1': 'Демо', '2': 20000000})]},
                    {**row(**{'0': 'Банковские карты', '2': 11824000}),
                     'children': [row(**{'1': 'UzCard', '2': 11824000})]}]
        elif groups == ['OrderNum', 'PayTypes.Group', 'PayTypes']:
            rows = []
        else:
            raise AssertionError(groups)
        return httpx.Response(200, json={'result': {'rows': rows}})

    return handler, calls


def client_for(handler):
    return IikoClient(Settings(login='test', password='test', store_id=123),
                      transport=httpx.MockTransport(handler), poll_delay=0)


def send(source, handler, path='/api/olap/init', method='_send'):
    """Гоняем одиночный запрос мимо авторизации — политика повторов как есть."""
    async def run():
        async with httpx.AsyncClient(base_url=IIKO_ORIGIN,
                                     transport=httpx.MockTransport(handler)) as client:
            return await getattr(source, method)(client, path, {})
    return asyncio.run(run())


def test_lost_connect_does_not_reach_the_screen():
    handler, calls = cashier_handler(
        {'/api/olap/init': [httpx.ConnectTimeout('SYN потерян')]})
    result = asyncio.run(client_for(handler).load(DAY))

    assert result.revenue == 31824000
    assert result.receipt_count == 75
    # Потерянный запрос повторён, а не отменил соседей по gather_reads: восемь
    # отчётов (четыре дня и четыре для касс iiko и зачёта предоплат) + один повтор.
    assert calls.count('/api/olap/init') == 9
    assert result.full_total == 31824000 and result.payment_groups


def test_dead_pooled_connection_is_retried():
    handler, calls = cashier_handler(
        {'/api/olap/init': [httpx.RemoteProtocolError('сервер закрыл keep-alive')]})
    result = asyncio.run(client_for(handler).load(DAY))

    assert result.revenue == 31824000
    assert calls.count('/api/olap/init') == 9


def test_exhausted_connect_budget_surfaces_to_the_user():
    failures = {'/api/olap/init': [httpx.ConnectTimeout('маршрут мёртв')] * 50}
    handler, calls = cashier_handler(failures)

    with pytest.raises(DataError) as failure:
        asyncio.run(client_for(handler).load(DAY))

    assert 'Не удалось связаться с iiko' in str(failure.value)
    assert calls.count('/api/olap/init') >= IIKO_CONNECT_ATTEMPTS


def test_connect_budget_is_bounded():
    handler, calls = cashier_handler(
        {'/api/olap/init': [httpx.ConnectTimeout('маршрут мёртв')] * 50})

    with pytest.raises(httpx.ConnectTimeout):
        send(client_for(handler), handler)

    assert calls == ['/api/olap/init'] * IIKO_CONNECT_ATTEMPTS


def test_read_timeout_costs_more_and_is_retried_once():
    """Отчёт уже считался: второй прогон грузит iiko, третьего не даём."""
    handler, calls = cashier_handler(
        {'/api/olap/init': [httpx.ReadTimeout('iiko молчит')] * 50})

    with pytest.raises(httpx.ReadTimeout):
        send(client_for(handler), handler)

    assert calls == ['/api/olap/init'] * IIKO_READ_ATTEMPTS
    assert IIKO_READ_ATTEMPTS < IIKO_CONNECT_ATTEMPTS


def test_iiko_refusal_is_not_retried():
    """Отказ уровня логики iiko повтором не лечится — незачем его множить."""
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(500, json={'error': True})

    calls = []
    with pytest.raises(DataError) as failure:
        send(client_for(handler), handler, method='_post')

    assert 'HTTP 500' in str(failure.value)
    assert calls == ['/api/olap/init']


def test_retry_budget_fits_the_report_cache_deadline():
    """Худшая серия повторов обязана уложиться в 90 с ReportCache.

    Иначе вместо внятного 503 пользователь получает 504 по таймауту кэша."""
    timeout = iiko_module.IIKO_TIMEOUT
    worst = ((IIKO_CONNECT_ATTEMPTS - 1) * timeout.connect
             + (IIKO_READ_ATTEMPTS - 1) * timeout.read
             + max(timeout.connect, timeout.read)
             + (IIKO_CONNECT_ATTEMPTS + IIKO_READ_ATTEMPTS - 1) * REAL_RETRY_PAUSE)
    assert worst < 90
    # Коннект должен отваливаться заметно раньше чтения, иначе повтор бессмыслен.
    assert timeout.connect < timeout.read
