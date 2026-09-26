"""Итоги закупа: сколько потрачено и сколько длилась поездка.

Всё выводится из записанных покупок и поездок — отдельного состояния нет.
"""
from datetime import datetime
from decimal import Decimal


def trip_minutes(trip: dict) -> float | None:
    if not trip.get('started_at') or not trip.get('finished_at'):
        return None
    started = datetime.fromisoformat(trip['started_at'])
    finished = datetime.fromisoformat(trip['finished_at'])
    return max(0.0, (finished - started).total_seconds() / 60)


def spent(purchases: list[dict]) -> Decimal:
    return sum((Decimal(item['total']) for item in purchases), Decimal(0))
