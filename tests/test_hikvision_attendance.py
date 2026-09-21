from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from retro.integrations.hikvision import HikvisionEvent, HikvisionPerson
from retro.modules.accountant.hikvision import AttendanceService, AttendanceStore
from retro.modules.accountant.payroll import compute_pay
from retro.modules.accountant.roster import Employee, RosterStore


TZ = ZoneInfo('Asia/Tashkent')
DAY = date(2026, 9, 21)


def add_employee(store, name):
    return store.add(name=name, role='официант', rate='250000', group_name='Обслуживание зала')


def test_exact_normalized_names_link_once_and_then_employee_no_wins(tmp_path):
    store = RosterStore(tmp_path / 'accountant.sqlite3')
    aziza = add_employee(store, '  Азиза   Каримова ')
    add_employee(store, 'Бахром Алиев')

    report = store.link_hikvision_people((
        HikvisionPerson('100', 'азиза каримова'),
        HikvisionPerson('200', 'Нет в Retro'),
    ))
    repeated = store.link_hikvision_people((
        HikvisionPerson('100', 'Имя позже изменили'),
    ))

    linked = next(employee for employee in store.list() if employee.id == aziza.id)
    assert linked.hikvision_id == '100'
    assert report == {'people': 2, 'linked': 1, 'already_linked': 0,
                      'ambiguous': 0, 'unmatched': 1}
    assert repeated == {'people': 1, 'linked': 0, 'already_linked': 1,
                        'ambiguous': 0, 'unmatched': 0}


def test_duplicate_names_on_either_side_are_never_auto_linked(tmp_path):
    store = RosterStore(tmp_path / 'accountant.sqlite3')
    add_employee(store, 'Алишер Саидов')
    add_employee(store, 'алишер   саидов')
    add_employee(store, 'Малика Юлдашева')

    report = store.link_hikvision_people((
        HikvisionPerson('1', 'Алишер Саидов'),
        HikvisionPerson('2', 'Малика Юлдашева'),
        HikvisionPerson('3', 'малика юлдашева'),
        HikvisionPerson('4', None),
    ))

    assert all(employee.hikvision_id is None for employee in store.list())
    assert report['ambiguous'] == 3
    assert report['unmatched'] == 1


def test_event_dedup_and_delayed_older_event_keep_one_earliest_entry(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    employee = add_employee(RosterStore(path), 'Азиза Каримова')
    store = AttendanceStore(path)
    later = HikvisionEvent('entry', 'serial-2', '100',
                            datetime(2026, 9, 21, 10, 12, tzinfo=TZ))
    earlier = HikvisionEvent('entry', 'serial-1', '100',
                              datetime(2026, 9, 21, 9, 5, tzinfo=TZ))

    assert store.ingest(later, employee.id) is True
    assert store.ingest(later, employee.id) is False
    assert store.ingest(earlier, employee.id) is True

    entries = store.first_entries(DAY)
    assert len(entries) == 1
    assert entries[employee.id].occurred_at == earlier.occurred_at
    assert store.event_count() == 2


def test_unknown_employee_event_is_deduplicated_without_creating_entry(tmp_path):
    store = AttendanceStore(tmp_path / 'accountant.sqlite3')
    event = HikvisionEvent('entry', 'serial-1', 'unknown',
                            datetime(2026, 9, 21, 9, 5, tzinfo=TZ))

    assert store.ingest(event, None) is True
    assert store.ingest(event, None) is False
    assert store.first_entries(DAY) == {}
    assert store.event_count() == 1


def linked_employee(employee_id, name, employee_no):
    return Employee(employee_id, employee_id, name, 'официант', 'Обслуживание зала',
                    Decimal('250000'), employee_no)


def test_attendance_service_separates_missing_from_unavailable_and_unlinked(tmp_path):
    store = AttendanceStore(tmp_path / 'accountant.sqlite3')
    arrived = linked_employee(1, 'Вовремя', '10')
    missing = linked_employee(2, 'Нет входа', '20')
    unlinked = linked_employee(3, 'Нет ID', None)
    store.ingest(HikvisionEvent('entry', 's1', '10',
                               datetime(2026, 9, 21, 10, 0, tzinfo=TZ)), arrived.id)
    store.record_success(
        'entry', at=datetime(2026, 9, 22, 0, 5, tzinfo=TZ),
        cursor_at=datetime(2026, 9, 22, 0, 5, tzinfo=TZ),
        covered_from=datetime(2026, 9, 21, 0, 0, tzinfo=TZ),
        covered_through=datetime(2026, 9, 22, 0, 0, tzinfo=TZ))

    snapshot = AttendanceService(store, source='entry', enabled=True, poll_seconds=30).snapshot(
        DAY, [arrived, missing, unlinked], now=datetime(2026, 9, 22, 9, tzinfo=TZ))
    incomplete = AttendanceService(
        AttendanceStore(tmp_path / 'empty.sqlite3'), source='entry', enabled=True,
        poll_seconds=30).snapshot(
            DAY, [missing], now=datetime(2026, 9, 22, 9, tzinfo=TZ))

    assert [row.status for row in snapshot.rows] == ['on_time', 'missing', 'unlinked']
    assert snapshot.complete is True
    assert incomplete.rows[0].status == 'unavailable'
    assert incomplete.complete is False
    assert compute_pay(Decimal('250000'), 'unavailable', exception=False) is None


def test_current_day_without_entry_is_not_final_absence(tmp_path):
    store = AttendanceStore(tmp_path / 'accountant.sqlite3')
    person = linked_employee(1, 'Сотрудник', '10')
    store.record_success(
        'entry', at=datetime(2026, 9, 21, 11, tzinfo=TZ),
        cursor_at=datetime(2026, 9, 21, 11, tzinfo=TZ),
        covered_from=datetime(2026, 9, 21, 0, tzinfo=TZ),
        covered_through=datetime(2026, 9, 21, 11, tzinfo=TZ))

    snapshot = AttendanceService(store, source='entry', enabled=True, poll_seconds=30).snapshot(
        DAY, [person], now=datetime(2026, 9, 21, 11, tzinfo=TZ))

    assert snapshot.rows[0].status == 'unavailable'
    assert snapshot.complete is False


def test_sync_failure_preserves_cursor_and_reports_safe_code(tmp_path):
    store = AttendanceStore(tmp_path / 'accountant.sqlite3')
    first = datetime(2026, 9, 21, 9, tzinfo=TZ)
    store.record_success('entry', at=first, cursor_at=first, covered_from=first,
                         covered_through=first)
    store.record_failure('entry', at=datetime(2026, 9, 21, 10, tzinfo=TZ), code='timeout')

    state = store.sync_state('entry')

    assert state.cursor_at == first
    assert state.last_success_at == first
    assert state.last_error_code == 'timeout'
