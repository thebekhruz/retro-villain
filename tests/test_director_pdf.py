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


def _story_text(snapshot, analysis, monkeypatch):
    """Тексты PDF по порядку: шрифт встраивается подмножеством, и в байтах
    файла кириллицу не найти — читаем то, что уходит в document.build."""
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table
    from retro.modules.director import pdf as module

    captured = {}
    original = SimpleDocTemplate.build

    def build(self, story, **kwargs):
        captured['story'] = list(story)
        captured['footer'] = kwargs['onFirstPage']
        return original(self, story, **kwargs)

    monkeypatch.setattr(SimpleDocTemplate, 'build', build)
    module.render_report_pdf(snapshot, analysis)
    texts = []

    def walk(flowable):
        if isinstance(flowable, Paragraph):
            texts.append(flowable.text)
        elif isinstance(flowable, Table):
            for row in flowable._cellvalues:
                for cell in row:
                    # После сборки ячейка — кортеж флоуаблов, до неё — список.
                    for item in cell if isinstance(cell, (list, tuple)) else [cell]:
                        walk(item)
        else:
            for item in getattr(flowable, '_content', []):
                walk(item)

    for flowable in captured['story']:
        walk(flowable)
    return texts, captured['footer']


def _footer_strings(footer, *, page):
    """Строки, которые футер рисует на холсте страницы."""
    class Canvas:
        def __init__(self):
            self.strings = []

        def __getattr__(self, name):
            if name.startswith('draw'):
                return lambda *args: self.strings.append(args[-1])
            return lambda *args, **kwargs: None

    canvas = Canvas()
    footer(canvas, type('Document', (), {'leftMargin': 10, 'rightMargin': 10, 'page': page})())
    return canvas.strings


REPORT = {
    'period_start': '2026-10-07', 'period_end': '2026-10-16',
    'cash_total': '1200000.00', 'menu_revenue': '1000000.00', 'yandex_revenue': '100000.00',
    'yandex_menu_revenue': '100000.00', 'scope_excluded_revenue': '52000.00',
    'excluded_revenue': {}, 'calculation_version': '2026-09-24',
    'item_metrics': {
        'all': {'Плов': {'revenue': '700000'}, 'Самса': {'revenue': '200000'},
                'Салат (Бехруз)': {'revenue': '100000'}},
        'retro': {'Плов': {'revenue': '700000'}},
        'oxbridge': {'Самса': {'revenue': '200000'}},
        'banquet': {'Салат (Бехруз)': {'revenue': '100000'}},
    },
    'waiter_metrics': {'Музаффар': {'revenue': '1000000', 'gross_profit': '400000', 'margin_percent': '40.00'}},
}
ANALYSIS = {'summary': 'Плов требует проверки.', 'problems': [
    {'subject': 'Самса', 'direction': 'oxbridge', 'priority': 'high', 'action': 'review', 'reason': 'Маржа.'}]}


def test_pdf_shows_menu_revenue_once_without_the_duplicate_paragraph(monkeypatch):
    """T-501: под показателями больше нет абзаца с точными суммами, Яндексом
    и «вне банкетной выборки» — он повторял таблицу. Главная цифра — выручка
    по меню, как на экране; продажи до исключений в PDF не выводятся."""
    texts, _ = _story_text(REPORT, ANALYSIS, monkeypatch)
    joined = '\n'.join(texts)
    assert 'Карточки округлены' not in joined
    assert 'Вне банкетной выборки' not in joined
    assert 'Версия расчёта' not in joined
    assert 'Не вошли в выручку' not in joined
    labels = texts[texts.index('Выручка'):texts.index('Выручка') + 10]
    assert labels == ['Выручка', '1,0 млн', 'Retro', '700 000', 'Retro в школе', '200 000',
                      'Банкет', '100 000', 'Яндекс · оплаты', '100 000']
    assert texts[texts.index('Плов требует проверки.') - 1] == 'Вывод'


def test_pdf_uses_panel_direction_names_and_plain_profit_for_waiters(monkeypatch):
    texts, _ = _story_text(REPORT, ANALYSIS, monkeypatch)
    joined = '\n'.join(texts)
    assert 'Школа · меню' not in joined and 'Oxbridge' not in joined
    assert 'Направление: Retro в школе' in joined
    assert 'Валовая прибыль' not in joined and 'валовая' not in joined
    header = texts.index('Официант')
    assert texts[header:header + 4] == ['Официант', 'Выручка', 'Прибыль', 'Маржа']


def test_pdf_names_excluded_menu_groups_only_when_there_are_any(monkeypatch):
    report = dict(REPORT, excluded_revenue={'ДОСТАВКА ЯНДЕКС': '200000.00'})
    texts, _ = _story_text(report, ANALYSIS, monkeypatch)
    assert 'Не вошли в выручку исключённые группы меню: ДОСТАВКА ЯНДЕКС 200 000 сум.' in texts


def test_old_report_without_menu_revenue_falls_back_to_menu_items(monkeypatch):
    report = {key: value for key, value in REPORT.items() if key not in ('menu_revenue', 'calculation_version')}
    texts, footer = _story_text(report, ANALYSIS, monkeypatch)
    assert texts[texts.index('Выручка') + 1] == '1,0 млн'
    strings = _footer_strings(footer, page=2)
    assert 'Версия расчёта историческая' in strings
    assert 'Страница 2' in strings


def test_calculation_version_moves_to_the_page_footer(monkeypatch):
    _, footer = _story_text(REPORT, ANALYSIS, monkeypatch)
    assert _footer_strings(footer, page=1) == [
        'Retro Milliy - отчёт директора', 'Страница 1', 'Версия расчёта 2026-09-24']
