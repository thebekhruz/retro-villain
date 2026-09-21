import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import HikvisionConfig, Settings
from retro.integrations.hikvision import HikvisionError, HikvisionEvent, HikvisionPerson
from retro.integrations.hikvision_poller import HikvisionPoller
from retro.modules.accountant.hikvision import AttendanceStore
from retro.modules.accountant.roster import RosterStore


TZ = ZoneInfo('Asia/Tashkent')
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=TZ)
CONFIG = HikvisionConfig(
    base_url='https://203.0.113.10', username='reader', password='secret',
    source='entry', poll_seconds=30, timeout_seconds=8, verify_tls=True)


class StubClient:
    def __init__(self, *, people=(), events=(), error=None):
        self.people = tuple(people)
        self.events = tuple(events)
        self.error = error
        self.ranges = []
        self.people_calls = 0
        self.closed = False

    async def fetch_people(self):
        self.people_calls += 1
        if self.error:
            raise self.error
        return self.people

    async def fetch_events(self, start, end):
        self.ranges.append((start, end))
        if self.error:
            raise self.error
        return self.events

    async def close(self):
        self.closed = True


def run(awaitable):
    return asyncio.run(awaitable)


def test_first_run_backfills_previous_tashkent_day_and_links_before_ingest(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster = RosterStore(path)
    employee = roster.add(name='Азиза Каримова', role='официант', rate='250000',
                          group_name='Обслуживание зала')
    client = StubClient(
        people=(HikvisionPerson('100', 'азиза каримова'),),
        events=(HikvisionEvent('entry', 's1', '100',
                               datetime(2026, 9, 21, 9, 5, tzinfo=TZ)),))
    store = AttendanceStore(path)
    poller = HikvisionPoller(CONFIG, client, roster, store, now=lambda: NOW)

    result = run(poller.run_once())

    assert result.success is True
    assert result.linked == 1
    assert result.events == 1
    assert client.ranges == [(datetime(2026, 9, 20, 0, 0, tzinfo=TZ), NOW)]
    assert store.first_entries(NOW.date())[employee.id].employee_no == '100'
    assert store.sync_state('entry').cursor_at == NOW


def test_resume_overlaps_cursor_and_advances_only_after_success(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster, store = RosterStore(path), AttendanceStore(path)
    previous = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)
    store.record_success('entry', at=previous, cursor_at=previous,
                         covered_from=datetime(2026, 9, 20, 0, tzinfo=TZ),
                         covered_through=previous)
    client = StubClient()

    result = run(HikvisionPoller(CONFIG, client, roster, store, now=lambda: NOW).run_once())

    assert result.success is True
    assert client.ranges[0][0] == datetime(2026, 9, 21, 9, 55, tzinfo=TZ)
    state = store.sync_state('entry')
    assert state.cursor_at == NOW
    assert state.covered_from == datetime(2026, 9, 20, 0, tzinfo=TZ)


def test_new_link_reconciles_older_stored_events_outside_cursor_overlap(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster, store = RosterStore(path), AttendanceStore(path)
    employee = roster.add(name='Азиза Каримова', role='официант', rate='250000',
                          group_name='Обслуживание зала')
    old_event = HikvisionEvent(
        'entry', 'stored-before-roster', '100',
        datetime(2026, 9, 21, 8, 30, tzinfo=TZ))
    store.ingest(old_event, None, received_at=datetime(2026, 9, 21, 9, tzinfo=TZ))
    previous = datetime(2026, 9, 21, 11, 30, tzinfo=TZ)
    store.record_success(
        'entry', at=previous, cursor_at=previous,
        covered_from=datetime(2026, 9, 20, 0, tzinfo=TZ), covered_through=previous)
    client = StubClient(people=(HikvisionPerson('100', 'азиза каримова'),), events=())

    result = run(HikvisionPoller(CONFIG, client, roster, store, now=lambda: NOW).run_once())

    assert result.success is True
    assert result.linked == 1
    assert client.ranges == [(datetime(2026, 9, 21, 11, 25, tzinfo=TZ), NOW)]
    assert store.first_entries(NOW.date())[employee.id].occurred_at == old_event.occurred_at


def test_failed_poll_keeps_old_cursor_and_records_safe_error(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster, store = RosterStore(path), AttendanceStore(path)
    previous = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)
    store.record_success('entry', at=previous, cursor_at=previous,
                         covered_from=previous, covered_through=previous)
    client = StubClient(error=HikvisionError('timeout'))

    result = run(HikvisionPoller(CONFIG, client, roster, store, now=lambda: NOW).run_once())

    state = store.sync_state('entry')
    assert result.success is False
    assert result.error == 'timeout'
    assert state.cursor_at == previous
    assert state.last_error_code == 'timeout'
    assert 'secret' not in repr(result)


class FakePoller:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


def test_fastapi_lifespan_starts_and_stops_injected_poller(tmp_path):
    fake = FakePoller()
    app = create_app(
        Settings(hikvision=CONFIG, data_dir=tmp_path),
        accountant_db_path=tmp_path / 'accountant.sqlite3',
        expense_db_path=tmp_path / 'cashier.sqlite3',
        director_db_path=tmp_path / 'director.sqlite3',
        hikvision_poller=fake)

    with TestClient(app, base_url='http://127.0.0.1',
                    client=('127.0.0.1', 50000)) as client:
        assert client.get('/login').status_code == 200
        assert fake.started == 1

    assert fake.stopped == 1


def test_unconfigured_app_does_not_create_network_poller(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path),
        accountant_db_path=tmp_path / 'accountant.sqlite3',
        expense_db_path=tmp_path / 'cashier.sqlite3',
        director_db_path=tmp_path / 'director.sqlite3')

    assert app.state.hikvision_poller is None
