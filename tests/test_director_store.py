from pathlib import Path

from retro.modules.director.store import DirectorReportStore


def test_store_keeps_pdf_for_saved_report(tmp_path: Path):
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    report_id = store.create({'period_start': '2026-09-08', 'period_end': '2026-09-17'},
                             {'summary': 'Проверить меню'}, b'%PDF-1.4', '2026-09-18T13:00:00+05:00')
    assert store.get_pdf(report_id) == b'%PDF-1.4'
