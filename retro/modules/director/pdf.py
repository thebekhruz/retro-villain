from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def render_report_pdf(snapshot, analysis):
    font = Path('/usr/share/fonts/TTF/DejaVuSans.ttf')
    if not font.exists():
        raise RuntimeError('Не найден шрифт DejaVu Sans для русского PDF.')
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
    document.build(body)
    return stream.getvalue()
