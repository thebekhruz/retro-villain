"""Append-only audit records for financial mutations."""

import json
from datetime import datetime


def record_audit(connection, entity_type: str, entity_id: int | str, action: str,
                 before: dict | None, after: dict | None) -> None:
    connection.execute(
        'INSERT INTO accountant_finance_audit '
        '(entity_type, entity_id, action, before_json, after_json, changed_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (entity_type, str(entity_id), action,
         json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
         json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None,
         datetime.now().isoformat()))


def audit_entries(connection, *, entity_type: str | None = None,
                  entity_id: int | str | None = None) -> list[dict]:
    clauses = []
    values = []
    if entity_type is not None:
        clauses.append('entity_type = ?')
        values.append(entity_type)
    if entity_id is not None:
        clauses.append('entity_id = ?')
        values.append(str(entity_id))
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    rows = connection.execute(
        'SELECT id, entity_type, entity_id, action, before_json, after_json, changed_at '
        f'FROM accountant_finance_audit{where} ORDER BY id', values).fetchall()
    return [dict(id=row[0], entity_type=row[1], entity_id=row[2], action=row[3],
                 before=json.loads(row[4]) if row[4] else None,
                 after=json.loads(row[5]) if row[5] else None,
                 changed_at=row[6]) for row in rows]
