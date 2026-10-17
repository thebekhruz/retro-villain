"""Уведомления в браузер о каждом заказе за Счёт Шефа (T-509, ТЗ 16.10, п. 4.2)."""
import asyncio
import json
import os
from datetime import date, datetime, timezone

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from legacy_app import create_app
from test_accountant_month_close import DATABASES, postgres_url
from retro.config import Settings, parse_push_config
from retro.integrations.iiko import chef_orders_from_olap
from retro.modules.cashier.service import DataError, TZ
from retro.modules.founder.chef_push import ChefOrderPush, ChefOrderStore, notification
from retro.web_push import (PushError, PushSender, PushStore, VapidKeys, b64u, encrypt, public_bytes,
                            push_host_allowed, unb64u)

NOW = datetime(2026, 10, 21, 14, 5, tzinfo=TZ)
APPLE = 'https://web.push.apple.com/QGp1bG9fdG9rZW4'
FCM = 'https://fcm.googleapis.com/fcm/send/abc:def'


class Device:
    """Браузер: ключ устройства и общий секрет, как их выдаёт PushManager."""

    def __init__(self, endpoint=FCM):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.secret = os.urandom(16)
        self.endpoint = endpoint

    @property
    def subscription(self):
        return dict(endpoint=self.endpoint, p256dh=b64u(public_bytes(self.key.public_key())),
                    auth=b64u(self.secret))

    def json(self):
        sub = self.subscription
        return {'endpoint': sub['endpoint'], 'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}}

    def read(self, body: bytes) -> dict:
        # Независимая реализация RFC 8188 — так тело расшифрует браузер.
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.secret,
                                           version='aes128gcm'))


def jwt_claims(authorization: str, public_key: str) -> dict:
    token = authorization.split('t=')[1].split(',')[0]
    head, body, signature = token.split('.')
    raw = unb64u(signature)
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(public_key))
    key.verify(encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')),
               f'{head}.{body}'.encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(unb64u(head)) == {'typ': 'JWT', 'alg': 'ES256'}
    return json.loads(unb64u(body))


class Service:
    """Служба доставки: принимает POST и отвечает заданным кодом."""

    def __init__(self, status=201, payload=None):
        self.requests, self.status, self.payload = [], status, payload

    def __call__(self, request):
        self.requests.append(request)
        status = self.status(request) if callable(self.status) else self.status
        return httpx.Response(status, json=self.payload) if self.payload else httpx.Response(status)


# ── Шифрование и подпись ─────────────────────────────────────────────────────

def test_payload_decrypts_in_the_browser_and_the_token_suits_apple():
    device, keys = Device(), VapidKeys.generate()
    body = encrypt('{"title":"Счёт Шефа · стол 7"}'.encode(), device.subscription['p256dh'],
                   device.subscription['auth'])
    assert device.read(body) == {'title': 'Счёт Шефа · стол 7'}
    assert body[16:20] == (4096).to_bytes(4, 'big') and body[20] == 65  # заголовок aes128gcm
    claims = jwt_claims(keys.authorization(APPLE, 'mailto:push@retro-milliy.uz', now=1_000_000), keys.public_key)
    # Apple: aud — источник службы, срок не дальше суток, sub — mailto:/https:.
    assert claims == {'aud': 'https://web.push.apple.com', 'exp': 1_000_000 + 12 * 3600,
                      'sub': 'mailto:push@retro-milliy.uz'}
    assert keys.authorization(APPLE, 'mailto:a@b.uz').endswith(', k=' + keys.public_key)
    assert len(unb64u(keys.public_key)) == 65
    assert VapidKeys.from_private(keys.private_text).public_key == keys.public_key
    with pytest.raises(ValueError):
        VapidKeys.from_private('short')


def test_only_browser_push_services_are_accepted():
    for endpoint in (FCM, APPLE, 'https://updates.push.services.mozilla.com/wpush/v2/x',
                     'https://wns2-par02p.notify.windows.com/w/?token=x', 'https://web.push.apple.com/x'):
        assert push_host_allowed(endpoint), endpoint
    for endpoint in ('http://fcm.googleapis.com/x', 'https://example.com/push', 'https://evilpush.apple.com.ru/x',
                     'https://user:pw@fcm.googleapis.com/x', 'https://127.0.0.1/x', 'not a url'):
        assert not push_host_allowed(endpoint), endpoint


def test_sender_posts_an_encrypted_message_with_vapid_headers():
    device, keys, service = Device(APPLE), VapidKeys.generate(), Service()

    async def scenario():
        sender = PushSender(keys, 'https://dashboard.retro-milliy.uz', transport=httpx.MockTransport(service))
        await sender.send(device.subscription, {'title': 'Счёт Шефа', 'body': 'Плов ×2'})
        await sender.close()
    asyncio.run(scenario())
    [request] = service.requests
    assert str(request.url) == APPLE
    assert request.headers['content-encoding'] == 'aes128gcm' and request.headers['urgency'] == 'high'
    assert int(request.headers['ttl']) > 0
    assert jwt_claims(request.headers['authorization'], keys.public_key)['sub'] == 'https://dashboard.retro-milliy.uz'
    assert device.read(request.content) == {'title': 'Счёт Шефа', 'body': 'Плов ×2'}


@pytest.mark.parametrize('status, payload, code, gone', [
    (410, None, 'http_410', True), (404, None, 'http_404', True),
    (403, {'reason': 'BadJwtToken'}, 'http_403:BadJwtToken', False), (500, None, 'http_500', False)])
def test_sender_reports_refusals(status, payload, code, gone):
    async def scenario():
        sender = PushSender(VapidKeys.generate(), 'mailto:a@b.uz',
                            transport=httpx.MockTransport(Service(status, payload)))
        with pytest.raises(PushError) as error:
            await sender.send(Device().subscription, {'title': 'x'})
        await sender.close()
        return error.value
    error = asyncio.run(scenario())
    assert error.code == code and error.gone is gone


def test_sender_refuses_foreign_endpoints_and_broken_keys_without_a_request():
    service = Service()

    async def scenario():
        sender = PushSender(VapidKeys.generate(), 'mailto:a@b.uz', transport=httpx.MockTransport(service))
        with pytest.raises(PushError) as foreign:
            await sender.send({**Device().subscription, 'endpoint': 'https://example.com/x'}, {})
        with pytest.raises(PushError) as broken:
            await sender.send({**Device().subscription, 'p256dh': b64u(b'\x04' * 10)}, {})
        await sender.close()
        return foreign.value, broken.value
    foreign, broken = asyncio.run(scenario())
    assert foreign.gone and broken.gone and service.requests == []


def test_push_config_subject_follows_apple_rules():
    assert parse_push_config({}).subject == 'mailto:push@retro-milliy.uz'
    assert parse_push_config({}, public_url='https://dashboard.retro-milliy.uz').subject == \
        'https://dashboard.retro-milliy.uz'
    assert parse_push_config({}, public_url='http://192.168.1.5:8010').subject == 'mailto:push@retro-milliy.uz'
    assert parse_push_config({'VAPID_SUBJECT': 'mailto:boss@retro.uz'}).subject == 'mailto:boss@retro.uz'
    for bad in ({'VAPID_SUBJECT': 'boss@retro.uz'}, {'VAPID_SUBJECT': 'http://x.uz'},
                {'VAPID_PRIVATE_KEY': 'abc'}):
        with pytest.raises(ValueError):
            parse_push_config(bad)
    key = VapidKeys.generate().private_text
    assert parse_push_config({'VAPID_PRIVATE_KEY': key}).private_key == key


# ── Подписки и ключ в базе ───────────────────────────────────────────────────

def stores(tmp_path, database):
    return tmp_path / 'founder.sqlite3' if database == 'sqlite' else None


@pytest.mark.parametrize('database', DATABASES)
def test_one_server_key_for_every_process_and_subscriptions_survive(tmp_path, database):
    def check(target):
        first, second = PushStore(target), PushStore(target)  # два процесса на выкате
        assert first.vapid(NOW).public_key == second.vapid(NOW).public_key
        device = Device()
        first.save(device.subscription, owner='bekhruz', role='founder', user_agent='Safari', now=NOW)
        first.save(device.subscription, owner='bekhruz', role='founder', user_agent='Safari 2', now=NOW)
        [saved] = second.all()
        assert saved['owner'] == 'bekhruz' and saved['user_agent'] == 'Safari 2'
        second.mark(device.endpoint, NOW, 'http_500')
        assert first.get(device.endpoint)['last_error'] == 'http_500'
        assert first.remove(device.endpoint) and second.all() == []
    if database == 'postgres':
        with postgres_url() as url:
            check(url)
    else:
        check(tmp_path / 'founder.sqlite3')


# ── API кабинета ─────────────────────────────────────────────────────────────

USERS = {'boss': ('secret', 'founder'), 'kassa': ('secret', 'cashier')}


def client(tmp_path, transport):
    app = create_app(Settings(dashboard_panel_users=USERS, data_dir=tmp_path),
                     expense_db_path=tmp_path / 'cashier.sqlite3', accountant_db_path=tmp_path / 'accountant.sqlite3',
                     founder_db_path=tmp_path / 'founder.sqlite3', push_transport=transport)
    return TestClient(app, base_url='http://dashboard.example.com', client=('203.0.113.10', 50000))


def test_founder_subscribes_a_device_gets_a_test_and_unsubscribes(tmp_path):
    device, service = Device(), Service()
    with client(tmp_path, httpx.MockTransport(service)) as c:
        auth = ('boss', 'secret')
        key = c.get('/api/founder/push', auth=auth).json()['key']
        assert len(unb64u(key)) == 65
        assert c.post('/api/founder/push/subscribe', auth=auth, json=device.json()).json() == {'subscribed': True}
        [saved] = c.app.state.push_store.all()
        assert saved['owner'] == 'boss' and saved['role'] == 'founder'
        assert c.post('/api/founder/push/test', auth=auth, json={'endpoint': device.endpoint}).json() == {'sent': True}
        message = device.read(service.requests[0].content)
        assert message['title'] == 'Retro Milliy' and message['url'] == '/founder'
        assert jwt_claims(service.requests[0].headers['authorization'], key)['aud'] == 'https://fcm.googleapis.com'
        assert c.post('/api/founder/push/unsubscribe', auth=auth, json={'endpoint': device.endpoint}).json() == \
            {'subscribed': False}
        missing = c.post('/api/founder/push/test', auth=auth, json={'endpoint': device.endpoint})
        assert missing.status_code == 404


def test_subscription_is_checked_and_closed_to_other_roles(tmp_path):
    device = Device()
    with client(tmp_path, httpx.MockTransport(Service())) as c:
        auth = ('boss', 'secret')
        foreign = c.post('/api/founder/push/subscribe', auth=auth,
                         json={**device.json(), 'endpoint': 'https://example.com/hook/123456'})
        assert foreign.status_code == 422 and 'неизвестный адрес' in foreign.json()['detail']
        broken = device.json()
        broken['keys']['p256dh'] = 'A' * 87
        assert c.post('/api/founder/push/subscribe', auth=auth, json=broken).status_code == 422
        assert c.get('/api/founder/push', auth=('kassa', 'secret')).status_code == 403
        assert c.post('/api/founder/push/subscribe', auth=('kassa', 'secret'), json=device.json()).status_code == 403
        assert c.app.state.push_store.all() == []


def test_expired_subscription_is_dropped_on_test(tmp_path):
    device = Device()
    with client(tmp_path, httpx.MockTransport(Service(410))) as c:
        auth = ('boss', 'secret')
        c.post('/api/founder/push/subscribe', auth=auth, json=device.json())
        gone = c.post('/api/founder/push/test', auth=auth, json={'endpoint': device.endpoint})
        assert gone.status_code == 410 and 'заново' in gone.json()['detail']
        assert c.app.state.push_store.all() == []


def test_service_worker_is_public_fresh_and_always_shows_the_notification(tmp_path):
    with client(tmp_path, httpx.MockTransport(Service())) as c:
        answer = c.get('/sw.js')  # без входа: браузер обновляет обработчик в фоне
        assert answer.status_code == 200
        assert answer.headers['content-type'].startswith('text/javascript')
        assert answer.headers['cache-control'] == 'no-store'  # браузер всегда берёт свежий
        assert answer.headers['service-worker-allowed'] == '/'
        script = answer.text
    push = script[script.index("addEventListener('push'"):script.index("addEventListener('notificationclick'")]
    # Safari отзывает подписку, если push не показал уведомление.
    assert 'event.waitUntil(self.registration.showNotification(' in push
    assert "addEventListener('fetch'" not in script  # страницы не перехватываем


# ── Заказы iiko и уведомления ────────────────────────────────────────────────

def olap(day, order, table, waiter, dish, amount, quantity):
    return {'field0': {'value': day}, 'children': [{'field1': {'value': order}, 'children': [
        {'field2': {'value': table}, 'children': [{'field3': {'value': waiter}, 'children': [
            {'field4': {'value': dish}, 'field5': {'value': amount}, 'field6': {'value': quantity}}]}]}]}]}


def test_chef_orders_carry_dishes_and_quantities():
    rows = [olap('2026-10-21', 'o-1', 7, 'Алина', 'Плов', 120000, 2.0),
            olap('2026-10-21', 'o-1', 7, 'Музаффар', 'Чай', 15000, 1.0),
            olap('2026-10-21', 'o-2', None, None, 'Самса', 30000, 3.0)]
    orders = chef_orders_from_olap(rows)
    first = next(order for order in orders if order['order_id'] == 'o-1')
    assert first['amount'] == '135000' and first['waiters'] == ['Алина', 'Музаффар'] and first['table'] == 7
    assert [dish['name'] for dish in first['dishes']] == ['Плов', 'Чай']
    with pytest.raises(DataError):
        chef_orders_from_olap([olap('вчера', 'o-3', 1, 'А', 'Плов', 1, 1)])


def order(order_id, amount='135000', *, day='2026-10-21', table=7, dishes=None, waiters=('Алина',)):
    return dict(day=day, order_id=order_id, table=table, waiters=list(waiters), amount=amount,
                dishes=dishes if dishes is not None else [
                    dict(name='Плов', quantity='2.000', amount='120000'),
                    dict(name='Чай', quantity='1', amount='15000')])


def test_notification_text():
    message = notification(order('o-1'))
    assert message == {'title': 'Счёт Шефа · стол 7', 'body': 'Плов ×2, Чай ×1\n135 000 сум · Алина',
                       'url': '/founder', 'tag': 'chef-o-1'}
    many = [dict(name=f'Блюдо {n}', quantity='1.5', amount='1') for n in range(6)]
    long = notification(order('o-2', table=None, dishes=many, waiters=()))
    assert long['title'] == 'Счёт Шефа'
    assert long['body'] == 'Блюдо 0 ×1.5, Блюдо 1 ×1.5, Блюдо 2 ×1.5, Блюдо 3 ×1.5, ещё 2\n135 000 сум'


class Orders:
    def __init__(self, *rows):
        self.rows, self.calls, self.fail = list(rows), [], False

    async def load(self, start, end):
        self.calls.append((start, end))
        if self.fail:
            raise DataError('iiko недоступен')
        return [row for row in self.rows if start.isoformat() <= row['day'] <= end.isoformat()]


@pytest.mark.parametrize('database', DATABASES)
def test_every_new_chef_order_reaches_every_device_once(tmp_path, database):
    if database == 'postgres':
        with postgres_url() as url:
            each_order_once(url)
    else:
        each_order_once(tmp_path / 'founder.sqlite3')


def each_order_once(database):
    phone, laptop, gone = Device(APPLE), Device(FCM), Device('https://fcm.googleapis.com/fcm/send/old')
    service = Service(lambda request: 410 if str(request.url).endswith('/old') else 201)

    async def scenario():
        subscriptions = PushStore(database)
        sender = PushSender(subscriptions.vapid(NOW), 'mailto:a@b.uz', transport=httpx.MockTransport(service))
        rows = Orders(order('o-1', day='2026-10-20'), order('o-2'))
        watcher = ChefOrderPush(ChefOrderStore(database), subscriptions, sender=sender, loader=rows.load,
                                clock=lambda: NOW)
        # Первый запуск после выката: уже закрытые заказы запоминаем, не рассылая.
        assert [row['status'] for row in await watcher.run_once()] == ['baseline', 'baseline']
        assert rows.calls == [(date(2026, 10, 20), date(2026, 10, 21))] and service.requests == []
        # Устройств ещё нет — заказ записан, позже включённые уведомления его не шлют.
        rows.rows.append(order('o-3'))
        assert await watcher.run_once() == [dict(order_id='o-3', status='no_recipient')]
        for device in (phone, laptop, gone):
            subscriptions.save(device.subscription, owner='boss', role='founder', user_agent='', now=NOW)
        assert await watcher.run_once() == []
        rows.rows.append(order('o-4', '50000', dishes=[dict(name='Шашлык', quantity='1', amount='50000')]))
        assert await watcher.run_once() == [dict(order_id='o-4', status='sent', devices=2)]
        assert len(service.requests) == 3
        delivered = {str(request.url): request for request in service.requests}
        assert phone.read(delivered[APPLE].content)['body'] == 'Шашлык ×1\n50 000 сум · Алина'
        assert laptop.read(delivered[FCM].content)['title'] == 'Счёт Шефа · стол 7'
        assert [row['endpoint'] for row in subscriptions.all()] == [phone.endpoint, laptop.endpoint]  # 410 — удалена
        # Второй процесс (выкат) с той же базой заказ не повторяет.
        again = ChefOrderPush(ChefOrderStore(database), subscriptions, sender=sender, loader=rows.load,
                              clock=lambda: NOW)
        assert await again.run_once() == [] and await watcher.run_once() == []
        rows.fail = True
        assert await watcher.run_once() == []  # iiko недоступен — тихо ждём следующей проверки
        assert len(service.requests) == 3
        journal = {row['order_id']: row for row in watcher.store.rows()}
        assert journal['o-4']['status'] == 'sent' and journal['o-4']['devices'] == 2
        assert journal['o-1']['status'] == 'baseline'
        await sender.close()
    asyncio.run(scenario())


def test_chef_push_runs_only_with_iiko(tmp_path):
    with client(tmp_path, httpx.MockTransport(Service())) as c:
        assert c.app.state.chef_push is None  # iiko не настроен — заказов нет
        assert c.app.state.push_sender.keys.public_key == c.app.state.push_store.vapid(NOW).public_key
