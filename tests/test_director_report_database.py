"""Report generation must work on the production database dialect too."""
import asyncio
from datetime import date
import os
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from retro.db import Database
from retro.modules.director.routes import router
from retro.modules.director.service import DirectorService
from retro.modules.director.store import DirectorReportStore


@pytest.fixture(params=['sqlite', 'postgres'])
def store(request, tmp_path):
    if request.param == 'sqlite':
        yield DirectorReportStore(tmp_path / 'director.sqlite3')
        return
    url = os.getenv('RETRO_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
    import psycopg
    from psycopg import sql

    schema = 'probe_director_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query['options'] = f'-csearch_path={schema}'
    database = Database(urlunsplit(parts._replace(query=urlencode(query))))
    try:
        yield DirectorReportStore(database)
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_generate_archive_and_download_report_on_both_databases(store, monkeypatch):
    """The button's POST, repeat generation and PDF use the real store."""
    monkeypatch.setattr('retro.modules.director.routes.today_tashkent', lambda: date(2026, 10, 9))
    snapshot = dict(period_start='2026-10-02', period_end='2026-10-08',
                    cash_total='1000', yandex_revenue='0', item_metrics={}, waiter_metrics={})
    calls = []

    async def load(today, *, start, end):
        assert (start, end) == (date(2026, 10, 2), date(2026, 10, 8))
        return SimpleNamespace(json=lambda: dict(snapshot))

    async def analyze(value):
        calls.append(value.json())
        return {'summary': 'Проверить меню.', 'problems': []}

    app = FastAPI()
    app.include_router(router)
    app.state.director_lock = asyncio.Lock()
    app.state.director_store = store
    app.state.director_service = DirectorService(
        None, SimpleNamespace(analyze=analyze), store, loader=load)
    with TestClient(app) as client:
        assert store.get_for_period('2026-10-02', '2026-10-08') is None
        ids = []
        for _ in range(2):
            response = client.post('/api/director/reports?start=2026-10-02&end=2026-10-08')
            assert response.status_code == 201, response.text
            report_id = response.json()['id']
            ids.append(report_id)
            assert store.get_for_period('2026-10-02', '2026-10-08')['id'] == report_id
            pdf = client.get(f'/api/director/reports/{report_id}/pdf')
            assert pdf.status_code == 200
            assert pdf.headers['content-type'] == 'application/pdf'
            assert pdf.content.startswith(b'%PDF-')
        assert len(calls) == 1
        assert ids[0] != ids[1]
        archive = client.get('/api/director/reports')
        assert archive.status_code == 200
        assert [row['id'] for row in archive.json()['reports']] == ids[::-1]
        snapshot['cash_total'] = '2000'
        changed = client.post('/api/director/reports?start=2026-10-02&end=2026-10-08')
        assert changed.status_code == 201, changed.text
        assert len(calls) == 2
        assert client.get(f'/api/director/reports/{ids[0]}').json()['snapshot']['cash_total'] == '1000'


def test_period_lookup_uses_same_tie_order_as_archive(store, monkeypatch):
    ids = iter(['f' * 32, '0' * 32])
    monkeypatch.setattr('retro.modules.director.store.uuid4',
                        lambda: SimpleNamespace(hex=next(ids)))
    snapshot = dict(period_start='2026-10-02', period_end='2026-10-08')
    for _ in range(2):
        store.create(snapshot, {'summary': 'Меню'}, b'%PDF-1.4', '2026-10-09T18:00:00+05:00')
    expected = store.list_metadata()[0]['id']
    assert store.get_for_period('2026-10-02', '2026-10-08')['id'] == expected
