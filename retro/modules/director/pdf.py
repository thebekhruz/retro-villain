from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table


# Кириллицу в PDF рисует только подключённый TTF, а лежит он в разных местах:
# на сервере это DejaVu из пакета шрифтов, на рабочем ноутбуке — системный
# Arial Unicode. Отчёт не должен падать из-за того, где его открыли.
CYRILLIC_FONTS = (
    Path('/usr/share/fonts/TTF/DejaVuSans.ttf'),
    Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
    Path('/Library/Fonts/DejaVuSans.ttf'),
    Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf'),
)


def _cyrillic_font():
    for candidate in CYRILLIC_FONTS:
        if candidate.exists():
            return candidate
    raise RuntimeError('Не найден шрифт с кириллицей для PDF.')


def render_report_pdf(snapshot, analysis):
    font = _cyrillic_font()
    pdfmetrics.registerFont(TTFont('DejaVuSans', str(font)))
    stream = BytesIO()
    document = SimpleDocTemplate(stream, pagesize=A4, title='Retro Milliy - отчёт директора')
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = 'DejaVuSans'
    body = [Paragraph('Retro Milliy - отчёт директора', styles['Title']), Spacer(1, 18)]
    body.append(Paragraph('Период: %s - %s' % (snapshot['period_start'], snapshot['period_end']),
                          styles['BodyText']))
    body.append(Paragraph('Выручка касс: %s сум' % snapshot.get('cash_total', '—'), styles['BodyText']))
    body.append(Spacer(1, 12))
    body.append(Paragraph('Вывод: ' + analysis['summary'], styles['BodyText']))
    problems = analysis.get('problems', [])
    if problems:
        body.append(Spacer(1, 12))
        body.append(Paragraph('Топ проблем меню', styles['Heading2']))
        body.append(Table([['Позиция', 'Причина', 'Действие']] +
                          [[entry['subject'], entry['reason'], entry['action']] for entry in problems]))
    waiters = snapshot.get('waiter_metrics', {})
    if waiters:
        body.append(Spacer(1, 12))
        body.append(Paragraph('Официанты', styles['Heading2']))
        body.append(Table([['Официант', 'Выручка', 'Маржа']] +
                          [[name, value['revenue'], value['margin_percent'] or '—']
                           for name, value in waiters.items()]))
    document.build(body)
    return stream.getvalue()
