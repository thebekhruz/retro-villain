from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def render_report_pdf(snapshot, analysis):
    stream = BytesIO()
    document = SimpleDocTemplate(stream, pagesize=A4, title='Retro Milliy - отчёт директора')
    styles = getSampleStyleSheet()
    body = [Paragraph('Retro Milliy - отчёт директора', styles['Title']), Spacer(1, 18)]
    body.append(Paragraph('Период: %s - %s' % (snapshot['period_start'], snapshot['period_end']),
                          styles['BodyText']))
    body.append(Paragraph('Выручка касс: %s сум' % snapshot.get('cash_total', '—'), styles['BodyText']))
    body.append(Spacer(1, 12))
    body.append(Paragraph('Вывод: ' + analysis['summary'], styles['BodyText']))
    document.build(body)
    return stream.getvalue()
