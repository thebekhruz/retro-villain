"""Durable at-most-once financial POSTs, including ambiguous network failures.

A reservation is committed BEFORE executing a mutation. An interrupted process
leaves it pending; retry must inspect the journal rather than execute twice.
Completed responses can be replayed. Authentication always runs before this.
"""
import asyncio
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from fastapi.responses import JSONResponse, Response
from retro.runtime import secure_directory, secure_file


PATHS = {'/api/cashier/expenses', '/api/cashier/receipts', '/api/cashier/usd-balance'} | {
    '/api/accountant/' + suffix for suffix in (
        'handover', 'incomes', 'expenses', 'reserves', 'cash-opening', 'monthly-plan',
        'salary-payments', 'debts/pay', 'procurement', 'payroll/confirm')}


class FinancialRequests:
    def __init__(self, path):
        self.path = Path(path)
        secure_directory(self.path.parent)
        with closing(self._open()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS financial_requests (
                owner TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                status INTEGER, body BLOB, PRIMARY KEY(owner,key))''')

    def _open(self):
        connection = sqlite3.connect(self.path, timeout=10)
        secure_file(self.path)
        return connection

    def reserve(self, owner, key, fingerprint):
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT fingerprint,status,body FROM financial_requests '
                                     'WHERE owner=? AND key=?', (owner, key)).fetchone()
            if row is not None:
                return row
            connection.execute('INSERT INTO financial_requests(owner,key,fingerprint) VALUES (?,?,?)',
                               (owner, key, fingerprint))
        return None

    def finish(self, owner, key, status, body):
        with closing(self._open()) as connection, connection:
            if 400 <= status < 500:
                connection.execute('DELETE FROM financial_requests WHERE owner=? AND key=?', (owner, key))
            elif status < 400:
                connection.execute('UPDATE financial_requests SET status=?,body=? WHERE owner=? AND key=?',
                                   (status, body, owner, key))
            # A 5xx may follow a committed mutation; leave the reservation pending.

    async def dispatch(self, request, call_next):
        key = request.headers.get('Idempotency-Key')
        if request.method != 'POST' or request.url.path not in PATHS or not key:
            return await call_next(request)
        try:
            key = str(UUID(key))
        except ValueError:
            return JSONResponse({'detail': 'Некорректный ключ операции.'}, 422)
        owner = request.state.dashboard_user
        fingerprint = hashlib.sha256(request.url.path.encode() + b'\0' + await request.body()).hexdigest()
        existing = await asyncio.to_thread(self.reserve, owner, key, fingerprint)
        if existing:
            stored, status, body = existing
            if stored != fingerprint:
                return JSONResponse({'detail': 'Ключ операции уже использован для других данных.'}, 409)
            if status is None:
                return JSONResponse({'detail': 'Результат предыдущей записи ещё не подтверждён. '
                                     'Проверьте журнал перед новой операцией; повторное списание заблокировано.'}, 409)
            return Response(body, status_code=status, media_type='application/json')
        response = await call_next(request)
        body = b''.join([chunk async for chunk in response.body_iterator])
        await asyncio.to_thread(self.finish, owner, key, response.status_code, body)
        return Response(body, status_code=response.status_code, headers=dict(response.headers),
                        background=response.background)
