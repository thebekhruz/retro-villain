"""Снимок «Финансов дня» бухгалтера в Telegram-группу (T-479).

Уходит вечером вслед за снимком кассы (modules/cashier/telegram_report.py),
тем же ботом и в ту же группу. Данные — ответ `/api/accountant/day`, как у
кабинета и Excel-выгрузки «Финансов дня»: остаток на конец дня с
расшифровкой (`cash_lines`, та же, что на листе «Итог»), передача кассира,
операции дня, сейф и подотчёт Шоха, долги. Картинку рисует Pillow тем же
трафаретом, что и кассу.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from retro.modules.cashier.service import TZ
from retro.modules.cashier.telegram_report import (MUTED, PAD, WIDTH, Sheet, Skip, clock, day_title, font,
                                                   money)
from .expense_catalog import ITEMS
from .sheets_export import INFLOW, MONTHLY_ITEM, cash_lines, movement_label


@dataclass(frozen=True)
class AccountantReport:
    day: date
    taken_at: datetime
    data: dict


def background_request(app):
    """Обработчики кабинета читают `request.app.state`: даём им то же без HTTP."""
    return SimpleNamespace(app=app, state=SimpleNamespace(
        request_id='telegram-report', dashboard_user='telegram', dashboard_role='all'))


async def collect(app, day: date, *, now: datetime | None = None) -> AccountantReport:
    from .routes import day_view
    data = await day_view(background_request(app), day)
    if data.get('demo'):
        raise Skip('демо-данные')
    return AccountantReport(day=day, taken_at=(now or datetime.now(TZ)).astimezone(TZ), data=data)


def amount(value) -> Decimal | None:
    return Decimal(str(value)) if value not in (None, '') else None


def closing_balance(report: AccountantReport) -> Decimal | None:
    return amount((report.data['ledger'].get('cash_flow') or {}).get('closing_balance'))


def submitted(report: AccountantReport) -> tuple[str, bool]:
    """Плашка в шапке: сдан ли отчёт дня учредителю."""
    saved = report.data.get('day_report')
    if not saved:
        return 'Отчёт дня не сдан', False
    at = clock(saved.get('submitted_at'))
    return f'Отчёт сдан{" в " + at if at else ""}', not saved.get('changed')


def handover_lines(report: AccountantReport) -> list[tuple[str, Decimal | None]]:
    """Передача кассира — как справка на листе «Итог»."""
    handover = report.data.get('cashier_handover') or {}
    lines = []
    if handover.get('amount') is not None:
        at = clock(handover.get('handed_at'))
        if handover.get('confirmed_at'):
            calculation = handover.get('calculation') or handover.get('expected_amount')
            lines.append(('Расчёт кассы', amount(calculation)))
            lines.append((f'Получено, подтверждено в {clock(handover["confirmed_at"])}', amount(handover['amount'])))
        elif handover.get('checked'):
            if handover.get('calculation') is not None:
                lines.append(('Расчёт кассы', amount(handover['calculation'])))
            lines.append((f'Получено, записано вручную{" в " + at if at else ""}', amount(handover['amount'])))
        else:
            lines.append((f'Передано{" в " + at if at else ""} · не подтверждено', amount(handover['amount'])))
        if (amount(handover.get('shortfall')) or 0) > 0:
            lines.append(('Получено меньше расчёта на', amount(handover['shortfall'])))
    else:
        lines.append(('Передача кассира ещё не записана', None))
    if handover.get('expected') is not None:
        lines.append(('По расчёту кассы (iiko)', amount(handover['expected'])))
    return lines


# Статьи зарплаты, как их называют в группе; остальные — по справочнику.
PAY_LABELS = {'salary_staff': 'ЗП персонал', 'salary_technical': 'ЗП техперсонал',
              'salary_cashier': 'ЗП кассира', MONTHLY_ITEM: 'Оклады'}


def pay_category(item: dict) -> str | None:
    """Статья зарплаты для выплаты сотруднику; None — операция не про людей."""
    code = item.get('item_code')
    if item['type'] not in ('salary_payment', 'other_expense') or code not in ITEMS or ITEMS[code][0] != 'salary':
        return None
    return PAY_LABELS.get(code, ITEMS[code][1])


def operations(report: AccountantReport) -> list[tuple[str, Decimal, bool]]:
    """(подпись, сумма, приход ли) — журнал дня без «остатка на начало».

    Людей на картинке нет: выплаты сотрудникам — по строке на статью
    («ЗП персонал», «ЗП техперсонал»…) с числом выплат. По людям смотрят в
    кабинете и в Excel."""
    rows, pay = [], {}
    for item in report.data['ledger'].get('movements') or []:
        if item['type'] == 'opening':
            continue
        category = pay_category(item)
        if category:
            total, count = pay.get(category, (Decimal(0), 0))
            pay[category] = (total + Decimal(str(item['amount'])), count + 1)
            continue
        # Описание уже называет операцию («Касса за 15.10», «Шох: закуп»);
        # приход это или расход, видно по знаку суммы.
        rows.append((item.get('description') or movement_label(item), Decimal(str(item['amount'])),
                     item['type'] in INFLOW))
    for category, (total, count) in pay.items():
        rows.append((f'{category} · {count} {plural(count, "выплата", "выплаты", "выплат")}', total, False))
    return rows


def monthly_payees(report: AccountantReport) -> list[tuple[str, Decimal]]:
    """Кому выдали оклад за день — полным списком, по имени (T-481).

    Запись оклада называется «Месячная заработная плата · Имя»."""
    payees = []
    for item in report.data['ledger'].get('movements') or []:
        if item['type'] == 'other_expense' and item.get('item_code') == MONTHLY_ITEM:
            description = item.get('description') or ''
            name = description.split(' · ', 1)[1] if ' · ' in description else description
            payees.append((name or 'Без имени', Decimal(str(item['amount']))))
    return payees


def total_income(report: AccountantReport) -> Decimal | None:
    """Общий приход дня (T-481, T-482): остаток на начало дня + «Приход»
    операций (касса за вчера и прочие поступления). Нет остатка на начало
    или кассы — суммы нет: «требует проверки», а не ноль."""
    flow = report.data['ledger'].get('cash_flow') or {}
    opening, cashier = amount(flow.get('opening_balance')), amount(flow.get('received_from_cashier'))
    if opening is None or cashier is None:
        return None
    return opening + sum((value for _, value, incoming in operations(report) if incoming), Decimal(0))


def income_parts(report: AccountantReport) -> list[tuple[str, Decimal | None]]:
    """Из чего сложен «Общий приход»: на начало дня, касса за вчера и — если
    были — прочие поступления."""
    flow = report.data['ledger'].get('cash_flow') or {}
    cashier_day = report.data.get('cashier_date')
    parts = [('На начало дня', amount(flow.get('opening_balance'))),
             (f'Касса за {date.fromisoformat(cashier_day).strftime("%d.%m")}' if cashier_day else 'Касса за вчера',
              amount(flow.get('received_from_cashier')))]
    other = amount(flow.get('other_receipts'))
    if other:
        parts.append(('Прочие поступления', other))
    return parts


def plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    return few if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14 else many


def safe_lines(report: AccountantReport) -> list[tuple[str, str]]:
    """Сейф и подотчёт: доллары — в долларах."""
    data, lines = report.data, []
    reserves = data.get('reserves') or {}
    usd = amount((reserves.get('usd') or {}).get('balance'))
    if usd is not None:
        lines.append(('Доллары в сейфе', f'${money(usd)}'))
    dividends = amount((reserves.get('dividends') or {}).get('balance'))
    if dividends is not None:
        lines.append(('Дивиденды отложены', money(dividends)))
    gives = data.get('cashier_shokh_gives') or {}
    if gives.get('gives'):
        lines.append(('Шоху из кассы · не из остатка', money(gives.get('total'))))
    # «На руках у Шоха» и есть подотчёт по бухгалтерии (shokh.store.pocket_position
    # берёт reserves.shoh.balance) — одной строкой, без повтора (T-483).
    pocket = (data.get('shoh_pocket') or {}).get('pocket')
    if pocket is not None:
        lines.append(('На руках у Шоха на конец дня', money(pocket)))
    return lines


def debt_lines(report: AccountantReport) -> list[tuple[str, str]]:
    ledger, data = report.data['ledger'], report.data
    return [(label, money(value)) for label, value in (
        ('Долг сотрудникам по сменам', ledger.get('salary_debt')),
        ('Долги по расходам', ledger.get('manual_debt_total')),
        ('Перечислено поставщикам (не из кассы)', data.get('supplier_transfers_total'))) if value is not None]


def warnings(report: AccountantReport) -> list[str]:
    notes = []
    flow = report.data['ledger'].get('cash_flow') or {}
    if flow.get('missing_day'):
        missing = date.fromisoformat(flow['missing_day']).strftime('%d.%m.%Y')
        notes.append(f'Остаток не посчитан: нет данных кассира за {missing}.')
    handover = report.data.get('cashier_handover') or {}
    if handover.get('expected_changed'):
        notes.append('Касса изменилась после подтверждения передачи — подтвердите сумму заново.')
    if (amount(handover.get('shortfall')) or 0) > 0:
        notes.append(f'От кассира получено меньше расчёта на {money(handover["shortfall"])} сум.')
    saved = report.data.get('day_report')
    if saved and saved.get('changed'):
        notes.append('День поправили после сдачи отчёта — сдайте его заново.')
    return notes


def caption(report: AccountantReport) -> str:
    label, _ = submitted(report)
    return '\n'.join((f'Бухгалтер Retro · Финансы дня · {day_title(report.day)}',
                      f'Остаток на конец дня: {money(closing_balance(report))} сум',
                      label))


RENAMED = {'− Прочие расходы и сейф': '− Прочие расходы'}


def render(report: AccountantReport) -> bytes:
    sheet = Sheet()
    sheet.header('RETRO MILLIY · БУХГАЛТЕР · ФИНАНСЫ ДНЯ', report.day, report.taken_at, submitted(report))
    sheet.notes(warnings(report))

    # Деньги на расходы: остаток на конец дня и как он получился.
    rows = []
    for label, value in cash_lines(report.data):
        if label.startswith('='):
            continue
        # На картинке — просто «Прочие расходы» (T-481); в Excel подпись прежняя.
        label = RENAMED.get(label, label)
        rows.append((label, money(value), value is not None and value == 0, None))
    sheet.dark_card('ОСТАТОК НА КОНЕЦ ДНЯ', closing_balance(report), rows)

    group_font, item_font = font(30, True), font(28)

    def listing(title, lines):
        sheet.heading(title)
        for label, value in lines:
            sheet.row(label, value, left_font=item_font, right_font=font(28, True))
        sheet.y += 20

    listing('Передача кассира', [(label, money(value) if value is not None else '—')
                                 for label, value in handover_lines(report)])

    sheet.heading('Операции за день')
    journal = operations(report)
    if not journal:
        sheet.row('Операций за день нет', '', left_font=item_font, right_font=item_font, left_fill=MUTED)
    payees = monthly_payees(report)
    for label, value, incoming in journal:
        sheet.row(label, ('+ ' if incoming else '− ') + money(value), left_font=item_font, right_font=item_font,
                  left_fill=MUTED, right_fill='#4a8056' if incoming else MUTED)
        if label.startswith(PAY_LABELS[MONTHLY_ITEM] + ' · '):
            # Оклады — полным списком: кому и сколько (T-481).
            for name, paid in payees:
                sheet.row(name, money(paid), left_font=font(25), right_font=font(25), left_fill=MUTED,
                          right_fill=MUTED, x0=PAD + 32, x1=WIDTH - PAD - 150, height=38)
    outflow = sum((value for _, value, incoming in journal if not incoming), Decimal(0))
    sheet.y += 6
    sheet.rule()
    sheet.y += 14
    # Итог: «Общий приход» с разбивкой (на начало дня, касса за вчера, прочие
    # поступления) и «Расход» тем же шрифтом. Отдельного «Прихода» нет (T-482).
    sheet.row('Общий приход', money(total_income(report)), left_font=group_font, right_font=group_font)
    for label, value in income_parts(report):
        sheet.row(label, money(value), left_font=font(25), right_font=font(25), left_fill=MUTED,
                  right_fill=MUTED, x0=PAD + 32, height=38)
    sheet.y += 8
    sheet.row('Расход', money(outflow), left_font=group_font, right_font=group_font)
    sheet.y += 24

    safe = safe_lines(report)
    if safe:
        listing('Сейф и подотчёт', safe)
    debts = debt_lines(report)
    if debts:
        listing('Долги', debts)

    sheet.y += 6
    sheet.footer('Расчёт как в кабинете бухгалтера · «Финансы дня»')
    return sheet.done()
