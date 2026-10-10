#!/usr/bin/env python3
"""Сверка сменных зарплат с 02.10.2026 — только чтение (ТЗ 09.10, Б-03).

По дням: кто пришёл на смену (первые входы Hikvision и отметки бухгалтера),
сколько за смену начислено и когда выдано, какие выплаты легли в день
выплаты, общие зарплатные строки журнала («ЗП персонал» и т. п.), оклады и
долг сменным на конец дня. Ниже — находки: смены с посещаемостью без
зарплатных записей, двойной учёт (общая строка и выплаты по людям в один
день), выплаты не на следующий день после смены, удаления зарплатных записей
и сохранность истории 2–5 октября.

Скрипт ничего не пишет и миграций не запускает:
* SQLite — файл базы (и его -wal) копируется во временный каталог, читается
  копия с `PRAGMA query_only`; исходник не открывается вовсе;
* Postgres — одно соединение в транзакции READ ONLY; хранилища приложения не
  создаются (их конструкторы создают таблицы и колонки).

Запуск (лучше — на резервной копии, см. docs/salary-reconciliation-2026-10.md):
  python scripts/reconcile_salaries.py --data-dir /путь/к/data --out /tmp/reconcile
  DATABASE_URL=postgresql://… python scripts/reconcile_salaries.py --out /tmp/reconcile
В каталоге --out появятся salary-reconciliation.md и salary-reconciliation.json.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Только чистые определения: ни одно из этих импортов не открывает базу.
from retro.accounting_period import ACCOUNTING_START  # noqa: E402
from retro.db import is_postgres_url, to_postgres  # noqa: E402
from retro.modules.accountant.attendance import LATE_AFTER  # noqa: E402
from retro.modules.accountant.expense_catalog import ITEMS  # noqa: E402
from retro.modules.accountant.reserves import SHIFT_SALARY_CODES, is_monthly_salary  # noqa: E402
from retro.modules.accountant.salary_day import MANUAL_STATUS  # noqa: E402
from retro.modules.cashier.service import TZ  # noqa: E402

REPORT_NAME = 'salary-reconciliation'
HISTORY_DAYS = (date(2026, 10, 2), date(2026, 10, 5))
HISTORY_ENTITIES = ('salary_payment', 'accrual', 'movement', 'handover', 'debt', 'debt_payment',
                    'payroll_day', 'manual_attendance')
PRESENT = ('on_time', 'late', 'manual_present')


# ── Чтение без записи ───────────────────────────────────────────────────────

class Reader:
    """Одна читающая сессия. SQL — с плейсхолдерами `?`, как в коде хранилищ."""

    def __init__(self, connection, postgres: bool):
        self.connection = connection
        self.postgres = postgres
        self._columns = {}

    def rows(self, sql: str, params=()) -> list[tuple]:
        if self.postgres:
            return self.connection.execute(to_postgres(sql), tuple(params) or None).fetchall()
        return self.connection.execute(sql, tuple(params)).fetchall()

    def tables(self) -> set:
        if self.postgres:
            found = self.rows('SELECT table_name FROM information_schema.tables '
                              'WHERE table_schema = current_schema()')
        else:
            found = self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in found}

    def columns(self, table: str) -> set:
        if table not in self._columns:
            if self.postgres:
                found = self.rows('SELECT column_name FROM information_schema.columns '
                                  'WHERE table_schema = current_schema() AND table_name = ?', (table,))
                self._columns[table] = {row[0] for row in found}
            else:
                self._columns[table] = {row[1] for row in self.rows(f'PRAGMA table_info("{table}")')}
        return self._columns[table]

    def column(self, table: str, name: str, fallback: str = 'NULL') -> str:
        """Колонка, если она есть: у старой базы части колонок нет, а миграций мы не делаем."""
        return name if name in self.columns(table) else fallback


@contextmanager
def open_sqlite(target: Path):
    source = target / 'accountant.sqlite3' if target.is_dir() else target
    if not source.is_file():
        raise SystemExit(f'Нет базы бухгалтера: {source}')
    with tempfile.TemporaryDirectory(prefix='retro-reconcile-') as temporary:
        copy = Path(temporary) / source.name
        shutil.copyfile(source, copy)
        # Незаписанные в файл страницы лежат в -wal: без него копия была бы старее
        # исходника. -shm не берём — SQLite построит его заново по -wal.
        wal = source.with_name(source.name + '-wal')
        if wal.is_file():
            shutil.copyfile(wal, copy.with_name(copy.name + '-wal'))
        connection = sqlite3.connect(copy)
        try:
            connection.execute('PRAGMA query_only = ON')
            yield Reader(connection, postgres=False), dict(kind='sqlite', path=str(source))
        finally:
            connection.close()


@contextmanager
def open_postgres(url: str):
    import psycopg
    connection = psycopg.connect(url, autocommit=False)
    try:
        # Первая команда транзакции: дальше база откажет в любой записи.
        connection.execute('SET TRANSACTION READ ONLY')
        parts = urlsplit(url)
        yield (Reader(connection, postgres=True),
               dict(kind='postgres', host=parts.hostname or '', database=parts.path.lstrip('/')))
    finally:
        connection.rollback()
        connection.close()


# ── Сбор данных ─────────────────────────────────────────────────────────────

def money(value) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, '') else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def plain(value: Decimal) -> str:
    return format(value.normalize(), 'f') if value else '0'


def spaced(value: Decimal) -> str:
    return f'{value:,.0f}'.replace(',', ' ') if value == value.to_integral() else f'{value:,.2f}'.replace(',', ' ')


def dm(day: str) -> str:
    return f'{day[8:10]}.{day[5:7]}'


def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def load(reader: Reader) -> dict:
    has = reader.tables()
    data = dict(employees={}, names={}, entries=[], marks=[], accruals={}, payments=[], movements=[],
                handovers={}, reports=set(), payroll_days=set(), audit=[], missing_tables=[])
    for table in ('accountant_employees', 'accountant_accruals', 'accountant_salary_payments',
                  'accountant_movements', 'accountant_handover_days'):
        if table not in has:
            data['missing_tables'].append(table)
    if 'accountant_employees' in has:
        manual = reader.column('accountant_employees', 'manual_attendance', '0')
        since = reader.column('accountant_employees', 'manual_since')
        for row in reader.rows(f'SELECT id, name, role, group_name, rate, hikvision_id, {manual}, {since} '
                               'FROM accountant_employees'):
            data['employees'][row[0]] = dict(name=row[1], role=row[2], group=row[3], rate=row[4],
                                             linked=row[5] is not None, manual=bool(row[6]),
                                             manual_since=row[7])
            data['names'][row[0]] = row[1]
    if 'accountant_employee_versions' in has:
        # Удалённые в архив: имя — из последней версии реестра.
        for employee_id, name in reader.rows('SELECT employee_id, name FROM accountant_employee_versions '
                                             'ORDER BY effective_day'):
            data['names'].setdefault(employee_id, name)
    if 'hikvision_first_entries' in has:
        data['entries'] = reader.rows('SELECT work_day, employee_id, occurred_at FROM hikvision_first_entries')
    if 'hikvision_manual_absences' in has:
        # До колонки present таблица хранила только отсутствия.
        present = reader.column('hikvision_manual_absences', 'present', '0')
        data['marks'] = reader.rows(f'SELECT work_day, employee_id, {present} FROM hikvision_manual_absences')
    if 'accountant_accruals' in has:
        for row in reader.rows('SELECT id, work_day, employee_id, employee_name, group_name, '
                               'attendance_status, rate, amount FROM accountant_accruals'):
            data['accruals'][row[0]] = dict(id=row[0], work_day=row[1], employee_id=row[2], name=row[3],
                                            group=row[4], status=row[5], rate=row[6], amount=money(row[7]))
            data['names'].setdefault(row[2], row[3])
    if 'accountant_salary_payments' in has:
        created = reader.column('accountant_salary_payments', 'created_at')
        data['payments'] = [dict(id=row[0], accrual_id=row[1], paid_day=row[2], amount=money(row[3]),
                                 created_at=row[4])
                            for row in reader.rows(f'SELECT id, accrual_id, paid_day, amount, {created} '
                                                   'FROM accountant_salary_payments ORDER BY id')]
    if 'accountant_movements' in has:
        code = reader.column('accountant_movements', 'item_code')
        data['movements'] = [dict(id=row[0], day=row[1], kind=row[2], description=row[3],
                                  amount=money(row[4]), item_code=row[5], created_at=row[6])
                             for row in reader.rows(f'SELECT id, day, kind, description, amount, {code}, created_at '
                                                    "FROM accountant_movements WHERE kind = 'other_expense' "
                                                    'ORDER BY day, id')
                             if ITEMS.get(row[5], (None,))[0] == 'salary']
    if 'accountant_handover_days' in has:
        source = reader.column('accountant_handover_days', 'source')
        confirmed = reader.column('accountant_handover_days', 'confirmed_at')
        for day, amount, who, when in reader.rows(f'SELECT day, amount, {source}, {confirmed} '
                                                  'FROM accountant_handover_days'):
            # Как CashBook.flow: подтверждено / записал бухгалтер / ждёт подтверждения.
            status = 'confirmed' if when else 'accountant' if who in (None, 'accountant') else 'pending'
            data['handovers'][day] = dict(amount=money(amount), status=status)
    if 'accountant_day_reports' in has:
        data['reports'] = {row[0] for row in reader.rows('SELECT day FROM accountant_day_reports')}
    if 'accountant_payroll_days' in has:
        data['payroll_days'] = {row[0] for row in reader.rows('SELECT day FROM accountant_payroll_days')}
    if 'accountant_finance_audit' in has:
        for row in reader.rows("SELECT id, entity_type, entity_id, action, before_json, after_json, changed_at "
                               "FROM accountant_finance_audit WHERE action <> 'create' ORDER BY id"):
            data['audit'].append(dict(id=row[0], entity_type=row[1], entity_id=row[2], action=row[3],
                                      before=_json(row[4]), after=_json(row[5]), changed_at=row[6]))
    return data


def _json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def attendance(data: dict, day: str) -> dict:
    """Посещаемость смены: {сотрудник: статус} — как AttendanceService.snapshot.

    Проход Hikvision — вовремя или опоздал (после 10:00); явная отметка
    бухгалтера — был / не был; «отмечается вручную» без отметки — был с даты
    включения. Привязан к Hikvision, но прохода нет — no_entry (не пришёл или
    выгрузки за день нет); без привязки и отметки — unknown."""
    entries = {employee_id: occurred for work_day, employee_id, occurred in data['entries'] if work_day == day}
    marks = {employee_id: bool(present) for work_day, employee_id, present in data['marks'] if work_day == day}
    result = {}
    for employee_id in set(data['employees']) | set(entries) | set(marks):
        person = data['employees'].get(employee_id, {})
        entry, mark = entries.get(employee_id), marks.get(employee_id)
        if mark is not None and (person.get('manual') or entry is None):
            status = 'manual_present' if mark else 'manual_absent'
        elif person.get('manual') and (not person.get('manual_since') or day >= person['manual_since']):
            status = 'manual_present'
        elif entry is not None:
            moment = datetime.fromisoformat(entry).astimezone(TZ).time().replace(tzinfo=None)
            status = 'late' if moment > LATE_AFTER else 'on_time'
        elif person.get('manual'):
            status = 'manual_absent'
        else:
            status = 'no_entry' if person.get('linked') else 'unknown'
        result[employee_id] = status
    return result


def build(data: dict, since: date, until: date) -> dict:
    accruals = data['accruals']
    payments = data['payments']
    by_accrual = defaultdict(list)
    for payment in payments:
        by_accrual[payment['accrual_id']].append(payment)
    days = [(since + timedelta(days=offset)).isoformat() for offset in range((until - since).days + 1)]
    first = since.isoformat()
    name = lambda employee_id: data['names'].get(employee_id, f'сотрудник №{employee_id}')  # noqa: E731
    report_days, findings = [], []

    def finding(kind, level, day, text, **extra):
        findings.append(dict(kind=kind, level=level, day=day, text=text, **extra))

    for day in days:
        # Смена дня: начислено и выдано за неё — в какой бы день ни выдали.
        shift = [item for item in accruals.values() if item['work_day'] == day]
        paid_for_shift = defaultdict(Decimal)
        paid_people = set()
        for item in shift:
            for payment in by_accrual[item['id']]:
                paid_for_shift[payment['paid_day']] += payment['amount']
                if payment['amount'] > 0:
                    paid_people.add(item['employee_id'])
        # День выплаты: выплаты по людям, общие строки журнала, оклады.
        on_day = [payment for payment in payments if payment['paid_day'] == day]
        by_shift = defaultdict(Decimal)
        for payment in on_day:
            accrual = accruals.get(payment['accrual_id'])
            by_shift[accrual['work_day'] if accrual else '?'] += payment['amount']
        general = [row for row in data['movements'] if row['day'] == day and row['item_code'] in SHIFT_SALARY_CODES]
        monthly = [row for row in data['movements'] if row['day'] == day and is_monthly_salary(row['item_code'])]
        # Долг сменным на конец дня — как ledger.accruals: начислено с 02.10 по день
        # минус выданное по день включительно.
        debt = sum((item['amount'] - sum((payment['amount'] for payment in by_accrual[item['id']]
                                          if payment['paid_day'] <= day), Decimal(0))
                    for item in accruals.values() if first <= item['work_day'] <= day), Decimal(0))
        statuses = attendance(data, day)
        present = {employee_id for employee_id, status in statuses.items() if status in PRESENT}
        counts = defaultdict(int)
        for status in statuses.values():
            counts[status] += 1
        cash = data['handovers'].get(day)
        report_days.append(dict(
            day=day,
            attendance=dict(present=len(present), **{key: counts[key] for key in (
                'on_time', 'late', 'manual_present', 'manual_absent', 'no_entry', 'unknown')}),
            shift=dict(accrued_count=len(shift), accrued=plain(sum((item['amount'] for item in shift), Decimal(0))),
                       manual_count=sum(item['status'] == MANUAL_STATUS for item in shift),
                       confirmed=day in data['payroll_days'],
                       paid=plain(sum(paid_for_shift.values(), Decimal(0))),
                       paid_by_day={key: plain(value) for key, value in sorted(paid_for_shift.items())},
                       paid_people=len(paid_people)),
            payout=dict(count=len(on_day), amount=plain(sum((p['amount'] for p in on_day), Decimal(0))),
                        by_shift={key: plain(value) for key, value in sorted(by_shift.items())},
                        general=[dict(id=row['id'], code=row['item_code'], description=row['description'],
                                      amount=plain(row['amount'])) for row in general],
                        general_amount=plain(sum((row['amount'] for row in general), Decimal(0))),
                        monthly_amount=plain(sum((row['amount'] for row in monthly), Decimal(0)))),
            debt_end_of_day=plain(debt),
            cash=dict(status=cash['status'] if cash else 'none', amount=plain(cash['amount']) if cash else None,
                      report_submitted=day in data['reports'])))

        # ── Находки по смене дня ──
        payout_day = next_day(day)
        general_next = [row for row in data['movements']
                        if row['day'] == payout_day and row['item_code'] in SHIFT_SALARY_CODES]
        if day < until.isoformat() or shift:
            if present and not shift and not general_next:
                finding('attendance_without_salary', 'high', day,
                        f'Смена {dm(day)}: пришли {len(present)}, а зарплатных записей за смену нет — ни выплат '
                        f'по людям, ни начислений, ни общей строки {dm(payout_day)}.',
                        present=len(present))
            elif present and not shift and general_next:
                total = sum((row['amount'] for row in general_next), Decimal(0))
                finding('general_only', 'info', day,
                        f'Смена {dm(day)}: пришли {len(present)}; выдано общей строкой {dm(payout_day)} '
                        f'на {spaced(total)} сум, по людям не разложено.', amount=plain(total))
            unpaid = sorted(present - paid_people - {item['employee_id'] for item in shift if item['amount'] > 0})
            if shift and unpaid:
                finding('present_unpaid', 'check', day,
                        f'Смена {dm(day)}: пришли, но за смену ничего не выдано и не начислено — '
                        + ', '.join(name(employee_id) for employee_id in unpaid) + '.',
                        employees=[name(employee_id) for employee_id in unpaid])
            absent_paid = sorted(employee_id for employee_id in paid_people
                                 if statuses.get(employee_id) in ('manual_absent', 'no_entry'))
            if absent_paid:
                finding('paid_without_attendance', 'check', day,
                        f'Смена {dm(day)}: выдано без прохода или с отметкой «не был» — '
                        + ', '.join(f'{name(employee_id)} ({_status_text(statuses[employee_id])})'
                                    for employee_id in absent_paid) + '.',
                        employees=[name(employee_id) for employee_id in absent_paid])
        # ── Находки по дню выплаты ──
        if general and on_day:
            finding('double_count', 'high', day,
                    f'Выплата {dm(day)}: общая зарплатная строка на '
                    f'{spaced(sum((row["amount"] for row in general), Decimal(0)))} сум '
                    f'(«{general[0]["description"]}»{" и др." if len(general) > 1 else ""}) и выплаты по людям на '
                    f'{spaced(sum((p["amount"] for p in on_day), Decimal(0)))} сум в один день — '
                    'проверьте, не одни ли это деньги.')
        if on_day and (cash is None or cash['status'] == 'pending'):
            finding('payout_without_cash', 'info', day,
                    f'Выплата {dm(day)}: касса дня ' + ('не записана' if cash is None else 'ещё не подтверждена')
                    + ' — остаток дня не окончательный.')

    # Выплаты не на следующий день после смены.
    shifted = defaultdict(lambda: dict(count=0, amount=Decimal(0), employees=[]))
    for payment in payments:
        accrual = accruals.get(payment['accrual_id'])
        if accrual is None or not (first <= payment['paid_day'] <= until.isoformat()):
            continue
        if payment['paid_day'] != next_day(accrual['work_day']):
            group = shifted[(accrual['work_day'], payment['paid_day'])]
            group['count'] += 1
            group['amount'] += payment['amount']
            group['employees'].append(accrual['name'])
    for (work_day, paid_day), group in sorted(shifted.items()):
        lag = (date.fromisoformat(paid_day) - date.fromisoformat(work_day)).days
        how = 'в день смены' if lag == 0 else f'через {lag} дн.'
        finding('paid_day_not_next', 'check', paid_day,
                f'Выплата {dm(paid_day)} за смену {dm(work_day)} ({how}): {group["count"]} чел., '
                f'{spaced(group["amount"])} сум — ' + ', '.join(group['employees']) + '.',
                work_day=work_day, amount=plain(group['amount']), employees=group['employees'])

    # Удаления зарплатных записей за период: что когда-то было и чего теперь нет.
    deletions = []
    for entry in data['audit']:
        if entry['action'] != 'delete' or entry['entity_type'] not in ('salary_payment', 'accrual'):
            continue
        before = entry['before'] or {}
        day = before.get('paid_day') or before.get('work_day') or ''
        if day < (since - timedelta(days=1)).isoformat():
            continue
        accrual = accruals.get(before.get('accrual_id'))
        who = before.get('employee_name') or (accrual['name'] if accrual else '')
        deletions.append(dict(entity=entry['entity_type'], id=entry['entity_id'], day=day, employee=who,
                              amount=str(before.get('amount', '')), changed_at=entry['changed_at']))

    # История 2–5 октября: сколько записей есть и меняли ли их задним числом.
    low, high = (value.isoformat() for value in HISTORY_DAYS)
    history = dict(
        handovers=sum(low <= day <= high for day in data['handovers']),
        payments=sum(low <= payment['paid_day'] <= high for payment in payments),
        accruals=sum(low <= item['work_day'] <= high for item in accruals.values()),
        general_salary_rows=sum(low <= row['day'] <= high and row['item_code'] in SHIFT_SALARY_CODES
                                for row in data['movements']),
        first_entries=sum(low <= row[0] <= high for row in data['entries']),
        day_reports=sum(low <= day <= high for day in data['reports']))
    touched = []
    for entry in data['audit']:
        before = entry['before'] or {}
        day = str(before.get('paid_day') or before.get('work_day') or before.get('day') or '')
        # Операции дня, а не служебные переносы начальных остатков (у них свой аудит).
        if (low <= day <= high and entry['action'] in ('delete', 'update')
                and entry['entity_type'] in HISTORY_ENTITIES):
            touched.append(dict(entity=entry['entity_type'], id=entry['entity_id'], action=entry['action'],
                                day=day, changed_at=entry['changed_at']))
    history['changed'] = touched
    removed = sum(item['action'] == 'delete' for item in touched)
    if removed:
        finding('history_deleted', 'high', low,
                f'История 2–5 октября: удалено записей — {removed}. Список — в разделе истории.')
    if len(touched) > removed:
        finding('history_edited', 'check', low,
                f'История 2–5 октября: исправлено задним числом — {len(touched) - removed}. '
                'Список — в разделе истории.')
    level = {'high': 0, 'check': 1, 'info': 2}
    findings.sort(key=lambda item: (level[item['level']], item['day'], item['kind']))
    return dict(period=dict(since=since.isoformat(), until=until.isoformat()), days=report_days,
                findings=findings, deletions=deletions, history_2_5=history,
                missing_tables=data['missing_tables'],
                totals=dict(payments=plain(sum((p['amount'] for p in payments
                                                if first <= p['paid_day'] <= until.isoformat()), Decimal(0))),
                            general=plain(sum((row['amount'] for row in data['movements']
                                               if first <= row['day'] <= until.isoformat()
                                               and row['item_code'] in SHIFT_SALARY_CODES), Decimal(0)))))


def _status_text(status: str) -> str:
    return {'manual_absent': 'отметка «не был»', 'no_entry': 'прохода нет'}.get(status, status)


# ── Отчёт ───────────────────────────────────────────────────────────────────

LEVELS = {'high': 'Важно', 'check': 'Проверить', 'info': 'Справка'}
CASH = {'confirmed': 'подтверждена', 'accountant': 'вписана', 'pending': 'ждёт подтверждения', 'none': 'нет'}
ENTITIES = {'salary_payment': 'выплата', 'accrual': 'начисление', 'movement': 'операция журнала',
            'handover': 'касса', 'debt': 'долг', 'debt_payment': 'оплата долга',
            'payroll_day': 'подтверждение смены', 'manual_attendance': 'отметка «был / не был»'}


def markdown(result: dict, source: dict, generated: str) -> str:
    period = result['period']
    where = (f'копия SQLite-файла `{source["path"]}`' if source['kind'] == 'sqlite'
             else f'Postgres `{source["host"]}/{source["database"]}`, транзакция только для чтения')
    lines = [f'# Сверка сменных зарплат · {dm(period["since"])}–{dm(period["until"])}.{period["until"][:4]}', '',
             f'Источник: {where}. Снято: {generated}. Скрипт ничего не записывал.', '']
    if result['missing_tables']:
        lines += ['В базе нет таблиц: ' + ', '.join(result['missing_tables']) + '. Эти разделы пустые.', '']
    lines += ['## Находки', '']
    if not result['findings']:
        lines.append('Расхождений не найдено.')
    for item in result['findings']:
        lines.append(f'- **{LEVELS[item["level"]]}.** {item["text"]}')
    lines += ['', '## По дням', '',
              'Смена — день, когда работали (посещаемость). Выплата — день, когда деньги ушли из кассы. '
              'Долг — начислено с 02.10 и не выдано на конец дня.', '',
              '| День | Смена: пришли (опозд.) | Начислено за смену | Выдано за смену (когда) '
              '| Выплаты в этот день по людям | Общие строки ЗП | Оклады | Долг сменным | Касса дня |',
              '|---|---|---|---|---|---|---|---|---|']
    for day in result['days']:
        shift, payout, seen = day['shift'], day['payout'], day['attendance']
        when = ', '.join(f'{dm(key)}: {spaced(money(value))}' for key, value in shift['paid_by_day'].items()) or '—'
        by_shift = ', '.join(f'за {dm(key) if key != "?" else "?"}: {spaced(money(value))}'
                             for key, value in payout['by_shift'].items())
        cash = CASH[day['cash']['status']] + (' · отчёт сдан' if day['cash']['report_submitted'] else '')
        lines.append(
            f'| {dm(day["day"])} | {seen["present"]} ({seen["late"]}) | '
            f'{spaced(money(shift["accrued"]))} · {shift["accrued_count"]} чел. | {when} | '
            f'{spaced(money(payout["amount"]))}' + (f' ({by_shift})' if by_shift else '') + ' | '
            f'{spaced(money(payout["general_amount"])) if payout["general"] else "—"} | '
            f'{spaced(money(payout["monthly_amount"])) if money(payout["monthly_amount"]) else "—"} | '
            f'{spaced(money(day["debt_end_of_day"]))} | {cash} |')
    general = [(day['day'], row) for day in result['days'] for row in day['payout']['general']]
    lines += ['', '## Общие зарплатные строки журнала', '']
    if general:
        lines += ['| День | Статья | Описание | Сумма |', '|---|---|---|---|']
        lines += [f'| {dm(day)} | {ITEMS[row["code"]][1]} | {row["description"]} | {spaced(money(row["amount"]))} |'
                  for day, row in general]
    else:
        lines.append('Общих строк за период нет.')
    lines += ['', '## Удалённые зарплатные записи', '']
    if result['deletions']:
        lines += ['| День | Что | Сотрудник | Сумма | Когда удалено |', '|---|---|---|---|---|']
        lines += [f'| {dm(item["day"]) if item["day"] else "—"} | '
                  f'{"выплата" if item["entity"] == "salary_payment" else "начисление"} | {item["employee"] or "—"} | '
                  f'{spaced(money(item["amount"]))} | {item["changed_at"]} |' for item in result['deletions']]
    else:
        lines.append('Удалений за период нет.')
    history = result['history_2_5']
    lines += ['', '## История 2–5 октября', '',
              f'Касса — {history["handovers"]} дн., выплаты по людям — {history["payments"]}, начисления — '
              f'{history["accruals"]}, общие строки ЗП — {history["general_salary_rows"]}, первые входы — '
              f'{history["first_entries"]}, сданные отчёты — {history["day_reports"]}.', '']
    if history['changed']:
        lines += ['| День | Что | Действие | Когда |', '|---|---|---|---|']
        lines += [f'| {dm(item["day"])} | {ENTITIES.get(item["entity"], item["entity"])} №{item["id"]} | '
                  f'{"удалено" if item["action"] == "delete" else "исправлено"} | {item["changed_at"]} |'
                  for item in history['changed']]
    else:
        lines.append('Правок и удалений задним числом нет: история цела.')
    lines += ['', '## Как читать', '',
              '- «Пришли» — проход Hikvision или отметка «был» (у отмечаемых вручную — по умолчанию «был»). '
              'Привязан к Hikvision, но прохода нет — не считается ни пришедшим, ни отсутствующим.',
              '- Выплата за смену D обычно — в день D+1. Другие дни — в находках «выплата не на следующий день».',
              '- Двойной учёт: общая строка «ЗП персонал» и выплаты по людям в один день — возможно, одни и те же деньги.',
              '- Сумм для восстановления скрипт не придумывает: правильную сумму даёт только подтверждённый источник '
              '(тетрадь, ведомость, подписи).', '']
    return '\n'.join(lines)


# ── Запуск ──────────────────────────────────────────────────────────────────

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description='Сверка сменных зарплат — только чтение.')
    root.add_argument('--data-dir', type=Path, help='каталог с accountant.sqlite3 (или сам файл)')
    root.add_argument('--database-url', help='строка Postgres; по умолчанию — DATABASE_URL')
    root.add_argument('--since', type=date.fromisoformat, default=ACCOUNTING_START)
    root.add_argument('--until', type=date.fromisoformat, default=None,
                      help='по какой день; по умолчанию — сегодня по Ташкенту')
    root.add_argument('--out', type=Path, required=True, help='каталог для .md и .json')
    return root


def run(args) -> dict:
    url = args.database_url if args.database_url is not None else (
        os.getenv('DATABASE_URL', '') if args.data_dir is None else '')
    if args.data_dir is None and not is_postgres_url(url):
        raise SystemExit('Укажите --data-dir (SQLite) или --database-url / DATABASE_URL (Postgres).')
    until = args.until or datetime.now(TZ).date()
    if until < args.since:
        raise SystemExit('--until раньше --since.')
    opener = open_postgres(url) if is_postgres_url(url) else open_sqlite(args.data_dir)
    with opener as (reader, source):
        data = load(reader)
    result = build(data, args.since, until)
    generated = datetime.now(TZ).isoformat(timespec='seconds')
    args.out.mkdir(parents=True, exist_ok=True)
    payload = dict(generated_at=generated, source=source, **result)
    (args.out / f'{REPORT_NAME}.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.out / f'{REPORT_NAME}.md').write_text(markdown(result, source, generated), encoding='utf-8')
    return payload


def main(argv=None) -> int:
    payload = run(parser().parse_args(argv))
    counts = defaultdict(int)
    for item in payload['findings']:
        counts[item['level']] += 1
    print(f'Дней: {len(payload["days"])}; находки — важно {counts["high"]}, проверить {counts["check"]}, '
          f'справка {counts["info"]}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
