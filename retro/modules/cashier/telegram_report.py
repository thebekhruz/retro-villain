"""Снимок кассы дня в Telegram-группу (T-478).

Каждый вечер в 23:30 по Ташкенту (TELEGRAM_REPORT_TIME) сервер сам собирает
день так же, как его видит кассир: свежий снимок iiko, предоплаты, расходы,
поступления, выдачи на закуп, доллары и «К передаче бухгалтеру», рисует из
этого PNG и отправляет боту в группу. Браузера на сервере нет: картинку
рисует Pillow из тех же чисел, что отдаёт /api/cashier/day (till_summary
считает «К передаче» за экран и за выгрузку одной формулой).

23:30 — ещё до закрытия смены iiko (её закрывают за полночь), поэтому цифры
вечера предварительные: картинка и подпись говорят об этом, пока смена
открыта. Отправленный день помечается в базе — повторный выкат или второй
процесс не шлют его дважды. Упавшую отправку повторяем каждые 5 минут до
06:00 следующего утра; позже вечерний снимок уже не нужен.

Вслед за кассой уходит снимок «Финансов дня» бухгалтера (T-479,
modules/accountant/telegram_report.py) — отдельной картинкой, со своей
отметкой об отправке.
"""
import asyncio
import logging
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from retro.db import as_database
from retro.logging_config import log_safe_failure
from .expenses import Expense
from .service import DataError, Snapshot, TZ

CATCH_UP_UNTIL = time(6)
RETRY_MINUTES = 5
CLAIM_MINUTES = 10
WEEKDAYS = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')
UNKNOWN = 'требует проверки'
CASH_PAYMENT = 'Демо'

logger = logging.getLogger(__name__)


class Skip(Exception):
    """Отправлять нечего: iiko не настроен или пришли демо-данные."""


# ── Данные дня ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DayReport:
    day: date
    taken_at: datetime
    snapshot: Snapshot
    summary: dict
    expenses: tuple[Expense, ...]
    receipts: tuple[Expense, ...]
    gives: tuple[dict, ...]
    handover: dict | None


async def collect(state, day: date, *, now: datetime | None = None) -> DayReport:
    """День глазами кассира: те же источники, что у /api/cashier/day."""
    from retro.report_cache import load_iiko
    from .routes import with_prepayments
    from .till import handover_check, shokh_gives, till_summary, till_totals

    if not state.settings.configured:
        raise Skip('iiko не настроен')
    # Вечером день ещё живой: берём iiko заново, а не снимок получасовой давности.
    snapshot = await load_iiko(state, 'load', day, refresh=True, timeout=120)
    if snapshot.demo:
        raise Skip('демо-данные')
    try:
        entries = await load_iiko(state, 'load_prepayments', day, refresh=True, timeout=60)
        snapshot = with_prepayments(snapshot, entries)
    except (DataError, TimeoutError):
        pass  # без реестра выручка на картинке честно «требует проверки»

    def local():
        totals = till_totals(state, day)
        summary = till_summary(state, day, snapshot, totals=totals)
        expected = Decimal(summary['handover']) if summary['handover'] is not None else None
        return (summary, tuple(state.expenses.list(day)), tuple(state.expenses.list_receipts(day)),
                tuple(shokh_gives(state.accountant_finance, day)),
                handover_check(state, day, expected))

    summary, expenses, receipts, gives, handover = await asyncio.to_thread(local)
    return DayReport(day=day, taken_at=(now or datetime.now(TZ)).astimezone(TZ), snapshot=snapshot,
                     summary=summary, expenses=expenses, receipts=receipts, gives=gives, handover=handover)


# ── Числа и подписи ──────────────────────────────────────────────────────────

def money(value) -> str:
    """«1 250 000» — как на экране; тийины только если они есть."""
    if value is None:
        return UNKNOWN
    amount = Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    whole = int(abs(amount))
    text = f'{whole:,}'.replace(',', ' ')
    cents = abs(amount) - whole
    if cents:
        text += ',' + f'{cents:.2f}'[2:]
    return ('−' if amount < 0 else '') + text


def day_title(day: date) -> str:
    return f'{day.strftime("%d.%m.%Y")} · {WEEKDAYS[day.weekday()]}'


def clock(value) -> str | None:
    """«22:56» из ISO-времени (с поясом или по Ташкенту без пояса)."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(TZ)
    return moment.strftime('%H:%M')


def shift_text(report: DayReport) -> tuple[str, bool] | None:
    """(«Смена закрыта 22:56», закрыта ли) — как плашка на экране кассира."""
    shift = report.snapshot.shift
    if shift is None:
        return None
    if shift.open:
        return 'Смена открыта', False
    if not shift.closed_at:
        return None
    closed = str(shift.closed_at)
    when = clock(closed)
    if closed[:10] != report.day.isoformat():
        when = f'{closed[8:10]}.{closed[5:7]} {when}'
    return f'Смена закрыта {when}', True


def preliminary(report: DayReport) -> bool:
    shift = report.snapshot.shift
    return shift is None or shift.open


def revenue_numbers(snapshot: Snapshot):
    """Выручка, чеки и средний чек — те же поля, что берёт экран."""
    data = snapshot.json()
    grouped = snapshot.payment_groups is not None
    receipts = data.get('full_receipt_count') if grouped else data.get('receipt_count')
    average = data.get('full_average_receipt') if grouped else data.get('average_receipt')
    return snapshot.daily_revenue, receipts, Decimal(average) if average is not None else None


def payment_sections(snapshot: Snapshot) -> list[tuple[str, Decimal | None, list[tuple[str, Decimal]]]]:
    """Оплаты по кассам iiko и отдельно полученные за день предоплаты.

    Экран раскладывает предоплаты по способам оплаты внутри касс; здесь они
    одной группой — сумма всех групп та же, что «Выручка за день»."""
    sections = []
    if snapshot.payment_groups is not None:
        for group in snapshot.payment_groups:
            rows = [(('Наличные' if item.name == CASH_PAYMENT else item.name), item.paid)
                    for item in group.types if item.paid]
            if rows:
                sections.append((group.name, sum((amount for _, amount in rows), Decimal(0)), rows))
    else:
        rows = [(('Наличные' if item.name == CASH_PAYMENT else item.name), item.amount)
                for item in snapshot.payments if item.amount]
        if rows:
            sections.append(('Оплаты счетов', sum((amount for _, amount in rows), Decimal(0)), rows))
    if snapshot.prepayments is None:
        sections.append(('Предоплаты за день', None, []))
    elif snapshot.prepayments:
        methods: dict[str, Decimal] = {}
        for entry in snapshot.prepayments:
            name = entry.payment_method or 'Способ не указан'
            methods[name] = methods.get(name, Decimal(0)) + entry.amount
        sections.append((f'Предоплаты получены · {len(snapshot.prepayments)}',
                         sum(methods.values(), Decimal(0)), list(methods.items())))
    return sections


def handover_status(report: DayReport) -> tuple[str, str]:
    """(текст, тон: ok | warn | muted) — отметка под «К передаче»."""
    record = report.handover
    if not record or record.get('amount') is None:
        return 'Бухгалтеру ещё не передано', 'muted'
    amount = money(record['amount'])
    if record.get('confirmed_at'):
        return f'Бухгалтер подтвердил: получено {amount} сум', 'ok'
    if record.get('source') not in (None, 'cashier', 'auto'):
        return f'Приход записал бухгалтер: {amount} сум', 'ok'
    at = clock(record.get('handed_at'))
    current = report.summary.get('handover')
    if current is not None and abs(Decimal(current) - Decimal(str(record['amount']))) >= Decimal('0.01'):
        return f'Передано {amount} сум{" в " + at if at else ""} — расчёт с тех пор изменился', 'warn'
    return f'Передано бухгалтеру{" в " + at if at else ""}', 'ok'


def usd_note(summary: dict) -> str:
    """Подпись под долларами: сколько это в сумах и по какому курсу."""
    rate, deduction = summary.get('usd_rate'), summary.get('usd_deduction')
    if rate is None or deduction is None:
        return 'в сумах — нет курса Retro'
    return f'= {money(deduction)} сум по курсу {money(rate)}'


def handover_rows(summary: dict) -> list[tuple[str, str, bool, str | None]]:
    """Формула «К передаче» по шагам. Доллары — в долларах, сумы — подписью
    под строкой: в сейф кладут именно доллары."""
    def step(label, value, sign):
        muted = value is not None and Decimal(value) == 0
        return sign + label, money(value), muted, None
    rows = [step('Наличные', summary.get('demo_cash'), ''),
            step('Предоплаты наличными', summary.get('cash_prepayment'), '+ '),
            step('Поступления', summary.get('receipts'), '+ '),
            step('Расходы', summary.get('expenses'), '− '),
            step('На закуп', summary.get('shokh'), '− ')]
    usd = Decimal(summary.get('usd_total') or 0)
    rows.append(('− Доллары в сейф', f'${money(usd)}', not usd, usd_note(summary) if usd else None))
    return rows


def warnings(report: DayReport) -> list[str]:
    notes = []
    if preliminary(report):
        notes.append('Смена ещё открыта — цифры предварительные, день закроется после полуночи.')
    if report.snapshot.prepayments is None:
        notes.append('Предоплаты за день недоступны — выручка требует проверки.')
    for issue in (report.summary.get('prepayment_issue'), report.summary.get('usd_issue')):
        if issue:
            notes.append(issue)
    return notes


def caption(report: DayReport) -> str:
    revenue, _, _ = revenue_numbers(report.snapshot)
    lines = [f'Касса Retro · {day_title(report.day)}',
             f'Выручка: {money(revenue)} сум',
             f'К передаче бухгалтеру: {money(report.summary.get("handover"))} сум']
    if preliminary(report):
        lines.append('Смена ещё открыта — цифры предварительные.')
    return '\n'.join(lines)


# ── Картинка ─────────────────────────────────────────────────────────────────

ASSETS = Path(__file__).resolve().parents[2] / 'assets'
WIDTH, PAD = 1080, 56
GREEN, GREEN_2, GOLD = '#143e35', '#23574a', '#d8b977'
PAPER, INK, MUTED, BORDER, WHITE = '#f6f7f2', '#1c302b', '#5b675f', '#e3e7df', '#ffffff'
OK, WARN_INK, WARN_BG = '#4a8056', '#806125', '#fbf3e0'


def font(size: int, bold: bool = False):
    return ImageFont.truetype(str(ASSETS / ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')), size)


class Sheet:
    """Холст с курсором сверху вниз; лишнее снизу обрезается в конце."""

    def __init__(self, height: int = 6000):
        self.image = Image.new('RGB', (WIDTH, height), PAPER)
        self.draw = ImageDraw.Draw(self.image)
        self.y = 0

    def width(self, text, face):
        return self.draw.textlength(text, font=face)

    def fit(self, text, face, room):
        if self.width(text, face) <= room:
            return text
        while text and self.width(text + '…', face) > room:
            text = text[:-1]
        return text.rstrip() + '…'

    def wrap(self, text, face, room):
        lines, line = [], ''
        for word in text.split():
            candidate = f'{line} {word}'.strip()
            if self.width(candidate, face) <= room or not line:
                line = candidate
            else:
                lines.append(line)
                line = word
        return lines + ([line] if line else [])

    def row(self, left, right, *, left_font, right_font, left_fill=INK, right_fill=INK,
            x0=PAD, x1=WIDTH - PAD, height=None):
        """Подпись слева, сумма справа, на одной базовой линии."""
        right_width = self.width(right, right_font)
        label = self.fit(left, left_font, x1 - x0 - right_width - 24)
        size = max(left_font.size, right_font.size)
        base = self.y + size
        self.draw.text((x0, base), label, font=left_font, fill=left_fill, anchor='ls')
        self.draw.text((x1, base), right, font=right_font, fill=right_fill, anchor='rs')
        self.y += height or int(size * 1.55)

    def rule(self, color=BORDER, x0=PAD, x1=WIDTH - PAD):
        self.draw.line((x0, self.y, x1, self.y), fill=color, width=2)

    def header(self, eyebrow: str, day: date, taken_at: datetime, pill: tuple[str, bool] | None = None,
               title: str = 'Отчёт за'):
        """Зелёная шапка: чей отчёт, крупно дата отчёта, день недели, плашка."""
        draw = self.draw
        draw.rectangle((0, 0, WIDTH, 300), fill=GREEN)
        draw.text((PAD, 64), eyebrow, font=font(26, True), fill=GOLD, anchor='ls')
        draw.text((PAD, 140), f'{title} {day.strftime("%d.%m.%Y")}', font=font(62, True),
                  fill=WHITE, anchor='ls')
        draw.text((PAD, 192), WEEKDAYS[day.weekday()].capitalize(), font=font(32), fill='#cfdcd6',
                  anchor='ls')
        if pill:
            label, calm = pill
            face = font(26, True)
            width = self.width(label, face) + 44
            draw.rounded_rectangle((PAD, 222, PAD + width, 270), radius=24, fill=GREEN_2 if calm else GOLD)
            draw.text((PAD + 22, 255), label, font=face, fill=WHITE if calm else GREEN, anchor='ls')
        draw.text((WIDTH - PAD, 255), f'снимок {taken_at.strftime("%d.%m %H:%M")}', font=font(24),
                  fill='#cfdcd6', anchor='rs')
        self.y = 300 + 40

    def notes(self, notes: list[str]):
        """Предупреждения — сразу под шапкой, чтобы их не пролистали."""
        if not notes:
            return
        face = font(26)
        lines = [line for note in notes for line in self.wrap(note, face, WIDTH - 2 * PAD - 48)]
        box = 28 + len(lines) * 38 + 14
        self.draw.rounded_rectangle((PAD, self.y, WIDTH - PAD, self.y + box), radius=18, fill=WARN_BG)
        for index, line in enumerate(lines):
            self.draw.text((PAD + 24, self.y + 56 + index * 38), line, font=face, fill=WARN_INK, anchor='ls')
        self.y += box + 32

    def heading(self, text: str):
        self.draw.text((PAD, self.y + 26), text.upper(), font=font(24, True), fill=MUTED, anchor='ls')
        self.y += 48

    def big_number(self, x, y, value, *, fill, unit_fill, unit='сум'):
        number = money(value)
        big = font(72 if value is not None else 48, True)
        self.draw.text((x, y), number, font=big, fill=fill, anchor='ls')
        if value is not None and unit:
            self.draw.text((x + 12 + self.width(number, big), y), unit, font=font(30), fill=unit_fill,
                           anchor='ls')

    def dark_card(self, eyebrow: str, amount, rows, status: tuple[str, str] | None = None):
        """Тёмная карточка итога: число и расшифровка по шагам.

        rows — (подпись, сумма текстом, приглушить, пояснение под строкой | None)."""
        draw = self.draw
        status_lines = self.wrap(status[0], font(26, True), WIDTH - 2 * PAD - 64) if status else []
        height = (180 + len(rows) * 46 + sum(34 for row in rows if row[3]) + 20
                  + (30 + len(status_lines) * 36 if status_lines else 0) + 16)
        top = self.y
        draw.rounded_rectangle((PAD, top, WIDTH - PAD, top + height), radius=22, fill=GREEN)
        draw.text((PAD + 32, top + 56), eyebrow, font=font(24, True), fill=GOLD, anchor='ls')
        self.big_number(PAD + 32, top + 142, amount, fill=WHITE, unit_fill='#cfdcd6')
        self.y = top + 180
        for label, value, muted, note in rows:
            fill = '#8fa69e' if muted else '#e8efe9'
            self.row(label, value, left_font=font(27), right_font=font(27, not muted),
                     left_fill=fill, right_fill=fill, x0=PAD + 32, x1=WIDTH - PAD - 32, height=46)
            if note:
                draw.text((WIDTH - PAD - 32, self.y + 14), note, font=font(23), fill='#a9bdb5', anchor='rs')
                self.y += 34
        if status_lines:
            self.y += 14
            draw.line((PAD + 32, self.y, WIDTH - PAD - 32, self.y), fill=GREEN_2, width=2)
            self.y += 16
            color = {'ok': '#9fd3a8', 'warn': GOLD}.get(status[1], '#cfdcd6')
            for line in status_lines:
                draw.text((PAD + 32, self.y + 28), line, font=font(26, True), fill=color, anchor='ls')
                self.y += 36
        self.y = top + height + 34

    def footer(self, text: str):
        self.draw.text((PAD, self.y + 24), text, font=font(22), fill=MUTED, anchor='ls')
        self.y += 64

    def done(self) -> bytes:
        output = BytesIO()
        self.image.crop((0, 0, WIDTH, self.y)).save(output, 'PNG', optimize=True)
        return output.getvalue()


def render(report: DayReport) -> bytes:
    sheet = Sheet()
    draw = sheet.draw
    revenue, receipts, average = revenue_numbers(report.snapshot)
    summary = report.summary

    # Шапка: дата отчёта — главное на картинке.
    sheet.header('RETRO MILLIY · КАССА', report.day, report.taken_at, shift_text(report))
    sheet.notes(warnings(report))

    # Выручка за день.
    card_top = sheet.y
    draw.rounded_rectangle((PAD, card_top, WIDTH - PAD, card_top + 210), radius=22, fill=WHITE,
                           outline=BORDER, width=2)
    draw.text((PAD + 32, card_top + 56), 'ВЫРУЧКА ЗА ДЕНЬ', font=font(24, True), fill=MUTED, anchor='ls')
    sheet.big_number(PAD + 32, card_top + 138, revenue, fill=GREEN, unit_fill=MUTED)
    parts = []
    if receipts is not None:
        parts.append(f'{receipts} чеков')
    if average is not None:
        parts.append(f'средний чек {money(average.quantize(Decimal(1), rounding=ROUND_HALF_UP))}')
    if parts:
        draw.text((PAD + 32, card_top + 184), ' · '.join(parts), font=font(26), fill=MUTED, anchor='ls')
    sheet.y = card_top + 210 + 44

    heading = sheet.heading
    group_font, group_amount = font(30, True), font(30, True)
    item_font, item_amount = font(28), font(28)

    heading('Оплаты')
    sections = payment_sections(report.snapshot)
    if not sections:
        sheet.row('За этот день оплат нет', '', left_font=item_font, right_font=item_amount, left_fill=MUTED)
    for name, total, rows in sections:
        sheet.row(name, money(total), left_font=group_font, right_font=group_amount,
                  right_fill=WARN_INK if total is None else INK)
        for label, amount in rows:
            sheet.row(label, money(amount), left_font=item_font, right_font=item_amount,
                      left_fill=MUTED, right_fill=MUTED, x0=PAD + 28)
        sheet.y += 10
    redeemed = report.snapshot.redeemed_total
    if redeemed:
        for line in sheet.wrap(f'Зачтено предоплат в счета дня: {money(redeemed)} сум — '
                               'в выручку уже вошли в день получения.', font(24), WIDTH - 2 * PAD):
            draw.text((PAD, sheet.y + 24), line, font=font(24), fill=MUTED, anchor='ls')
            sheet.y += 34
    sheet.y += 24
    sheet.rule()
    sheet.y += 28

    def listing(title, items, empty):
        total = sum((amount for _, amount in items), Decimal(0))
        sheet.row(title, money(total) if items else '0', left_font=group_font, right_font=group_amount)
        if not items:
            sheet.row(empty, '', left_font=item_font, right_font=item_amount, left_fill=MUTED, x0=PAD + 28)
        for label, amount in items:
            sheet.row(label, money(amount), left_font=item_font, right_font=item_amount,
                      left_fill=MUTED, right_fill=MUTED, x0=PAD + 28)
        sheet.y += 14

    heading('Касса за день')
    listing('Расходы наличными', [(item.description, item.amount) for item in report.expenses], 'Расходов нет')
    listing('На закуп', [(f'Шоху{" · " + item["created_at"][11:16] if item.get("created_at") else ""}',
                          Decimal(item['amount'])) for item in report.gives], 'Не выдавали')
    listing('Поступления', [(item.description, item.amount) for item in report.receipts], 'Поступлений нет')
    usd_total = Decimal(summary.get('usd_total') or 0)
    if usd_total:
        rate = summary.get('usd_rate')
        sheet.row('Доллары в сейф', f'${money(usd_total)}', left_font=group_font, right_font=group_amount)
        sheet.row(f'по курсу Retro {money(rate)}' if rate else 'курс Retro не загружен',
                  money(summary.get('usd_deduction')), left_font=item_font, right_font=item_amount,
                  left_fill=MUTED, right_fill=MUTED, x0=PAD + 28)
    sheet.y += 30

    # К передаче бухгалтеру — тёмная карточка, как правая колонка кассира.
    sheet.dark_card('К ПЕРЕДАЧЕ БУХГАЛТЕРУ', summary.get('handover'), handover_rows(summary),
                    handover_status(report))

    fetched = report.snapshot.fetched_at
    footer = f'Данные iiko на {fetched.astimezone(TZ).strftime("%d.%m %H:%M") if fetched else "—"}' \
             ' · расчёт как в кабинете кассира'
    sheet.footer(footer)
    return sheet.done()


# ── Отправка и расписание ────────────────────────────────────────────────────

class SentStore:
    """Какой отчёт за какой день уже ушёл в группу: строка-заявка не даёт двум
    процессам отправить его дважды. Ключ — вид отчёта и день (T-479)."""

    def __init__(self, database):
        self.db = as_database(database)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS telegram_reports (
                kind TEXT NOT NULL, day TEXT NOT NULL, claimed_at TEXT NOT NULL, sent_at TEXT,
                message_id TEXT, PRIMARY KEY(kind, day))''')

    def sent(self, kind: str, day: date) -> bool:
        with closing(self.db.connect()) as connection:
            row = connection.execute('SELECT sent_at FROM telegram_reports WHERE kind = ? AND day = ?',
                                     (kind, day.isoformat())).fetchone()
        return bool(row and row[0])

    def claim(self, kind: str, day: date, now: datetime) -> bool:
        stamp, expired = now.isoformat(), (now - timedelta(minutes=CLAIM_MINUTES)).isoformat()
        with closing(self.db.connect()) as connection, connection:
            inserted = connection.execute(
                'INSERT OR IGNORE INTO telegram_reports (kind, day, claimed_at) VALUES (?, ?, ?)',
                (kind, day.isoformat(), stamp)).rowcount
            if inserted:
                return True
            return connection.execute(
                'UPDATE telegram_reports SET claimed_at = ? WHERE kind = ? AND day = ? '
                'AND sent_at IS NULL AND claimed_at < ?', (stamp, kind, day.isoformat(), expired)).rowcount == 1

    def release(self, kind: str, day: date):
        with closing(self.db.connect()) as connection, connection:
            connection.execute('DELETE FROM telegram_reports WHERE kind = ? AND day = ? AND sent_at IS NULL',
                               (kind, day.isoformat()))

    def finish(self, kind: str, day: date, message_id, now: datetime):
        with closing(self.db.connect()) as connection, connection:
            connection.execute('UPDATE telegram_reports SET sent_at = ?, message_id = ? WHERE kind = ? AND day = ?',
                               (now.isoformat(), str(message_id), kind, day.isoformat()))


def due_day(now: datetime, send_at: time) -> date | None:
    """День, чей вечерний снимок пора слать: сегодняшний после 23:30, а до
    06:00 — вчерашний, если вечером отправка не прошла (выкат, сбой iiko)."""
    local = now.astimezone(TZ)
    if local.time() >= send_at:
        return local.date()
    if local.time() < CATCH_UP_UNTIL and send_at > CATCH_UP_UNTIL:
        return local.date() - timedelta(days=1)
    return None


async def cashier_photo(app, day: date, now: datetime) -> tuple[bytes, str]:
    report = await collect(app.state, day, now=now)
    return await asyncio.to_thread(render, report), caption(report)


async def accountant_photo(app, day: date, now: datetime) -> tuple[bytes, str]:
    from retro.modules.accountant import telegram_report as accountant
    report = await accountant.collect(app, day, now=now)
    return await asyncio.to_thread(accountant.render, report), accountant.caption(report)


# Порядок отправки: сначала касса, потом бухгалтер — как идут деньги.
REPORTS = {'cashier': cashier_photo, 'accountant': accountant_photo}
FILE_NAMES = {'cashier': 'kassa', 'accountant': 'buhgalter'}


class TelegramDailyReport:
    def __init__(self, app, bot, store, config, *, clock=lambda: datetime.now(TZ), reports=None):
        self.app, self.bot, self.store, self.config = app, bot, store, config
        self.clock, self.reports = clock, reports or REPORTS
        self.retry_after: dict[tuple[str, date], datetime] = {}
        self.worker = None

    async def send(self, kind: str, day: date) -> int:
        """Отправка отчёта прямо сейчас — и по расписанию, и для проверки."""
        photo, text = await self.reports[kind](self.app, day, self.clock())
        return await self.bot.send_photo(self.config.chat_id, photo, text,
                                         filename=f'Retro-{FILE_NAMES.get(kind, kind)}-{day.isoformat()}.png')

    async def run_once(self) -> list[tuple[str, date]]:
        now = self.clock()
        day = due_day(now, self.config.send_at)
        if day is None:
            return []
        sent = []
        for kind in self.reports:
            if self.retry_after.get((kind, day), now) > now:
                continue
            if await asyncio.to_thread(self.store.sent, kind, day):
                continue
            if not await asyncio.to_thread(self.store.claim, kind, day, now):
                continue
            try:
                message_id = await self.send(kind, day)
            except Skip:
                await asyncio.to_thread(self.store.finish, kind, day, 'skip', now)
                continue
            except Exception as error:
                await asyncio.to_thread(self.store.release, kind, day)
                self.retry_after[(kind, day)] = now + timedelta(minutes=RETRY_MINUTES)
                log_safe_failure('telegram-report', error, operation=f'send:{kind}:{day.isoformat()}')
                logger.warning('telegram %s report %s failed: %s', kind, day,
                               getattr(error, 'code', type(error).__name__))
                continue
            await asyncio.to_thread(self.store.finish, kind, day, message_id, self.clock())
            self.retry_after.pop((kind, day), None)
            logger.info('telegram %s report %s sent', kind, day)
            sent.append((kind, day))
        return sent

    def start(self, every: float = 60):
        if not self.config.auto:
            return

        async def work():
            await asyncio.sleep(30)  # не мешаем старту и первым запросам после выката
            while True:
                try:
                    await self.run_once()
                except Exception as error:
                    log_safe_failure('telegram-report', error, operation='schedule')
                await asyncio.sleep(every)
        self.worker = asyncio.create_task(work())

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.bot.close()
