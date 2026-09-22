from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# Шрифт лежит в репозитории: на хостинге системных шрифтов нет вовсе, и
# отчёт не собирался — кнопка «Сформировать отчёт» падала с ошибкой про
# отсутствующий шрифт. Системные пути оставлены запасными.
BUNDLED_FONT = Path(__file__).resolve().parent.parent.parent / 'assets' / 'DejaVuSans.ttf'
CYRILLIC_FONTS = (
    BUNDLED_FONT,
    Path('/usr/share/fonts/TTF/DejaVuSans.ttf'),
    Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
    Path('/Library/Fonts/DejaVuSans.ttf'),
    Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf'),
)
CYRILLIC_BOLD_FONTS = (
    BUNDLED_FONT.with_name('DejaVuSans-Bold.ttf'),
    Path('/usr/share/fonts/TTF/DejaVuSans-Bold.ttf'),
    Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    Path('/Library/Fonts/DejaVuSans-Bold.ttf'),
    Path('/System/Library/Fonts/Supplemental/Arial Unicode Bold.ttf'),
)

INK = colors.HexColor('#173D35')
MUTED = colors.HexColor('#66776F')
GOLD = colors.HexColor('#B88A45')
PALE = colors.HexColor('#F3F6F1')
PALE_GOLD = colors.HexColor('#FBF5E9')
LINE = colors.HexColor('#DDE5DE')
WHITE = colors.white

DIRECTION_LABELS = {
    'all': 'Все направления',
    'retro': 'Retro',
    'oxbridge': 'Oxbridge',
    'banquet': 'Банкет',
    'yandex': 'Яндекс',
}
PRIORITY_LABELS = {'high': 'Высокий', 'medium': 'Средний', 'low': 'Низкий'}
ACTION_LABELS = {
    'remove': 'Убрать',
    'replace': 'Заменить',
    'promote': 'Продвигать',
    'review': 'Проверить',
}


def _font(candidates):
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError('Не найден шрифт с кириллицей для PDF.')


def _register_fonts():
    regular = _font(CYRILLIC_FONTS)
    bold = next((candidate for candidate in CYRILLIC_BOLD_FONTS if candidate.exists()), regular)
    pdfmetrics.registerFont(TTFont('RetroSans', str(regular)))
    pdfmetrics.registerFont(TTFont('RetroSans-Bold', str(bold)))


def _styles():
    base = getSampleStyleSheet()
    return {
        'title': ParagraphStyle(
            'RetroTitle', parent=base['Title'], fontName='RetroSans-Bold', fontSize=23,
            leading=28, textColor=INK, spaceAfter=5 * mm,
        ),
        'subtitle': ParagraphStyle(
            'RetroSubtitle', parent=base['BodyText'], fontName='RetroSans', fontSize=9,
            leading=13, textColor=MUTED,
        ),
        'section': ParagraphStyle(
            'RetroSection', parent=base['Heading2'], fontName='RetroSans-Bold', fontSize=14,
            leading=18, textColor=INK, spaceBefore=5 * mm, spaceAfter=3 * mm,
        ),
        'body': ParagraphStyle(
            'RetroBody', parent=base['BodyText'], fontName='RetroSans', fontSize=9.5,
            leading=14, textColor=colors.HexColor('#25332E'),
        ),
        'metric_label': ParagraphStyle(
            'MetricLabel', parent=base['BodyText'], fontName='RetroSans', fontSize=7.5,
            leading=10, textColor=MUTED, alignment=TA_CENTER,
        ),
        'metric_value': ParagraphStyle(
            'MetricValue', parent=base['BodyText'], fontName='RetroSans-Bold', fontSize=11,
            leading=14, textColor=INK, alignment=TA_CENTER,
        ),
        'problem_title': ParagraphStyle(
            'ProblemTitle', parent=base['BodyText'], fontName='RetroSans-Bold', fontSize=10,
            leading=13, textColor=INK,
        ),
        'problem_meta': ParagraphStyle(
            'ProblemMeta', parent=base['BodyText'], fontName='RetroSans', fontSize=7.5,
            leading=10, textColor=GOLD,
        ),
        'table_header': ParagraphStyle(
            'TableHeader', parent=base['BodyText'], fontName='RetroSans-Bold', fontSize=7.5,
            leading=10, textColor=WHITE,
        ),
        'table_cell': ParagraphStyle(
            'TableCell', parent=base['BodyText'], fontName='RetroSans', fontSize=8,
            leading=10.5, textColor=colors.HexColor('#25332E'),
        ),
        'table_number': ParagraphStyle(
            'TableNumber', parent=base['BodyText'], fontName='RetroSans', fontSize=8,
            leading=10.5, textColor=colors.HexColor('#25332E'), alignment=TA_RIGHT,
        ),
    }


def _text(value):
    normalized = str(value or '').strip().translate({
        ord('\u2010'): '-', ord('\u2011'): '-', ord('\u2012'): '-',
        ord('\u2013'): '-', ord('\u2014'): '-', ord('\u2212'): '-',
    })
    return escape(normalized)


def _number(value, *, suffix=''):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return '-'
    if amount == amount.to_integral_value():
        rendered = f'{int(amount):,}'.replace(',', ' ')
    else:
        rendered = f'{amount:,.2f}'.replace(',', ' ').replace('.', ',')
    return rendered + suffix


def _compact_money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return '-'
    if abs(amount) >= Decimal('1000000'):
        rendered = f'{amount / Decimal("1000000"):.1f}'.replace('.', ',')
        return rendered + ' млн'
    return _number(amount)


def _period(snapshot):
    try:
        start = date.fromisoformat(snapshot['period_start']).strftime('%d.%m.%Y')
        end = date.fromisoformat(snapshot['period_end']).strftime('%d.%m.%Y')
    except (KeyError, TypeError, ValueError):
        return 'Период не указан'
    return f'{start} - {end}'


def _group_revenue(snapshot, name):
    values = snapshot.get('item_metrics', {}).get(name, {}).values()
    total = Decimal(0)
    for metric in values:
        try:
            total += Decimal(str(metric.get('revenue', 0)))
        except (InvalidOperation, TypeError, ValueError, AttributeError):
            continue
    return total


def _page(canvas, document):
    canvas.saveState()
    width, _ = A4
    canvas.setStrokeColor(LINE)
    canvas.line(document.leftMargin, 15 * mm, width - document.rightMargin, 15 * mm)
    canvas.setFont('RetroSans', 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(document.leftMargin, 10 * mm, 'Retro Milliy - отчёт директора')
    canvas.drawRightString(width - document.rightMargin, 10 * mm, f'Страница {document.page}')
    canvas.restoreState()


def _metric_table(snapshot, styles):
    metrics = [
        ('Всего', snapshot.get('cash_total', 0)),
        ('Retro', _group_revenue(snapshot, 'retro')),
        ('Oxbridge', _group_revenue(snapshot, 'oxbridge')),
        ('Банкет', _group_revenue(snapshot, 'banquet')),
        ('Яндекс', snapshot.get('yandex_revenue', 0)),
    ]
    cells = []
    for label, value in metrics:
        cells.append([
            Paragraph(_text(label), styles['metric_label']),
            Paragraph(_compact_money(value), styles['metric_value']),
        ])
    table = Table([cells], colWidths=[(A4[0] - 36 * mm) / len(cells)])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PALE),
        ('BOX', (0, 0), (-1, -1), 0.6, LINE),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4 * mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4 * mm),
        ('TOPPADDING', (0, 0), (-1, -1), 3 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3 * mm),
    ]))
    return table


def _problem_block(index, problem, styles):
    direction = DIRECTION_LABELS.get(problem.get('direction'), problem.get('direction', '-'))
    priority = PRIORITY_LABELS.get(problem.get('priority'), problem.get('priority', '-'))
    action = ACTION_LABELS.get(problem.get('action'), problem.get('action', '-'))
    meta = (f'Направление: {direction}   •   Приоритет: {priority}   '
            f'•   Действие: {action}')
    content = [[
        Paragraph(f'{index}. {_text(problem.get("subject", "Без названия"))}',
                  styles['problem_title']),
    ], [
        Paragraph(_text(meta), styles['problem_meta']),
    ], [
        Paragraph(_text(problem.get('reason', '-')), styles['body']),
    ]]
    block = Table(content, colWidths=[A4[0] - 36 * mm])
    block.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PALE_GOLD),
        ('BOX', (0, 0), (-1, -1), 0.6, colors.HexColor('#E9D8B7')),
        ('LEFTPADDING', (0, 0), (-1, -1), 4 * mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4 * mm),
        ('TOPPADDING', (0, 0), (-1, 0), 3 * mm),
        ('TOPPADDING', (0, 1), (-1, -1), 1 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, 1), 1 * mm),
        ('BOTTOMPADDING', (0, -1), (-1, -1), 3 * mm),
    ]))
    return KeepTogether([block, Spacer(1, 2 * mm)])


def _waiter_table(waiters, styles):
    rows = [[
        Paragraph('Официант', styles['table_header']),
        Paragraph('Выручка', styles['table_header']),
        Paragraph('Валовая прибыль', styles['table_header']),
        Paragraph('Маржа', styles['table_header']),
    ]]
    ordered = sorted(waiters.items(), key=lambda item: Decimal(str(item[1].get('revenue', 0))),
                     reverse=True)
    for name, value in ordered:
        margin = value.get('margin_percent')
        rows.append([
            Paragraph(_text(name), styles['table_cell']),
            Paragraph(_number(value.get('revenue'), suffix=' сум'), styles['table_number']),
            Paragraph(_number(value.get('gross_profit'), suffix=' сум'), styles['table_number']),
            Paragraph(_number(margin, suffix='%') if margin is not None else '-',
                      styles['table_number']),
        ])
    table = Table(rows, colWidths=[60 * mm, 39 * mm, 43 * mm, 22 * mm], repeatRows=1)
    commands = [
        ('BACKGROUND', (0, 0), (-1, 0), INK),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 0.6, LINE),
        ('INNERGRID', (0, 1), (-1, -1), 0.35, LINE),
        ('LEFTPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('TOPPADDING', (0, 0), (-1, -1), 2.2 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.2 * mm),
    ]
    for index in range(1, len(rows)):
        if index % 2 == 0:
            commands.append(('BACKGROUND', (0, index), (-1, index), PALE))
    table.setStyle(TableStyle(commands))
    return table


def render_report_pdf(snapshot, analysis):
    _register_fonts()
    styles = _styles()
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=A4,
        title='Retro Milliy - отчёт директора',
        author='Retro Milliy',
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=22 * mm,
    )
    story = [
        Paragraph('Retro Milliy', styles['title']),
        Paragraph(f'Отчёт директора   •   {_period(snapshot)}', styles['subtitle']),
        Spacer(1, 6 * mm),
        Paragraph('Выручка по направлениям, сум', styles['subtitle']),
        Spacer(1, 2 * mm),
        _metric_table(snapshot, styles),
        Paragraph('Вывод Claude', styles['section']),
        Paragraph(_text(analysis.get('summary', 'Вывод не сформирован.')), styles['body']),
    ]
    problems = analysis.get('problems', [])
    if problems:
        story.append(Paragraph('Проблемы и действия', styles['section']))
        story.extend(_problem_block(index, problem, styles)
                     for index, problem in enumerate(problems, start=1))
    waiters = snapshot.get('waiter_metrics', {})
    if waiters:
        story.extend([
            PageBreak(),
            Paragraph('Официанты', styles['section']),
            Paragraph('Выручка и валовая прибыль за тот же период.', styles['subtitle']),
            Spacer(1, 3 * mm),
            _waiter_table(waiters, styles),
        ])
    document.build(story, onFirstPage=_page, onLaterPages=_page)
    return stream.getvalue()
