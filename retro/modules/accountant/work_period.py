"""Период работы временного сотрудника (T-434).

Бухгалтер заводит временного (хостес на подмену, официант на банкет) с
должностью и, если знает, сроком «с — по». Период — свойство человека, а не
дня: «Сотрудники», «Зарплата · день», доп. выплаты, отметки и начисления
видят его только в дни периода, и ничего записать ему вне периода нельзя.
Без периода (и у сменных) всё как прежде.

День периода — день СМЕНЫ. Выплату за последнюю смену делают назавтра:
клетка выплаты 11.10 — это смена 10.10, и она в периоде «по 10.10».

Здесь же — блокировка сотрудника в Postgres: изменение периода и запись денег
или отметки по нему идут строго по одному, иначе сокращение периода и выплата
в ту же секунду разошлись бы (проверка одного не видит записи другого).
Модуль не импортирует ledger и roster: оба берут его к себе.
"""

from __future__ import annotations

from datetime import date

from retro.db import PostgresConnection, table_columns, table_exists

SHIFT = 'shift'
TEMPORARY = 'temporary'
EMPLOYMENT_TYPES = (SHIFT, TEMPORARY)

# Ключ блокировки сотрудника в Postgres: «RETE» в старших битах, номер — в младших.
# У дня свой ключ (ledger.DAY_LOCK_BASE): они не пересекаются.
EMPLOYEE_LOCK_BASE = 0x52455445 << 32

# Что нельзя сделать вне периода — окончание отказа после «… работает с … по …».
ACTIONS = {
    'cell': 'выплату за смену {day} записать нельзя.',
    'extra': 'доп. выплату за смену {day} записать нельзя.',
    'mark': 'смену {day} отметить нельзя.',
    'exception': 'исключение за смену {day} дать нельзя.',
    'accrual': 'смену {day} начислить нельзя.',
}


def dm(day: date) -> str:
    return day.strftime('%d.%m')


def as_day(value) -> date | None:
    """Дата из поля: date, ISO-строка или пусто (None — без границы)."""
    if value is None or value == '':
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError('Дата периода — в виде ГГГГ-ММ-ДД.') from None


def normalize(employment_type, work_from, work_to) -> tuple[str, date | None, date | None]:
    """Тип и период из формы — проверенные. Период бывает только у временного;
    можно указать одну границу; «по» не раньше «с»."""
    kind = employment_type.strip() if isinstance(employment_type, str) else employment_type
    kind = kind or SHIFT
    if kind not in EMPLOYMENT_TYPES:
        raise ValueError('Тип сотрудника — сменный или временный.')
    start, end = as_day(work_from), as_day(work_to)
    if kind == SHIFT and (start or end):
        raise ValueError('Период работы указывают только у временного сотрудника.')
    if start and end and end < start:
        raise ValueError(f'Период работы: «по» ({dm(end)}) раньше, чем «с» ({dm(start)}).')
    return kind, start, end


def in_period(day: date, start: date | None, end: date | None) -> bool:
    return (start is None or day >= start) and (end is None or day <= end)


def label(start: date | None, end: date | None) -> str | None:
    """Период в пометке «временный · …»: 08.10–10.10, с 08.10, по 10.10."""
    if start and end:
        return dm(start) if start == end else f'{dm(start)}–{dm(end)}'
    if start:
        return f'с {dm(start)}'
    if end:
        return f'по {dm(end)}'
    return None


def span_words(start: date | None, end: date | None) -> str:
    """Период словами для отказа: «с 08.10 по 10.10», «только 08.10»."""
    if start and end:
        return f'только {dm(start)}' if start == end else f'с {dm(start)} по {dm(end)}'
    return label(start, end) or ''


def outside_text(name: str, start: date | None, end: date | None, work_day: date, action: str) -> str:
    """«Карамат работает с 08.10 по 10.10 — смену 12.10 отметить нельзя.»"""
    return f'{name} работает {span_words(start, end)} — ' + ACTIONS[action].format(day=dm(work_day))


def lock_employee(connection, employee_id: int) -> None:
    """Запись по сотруднику и смена его периода — по одному (Postgres).
    В SQLite это уже делает BEGIN IMMEDIATE. Берётся после блокировок дней."""
    if isinstance(connection, PostgresConnection):
        connection.execute('SELECT pg_advisory_xact_lock(CAST(? AS BIGINT))',
                           (EMPLOYEE_LOCK_BASE + int(employee_id),))


def _has_period(connection, table: str) -> bool:
    return table_exists(connection, table) and 'work_from' in table_columns(connection, table)


def periods(connection, employee_ids) -> dict[int, tuple[str, date | None, date | None]]:
    """Имя и период сотрудников: из реестра, у удалённых — из последней версии
    (период переносится в версии, поэтому прошлые дни ушедшего его помнят)."""
    wanted = {int(value) for value in employee_ids}
    if not wanted or not _has_period(connection, 'accountant_employees'):
        return {}
    marks = ','.join('?' for _ in wanted)
    found = {row[0]: (row[1], as_day(row[2]), as_day(row[3])) for row in connection.execute(
        f'SELECT id, name, work_from, work_to FROM accountant_employees WHERE id IN ({marks})',
        tuple(sorted(wanted)))}
    missing = wanted - found.keys()
    if missing and _has_period(connection, 'accountant_employee_versions'):
        marks = ','.join('?' for _ in missing)
        for employee_id, _, name, start, end in connection.execute(
                f'SELECT employee_id, effective_day, name, work_from, work_to FROM accountant_employee_versions '
                f'WHERE employee_id IN ({marks}) ORDER BY employee_id, effective_day', tuple(sorted(missing))):
            found[employee_id] = (name, as_day(start), as_day(end))
    return found


def guard(connection, employee_id: int, work_day: date, action: str) -> str | None:
    """Текст отказа, если смена `work_day` вне периода сотрудника; None — можно."""
    found = periods(connection, (employee_id,)).get(int(employee_id))
    if found is None:
        return None
    name, start, end = found
    return None if in_period(work_day, start, end) else outside_text(name, start, end, work_day, action)


def _days(connection, table: str, sql: str, employee_id: int) -> set[date]:
    if not table_exists(connection, table):
        return set()
    return {date.fromisoformat(row[0]) for row in connection.execute(sql, (employee_id,))}


def _listed(days) -> str:
    ordered = sorted(days)
    text = ', '.join(dm(day) for day in ordered[:8])
    return text + (f' и ещё {len(ordered) - 8}' if len(ordered) > 8 else '')


def conflicts(connection, employee_id: int, start: date | None, end: date | None) -> str | None:
    """Что уже записано по сотруднику вне периода [start, end] — текстом отказа.

    Начисления и выплаты (клетка «Зарплаты · день» — это пара начисление +
    выплата), доп. выплаты, ручные отметки «был / не был» и однодневные
    исключения — по дню смены. Проходы Hikvision в счёт не идут: это не
    запись бухгалтера, человек просто не виден вне периода."""
    found = [
        ('начисления или выплаты за смены', _days(
            connection, 'accountant_accruals',
            'SELECT DISTINCT work_day FROM accountant_accruals WHERE employee_id = ?', employee_id)),
        ('доп. выплаты за смены', _days(
            connection, 'accountant_extra_payouts',
            'SELECT DISTINCT work_day FROM accountant_extra_payouts WHERE employee_id = ?', employee_id)),
        ('отметки «был / не был» за', _days(
            connection, 'hikvision_manual_absences',
            'SELECT DISTINCT work_day FROM hikvision_manual_absences WHERE employee_id = ?', employee_id)),
        ('исключение за', _days(
            connection, 'accountant_exceptions',
            'SELECT DISTINCT day FROM accountant_exceptions WHERE employee_id = ?', employee_id)),
    ]
    parts = [f'{what} {_listed(outside)}' for what, days in found
             if (outside := {day for day in days if not in_period(day, start, end)})]
    if not parts:
        return None
    return ('Период не изменить: вне новых дат уже есть ' + '; '.join(parts)
            + '. Сначала уберите их или выберите другие даты.')
