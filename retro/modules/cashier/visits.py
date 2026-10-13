"""Дата визита гостя для предоплаты (T-440, 13.10.2026).

iiko не хранит дату события вместе с авансом, а Cloud не связывает бронь с
заказом кассы. Дату восстанавливаем из того, что точно известно:

1. Заказ закрыт — гость пришёл: дата визита = операционный день закрытия.
2. Заказ открыт — визит впереди: будущая бронь Cloud на стол этого заказа,
   если на столе ровно один открытый заказ с предоплатой и ровно одна бронь.
3. Иначе — даты броней этого стола кассиру на выбор. Неоднозначный стол
   никогда не превращается в угаданную дату.

Проверено на авансах 06–13.10.2026: 12 закрытых заказов, 2 однозначные брони,
2 на выбор (стол 23: два открытых заказа, три брони), 1 без брони.
"""
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from .service import DataError

VISIT_WINDOW_DAYS = 90       # банкет оплачивают заранее — так же глубоко ищет реестр
RESERVE_HORIZON_DAYS = 180
DAY_STARTS_AT = 6            # закрытый до 6 утра заказ — визит предыдущего вечера

SOURCE_CLOSED, SOURCE_RESERVE = 'closed', 'reserve'


@dataclass(frozen=True)
class Visit:
    day: date | None
    source: str = ''
    options: tuple = ()


def visit_day(closed_at):
    return (closed_at - timedelta(hours=DAY_STARTS_AT)).date()


def link_visits(orders, closed, tables, reserves, today):
    """orders — заказы, которым нужна дата; closed — {заказ: время закрытия};
    tables — {заказ: (стол, возвращена)} по всем предоплатам окна;
    reserves — брони Cloud (Reserve). Возвращает {заказ: Visit}."""
    open_on_table = Counter(table for order, (table, returned) in tables.items()
                            if table and not returned and order not in closed)
    by_table = defaultdict(list)
    for reserve in reserves:
        if reserve.visit_at.date() >= today:
            by_table[reserve.table].append(reserve)
    result = {}
    for order in dict.fromkeys(str(order) for order in orders if order):
        if order in closed:
            result[order] = Visit(visit_day(closed[order]), SOURCE_CLOSED)
            continue
        table, returned = tables.get(order, (None, False))
        candidates = by_table.get(table, []) if table and not returned else []
        if len(candidates) == 1 and open_on_table[table] == 1:
            result[order] = Visit(candidates[0].visit_at.date(), SOURCE_RESERVE)
        else:
            result[order] = Visit(None, '', tuple(sorted({item.visit_at.date() for item in candidates})))
    return result


async def load_visits(iiko, cloud, orders, today):
    """Даты визита для заказов реестра. Не ответил журнал или Cloud — дат меньше,
    но это не ошибка; не ответил отчёт о закрытии — не выводим ничего."""
    log = logging.getLogger(__name__)
    orders = [str(order) for order in orders if order]
    if not orders:
        return {}
    start = today - timedelta(days=VISIT_WINDOW_DAYS)
    try:
        tables = await iiko.prepaid_tables(start, today)
    except DataError as error:
        log.warning('prepayment visits: event log unavailable: %s', error)
        tables = {}
    closed = await iiko.order_close_times(set(tables) | set(orders), start - timedelta(days=1), today)
    reserves = ()
    if cloud is not None and cloud.configured and any(order not in closed for order in orders):
        try:
            reserves = await cloud.reserves(today, today + timedelta(days=RESERVE_HORIZON_DAYS))
        except (DataError, httpx.HTTPError, TimeoutError) as error:
            log.warning('prepayment visits: Cloud reserves unavailable: %s', error)
    return link_visits(orders, closed, tables, reserves, today)
