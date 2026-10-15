"""Сборка экрана KPI: чеки периода и базы из iiko через кэш отчётов, расчёт, фото из реестра."""
import re
from datetime import date, datetime

from retro.modules.cashier.service import TZ, today_tashkent

from .rules import HALL, KpiError, aggregate, baseline_window, compute, login_counts
from .source import margin_per_unit

OPEN_TTL, OPEN_STALE = 15 * 60, 60 * 60          # окно с сегодняшним днём: раз в 15 минут, «Обновить» — сразу
CLOSED_TTL, CLOSED_STALE = 6 * 60 * 60, 24 * 60 * 60   # прошедшие дни iiko сам не меняет
MARGIN_WEEKS = 8


def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').replace('ё', 'е').replace('Ё', 'Е')).strip().casefold()


def initials(name: str) -> str:
    parts = [part for part in re.split(r'[\s|]+', name or '') if part]
    return ''.join(part[0] for part in parts[:2]).upper()


def match_employee(waiter: str, cards: list, links: dict):
    """Сотрудник реестра для логина iiko: явная привязка, иначе совпадение по имени."""
    link = links.get(waiter)
    if link is not None:
        if not link.get('active', True):
            return None
        if link.get('employee_id'):
            return next((card for card in cards if card['id'] == link['employee_id']), None)
    key = _norm(waiter)
    if not key:
        return None
    exact = [card for card in cards if _norm(card['name']) == key]
    if len(exact) == 1:
        return exact[0]
    words = key.split(' ')
    partial = [card for card in cards if words and all(word in _norm(card['name']).split(' ') for word in words)]
    return partial[0] if len(partial) == 1 else None


async def cached_orders(state, start: date, end: date, *, refresh=False):
    today = today_tashkent()
    if end < start:
        return dict(orders=[], fetched_at=None)
    closed = end < today
    ttl, stale = (CLOSED_TTL, CLOSED_STALE) if closed else (OPEN_TTL, OPEN_STALE)

    async def operation():
        orders = await state.kpi_source.orders(start, end)
        return dict(orders=orders, fetched_at=datetime.now(TZ).isoformat(timespec='seconds'))

    return await state.reports.get(('kpi-orders', id(state.iiko), start.isoformat(), end.isoformat()), operation,
                                   ttl=ttl, timeout=150, refresh=refresh, label='kpi_orders', stale=stale)


async def cached_margins(state, start: date, end: date, *, refresh=False):
    async def operation():
        return await state.kpi_source.margins(start, end)
    return await state.reports.get(('kpi-margins', id(state.iiko), start.isoformat(), end.isoformat()), operation,
                                   ttl=CLOSED_TTL, timeout=150, refresh=refresh, label='kpi_margins', stale=CLOSED_STALE)


async def fill_margins(state, targets, period_start: date, *, refresh=False) -> list:
    """Опорная маржа позиции, если директор её не задал: средняя по iiko за 8 недель до периода."""
    missing = [target for target in targets if not target.get('margin')]
    if not missing:
        return targets
    start, end = baseline_window(period_start, MARGIN_WEEKS)
    totals = await cached_margins(state, start, end, refresh=refresh)
    for target in missing:
        value = margin_per_unit(totals, target['dishes'])
        if value is None or value <= 0:
            raise KpiError(f'У позиции «{target["title"]}» нет продаж или себестоимости в iiko за {start:%d.%m}–{end:%d.%m}: '
                           'задайте маржу на порцию вручную.')
        target['margin'] = round(value)
    return targets


def period_days(period, today: date):
    start, end = date.fromisoformat(period['start']), date.fromisoformat(period['end'])
    total = (end - start).days + 1
    passed = 0 if today < start else min(total, (today - start).days + 1)
    return start, end, total, passed


async def period_view(state, period, *, refresh=False, photo_url=None, by_name_only=False) -> dict:
    """JSON экрана «Неделя»: шапка периода, рейтинг, разворот по людям."""
    today = today_tashkent()
    start, end, total, passed = period_days(period, today)
    settings = period['settings']
    targets = await fill_margins(state, [dict(t) for t in settings['targets']], start, refresh=refresh)
    scheme = settings['scheme']
    links = state.kpi_store.links()
    # Опт-ин (T-466): в KPI идут только логины, которые директор отметил как официантов зала.
    marked = {waiter for waiter, link in links.items() if link.get('active')}
    logins = []
    if period['status'] == 'closed' and period.get('snapshot'):
        result = period['snapshot']['result']
        fetched_at = period['snapshot'].get('fetched_at')
        logins = period['snapshot'].get('logins') or []
    else:
        base_start, base_end = baseline_window(start, scheme['baseline_weeks'])
        current = await cached_orders(state, start, min(end, today), refresh=refresh)
        base = await cached_orders(state, base_start, base_end, refresh=refresh)
        result = compute(aggregate(current['orders'], targets, marked), aggregate(base['orders'], targets, marked),
                         targets, scheme, days_total=total)
        fetched_at = current['fetched_at'] or base['fetched_at']
        # Карточки логинов: все, кто пробивал чеки зала в периоде или в базовом окне, с числом чеков периода.
        now_counts, base_counts = login_counts(current['orders']), login_counts(base['orders'])
        logins = [dict(waiter=waiter, checks=now_counts.get(waiter, 0), base_checks=base_counts.get(waiter, 0))
                  for waiter in sorted(set(now_counts) | set(base_counts), key=lambda w: (-now_counts.get(w, 0), w))]
    cards = state.accountant_roster.manager_cards() if not by_name_only else []
    for item in logins:
        link = links.get(item['waiter'])
        employee = match_employee(item['waiter'], cards, links) if link and link.get('active') else None
        item.update(active=bool(link and link.get('active')), linked=link is not None,
                    employee_id=(link or {}).get('employee_id'),
                    employee_name=employee['name'] if employee else None)
    rows = []
    for row in result['rows']:
        employee = match_employee(row['waiter'], cards, links)
        photo = None
        if employee and employee.get('photo_updated_at') and photo_url:
            photo = dict(url=photo_url(employee), updated_at=employee['photo_updated_at'])
        rows.append(dict(row, initials=initials(row['waiter']), employee_id=employee['id'] if employee else None,
                         employee_name=employee['name'] if employee else None, photo=photo,
                         linked=row['waiter'] in links))
    return dict(period=dict(id=period['id'], kind=period['kind'], start=period['start'], end=period['end'],
                            status=period['status'], days_total=total, days_passed=passed, today=today.isoformat(),
                            closed_at=period.get('closed_at')),
                scheme=scheme, targets=targets, fetched_at=fetched_at, logins=logins,
                totals={key: result[key] for key in ('team_uplift', 'net_uplift', 'fund', 'restaurant_net',
                                                      'participants', 'waiters', 'goal', 'cap')},
                hall=result['hall'], rows=rows)


def snapshot_for_close(view: dict) -> dict:
    return dict(result=dict(rows=view['rows'], hall=view['hall'], **view['totals']), fetched_at=view['fetched_at'],
                targets=view['targets'], scheme=view['scheme'], period=view['period'], logins=view['logins'])
