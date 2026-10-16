"""T-478: снимок кассы дня в Telegram-группу в 23:30."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO

import httpx
import pytest
from PIL import Image

from legacy_app import create_app
from test_accountant_month_close import DATABASES, postgres_url
from retro.config import Settings, TelegramReportConfig, parse_telegram_report_config
from retro.integrations.telegram import TelegramBot, TelegramError
from retro.modules.cashier.expenses import Expense
from retro.modules.cashier.service import TZ, ShiftStatus, demo_snapshot, today_tashkent
from retro.modules.cashier.telegram_report import (DayReport, SentStore, Skip, TelegramDailyReport, caption,
                                                   due_day, money, render)

TOKEN = '123456789:' + 'A' * 35
DAY = date(2026, 10, 16)


def report(*, shift_open=True, prepayments=True, handover=None):
    snapshot = replace(demo_snapshot(DAY), demo=False,
                       shift=ShiftStatus(shift_open, '2026-10-16T09:58:00',
                                         None if shift_open else '2026-10-17T00:12:40'))
    if not prepayments:
        snapshot = replace(snapshot, prepayments=None)
    summary = dict(demo_cash='2000000', cash_prepayment='3000000', receipts='150000', expenses='390000',
                   shokh='500000', usd_total='100', usd_rate='12650', usd_deduction='1265000',
                   usd_issue=None, prepayment_issue=None, handover='2995000')
    return DayReport(DAY, datetime(2026, 10, 16, 23, 30, tzinfo=TZ), snapshot, summary,
                     (Expense(1, DAY, 'Зарплата кассира', Decimal(250000), True),
                      Expense(2, DAY, 'Лёд ' * 60, Decimal(140000))),
                     (Expense(3, DAY, 'Возврат долга', Decimal(150000)),),
                     ({'amount': '500000', 'created_at': '2026-10-16T14:05:00+05:00'},), handover)


# ── Настройки ────────────────────────────────────────────────────────────────

def test_config_is_optional_and_set_together():
    assert parse_telegram_report_config({}) is None
    with pytest.raises(ValueError, match='вместе'):
        parse_telegram_report_config({'TELEGRAM_BOT_TOKEN': TOKEN})
    with pytest.raises(ValueError, match='@BotFather'):
        parse_telegram_report_config({'TELEGRAM_BOT_TOKEN': 'nope', 'TELEGRAM_REPORT_CHAT_ID': '-1001234567890'})
    with pytest.raises(ValueError, match='ЧЧ:ММ'):
        parse_telegram_report_config({'TELEGRAM_BOT_TOKEN': TOKEN, 'TELEGRAM_REPORT_CHAT_ID': '-1001234567890',
                                      'TELEGRAM_REPORT_TIME': '24:00'})
    config = parse_telegram_report_config({'TELEGRAM_BOT_TOKEN': TOKEN, 'TELEGRAM_REPORT_CHAT_ID': '-1001234567890'})
    assert config.send_at == time(23, 30) and config.auto
    assert TOKEN not in repr(config)
    manual = parse_telegram_report_config({'TELEGRAM_BOT_TOKEN': TOKEN, 'TELEGRAM_REPORT_CHAT_ID': '-1001234567890',
                                           'TELEGRAM_REPORT_TIME': '22:45', 'TELEGRAM_REPORT_AUTO': 'false'})
    assert manual.send_at == time(22, 45) and not manual.auto


# ── Картинка и подпись ───────────────────────────────────────────────────────

def test_money_matches_screen_format():
    assert money('1250000') == '1 250 000'
    assert money('120.5') == '120,50'
    assert money('-3000') == '−3 000'
    assert money(None) == 'требует проверки'


def test_render_is_phone_width_png_and_caption_carries_date():
    for item in (report(), report(shift_open=False, handover={'amount': '2995000', 'source': 'cashier',
                                                              'handed_at': '2026-10-16T23:05:00+05:00'}),
                 report(prepayments=False)):
        image = Image.open(BytesIO(render(item)))
        assert image.format == 'PNG' and image.width == 1080 and 1200 < image.height < 6000
    text = caption(report())
    assert text.startswith('Касса Retro · 16.10.2026 · пятница')
    assert 'Выручка: 21 450 000 сум' in text
    assert 'предварительные' in text
    assert 'предварительные' not in caption(report(shift_open=False))
    assert 'Выручка: требует проверки' in caption(report(prepayments=False))


# ── Бот ──────────────────────────────────────────────────────────────────────

def test_bot_sends_photo_and_never_exposes_token():
    seen = {}

    def handler(request):
        seen['path'] = request.url.path
        seen['body'] = request.content
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 77}})

    async def scenario():
        bot = TelegramBot(TOKEN, transport=httpx.MockTransport(handler))
        assert await bot.send_photo('-100123', b'\x89PNG', 'Касса') == 77
        await bot.close()
        refused = TelegramBot(TOKEN, transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={'ok': False, 'description': 'bot was kicked'})))
        with pytest.raises(TelegramError) as error:
            await refused.send_photo('-100123', b'\x89PNG', 'Касса')
        assert error.value.code == 'rejected' and TOKEN not in str(error.value)
        await refused.close()

        def broken(request):
            raise httpx.ConnectError(f'cannot reach {request.url}')
        offline = TelegramBot(TOKEN, transport=httpx.MockTransport(broken))
        with pytest.raises(TelegramError) as error:
            await offline.send_photo('-100123', b'\x89PNG', 'Касса')
        assert error.value.code == 'network' and TOKEN not in str(error.value)
        assert error.value.__cause__ is None and error.value.__suppress_context__
        await offline.close()
    asyncio.run(scenario())
    assert seen['path'] == f'/bot{TOKEN}/sendPhoto'
    assert b'name="chat_id"' in seen['body'] and b'-100123' in seen['body']


# ── Расписание ───────────────────────────────────────────────────────────────

def at(day, hour, minute):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def test_due_day_is_tonight_after_send_time_and_catches_up_until_morning():
    send_at = time(23, 30)
    assert due_day(at(DAY, 23, 29), send_at) is None
    assert due_day(at(DAY, 23, 30), send_at) == DAY
    assert due_day(at(date(2026, 10, 17), 1, 15), send_at) == DAY
    assert due_day(at(date(2026, 10, 17), 6, 0), send_at) is None
    assert due_day(at(date(2026, 10, 17), 12, 0), send_at) is None


class FakeBot:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    async def send_photo(self, chat_id, photo, text, *, filename):
        if self.fail:
            self.fail -= 1
            raise TelegramError('network')
        self.sent.append((chat_id, filename, text))
        return len(self.sent)

    async def close(self):
        pass


def photo(name, marks=None):
    """Картинка-заглушка; marks — изменяемый отпечаток содержимого (T-484)."""
    async def build(app, day, now):
        return b'\x89PNG', f'{name} · {day.strftime("%d.%m.%Y")}', (marks or {}).get(name, 'v1')
    return build


def scheduler(database, bot, now, reports=None):
    config = TelegramReportConfig(token=TOKEN, chat_id='-100123')
    app = type('App', (), {'state': object()})()
    return TelegramDailyReport(app, bot, SentStore(database), config, clock=lambda: now[0],
                               reports=reports or {'cashier': photo('Касса'), 'accountant': photo('Бухгалтер')})


@pytest.mark.parametrize('database', DATABASES)
def test_scheduler_sends_each_evening_once(tmp_path, database):
    if database == 'postgres':
        with postgres_url() as url:
            run_evening(url)
    else:
        run_evening(tmp_path / 'tg.sqlite3')


def run_evening(database):
    async def scenario():
        bot, now = FakeBot(), [at(DAY, 23, 0)]
        reporter = scheduler(database, bot, now)
        assert await reporter.run_once() == []
        now[0] = at(DAY, 23, 31)
        assert await reporter.run_once() == [('cashier', DAY), ('accountant', DAY)]
        now[0] = at(DAY, 23, 45)
        assert await reporter.run_once() == []
        # Второй процесс (или выкат) с той же базой день не повторяет.
        again = scheduler(database, bot, now)
        assert await again.run_once() == []
        # Сначала касса, потом бухгалтер.
        assert bot.sent == [('-100123', 'Retro-kassa-2026-10-16.png', 'Касса · 16.10.2026'),
                            ('-100123', 'Retro-buhgalter-2026-10-16.png', 'Бухгалтер · 16.10.2026')]
    asyncio.run(scenario())


def test_failed_report_retries_alone_and_catches_up_after_midnight(tmp_path):
    calls = []

    def flaky(name, failures):
        async def build(app, day, now):
            calls.append(name)
            if failures:
                failures.pop()
                raise TelegramError('network')
            return b'\x89PNG', name, 'v1'
        return build

    async def scenario():
        bot, now = FakeBot(), [at(DAY, 23, 30)]
        reporter = scheduler(tmp_path / 'tg.sqlite3', bot, now,
                             reports={'cashier': flaky('Касса', []), 'accountant': flaky('Бухгалтер', [1])})
        assert await reporter.run_once() == [('cashier', DAY)]
        now[0] = at(DAY, 23, 32)
        assert await reporter.run_once() == []  # ждём 5 минут после сбоя
        now[0] = at(date(2026, 10, 17), 0, 10)
        assert await reporter.run_once() == [('accountant', DAY)]
        assert calls == ['Касса', 'Бухгалтер', 'Бухгалтер']
        assert [item[2] for item in bot.sent] == ['Касса', 'Бухгалтер']
    asyncio.run(scenario())


def test_skip_marks_day_without_sending(tmp_path):
    async def skip(app, day, now):
        raise Skip('демо-данные')

    async def scenario():
        bot, now = FakeBot(), [at(DAY, 23, 31)]
        reporter = scheduler(tmp_path / 'tg.sqlite3', bot, now, reports={'cashier': skip})
        assert await reporter.run_once() == []
        assert reporter.store.sent('cashier', DAY) and not bot.sent
    asyncio.run(scenario())


def test_unchanged_report_is_not_sent_twice_and_change_is_marked(tmp_path):
    async def scenario():
        bot, now, marks = FakeBot(), [at(DAY, 18, 0)], {}
        reporter = scheduler(tmp_path / 'tg.sqlite3', bot, now, reports={'cashier': photo('Касса', marks)})
        assert await reporter.deliver('cashier', DAY) == 'sent'
        assert await reporter.deliver('cashier', DAY) == 'unchanged'
        marks['Касса'] = 'v2'  # в кассе что-то поменялось
        assert await reporter.deliver('cashier', DAY) == 'sent'
        assert await reporter.deliver('cashier', DAY) == 'unchanged'
        assert [item[2] for item in bot.sent] == ['Касса · 16.10.2026',
                                                  'Исправленный отчёт\nКасса · 16.10.2026']
    asyncio.run(scenario())


def test_evening_sends_only_reports_nobody_submitted(tmp_path):
    async def scenario():
        bot, now = FakeBot(), [at(DAY, 21, 40)]
        reporter = scheduler(tmp_path / 'tg.sqlite3', bot, now)
        # Кассир нажал «Передать бухгалтеру» в 21:40 — касса ушла сразу.
        await reporter.notify('cashier', DAY)
        now[0] = at(DAY, 23, 31)
        # Бухгалтер отчёт не сдал — в 23:30 уходит только он.
        assert await reporter.run_once() == [('accountant', DAY)]
        assert [item[1] for item in bot.sent] == ['Retro-kassa-2026-10-16.png', 'Retro-buhgalter-2026-10-16.png']
    asyncio.run(scenario())


def test_failed_notify_does_not_raise_and_evening_catches_up(tmp_path):
    async def scenario():
        bot, now = FakeBot(fail=1), [at(DAY, 21, 40)]
        reporter = scheduler(tmp_path / 'tg.sqlite3', bot, now, reports={'cashier': photo('Касса')})
        await reporter.notify('cashier', DAY)  # Telegram недоступен — передача кассира не ломается
        assert not reporter.store.sent('cashier', DAY)
        now[0] = at(DAY, 23, 31)
        assert await reporter.run_once() == [('cashier', DAY)]
    asyncio.run(scenario())


# ── Ручная проверка из кабинета ──────────────────────────────────────────────

def configured_app(tmp_path, **extra):
    settings = Settings(data_dir=tmp_path, login='synthetic', password='synthetic', store_id=1, **extra)
    app = create_app(settings)

    async def load(day):
        return replace(demo_snapshot(day), demo=False, fetched_at=datetime.now(TZ))

    async def load_prepayments(day):
        return demo_snapshot(day).prepayments
    app.state.iiko.load, app.state.iiko.load_prepayments = load, load_prepayments
    return app


def test_preview_is_admin_only_png(tmp_path):
    async def scenario():
        app = configured_app(tmp_path, dashboard_panel_users={'kassa': ('secret', 'cashier'),
                                                              'boss': ('secret', 'admin')})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)),
                                     base_url='http://127.0.0.1') as client:
            day = today_tashkent()
            url = f'/api/cashier/telegram-report/preview?date={day}'
            assert (await client.get(url, auth=('kassa', 'secret'))).status_code == 403
            answer = await client.get(url, auth=('boss', 'secret'))
            assert answer.status_code == 200 and answer.headers['content-type'] == 'image/png'
            assert Image.open(BytesIO(answer.content)).width == 1080
            # Без бота отправлять некуда.
            sent = await client.post(f'/api/cashier/telegram-report?date={day}', auth=('boss', 'secret'),
                                     headers={'Origin': 'http://127.0.0.1'})
            assert sent.status_code == 409
        await app.state.cashier_days.close()
    asyncio.run(scenario())


def test_manual_send_goes_to_group_once_while_unchanged(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 5}})

    async def scenario():
        settings = Settings(data_dir=tmp_path, login='synthetic', password='synthetic', store_id=1,
                            telegram_report=TelegramReportConfig(token=TOKEN, chat_id='-100123', auto=False))
        app = create_app(settings, telegram_transport=httpx.MockTransport(handler))

        async def load(day):
            return replace(demo_snapshot(day), demo=False, fetched_at=datetime.now(TZ))

        async def load_prepayments(day):
            return demo_snapshot(day).prepayments
        app.state.iiko.load, app.state.iiko.load_prepayments = load, load_prepayments
        day = today_tashkent()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)),
                                     base_url='http://127.0.0.1') as client:
            answer = await client.post(f'/api/cashier/telegram-report?date={day}',
                                       headers={'Origin': 'http://127.0.0.1'})
            assert answer.status_code == 200, answer.text
            assert answer.json() == {'date': day.isoformat(), 'status': 'sent'}
            # Тот же отчёт без изменений второй раз не уходит (T-484).
            again = await client.post(f'/api/cashier/telegram-report?date={day}',
                                      headers={'Origin': 'http://127.0.0.1'})
            assert again.json() == {'date': day.isoformat(), 'status': 'unchanged'}
        assert len(requests) == 1 and b'image/png' in requests[0].content
        # Отправленный вручную — отправлен: в 23:30 его не повторят.
        assert app.state.telegram_report.store.sent('cashier', day)
        await app.state.cashier_days.close()
    asyncio.run(scenario())


def test_store_adds_fingerprint_to_the_table_created_before_t484(tmp_path):
    import sqlite3
    path = tmp_path / 'tg.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('''CREATE TABLE telegram_reports (kind TEXT NOT NULL, day TEXT NOT NULL,
            claimed_at TEXT NOT NULL, sent_at TEXT, message_id TEXT, PRIMARY KEY(kind, day))''')
        connection.execute("INSERT INTO telegram_reports VALUES ('cashier', '2026-10-16', '', 'x', '1')")
    store = SentStore(path)
    # Отправленный до T-484 без отпечатка — отправлен; отпечатка нет.
    assert store.sent('cashier', DAY) and store.last_fingerprint('cashier', DAY) is None
    assert store.claim('cashier', DAY, at(DAY, 23, 40))
    store.finish('cashier', DAY, 2, at(DAY, 23, 40), 'v2')
    assert store.last_fingerprint('cashier', DAY) == 'v2'
