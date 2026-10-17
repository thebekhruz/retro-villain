"""ТЗ 16.10, п. 4.2: Счёт Шефа за неделю дошёл до порога — сообщение учредителю."""
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from test_accountant_month_close import DATABASES, postgres_url
from retro.config import (CHEF_WEEK_LIMIT, CHEF_WEEK_REPEAT_STEP, ChefAlertConfig, Settings,
                          parse_chef_alert_config, parse_telegram_report_config)
from retro.integrations.telegram import TelegramBot, TelegramError
from retro.modules.cashier.service import DataError, TZ
from retro.modules.founder import overview
from retro.modules.founder.chef_alert import ChefAlertStore, ChefWeekAlert, message, status_view

TOKEN = '123456789:' + 'A' * 35
CHAT = '-1001234567890'
MONDAY = date(2026, 10, 19)
WEDNESDAY = date(2026, 10, 21)
SUNDAY = date(2026, 10, 25)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def bill(day, amount, *, order=None, table=5, waiters=('Алина',)):
    return dict(day=day.isoformat(), order_id=order or f'{day}-{amount}', table=table,
                waiters=list(waiters), amount=str(amount), cost='1')


class FakeBot:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    async def send_message(self, chat_id, text):
        if self.fail:
            self.fail -= 1
            raise TelegramError('network')
        self.sent.append((chat_id, text))
        return len(self.sent)

    async def close(self):
        pass


class Week:
    """Счета iiko, которые «приходят» к очередной проверке."""

    def __init__(self, *rows):
        self.rows, self.calls = list(rows), []

    async def load(self, start, end):
        self.calls.append((start, end))
        return [row for row in self.rows if start.isoformat() <= row['day'] <= end.isoformat()]


def alert(database, rows, now, *, bot=None, chat=CHAT, **config):
    settings = ChefAlertConfig(token=TOKEN if chat else '', chat_id=chat or '',
                               public_url='https://panel.example.uz', **config)
    return ChefWeekAlert(ChefAlertStore(database), settings, loader=rows.load,
                         bot=bot if chat else None, clock=lambda: now[0])


# ── Настройки ────────────────────────────────────────────────────────────────

def test_config_defaults_and_overrides():
    config = parse_chef_alert_config({})
    assert config.week_limit == CHEF_WEEK_LIMIT == Decimal(30_000_000)
    assert config.repeat_step == CHEF_WEEK_REPEAT_STEP == Decimal(10_000_000)
    assert not config.recipient and config.public_url == ''
    custom = parse_chef_alert_config({'CHEF_WEEK_LIMIT': '25 000 000', 'CHEF_WEEK_REPEAT_STEP': '0',
                                      'TELEGRAM_BOT_TOKEN': TOKEN, 'TELEGRAM_FOUNDER_CHAT_ID': '123456789',
                                      'RAILWAY_PUBLIC_DOMAIN': 'retro.up.railway.app'})
    assert custom.week_limit == Decimal(25_000_000) and custom.repeat_step == 0
    assert custom.recipient and custom.public_url == 'https://retro.up.railway.app'
    assert TOKEN not in repr(custom)
    own = parse_chef_alert_config({'DASHBOARD_PUBLIC_URL': 'https://dashboard.example.uz/',
                                   'RAILWAY_PUBLIC_DOMAIN': 'retro.up.railway.app'})
    assert own.public_url == 'https://dashboard.example.uz'


def test_config_refuses_half_set_or_broken_values():
    with pytest.raises(ValueError, match='TELEGRAM_BOT_TOKEN'):
        parse_chef_alert_config({'TELEGRAM_FOUNDER_CHAT_ID': '123456789'})
    with pytest.raises(ValueError, match='CHEF_WEEK_LIMIT'):
        parse_chef_alert_config({'CHEF_WEEK_LIMIT': '0'})
    with pytest.raises(ValueError, match='CHEF_WEEK_LIMIT'):
        parse_chef_alert_config({'CHEF_WEEK_LIMIT': '30 млн'})
    with pytest.raises(ValueError, match='TELEGRAM_FOUNDER_CHAT_ID'):
        parse_chef_alert_config({'TELEGRAM_BOT_TOKEN': TOKEN, 'TELEGRAM_FOUNDER_CHAT_ID': 'founder'})
    with pytest.raises(ValueError, match='DASHBOARD_PUBLIC_URL'):
        parse_chef_alert_config({'DASHBOARD_PUBLIC_URL': 'https://panel.example.uz/founder'})


def test_bot_for_the_founder_alone_needs_no_cashier_group():
    environ = {'TELEGRAM_BOT_TOKEN': TOKEN, 'TELEGRAM_FOUNDER_CHAT_ID': '123456789'}
    assert parse_telegram_report_config(environ) is None
    assert parse_chef_alert_config(environ).recipient
    # Без чата учредителя прежнее правило в силе: токен и группа задаются вместе.
    with pytest.raises(ValueError, match='вместе'):
        parse_telegram_report_config({'TELEGRAM_BOT_TOKEN': TOKEN})


# ── Сумма недели ─────────────────────────────────────────────────────────────

def test_week_sum_is_menu_amount_with_inclusive_limit_and_top_three():
    rows = [bill(MONDAY, 12_000_000, order='a', waiters=('Алина', 'Бобур')),
            bill(WEDNESDAY, 9_000_000, order='b', table=None, waiters=()),
            bill(WEDNESDAY, 4_000_000, order='c'), bill(SUNDAY, 5_000_000, order='d'),
            bill(MONDAY - timedelta(days=1), 99_000_000, order='old')]  # прошлая неделя
    week = overview.chef_week(rows, start=MONDAY, end=SUNDAY, limit=Decimal(30_000_000))
    assert week['total'] == '30000000.00' and week['bills'] == 4
    assert week['reached'] is True and week['percent'] == 100  # ровно порог — уже «достигнут»
    assert [item['amount'] for item in week['top']] == ['12000000.00', '9000000.00', '5000000.00']
    below = overview.chef_week(rows[:3], start=MONDAY, end=SUNDAY, limit=Decimal(25_001_000))
    assert below['reached'] is False and below['percent'] == 99  # 99,996 % — ещё не 100


def test_message_names_week_sum_limit_bills_top_and_link():
    week = overview.chef_week([bill(MONDAY, 20_000_000, waiters=('Алина', 'Бобур')),
                               bill(WEDNESDAY, 11_250_000, table=None, waiters=())],
                              start=MONDAY, end=SUNDAY, limit=Decimal(30_000_000))
    text = message(week, kind='first', link='https://panel.example.uz')
    assert text.splitlines() == [
        'Счёт Шефа за неделю дошёл до порога',
        'Неделя 19.10–25.10: 31 250 000 сум при пороге 30 000 000.',
        'Счетов: 2.',
        'Самые крупные:',
        '• 19.10, стол 5, Алина, Бобур: 20 000 000',
        '• 21.10, без официанта: 11 250 000',
        'Кабинет: https://panel.example.uz/founder',
    ]
    again = message(week, kind='repeat', last=dict(amount='20000000.00', sent_at=at(MONDAY, 14).isoformat()))
    assert again.startswith('Счёт Шефа за неделю продолжает расти\n')
    assert 'С сообщения 19.10: +11 250 000.' in again and 'Кабинет' not in again


# ── Расписание ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('database', DATABASES)
def test_one_message_when_the_week_reaches_the_limit(tmp_path, database):
    if database == 'postgres':
        with postgres_url() as url:
            reach_once(url)
    else:
        reach_once(tmp_path / 'founder.sqlite3')


def reach_once(database):
    async def scenario():
        rows, bot, now = Week(bill(MONDAY, 29_999_999)), FakeBot(), [at(WEDNESDAY, 9, 45)]
        checker = alert(database, rows, now, bot=bot)
        assert await checker.run_once() is None and rows.calls == []  # до 10:00 не проверяем
        now[0] = at(WEDNESDAY, 10, 0)
        assert await checker.run_once() is None  # порог не достигнут — ничего
        assert rows.calls == [(MONDAY, WEDNESDAY)]
        assert checker.store.events('2026-W43') == []
        rows.rows.append(bill(WEDNESDAY, 1))  # ровно 30 000 000
        now[0] = at(WEDNESDAY, 10, 15)
        sent = await checker.run_once()
        assert sent['status'] == 'sent' and sent['kind'] == 'first'
        now[0] = at(WEDNESDAY, 10, 30)
        assert await checker.run_once() is None
        # Второй процесс (выкат) с той же базой событие не повторяет.
        assert await alert(database, rows, now, bot=bot).run_once() is None
        assert len(bot.sent) == 1 and bot.sent[0][0] == CHAT
        assert bot.sent[0][1].startswith('Счёт Шефа за неделю дошёл до порога\n')
        [event] = checker.store.events('2026-W43')
        assert event['status'] == 'sent' and event['amount'] == '30000000.00'
        assert event['threshold'] == '30000000.00' and event['bills'] == 2 and event['message_id'] == '1'
        assert event['sent_at'].startswith('2026-10-21T10:15')
    asyncio.run(scenario())


def test_repeat_only_after_another_step_and_once_a_day(tmp_path):
    async def scenario():
        rows, bot, now = Week(bill(MONDAY, 31_000_000)), FakeBot(), [at(MONDAY, 12)]
        checker = alert(tmp_path / 'founder.sqlite3', rows, now, bot=bot)
        assert (await checker.run_once())['kind'] == 'first'
        rows.rows.append(bill(MONDAY, 15_000_000))  # +15 млн, но в тот же день
        now[0] = at(MONDAY, 18)
        assert await checker.run_once() is None
        now[0] = at(MONDAY + timedelta(days=1), 10)
        repeat = await checker.run_once()
        assert repeat['kind'] == 'repeat' and repeat['total'] == '46000000.00'
        assert 'С сообщения 19.10: +15 000 000.' in bot.sent[-1][1]
        rows.rows.append(bill(WEDNESDAY, 9_999_999))  # меньше шага с прошлого сообщения
        now[0] = at(WEDNESDAY, 12)
        assert await checker.run_once() is None
        rows.rows.append(bill(WEDNESDAY, 1))  # теперь ровно шаг
        now[0] = at(WEDNESDAY, 12, 15)
        assert (await checker.run_once())['kind'] == 'repeat'
        assert len(bot.sent) == 3
        assert [event['seq'] for event in checker.store.events('2026-W43')] == [1, 2, 3]
    asyncio.run(scenario())


def test_zero_step_means_a_single_message(tmp_path):
    async def scenario():
        rows, bot, now = Week(bill(MONDAY, 31_000_000)), FakeBot(), [at(MONDAY, 12)]
        checker = alert(tmp_path / 'founder.sqlite3', rows, now, bot=bot, repeat_step=Decimal(0))
        await checker.run_once()
        rows.rows.append(bill(MONDAY, 90_000_000))
        now[0] = at(WEDNESDAY, 12)
        assert await checker.run_once() is None and len(bot.sent) == 1
    asyncio.run(scenario())


def test_new_week_starts_a_new_count(tmp_path):
    async def scenario():
        rows, bot, now = Week(bill(SUNDAY, 35_000_000)), FakeBot(), [at(SUNDAY, 23, 45)]
        checker = alert(tmp_path / 'founder.sqlite3', rows, now, bot=bot)
        assert (await checker.run_once())['week'] == '2026-W43'
        now[0] = at(SUNDAY + timedelta(days=1), 10)
        assert await checker.run_once() is None  # прошлая неделя новую не наполняет
        assert rows.calls[-1] == (SUNDAY + timedelta(days=1), SUNDAY + timedelta(days=1))
        rows.rows.append(bill(SUNDAY + timedelta(days=2), 30_000_000))
        now[0] = at(SUNDAY + timedelta(days=2), 11)
        fresh = await checker.run_once()
        assert fresh['week'] == '2026-W44' and fresh['kind'] == 'first' and fresh['seq'] == 1
        assert bot.sent[-1][1].startswith('Счёт Шефа за неделю дошёл до порога\nНеделя 26.10–01.11:')
    asyncio.run(scenario())


def test_without_founder_chat_only_the_journal_and_no_crash(tmp_path):
    async def scenario():
        rows, now = Week(bill(MONDAY, 40_000_000)), [at(MONDAY, 12)]
        checker = alert(tmp_path / 'founder.sqlite3', rows, now, chat=None)
        assert (await checker.run_once())['status'] == 'no_recipient'
        now[0] = at(MONDAY, 12, 15)
        assert await checker.run_once() is None  # событие уже в журнале
        [event] = checker.store.events('2026-W43')
        assert event['status'] == 'no_recipient' and event['message_id'] is None
        assert status_view([event], checker.config)['status'] == 'no_recipient'
        await checker.close()
        # Чат задали посреди недели — первое сообщение всё же уходит.
        bot = FakeBot()
        later = alert(tmp_path / 'founder.sqlite3', rows, now, bot=bot)
        assert status_view(later.store.events('2026-W43'), later.config) is None
        assert (await later.run_once())['seq'] == 2 and len(bot.sent) == 1
    asyncio.run(scenario())


def test_telegram_failure_is_logged_and_retried_on_the_next_tick(tmp_path):
    async def scenario():
        rows, bot, now = Week(bill(MONDAY, 30_000_000)), FakeBot(fail=1), [at(MONDAY, 12)]
        checker = alert(tmp_path / 'founder.sqlite3', rows, now, bot=bot)
        failed = await checker.run_once()
        assert failed['status'] == 'failed' and failed['error'] == 'network' and not bot.sent
        [event] = checker.store.events('2026-W43')
        assert event['status'] == 'failed' and event['error'] == 'network'
        assert status_view([event], checker.config)['error'] == 'network'
        now[0] = at(MONDAY, 12, 15)
        assert (await checker.run_once())['status'] == 'sent' and len(bot.sent) == 1
        [event] = checker.store.events('2026-W43')
        assert event['status'] == 'sent' and event['attempts'] == 2 and event['seq'] == 1
    asyncio.run(scenario())


def test_iiko_failure_skips_the_tick(tmp_path):
    class Broken:
        async def load(self, start, end):
            raise DataError('iiko недоступен')

    async def scenario():
        bot = FakeBot()
        checker = alert(tmp_path / 'founder.sqlite3', Broken(), [at(MONDAY, 12)], bot=bot)
        assert await checker.run_once() is None and not bot.sent
    asyncio.run(scenario())


def test_abandoned_claim_is_taken_over_after_ten_minutes(tmp_path):
    store = ChefAlertStore(tmp_path / 'founder.sqlite3')
    summary = overview.chef_week([bill(MONDAY, 31_000_000)], start=MONDAY, end=SUNDAY, limit=CHEF_WEEK_LIMIT)
    assert store.claim('2026-W43', 1, summary, at(MONDAY, 12))
    assert not store.claim('2026-W43', 1, summary, at(MONDAY, 12, 5))
    assert store.claim('2026-W43', 1, summary, at(MONDAY, 12, 11))
    store.finish('2026-W43', 1, 'sent', at(MONDAY, 12, 11), 9)
    assert not store.claim('2026-W43', 1, summary, at(MONDAY, 13))


# ── Бот ──────────────────────────────────────────────────────────────────────

def test_bot_sends_plain_text_without_exposing_token():
    seen = {}

    def handler(request):
        seen['path'], seen['body'] = request.url.path, request.content
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 12}})

    async def scenario():
        bot = TelegramBot(TOKEN, transport=httpx.MockTransport(handler))
        assert await bot.send_message(CHAT, 'Счёт Шефа') == 12
        await bot.close()
        refused = TelegramBot(TOKEN, transport=httpx.MockTransport(lambda request: httpx.Response(403)))
        with pytest.raises(TelegramError) as error:
            await refused.send_message(CHAT, 'Счёт Шефа')
        assert error.value.code == 'rejected' and TOKEN not in str(error.value)
        await refused.close()
    asyncio.run(scenario())
    assert seen['path'] == f'/bot{TOKEN}/sendMessage'
    assert '"chat_id":"-1001234567890"' in seen['body'].decode().replace(' ', '')
    assert 'parse_mode' not in seen['body'].decode()


# ── Кабинет учредителя ───────────────────────────────────────────────────────

class FakeIiko:
    def __init__(self, rows):
        self.rows = rows

    async def load_chef_bills(self, start, end):
        return [row for row in self.rows if start.isoformat() <= row['day'] <= end.isoformat()]


def cabinet(tmp_path, rows, **settings):
    app = create_app(Settings(manual_handover_only=True, **settings),
                     expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     founder_db_path=tmp_path / 'founder.sqlite3')
    app.state.iiko = FakeIiko(rows)
    return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))


@pytest.fixture
def wednesday():
    with patch('retro.modules.founder.cabinet.today_tashkent', return_value=WEDNESDAY), \
            patch('retro.modules.founder.routes.today_tashkent', return_value=WEDNESDAY):
        yield WEDNESDAY


def test_chef_block_shows_week_progress_and_alert_state(tmp_path, wednesday):
    rows = [bill(MONDAY, 9_000_000), bill(WEDNESDAY, 3_400_000)]
    with cabinet(tmp_path, rows) as c:
        data = c.get('/api/founder/chef-account').json()
    week = data['week_alert']
    assert data['week_total'] == week['total'] == '12400000.00'
    assert week['limit'] == '30000000.00' and week['percent'] == 41 and week['reached'] is False
    assert week['start'] == '2026-10-19' and week['end'] == '2026-10-25' and week['week'] == '2026-W43'
    assert week['recipient'] is False and week['last'] is None


def test_chef_block_reports_the_sent_alert(tmp_path, wednesday):
    rows = [bill(MONDAY, 31_000_000)]
    config = ChefAlertConfig(token=TOKEN, chat_id=CHAT)
    with cabinet(tmp_path, rows, chef_alert=config) as c:
        store = c.app.state.chef_alerts
        summary = overview.chef_week(rows, start=MONDAY, end=SUNDAY, limit=CHEF_WEEK_LIMIT)
        store.claim('2026-W43', 1, summary, at(WEDNESDAY, 10, 15))
        store.finish('2026-W43', 1, 'sent', at(WEDNESDAY, 10, 15), 4)
        week = c.get('/api/founder/chef-account').json()['week_alert']
    assert week['reached'] is True and week['recipient'] is True
    assert week['last']['status'] == 'sent' and week['last']['at'].startswith('2026-10-21T10:15')
