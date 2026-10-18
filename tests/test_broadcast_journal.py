"""T-505: журнал рассылок гостям (ТЗ 16.10, п. 5.3).

Запись о каждой рассылке через панель: кто, когда, текст, скольким гостям,
вручную или автоматически, и последний известный итог бота. Повтор с тем же
operation_id строку не множит. На SQLite и на Postgres (RETRO_TEST_POSTGRES_URL).
"""
import os
from contextlib import closing
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import Settings
from retro.db import Database
from retro.modules.cashier.service import DataError
from retro.modules.founder.broadcast_journal import BroadcastJournal

POSTGRES_URL = os.getenv('RETRO_TEST_POSTGRES_URL', '')
OPERATION = '12345678-1234-4123-8123-123456789012'
# id задачи у бота — не UUID: экран опрашивает статус именно по нему.
JOB = 'job_' + 'x' * 20
AT = '2026-10-17T14:05:00+05:00'


@pytest.fixture(params=['sqlite', 'postgres'])
def journal(request, tmp_path):
    if request.param == 'sqlite':
        yield BroadcastJournal(tmp_path / 'founder.sqlite3')
        return
    if not POSTGRES_URL:
        pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
    import psycopg
    from psycopg import sql
    schema = 'journal_' + uuid4().hex
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(POSTGRES_URL)
    query = dict(parse_qsl(parts.query))
    query['options'] = '-csearch_path=' + schema
    try:
        yield BroadcastJournal(Database(urlunsplit(parts._replace(query=urlencode(query)))))
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


def job(status='queued', sent=0, blocked=0, failed=0, audience=3, job_id=JOB):
    return {'id': job_id, 'status': status, 'audience': audience,
            'sent': sent, 'blocked': blocked, 'failed': failed}


def test_journal_records_once_per_operation_and_updates_status(journal):
    journal.record(OPERATION, job(), text='  Новое меню  ', recipients=3,
                   sent_by='manager', at=AT)
    # Двойное нажатие / обрыв связи: тот же operation_id — та же строка.
    journal.record(OPERATION, job('running', sent=1), text='Новое меню', recipients=3,
                   sent_by='manager', at=AT)
    assert journal.update_status(job('completed', sent=2, blocked=1),
                                 '2026-10-17T14:06:00+05:00') == 1
    assert journal.update_status(job('completed', job_id='other_' + 'y' * 20), AT) == 0
    [entry] = journal.list()
    assert entry == {
        'operation_id': OPERATION, 'job_id': JOB, 'kind': 'manual', 'sent_by': 'manager',
        'sent_at': AT, 'text': 'Новое меню', 'recipients': 3, 'status': 'completed',
        'audience': 3, 'sent': 2, 'blocked': 1, 'failed': 0,
        'status_at': '2026-10-17T14:06:00+05:00'}


def test_journal_allows_auto_kind_lists_newest_first_and_rejects_unknown(journal):
    journal.record(OPERATION, job(), text='Первое', recipients=1, sent_by='', at=AT)
    journal.record(str(uuid4()), job(job_id='auto_' + 'z' * 20), text='Второе', recipients=2,
                   sent_by='', at=AT, kind='auto')
    entries = journal.list()
    assert [(row['text'], row['kind']) for row in entries] == [('Второе', 'auto'), ('Первое', 'manual')]
    assert len(journal.list(1)) == 1
    with pytest.raises(ValueError):
        journal.record(str(uuid4()), job(), text='Текст', recipients=1, sent_by='', at=AT,
                       kind='ai')
    with pytest.raises(ValueError):
        journal.list(0)


def test_journal_kind_column_rejects_unknown_values_in_database(tmp_path):
    store = BroadcastJournal(tmp_path / 'founder.sqlite3')
    with closing(store.db.connect()) as connection, pytest.raises(Exception):
        with connection:
            connection.execute(
                "INSERT INTO founder_broadcasts(operation_id,kind,sent_at,text) "
                "VALUES ('x','ai','2026-10-17','t')")


class Bot:
    """Бот, у которого id задачи не совпадает с нашим operation_id."""

    def __init__(self):
        self.calls = []
        self.status_value = job('running', sent=1)
        self.fail_status = False

    async def start(self, operation_id, text, recipient_ids, author=None):
        self.calls.append(('start', operation_id))
        return job(audience=len(recipient_ids))

    async def status(self, job_id):
        self.calls.append(('status', job_id))
        if self.fail_status:
            raise DataError('API рассылок недоступен.')
        return self.status_value


@pytest.fixture
def api(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    bot = Bot()
    app.state.broadcasts = bot
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        yield client, bot


def send(client, operation=OPERATION):
    return client.post('/api/founder/broadcast', json={
        'operation_id': operation, 'text': '  Новое меню  ',
        'recipient_ids': ['A' * 32, 'B' * 32, 'B' * 32]})


def test_broadcast_status_accepts_bot_job_id_that_is_not_uuid(api):
    client, bot = api
    started = send(client).json()
    assert started['id'] == JOB
    status = client.get('/api/founder/broadcast/' + started['id'])
    assert status.status_code == 200
    assert status.json()['sent'] == 1
    assert ('status', JOB) in bot.calls
    assert client.get('/api/founder/broadcast/short').status_code == 422


def test_broadcast_journal_records_send_once_and_follows_status(api):
    client, bot = api
    assert client.get('/api/founder/broadcast/journal').json() == {'entries': []}
    assert send(client).status_code == 202
    assert send(client).status_code == 202  # повтор того же operation_id
    bot.status_value = job('completed', sent=1, blocked=1, failed=0, audience=2)
    client.get('/api/founder/broadcast/' + JOB)
    entries = client.get('/api/founder/broadcast/journal').json()['entries']
    assert len(entries) == 1
    entry = entries[0]
    assert (entry['operation_id'], entry['job_id'], entry['kind'], entry['text'],
            entry['recipients']) == (OPERATION, JOB, 'manual', 'Новое меню', 2)
    assert (entry['status'], entry['sent'], entry['blocked'], entry['failed']) == (
        'completed', 1, 1, 0)
    assert entry['status_at']
    # Завершённую рассылку журнал у бота не переспрашивает.
    calls = len(bot.calls)
    client.get('/api/founder/broadcast/journal')
    assert len(bot.calls) == calls


def test_broadcast_journal_refreshes_unfinished_entries_and_survives_bot_errors(api):
    client, bot = api
    send(client)
    bot.fail_status = True
    entry = client.get('/api/founder/broadcast/journal').json()['entries'][0]
    assert entry['status'] == 'queued'  # последний известный итог, без ошибки экрана
    bot.fail_status = False
    bot.status_value = job('completed', sent=2, audience=2)
    entry = client.get('/api/founder/broadcast/journal').json()['entries'][0]
    assert (entry['status'], entry['sent']) == ('completed', 2)


def test_broadcast_journal_does_not_poll_stale_unfinished_entries(api):
    client, bot = api
    client.app.state.broadcast_journal.record(
        str(uuid4()), job(job_id='old_' + 'q' * 20), text='Давно', recipients=1,
        sent_by='', at='2026-01-01T10:00:00+05:00')
    client.get('/api/founder/broadcast/journal')
    assert bot.calls == []


def test_broadcast_survives_journal_failure(api):
    client, bot = api

    class Broken:
        def record(self, *args, **kwargs):
            raise RuntimeError('диск полон')

        def update_status(self, *args, **kwargs):
            raise RuntimeError('диск полон')

    client.app.state.broadcast_journal = Broken()
    assert send(client).status_code == 202
    assert client.get('/api/founder/broadcast/' + JOB).status_code == 200


def test_broadcast_journal_page_section_is_present_and_translated():
    # Один блок «Журнал рассылок» (T-504): журнал бота, а без него — этот журнал панели.
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'retro' / 'static'
    html = (root / 'founder.html').read_text()
    assert 'id="broadcast-history-list"' in html and 'Журнал рассылок' in html
    assert html.count('Журнал рассылок') == 1
    assert "'/api/founder/broadcast/journal'" in (root / 'founder-broadcast-history.js').read_text()
    uz = (root / 'i18n-uz.js').read_text()
    for phrase in ('Журнал рассылок', 'Рассылок ещё не было.', 'Из панели', 'Автоматическая',
                   'Последние 30 рассылок через эту панель. Получатели по статусам и досылка '
                   'появятся после обновления бота.'):
        assert f"'{phrase}':" in uz
