from retro.modules.director.pdf import render_report_pdf


def test_director_pdf_contains_report_summary():
    pdf = render_report_pdf({'period_start': '2026-09-08', 'period_end': '2026-09-17',
                             'cash_total': '1000000'}, {'summary': 'Плов требует проверки'})
    assert pdf.startswith(b'%PDF-')
