from retro.modules.director.pdf import render_report_pdf


def test_director_pdf_contains_report_summary():
    snapshot = {
        'period_start': '2026-09-08',
        'period_end': '2026-09-17',
        'cash_total': '1000000',
        'yandex_revenue': '100000',
        'item_metrics': {
            'retro': {'Плов': {'revenue': '700000'}},
            'oxbridge': {'Самса': {'revenue': '200000'}},
            'banquet': {'Салат (Бехруз)': {'revenue': '100000'}},
        },
        'waiter_metrics': {
            'Музаффар': {
                'revenue': '1000000', 'gross_profit': '400000', 'margin_percent': '40.00',
            },
        },
    }
    analysis = {
        'summary': 'Плов требует проверки.',
        'problems': [{
            'subject': 'Плов', 'direction': 'retro', 'priority': 'high',
            'action': 'review', 'reason': 'Отрицательная маржа.',
        }],
    }
    pdf = render_report_pdf(snapshot, analysis)
    assert pdf.startswith(b'%PDF-')
    assert b'/ToUnicode' in pdf
    assert pdf.count(b'/Type /Page') >= 2


def test_report_font_travels_with_the_code():
    """На хостинге системных шрифтов нет вовсе: отчёт собирался только на
    машине разработчика, а кнопка на сервере падала."""
    from retro.modules.director.pdf import BUNDLED_FONT
    assert BUNDLED_FONT.exists()
    assert BUNDLED_FONT.with_name('DejaVuSans-Bold.ttf').exists()
