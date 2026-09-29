from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from retro.integrations.hikvision import HikvisionEvent, HikvisionPerson
from retro.modules.accountant.hikvision import AttendanceService, AttendanceStore
from retro.modules.accountant.payroll import compute_pay
from retro.modules.accountant.roster import (
    Employee, RosterStore, UNASSIGNED_GROUP, UNASSIGNED_ROLE,
)


TZ = ZoneInfo('Asia/Tashkent')
DAY = date(2026, 9, 21)


def add_employee(store, name):
    return store.add(name=name, role='официант', rate='250000', group_name='Обслуживание зала')


def test_unique_name_parts_link_regardless_of_order_and_then_employee_no_wins(tmp_path):
    store = RosterStore(tmp_path / 'accountant.sqlite3')
    aziza = add_employee(store, '  Азиза   Каримова ')
    add_employee(store, 'Бахром Алиев')

    report = store.link_hikvision_people((
        HikvisionPerson('100', 'каримова азиза'),
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


def test_explicit_device_import_creates_missing_people_and_is_idempotent(tmp_path):
    store = RosterStore(tmp_path / 'accountant.sqlite3')
    existing = add_employee(store, 'Азиза Каримова')
    people = (
        HikvisionPerson('100', 'каримова азиза'),
        HikvisionPerson('200', 'Бахром Алиев'),
        HikvisionPerson('300', 'Повтор Имени'),
        HikvisionPerson('301', 'повтор имени'),
        HikvisionPerson('400', None),
    )

    first = store.import_hikvision_people(people)
    second = store.import_hikvision_people(people)
    employees = store.list()

    assert first == {'people': 5, 'created': 1, 'linked': 1,
                     'already_linked': 0, 'ambiguous': 3}
    assert second == {'people': 5, 'created': 0, 'linked': 0,
                      'already_linked': 2, 'ambiguous': 3}
    assert len(employees) == 2
    assert next(row for row in employees if row.id == existing.id).hikvision_id == '100'
    created = next(row for row in employees if row.id != existing.id)
    assert created.name == 'Бахром Алиев'
    assert created.role == UNASSIGNED_ROLE
    assert created.group_name == UNASSIGNED_GROUP
    assert created.rate is None
    assert created.hikvision_id == '200'


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


def covered(store, *, since, until):
    store.record_success('entry', at=until, cursor_at=until, covered_from=since, covered_through=until)


def test_day_before_the_sync_started_can_be_filled_by_hand(tmp_path):
    """Выгрузка идёт только вперёд: за день до её начала входов не будет никогда.

    Без ручной отметки такая смена не начисляется вообще — деньги зависают.
    """
    store = AttendanceStore(tmp_path / 'accountant.sqlite3')
    person = linked_employee(1, 'Нет входа', '20')
    unlinked = linked_employee(2, 'Нет ID', None)
    covered(store, since=datetime(2026, 9, 22, 0, tzinfo=TZ), until=datetime(2026, 9, 23, 9, tzinfo=TZ))
    service = AttendanceService(store, source='entry', enabled=True, poll_seconds=30)
    now = datetime(2026, 9, 23, 9, tzinfo=TZ)

    assert service.manual_markable(DAY, person, now=now) is True
    # Без привязки человека переводят на ручную отметку целиком, а не по дню.
    assert service.manual_markable(DAY, unlinked, now=now) is False
    assert service.snapshot(DAY, [person], now=now).rows[0].status == 'unavailable'

    store.set_manual_mark(person.id, DAY, True, 'Бухгалтер')

    assert service.snapshot(DAY, [person], now=now).rows[0].status == 'manual_present'
    assert compute_pay(Decimal('250000'), 'manual_present', exception=False) == Decimal('250000')


def test_covered_day_keeps_the_device_answer_over_a_stray_mark(tmp_path):
    """День выгружен целиком — «нет прохода» значит «не пришёл», и отметка его не перебьёт."""
    store = AttendanceStore(tmp_path / 'accountant.sqlite3')
    person = linked_employee(1, 'Нет входа', '20')
    arrived = linked_employee(2, 'Вовремя', '10')
    store.ingest(HikvisionEvent('entry', 's1', '10', datetime(2026, 9, 21, 10, 0, tzinfo=TZ)), arrived.id)
    covered(store, since=datetime(2026, 9, 21, 0, tzinfo=TZ), until=datetime(2026, 9, 22, 0, tzinfo=TZ))
    store.set_manual_mark(person.id, DAY, True, 'Бухгалтер')
    service = AttendanceService(store, source='entry', enabled=True, poll_seconds=30)
    now = datetime(2026, 9, 22, 9, tzinfo=TZ)

    assert service.manual_markable(DAY, person, now=now) is False
    assert service.manual_markable(DAY, arrived, now=now) is False
    assert [row.status for row in service.snapshot(DAY, [person, arrived], now=now).rows] == ['missing', 'on_time']


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


def test_paid_attendance_keeps_real_entry_and_lateness(tmp_path):
    store = AttendanceStore(tmp_path / 'attendance.sqlite3')
    person = linked_employee(1, 'Сотрудник', '10')
    at = datetime.combine(DAY, time(10, 35), TZ)
    from retro.modules.accountant.hikvision import FirstEntry
    store.first_entries = lambda day: {person.id: FirstEntry(person.id, '10', at)}
    service = AttendanceService(store, source='entry', enabled=True, poll_seconds=30,
                                paid_employees=lambda day: {person.id})
    row = service.snapshot(DAY, [person], now=at).rows[0]
    assert row.status == 'late'
    assert row.occurred_at == at
