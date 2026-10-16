"""T-479: снимок «Финансов дня» бухгалтера в Telegram — те же числа, что лист «Итог»."""
import asyncio
from datetime import datetime
from decimal import Decimal
from io import BytesIO

from PIL import Image

from retro.modules.accountant import telegram_report as accountant
from retro.modules.accountant.sheets_export import cash_lines
from retro.modules.cashier.service import TZ
from test_accountant_design_parity import DAY, cash, client, day_json, pay_monthly, workbook

TAKEN = datetime(2026, 9, 16, 23, 30, tzinfo=TZ)


def filled_day(c, staff=True):
    cash(c)
    finance = c.app.state.accountant_finance
    for name, salary in (('Азиз', '8000000'), ('Лина', '6000000')) if staff else ():
        person = c.app.state.accountant_roster.add_monthly(name=name, role='менеджер', salary=salary)
        assert pay_monthly(c, person.id, '1000000').status_code == 201
    finance.give_procurement(DAY, 'Шох', 'закуп за день', '700000', cashier_amount=Decimal('5000000'))
    finance.add_expense(DAY, 'admin_other', 'Канцтовары', '240000', cashier_amount=Decimal('5000000'))
    finance.add_income(DAY, 'income_other', 'Возврат', '50000')
    finance.reserve_entry(DAY, 'usd', 'opening', '350', 'Пересчитано в сейфе')


def test_snapshot_matches_the_total_sheet(tmp_path):
    with client(tmp_path) as c:
        filled_day(c)
        report = asyncio.run(accountant.collect(c.app, DAY, now=TAKEN))
        assert report.data == day_json(c) | {'cashier_handover': report.data['cashier_handover']}
        book = workbook(c.get('/api/accountant/day/export', params={'date': DAY.isoformat()}))
        total = {row[0].value: row[1].value for row in book['Итог'].iter_rows(min_row=3) if row[0].value}
        # Расшифровка остатка на картинке — та же, что лист «Итог».
        for label, value in cash_lines(report.data):
            assert total[label] == value
        assert accountant.closing_balance(report) == total['= Остаток на конец дня']

        journal = accountant.operations(report)
        assert ('Шох: закуп за день', Decimal('700000'), False) in journal
        assert ('Прочие поступления · Возврат', Decimal('50000'), True) in journal
        assert ('Касса за 15.09.2026', Decimal('5000000'), True) in journal
        # Людей на картинке нет: оклады обоих — одной строкой статьи.
        assert ('Оклады · 2 выплаты', Decimal('2000000'), False) in journal
        assert not any('Азиз' in label or 'Лина' in label for label, _, _ in journal)
        assert sum(value for _, value, incoming in journal if not incoming) == Decimal('2940000')
        # Оклады — полным списком по именам (T-481).
        assert accountant.monthly_payees(report) == [('Азиз', Decimal('1000000')), ('Лина', Decimal('1000000'))]
        # Общий приход = на начало дня + приход дня (касса за вчера и прочие поступления).
        assert accountant.total_income(report) == Decimal('1000000') + Decimal('5000000') + Decimal('50000')
        # Разбивка под «Общим приходом» складывается в него.
        assert accountant.income_parts(report) == [('На начало дня', Decimal('1000000')),
                                                   ('Касса за 15.09', Decimal('5000000')),
                                                   ('Прочие поступления', Decimal('50000'))]
        assert sum(value for _, value in accountant.income_parts(report)) == accountant.total_income(report)
        assert accountant.RENAMED['− Прочие расходы и сейф'] == '− Прочие расходы'
        # Доллары в сейфе — в долларах.
        assert ('Доллары в сейфе', '$350') in accountant.safe_lines(report)
        # «На руках у Шоха» = подотчёт по бухгалтерии: строка одна (T-483).
        labels = [label for label, _ in accountant.safe_lines(report)]
        assert 'На руках у Шоха на конец дня' in labels and 'Подотчёт Шоха по бухгалтерии' not in labels

        text = accountant.caption(report)
        assert text.splitlines()[0] == 'Бухгалтер Retro · Финансы дня · 16.09.2026 · среда'
        assert 'Отчёт дня не сдан' in text
        image = Image.open(BytesIO(accountant.render(report)))
        assert image.format == 'PNG' and image.width == 1080


def test_salary_payments_group_by_category_without_names():
    movement = lambda kind, code, amount, name: dict(type=kind, item_code=code, amount=amount,
                                                    description=f'{name} · за 15.09')
    data = {'ledger': {'movements': [
        movement('salary_payment', 'salary_staff', '300000', 'Азиз'),
        movement('salary_payment', 'salary_staff', '250000', 'Лина'),
        movement('salary_payment', 'salary_technical', '130000', 'Гульшан'),
        movement('other_expense', 'admin_other', '40000', 'Канцтовары'),
    ]}}
    report = accountant.AccountantReport(DAY, TAKEN, data)
    assert accountant.operations(report) == [
        ('Канцтовары · за 15.09', Decimal('40000'), False),
        ('ЗП персонал · 2 выплаты', Decimal('550000'), False),
        ('ЗП техперсонал · 1 выплата', Decimal('130000'), False)]


def test_empty_day_still_renders(tmp_path):
    with client(tmp_path) as c:
        report = asyncio.run(accountant.collect(c.app, DAY, now=TAKEN))
        assert accountant.operations(report) == []
        assert accountant.monthly_payees(report) == []
        # Кассы за вчера нет — общего прихода тоже нет, а не ноль.
        assert accountant.total_income(report) is None
        assert Image.open(BytesIO(accountant.render(report))).width == 1080


def test_preview_route_is_admin_only(tmp_path):
    with client(tmp_path, dashboard_panel_users={'buh': ('secret', 'accountant'),
                                                 'boss': ('secret', 'admin')}) as c:
        filled_day(c, staff=False)
        url = f'/api/accountant/telegram-report/preview?date={DAY.isoformat()}'
        assert c.get(url, auth=('buh', 'secret')).status_code == 403
        answer = c.get(url, auth=('boss', 'secret'))
        assert answer.status_code == 200 and answer.headers['content-type'] == 'image/png'
        sent = c.post(f'/api/accountant/telegram-report?date={DAY.isoformat()}', auth=('boss', 'secret'),
                      headers={'Origin': 'http://127.0.0.1'})
        assert sent.status_code == 409  # бот не настроен
