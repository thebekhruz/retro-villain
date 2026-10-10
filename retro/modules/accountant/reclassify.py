"""Перенос старых строк журнала в понятные статьи (ТЗ 09.10, Б-08).

До T-431 у журнала не было статьи для доп. зарплаты и выплат временному
персоналу, и бухгалтер записывала их «Прочими расходами»; туда же попали
дивиденды учредителю (07.10: «временные сотрудники» 2 450 000 и «дивиденды»
35 000 000). Здесь — поиск таких строк по тексту и перенос в нужную статью.
Сумма, день и ссылка строки не меняются, в аудите остаётся «было → стало».

Само приложение ничего не переносит: это делает человек скриптом
scripts/reclassify_expenses.py после сверки смысла каждой строки. Дивиденды
переносятся только в «Дивиденды напрямую из кассы»: деньги по такой строке уже
списаны из кассы, а выдача из сейфа кассу не трогает — другой смысл.
"""

import re
from contextlib import closing
from datetime import date

from .audit import record_audit
from .expense_catalog import CASH_DIVIDENDS_ITEM, EXTRA_SALARY_ITEM, ITEMS
from .ledger import FinanceStore, LedgerError, ensure_open, is_monthly_link

# Признак в тексте строки → статья, куда её предлагается перенести.
RULES = ((re.compile(r'временн', re.IGNORECASE), EXTRA_SALARY_ITEM),
         (re.compile(r'дивиденд', re.IGNORECASE), CASH_DIVIDENDS_ITEM))
TARGETS = frozenset(code for _, code in RULES)


def note_of(description: str, item_code: str | None) -> str:
    """Что ввёл человек: описание без подписи статьи «Подпись · …»."""
    label = ITEMS.get(item_code, (None, None))[1]
    if label and description.startswith(label + ' · '):
        return description[len(label) + 3:]
    return '' if description == label else description


def target_for(description: str) -> str | None:
    return next((code for pattern, code in RULES if pattern.search(description or '')), None)


def _blocked(connection, row) -> str | None:
    """Почему строку нельзя перенести этим путём, или None."""
    movement_id, day, _amount, _code, _description, reference = row
    if is_monthly_link(reference):
        return 'выплата оклада привязана к сотруднику'
    if connection.execute('SELECT 1 FROM accountant_debt_payments WHERE movement_id = ?',
                          (movement_id,)).fetchone():
        return 'это оплата долга — статья у долга своя, переносить вместе с долгом вручную'
    try:
        ensure_open(connection, date.fromisoformat(day))
    except LedgerError:
        return 'месяц закрыт'
    return None


def candidates(db) -> list[dict]:
    """Расходы, текст которых похож на доп. зарплату или дивиденды, а статья
    другая. Только чтение: ничего не пишет."""
    with closing(db.connect()) as connection:
        rows = connection.execute(
            'SELECT id, day, amount, item_code, description, reference FROM accountant_movements '
            "WHERE kind = 'other_expense' ORDER BY day, id").fetchall()
        found = []
        for row in rows:
            target = target_for(row[4])
            if target is None or row[3] in TARGETS:
                continue
            found.append(dict(id=row[0], day=row[1], amount=row[2], item_code=row[3], description=row[4],
                              target=target, blocked=_blocked(connection, row)))
    return found


def reclassify(db, movement_id: int, target: str) -> dict:
    """Перенести строку-расход в статью `target`. Сумма, день и ссылка те же;
    описание — «Новая подпись · то, что ввёл человек». Возвращает строку после.

    Закрытый месяц, выплаты окладов и оплаты долгов не трогаем — объяснение в
    LedgerError."""
    if target not in TARGETS:
        raise LedgerError('Переносить можно только в доп. зарплату или дивиденды из кассы.')
    with closing(db.connect()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            row = connection.execute(
                'SELECT id, day, amount, item_code, description, reference FROM accountant_movements '
                "WHERE id = ? AND kind = 'other_expense'", (movement_id,)).fetchone()
            if row is None:
                raise LedgerError(f'Расход №{movement_id} не найден.')
            if row[3] == target:
                raise LedgerError(f'Расход №{movement_id} уже в этой статье.')
            reason = _blocked(connection, row)
            if reason:
                raise LedgerError(f'Расход №{movement_id} не перенесён: {reason}.')
            before = FinanceStore._row_dict(connection, 'accountant_movements', movement_id)
            note = note_of(row[4], row[3])
            label = ITEMS[target][1]
            connection.execute('UPDATE accountant_movements SET item_code = ?, description = ? WHERE id = ?',
                               (target, f'{label} · {note}' if note else label, movement_id))
            after = FinanceStore._row_dict(connection, 'accountant_movements', movement_id)
            record_audit(connection, 'movement', movement_id, 'reclassify', before, after)
            connection.commit()
            return after
        except Exception:
            connection.rollback()
            raise
