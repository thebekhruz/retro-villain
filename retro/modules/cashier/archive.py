"""Durable, source-scoped daily snapshots. Money stays decimal text in both DBs."""
import hashlib
import json
from contextlib import closing
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from retro.db import as_database
from retro.accounting_period import ACCOUNTING_START
from .service import Snapshot, Payment, RevenueBreakdown, ShiftStatus, DataError, TZ, RETRO_REGISTER

CALCULATION_VERSION = 'cashier-2026-09-28-v1'


def archive_boundary(day):
    # A calendar rollover is not a confirmed shift close. Re-read after the
    # overnight grace period and reconcile the recent week every morning.
    return datetime.combine(day + timedelta(days=1), time(6), TZ)


def decode_snapshot(payload):
    value = json.loads(payload)
    def amount(key):
        return Decimal(value[key]) if value.get(key) is not None else None
    breakdown = value.get('revenue_breakdown')
    # Смену в снимок стали класть позже: у старых записей её нет — это «неизвестно».
    shift = value.get('shift')
    return Snapshot(
        id=value['snapshot_id'], day=date.fromisoformat(value['date']),
        revenue=amount('revenue'), receipt_count=value['receipt_count'],
        payments=tuple(Payment(p['name'], Decimal(p['amount'])) for p in value['payments']),
        fetched_at=datetime.fromisoformat(value['fetched_at']),
        revenue_breakdown=RevenueBreakdown(*(Decimal(breakdown[key]) for key in
            ('retro', 'school', 'bekhruz_banquet'))) if breakdown else None,
        cash_prepayment=amount('cash_prepayment'), new_prepayment=amount('new_prepayment'),
        # Предоплаты могли не посчитаться (возврат аванса) — тогда они None, а
        # причина хранится рядом и переживает перезапуск вместе со снимком.
        prepayment_issue=value.get('prepayment_issue'),
        register_payment_sales=amount('register_payment_sales'),
        register_received_total=amount('register_received_total'), source='database',
        shift=ShiftStatus(bool(shift['open']), shift.get('opened_at'), shift.get('closed_at'))
        if isinstance(shift, dict) else None)


class CashierArchive:
    def __init__(self, database, settings):
        self.db = as_database(database)
        # No credentials in a source identifier; changing store or calculation
        # version must never return another restaurant's/algorithm's totals.
        scope = [settings.base_url, settings.store_id, RETRO_REGISTER, CALCULATION_VERSION]
        self.source = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS cashier_day_snapshots (
                source TEXT NOT NULL, day TEXT NOT NULL, fetched_at TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(source, day))''')
            connection.execute('''CREATE TABLE IF NOT EXISTS cashier_day_revisions (
                source TEXT NOT NULL, day TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(source, day, snapshot_id))''')
            connection.execute('''CREATE TABLE IF NOT EXISTS cashier_day_leases (
                source TEXT NOT NULL, day TEXT NOT NULL, owner TEXT NOT NULL,
                expires_at TEXT NOT NULL, PRIMARY KEY(source, day))''')
            connection.execute('''CREATE TABLE IF NOT EXISTS cashier_archive_start (
                source TEXT PRIMARY KEY, first_day TEXT NOT NULL)''')

    def get(self, day):
        with closing(self.db.connect()) as connection:
            row = connection.execute(
                'SELECT payload FROM cashier_day_snapshots WHERE source=? AND day=?',
                (self.source, day.isoformat())).fetchone()
        return decode_snapshot(row[0]) if row else None

    def save(self, snapshot):
        if snapshot.demo or snapshot.stale or snapshot.fetched_at.tzinfo is None:
            raise DataError('В архив можно сохранить только проверенный снимок iiko.')
        payload = json.dumps(snapshot.json(), ensure_ascii=False, sort_keys=True)
        key = (self.source, snapshot.day.isoformat())
        fetched = snapshot.fetched_at.astimezone(TZ).isoformat(timespec='microseconds')
        with closing(self.db.connect()) as connection, connection:
            # Preserve the former historical revision inside the same transaction.
            connection.execute('''INSERT INTO cashier_day_revisions
                (source, day, snapshot_id, payload)
                SELECT source, day, fetched_at, payload FROM cashier_day_snapshots
                WHERE source=? AND day=? AND fetched_at < ? AND day < ?
                ON CONFLICT(source, day, snapshot_id) DO NOTHING''',
                (*key, fetched, snapshot.fetched_at.astimezone(TZ).date().isoformat()))
            connection.execute('''INSERT INTO cashier_day_snapshots(source,day,fetched_at,payload)
                VALUES (?,?,?,?) ON CONFLICT(source,day) DO UPDATE SET
                fetched_at=excluded.fetched_at, payload=excluded.payload
                WHERE cashier_day_snapshots.fetched_at <= excluded.fetched_at''',
                (*key, fetched, payload))

    def acquire(self, day, owner, now, seconds=120):
        with closing(self.db.connect()) as connection, connection:
            result = connection.execute('''INSERT INTO cashier_day_leases
                (source,day,owner,expires_at) VALUES (?,?,?,?)
                ON CONFLICT(source,day) DO UPDATE SET owner=excluded.owner,
                expires_at=excluded.expires_at WHERE cashier_day_leases.expires_at <= ?''',
                (self.source, day.isoformat(), owner, (now + timedelta(seconds=seconds)).isoformat(),
                 now.isoformat()))
            return result.rowcount == 1

    def release(self, day, owner):
        with closing(self.db.connect()) as connection, connection:
            connection.execute('DELETE FROM cashier_day_leases WHERE source=? AND day=? AND owner=?',
                               (self.source, day.isoformat(), owner))

    def due_days(self, now):
        """Recent corrections first, then gaps since installation (including downtime)."""
        today = now.astimezone(TZ).date()
        initial = ACCOUNTING_START if today >= ACCOUNTING_START else today - timedelta(days=7)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''INSERT INTO cashier_archive_start(source,first_day) VALUES (?,?)
                ON CONFLICT(source) DO NOTHING''', (self.source, initial.isoformat()))
            first = date.fromisoformat(connection.execute(
                'SELECT first_day FROM cashier_archive_start WHERE source=?', (self.source,)).fetchone()[0])
            if today >= ACCOUNTING_START:
                # Даже установка спустя месяц должна догнать все дни с запуска
                # учёта. Ранее сохранённые снимки остаются доступными в архиве.
                first = ACCOUNTING_START
            rows = connection.execute(
                'SELECT day,fetched_at FROM cashier_day_snapshots WHERE source=? AND day>=?',
                (self.source, first.isoformat())).fetchall()
        saved = {date.fromisoformat(day): datetime.fromisoformat(fetched) for day, fetched in rows}
        recent = [today - timedelta(days=n) for n in range(1, 8)
                  if today < ACCOUNTING_START or today - timedelta(days=n) >= ACCOUNTING_START]
        gaps = (first + timedelta(days=n) for n in range(max(0, (today - first).days - 7)))
        cutoff = datetime.combine(today, time(6), TZ)
        for day in [*recent, *gaps]:
            if now < archive_boundary(day):
                continue
            required = max(archive_boundary(day), cutoff) if day in recent and now >= cutoff else archive_boundary(day)
            if day not in saved or saved[day] < required:
                yield day
