from pathlib import Path
from datetime import datetime, timedelta

from retro.modules.director.store import DirectorReportStore


def test_store_keeps_pdf_for_saved_report(tmp_path: Path):
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    report_id = store.create({'period_start': '2026-09-08', 'period_end': '2026-09-17'},
                             {'summary': 'Проверить меню'}, b'%PDF-1.4', '2026-09-18T13:00:00+05:00')
    assert store.get_pdf(report_id) == b'%PDF-1.4'


def report_snapshot(start, end):
    return {'period_start': start, 'period_end': end, 'cash_total': '1'}


def test_same_period_replaces_without_duplicate(tmp_path):
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    first = store.create_or_replace(
        report_snapshot('2026-09-01', '2026-09-10'), {'summary': 'one'}, b'one',
        '2026-09-11T10:00:00+05:00', 10)
    second = store.create_or_replace(
        report_snapshot('2026-09-01', '2026-09-10'), {'summary': 'two'}, b'two',
        '2026-09-11T11:00:00+05:00', 10)

    assert second == first
    assert len(store.list_metadata()) == 1
    assert store.get(first)['analysis']['summary'] == 'two'
    assert store.get_for_period('2026-09-01', '2026-09-10')['id'] == first
    assert store.get_for_period('2026-01-01', '2026-01-10') is None


def test_report_retention_keeps_newest_periods(tmp_path):
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    created = datetime.fromisoformat('2026-09-01T10:00:00+05:00')
    for index in range(3):
        start = datetime(2026, 9, 1).date() + timedelta(days=index * 10)
        store.create_or_replace(
            report_snapshot(start.isoformat(), (start + timedelta(days=9)).isoformat()),
            {'summary': str(index)}, str(index).encode(),
            (created + timedelta(days=index)).isoformat(), 2)

    assert [row['analysis_summary'] for row in store.list_metadata()] == ['2', '1']
