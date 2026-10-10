"""T-428: сверка сменных зарплат с 02.10 (scripts/reconcile_salaries.py) — только чтение.

Скрипт находит смены с посещаемостью без зарплатных записей, двойной учёт
(общая строка и выплаты по людям в один день), выплаты не на следующий день,
удаления и правки истории 2–5 октября. Базу он не меняет: SQLite читает с
копии, Postgres — в транзакции READ ONLY, миграций не запускает.
"""

import hashlib
import importlib.util
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from retro.integrations.hikvision import HikvisionEvent
from retro.modules.accountant import reserves, salary_day
from retro.modules.accountant.hikvision import AttendanceStore
from retro.modules.accountant.ledger import FinanceStore
from retro.modules.accountant.payroll import PayrollRow
from retro.modules.accountant.roster import RosterStore
from retro.modules.cashier.service import TZ

POSTGRES_URL = os.getenv('RETRO_TEST_POSTGRES_URL', '')
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'reconcile_salaries.py'


def load_script():
    spec = importlib.util.spec_from_file_location('reconcile_salaries', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reconcile = load_script()


def oct_(day):
    return date(2026, 10, day)


def seed(target, monkeypatch):
    """Неделя с 02.10: история 2–5 октября, смена без зарплаты, общая строка
    рядом с выплатами по людям, выдача в день смены, выдача отсутствовавшей и
    отменённая выплата."""
    monkeypatch.setattr('retro.modules.accountant.roster.today_tashkent', lambda: oct_(1))
    monkeypatch.setattr(salary_day, 'today_tashkent', lambda: oct_(10))
    roster, finance, attendance = RosterStore(target), FinanceStore(target), AttendanceStore(target)
    people = dict(
        a=roster.add(name='Баходиров Ихтиер', role='Менеджер', rate='360000', group_name='Управление'),
        b=roster.add(name='Каримов Жахонгир', role='Менеджер', rate='360000', group_name='Управление'),
        c=roster.add(name='Абдулганиева Сельвина', role='Хостес', rate='360000', group_name='Встреча гостей'),
        d=roster.add(name='Карамат', role='Хостес · временная', rate='150000', group_name='Встреча гостей'))
    for offset in range(8):
        finance.record_handover(date(2026, 10, 2 + offset), Decimal('5000000'))
    finance.set_cash_opening(oct_(2), '20000000', 'Архивный остаток')
    finance.set_cash_opening(oct_(5), '20000000', 'Пересчёт на начало нового периода')
    for index, (who, day, hour, minute) in enumerate([('a', 2, 9, 30), ('a', 5, 9, 40), ('a', 6, 9, 35),
                                                     ('b', 6, 10, 58), ('a', 7, 9, 38), ('a', 8, 9, 31),
                                                     ('b', 8, 10, 58)]):
        attendance.ingest(HikvisionEvent('retro-main-entry', f'e-{index}', str(100 + index),
                                         datetime(2026, 10, day, hour, minute, tzinfo=TZ)), people[who].id)
    attendance.set_manual_mark(people['c'].id, oct_(7), False, 'Бухгалтер')
    # 2–5 октября: выдача 03.10 за смену 02.10 по человеку и общая строка 04.10.
    finance.confirm_payroll(oct_(2), [PayrollRow(people['a'].id, people['a'].name, 'Менеджер',
        'Управление', 'on_time', None, Decimal('360000'), Decimal('360000'), False)], 'Бухгалтер')
    finance.pay_salary(finance.accrued_employees(oct_(2))[people['a'].id], oct_(3), '360000')
    finance.add_expense(oct_(4), 'salary_staff', 'Зарплата персонал', '1850000')
    # Смена 05.10: Ихтиер пришёл — выдачи 06.10 нет ни по людям, ни общей строкой.
    # Выплата 07.10: сначала общая строка, потом выдачи по людям — двойной учёт.
    finance.add_expense(oct_(7), 'salary_staff', 'Персонал за 06.10', '500000')
    finance.set_salary_day_cell(oct_(7), people['a'].id, '360000', '0')
    # Выплата 08.10 за смену 07.10 — Сельвине, у которой отметка «не был».
    finance.set_salary_day_cell(oct_(8), people['c'].id, '360000', '0')
    # Карамат: смену 08.10 подтвердили по-старому и выдали в тот же день.
    row = PayrollRow(people['d'].id, 'Карамат', 'Хостес · временная', 'Встреча гостей', 'manual_present',
                     None, Decimal('150000'), Decimal('150000'), False)
    finance.confirm_payroll(oct_(8), [row], 'Бухгалтер')
    accrual = finance.accrued_employees(oct_(8))[people['d'].id]
    finance.pay_salary(accrual, oct_(8), '150000')
    # 09.10: выдачу Сельвине за смену 08.10 записали и отменили.
    finance.set_salary_day_cell(oct_(9), people['c'].id, '360000', '0')
    finance.set_salary_day_cell(oct_(9), people['c'].id, '0', '360000')
    finance.set_salary_day_cell(oct_(9), people['a'].id, '360000', '0')
    return people


def kinds(payload):
    return sorted((item['kind'], item['day']) for item in payload['findings'])


def check_findings(payload):
    assert kinds(payload) == [
        ('attendance_without_salary', '2026-10-05'),
        ('double_count', '2026-10-07'),
        ('paid_day_not_next', '2026-10-08'),
        ('paid_without_attendance', '2026-10-07'),
        ('present_unpaid', '2026-10-06'),
        ('present_unpaid', '2026-10-07'),
        ('present_unpaid', '2026-10-08')]
    found = {(item['kind'], item['day']): item for item in payload['findings']}
    assert found[('present_unpaid', '2026-10-06')]['employees'] == ['Каримов Жахонгир']
    assert found[('present_unpaid', '2026-10-07')]['employees'] == ['Баходиров Ихтиер']
    assert found[('present_unpaid', '2026-10-08')]['employees'] == ['Каримов Жахонгир']
    assert found[('paid_without_attendance', '2026-10-07')]['employees'] == ['Абдулганиева Сельвина']
    assert found[('paid_day_not_next', '2026-10-08')]['work_day'] == '2026-10-08'
    assert found[('paid_day_not_next', '2026-10-08')]['employees'] == ['Карамат']
    days = {day['day']: day for day in payload['days']}
    assert days['2026-10-07']['payout']['general_amount'] == '500000'
    assert days['2026-10-07']['payout']['by_shift'] == {'2026-10-06': '360000'}
    assert days['2026-10-08']['payout']['by_shift'] == {'2026-10-07': '360000', '2026-10-08': '150000'}
    assert days['2026-10-05']['attendance']['present'] == 1
    assert days['2026-10-08']['attendance']['late'] == 1
    assert days['2026-10-06']['shift']['paid_by_day'] == {'2026-10-07': '360000'}
    # Отменённая выдача 09.10 — в удалениях, с суммой и днём.
    deleted = [(item['entity'], item['day'], item['amount']) for item in payload['deletions']]
    assert ('salary_payment', '2026-10-09', '360000') in deleted
    assert ('accrual', '2026-10-08', '360000') in deleted
    # История 2–5 октября на месте и задним числом не правилась.
    history = payload['history_2_5']
    assert (history['payments'], history['general_salary_rows'], history['changed']) == (1, 1, [])
    assert payload['totals'] == dict(payments='1230000', general='500000')


def snapshot(directory: Path) -> dict:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.iterdir()) if path.is_file()}


def test_sqlite_copy_is_read_and_the_source_is_untouched(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    seed(data / 'accountant.sqlite3', monkeypatch)
    before = snapshot(data)
    out = tmp_path / 'out'
    assert reconcile.main(['--data-dir', str(data), '--until', '2026-10-09', '--out', str(out)]) == 0
    assert snapshot(data) == before
    payload = json.loads((out / 'salary-reconciliation.json').read_text(encoding='utf-8'))
    assert payload['source'] == dict(kind='sqlite', path=str(data / 'accountant.sqlite3'))
    check_findings(payload)
    text = (out / 'salary-reconciliation.md').read_text(encoding='utf-8')
    assert 'Смена 05.10: пришли 1, а зарплатных записей за смену нет' in text
    assert 'Выплата 07.10: общая зарплатная строка на 500 000 сум' in text
    assert 'Правок и удалений задним числом нет: история цела.' in text


def test_sqlite_reader_refuses_writes(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    seed(data / 'accountant.sqlite3', monkeypatch)
    with reconcile.open_sqlite(data) as (reader, _):
        with pytest.raises(sqlite3.OperationalError):
            reader.rows("INSERT INTO accountant_movements (day, kind, description, amount, created_at) "
                        "VALUES ('2026-10-09', 'other_expense', 'x', '1', 'x')")


def test_an_old_schema_is_read_without_migrating_it(tmp_path):
    """База до колонок источника кассы, ручных отметок и item_code: скрипт её читает и не дополняет."""
    path = tmp_path / 'accountant.sqlite3'
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript('''
            CREATE TABLE accountant_employees (id INTEGER PRIMARY KEY, source_row INTEGER, name TEXT,
                role TEXT, group_name TEXT, rate TEXT, hikvision_id TEXT);
            CREATE TABLE accountant_accruals (id INTEGER PRIMARY KEY, work_day TEXT, employee_id INTEGER,
                employee_name TEXT, group_name TEXT, attendance_status TEXT, rate TEXT, amount TEXT);
            CREATE TABLE accountant_salary_payments (id INTEGER PRIMARY KEY, accrual_id INTEGER, paid_day TEXT,
                amount TEXT, created_at TEXT);
            CREATE TABLE accountant_movements (id INTEGER PRIMARY KEY, day TEXT, kind TEXT, description TEXT,
                amount TEXT, reference TEXT, created_at TEXT);
            CREATE TABLE accountant_handover_days (day TEXT PRIMARY KEY, amount TEXT, checked_at TEXT);
            CREATE TABLE hikvision_manual_absences (work_day TEXT, employee_id INTEGER, approver TEXT,
                created_at TEXT);
            INSERT INTO accountant_employees VALUES (1, 1, 'Карамат', 'Хостес', 'Встреча гостей', '150000', NULL);
            INSERT INTO accountant_accruals VALUES (1, '2026-10-02', 1, 'Карамат', 'Встреча гостей', 'present',
                '150000', '150000');
            INSERT INTO accountant_salary_payments VALUES (1, 1, '2026-10-03', '150000', '2026-10-03');
            INSERT INTO accountant_handover_days VALUES ('2026-10-03', '100', '2026-10-03');
            INSERT INTO hikvision_manual_absences VALUES ('2026-10-02', 1, 'Бухгалтер', '2026-10-02');
        ''')
    schema = lambda: sqlite3.connect(path).execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()  # noqa: E731
    before, digest = schema(), hashlib.sha256(path.read_bytes()).hexdigest()
    out = tmp_path / 'out'
    reconcile.main(['--data-dir', str(path), '--since', '2026-10-02', '--until', '2026-10-04', '--out', str(out)])
    assert schema() == before and hashlib.sha256(path.read_bytes()).hexdigest() == digest
    payload = json.loads((out / 'salary-reconciliation.json').read_text(encoding='utf-8'))
    days = {day['day']: day for day in payload['days']}
    assert days['2026-10-03']['payout']['amount'] == '150000'
    assert days['2026-10-02']['attendance']['manual_absent'] == 1
    assert days['2026-10-03']['cash']['status'] == 'accountant'
    # Выдано, хотя отмечено «не был»: старую отметку прочитали как отсутствие.
    assert [item['kind'] for item in payload['findings']] == ['paid_without_attendance']


def test_codes_and_thresholds_come_from_the_application():
    assert reconcile.SHIFT_SALARY_CODES == reserves.SHIFT_SALARY_CODES
    assert reconcile.MANUAL_STATUS == salary_day.MANUAL_STATUS


@pytest.mark.skipif(not POSTGRES_URL, reason='RETRO_TEST_POSTGRES_URL не задан')
def test_postgres_is_read_in_a_read_only_transaction(tmp_path, monkeypatch):
    import psycopg
    from psycopg import sql
    schema = 'reconcile_' + uuid4().hex
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(POSTGRES_URL)
    url = urlunsplit(parts._replace(query=urlencode(dict(parse_qsl(parts.query), options='-csearch_path=' + schema))))
    try:
        seed(url, monkeypatch)
        count = lambda: admin.execute(sql.SQL(  # noqa: E731
            'SELECT (SELECT COUNT(*) FROM {s}.accountant_salary_payments), '
            '(SELECT COUNT(*) FROM {s}.accountant_finance_audit)').format(s=sql.Identifier(schema))).fetchone()
        before = count()
        out = tmp_path / 'out'
        reconcile.main(['--database-url', url, '--until', '2026-10-09', '--out', str(out)])
        assert count() == before
        payload = json.loads((out / 'salary-reconciliation.json').read_text(encoding='utf-8'))
        assert payload['source']['kind'] == 'postgres' and 'password' not in json.dumps(payload['source'])
        check_findings(payload)
        with reconcile.open_postgres(url) as (reader, _):
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                reader.rows("INSERT INTO accountant_movements (day, kind, description, amount, created_at) "
                            "VALUES ('2026-10-09', 'other_expense', 'x', '1', 'x')")
        assert count() == before
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()
