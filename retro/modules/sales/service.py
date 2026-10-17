"""Продажи за любой период и часть дня — из нашей истории, а не отчётом iiko на каждый вопрос.

Правила (docs/sales-history.md):

* направление строки — по кассе и отделению, как у кассира: касса школы —
  «Школа», отделение «Бехруз (Свадьба)» — «Банкет», остальное на кассе Retro —
  «Retro». Незнакомая касса — `other`, её видно в предупреждениях;
* окно часов — по времени закрытия чека (или открытия, `basis='open'`) по
  Ташкенту, правая граница не входит. Обед — `Settings.lunch_hours`;
* дней без загруженных данных в итогах нет и нулём они не становятся — они
  перечислены в `missing_days`. Сегодня — только закрытые на сейчас чеки
  (`partial`), время данных — `updated_at`;
* чек (`receipts`) — заказ с выручкой больше нуля; заказы «без оплаты»
  (Счёт Шефа, дегустация) считаются в `orders`, но не в средний чек.
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from retro.config import parse_clock
from retro.modules.cashier.archive import archive_boundary
from retro.modules.cashier.service import BANQUET_SECTION, RETRO_REGISTER, SCHOOL_REGISTER, DataError, TZ
from retro.modules.menu.catalog import READY, MenuCatalog

DIRECTIONS = ('retro', 'school', 'banquet')
DIRECTION_NAMES = {'retro': 'Retro', 'school': 'Школа', 'banquet': 'Банкет', 'other': 'Другое'}
# Наша база и контрольный отчёт iiko считаются одним OLAP с одними фильтрами:
# расходиться они могут только на округлении копеек.
TOLERANCE = Decimal('1')
QUANTITY_TOLERANCE = Decimal('0.001')
# Строки периода собираются в памяти: квартал — около ста тысяч строк.
MAX_RANGE_DAYS = 92
TOP_LIMIT = 50
# Сегодняшний день досинхронизируется перед ответом, если данным больше двух
# минут; дольше двадцати секунд ответ не ждёт — отдаёт сохранённое с пометкой.
FRESH_SECONDS = 120
FRESH_WAIT = 20
DISH_TYPE = 'Блюдо'


def directions_of(direction):
    """Направления ответа: `all` — все; иначе одно или несколько через запятую.
    Плитка обеда директора просит `retro,school` — без банкетного зала (T-500)."""
    if direction == 'all':
        return None
    names = tuple(direction.split(',')) if isinstance(direction, str) else ()
    if not names or len(set(names)) != len(names) or not set(names) <= set(DIRECTIONS):
        raise DataError('Направление — all, retro, school или banquet; несколько — через запятую.')
    return names


def direction_of(register, section):
    if register == SCHOOL_REGISTER:
        return 'school'
    if register == RETRO_REGISTER:
        if section == BANQUET_SECTION or 'бехруз' in (section or '').casefold():
            return 'banquet'
        return 'retro'
    return 'other'


def money(value):
    return None if value is None else str(value)


def whole(value):
    return str(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


# ── День: чеки из строк и сверка с контрольным отчётом ──────────────────────

def build_orders(lines, operations):
    """Чек = заказ iiko. Направление чека — то, где у него больше выручки."""
    orders = {}
    for line in lines:
        order = orders.get(line['order_id'])
        if order is None:
            order = orders[line['order_id']] = dict(
                order_id=line['order_id'], order_num=line['order_num'], opened_at=line['opened_at'],
                closed_at=line['closed_at'], register=line['register'], section=line['section'],
                lines=0, quantity=Decimal(0), gross=Decimal(0), revenue=Decimal(0), discount=Decimal(0),
                cost=Decimal(0), by_direction={})
        order['lines'] += 1
        for key in ('quantity', 'gross', 'revenue', 'discount'):
            order[key] += line[key]
        order['cost'] = None if order['cost'] is None or line['cost'] is None else order['cost'] + line['cost']
        if line['opened_at'] and (not order['opened_at'] or line['opened_at'] < order['opened_at']):
            order['opened_at'] = line['opened_at']
        if line['closed_at'] and (not order['closed_at'] or line['closed_at'] > order['closed_at']):
            order['closed_at'] = line['closed_at']
        share = order['by_direction']
        share[line['direction']] = share.get(line['direction'], Decimal(0)) + abs(line['revenue'])
    result = []
    for order in orders.values():
        shares = order.pop('by_direction')
        order['direction'] = max(shares, key=lambda name: shares[name])
        paid = operations.get(order['order_id'])
        for kind in ('paid', 'prepaid', 'other'):
            order[kind] = paid[kind] if paid else None
        result.append(order)
    return result


def totals_by_direction(lines):
    totals = {}
    orders = defaultdict(set)
    for line in lines:
        item = totals.setdefault(line['direction'], dict(revenue=Decimal(0), gross=Decimal(0),
                                                         quantity=Decimal(0)))
        for key in item:
            item[key] += line[key]
        orders[line['direction']].add(line['order_id'])
    for name, item in totals.items():
        item['orders'] = len(orders[name])
    return totals


def prepare_day(data):
    """Ответ iiko за день → строки с направлением, чеки и итог сверки."""
    lines = [dict(line, direction=direction_of(line['register'], line['section'])) for line in data['lines']]
    operations = data['operations']
    orders = build_orders(lines, operations)
    ours = totals_by_direction(lines)
    iiko = {}
    for item in data['control']:
        target = iiko.setdefault(direction_of(item['register'], item['section']),
                                 dict(revenue=Decimal(0), gross=Decimal(0), quantity=Decimal(0), orders=0))
        for key in target:
            target[key] += item[key]
    differences = []
    for name in sorted(set(ours) | set(iiko)):
        mine = ours.get(name, dict(revenue=Decimal(0), gross=Decimal(0), quantity=Decimal(0), orders=0))
        theirs = iiko.get(name, dict(revenue=Decimal(0), gross=Decimal(0), quantity=Decimal(0), orders=0))
        label = DIRECTION_NAMES.get(name, name)
        for key, limit, word in (('revenue', TOLERANCE, 'выручка'), ('gross', TOLERANCE, 'сумма до скидки'),
                                 ('quantity', QUANTITY_TOLERANCE, 'количество')):
            if abs(mine[key] - theirs[key]) > limit:
                differences.append(f'{label}: {word} в базе {mine[key]}, в iiko {theirs[key]}')
        if mine['orders'] != theirs['orders']:
            differences.append(f'{label}: заказов в базе {mine["orders"]}, в iiko {theirs["orders"]}')
    revenue = sum((line['revenue'] for line in lines), Decimal(0))
    operation_total = sum((sum(item.values(), Decimal(0)) for item in operations.values()), Decimal(0))
    if abs(operation_total - revenue) > TOLERANCE:
        differences.append(f'Выручка по видам операций {operation_total}, по строкам {revenue}')
    unmatched = {line['order_id'] for line in lines} ^ set(operations)
    if unmatched:
        differences.append(f'Заказов без пары между строками и операциями: {len(unmatched)}')
    if 'other' in ours:
        differences.append('В продажах есть незнакомая касса iiko — направление «Другое».')
    iiko_revenue = sum((item['revenue'] for item in iiko.values()), Decimal(0))

    def plain(table):
        return {name: {key: str(value) if isinstance(value, Decimal) else value for key, value in item.items()}
                for name, item in table.items()}

    reconciliation = dict(
        tolerance=str(TOLERANCE), lines=plain(ours), iiko=plain(iiko),
        operations=dict(paid=money(sum((item['paid'] for item in operations.values()), Decimal(0))),
                        prepaid=money(sum((item['prepaid'] for item in operations.values()), Decimal(0))),
                        other=money(sum((item['other'] for item in operations.values()), Decimal(0))),
                        orders=len(operations)),
        differences=differences)
    return dict(lines=lines, orders=orders, revenue=revenue, iiko_revenue=iiko_revenue,
                status='mismatch' if differences else 'ok', reconciliation=reconciliation)


# ── Окно часов ──────────────────────────────────────────────────────────────

def clock_text(minutes):
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def make_window(from_time=None, to_time=None, *, lunch=None, basis='close'):
    """Окно дня в минутах от полуночи. Без границ — весь день (None)."""
    if basis not in ('close', 'open'):
        raise DataError('Время чека — close (закрытие) или open (открытие).')
    if lunch is not None:
        if from_time is not None or to_time is not None:
            raise DataError('Укажите либо обед, либо часы — не одновременно.')
        start, end = lunch
        kind = 'lunch'
    elif from_time is None and to_time is None:
        return None
    else:
        start = parse_clock(from_time, 'from') if from_time is not None else 0
        end = parse_clock(to_time, 'to') if to_time is not None else 24 * 60
        kind = 'hours'
    if not 0 <= start < end <= 24 * 60:
        raise DataError('Начало окна должно быть раньше конца в пределах одних суток.')
    return dict(kind=kind, start=start, end=end, basis=basis,
                label=f'{clock_text(start)}–{clock_text(end)}',
                time='по времени ' + ('закрытия' if basis == 'close' else 'открытия') + ' чека, Ташкент')


def minute_of(stamp):
    """Минуты от полуночи по ISO-строке, записанной в поясе Ташкента."""
    return int(stamp[11:13]) * 60 + int(stamp[14:16])


class SalesService:
    def __init__(self, store, *, settings, sync=lambda: None, catalog=lambda: MenuCatalog([]),
                 cashier=lambda: None, clock=lambda: datetime.now(TZ)):
        self.store, self.settings, self.clock = store, settings, clock
        self._sync, self._catalog, self._cashier = sync, catalog, cashier

    @property
    def sync(self):
        return self._sync()

    @property
    def lunch(self):
        return self.settings.lunch_hours

    def period(self, start, end):
        today = self.clock().date()
        end = end or start or today
        start = start or end
        if start > end:
            raise DataError('Дата начала должна быть не позже даты конца.')
        if end > today:
            raise DataError('Будущие даты недоступны.')
        if (end - start).days >= MAX_RANGE_DAYS:
            raise DataError(f'Период — не длиннее {MAX_RANGE_DAYS} дней.')
        return start, end

    async def refresh_today(self, start, end):
        """Сегодня перед ответом — свежее двух минут; ошибка не роняет ответ."""
        sync, today = self.sync, self.clock().date()
        if sync is None:
            return None
        missing, error = [], None
        if start <= today <= end:
            error = await sync.ensure_fresh(today, max_age=FRESH_SECONDS, wait=FRESH_WAIT)
        states = await asyncio.to_thread(self.store.states, start, end)
        cursor = max(start, self.settings.sales_history_start)
        while cursor <= end:
            if cursor not in states and cursor != today:
                missing.append(cursor)
            cursor += timedelta(days=1)
        # Пропущенные дни не грузим в ответе — ставим первыми в очередь фона.
        sync.want(missing)
        return error

    async def summary(self, start=None, end=None, *, window=None, direction='all', top=10, sort='quantity',
                      include_non_dishes=False, refresh=True):
        start, end = self.period(start, end)
        selected = directions_of(direction)
        if not isinstance(top, int) or isinstance(top, bool) or not 0 <= top <= TOP_LIMIT:
            raise DataError(f'Число позиций в рейтинге — от 0 до {TOP_LIMIT}.')
        if sort not in ('quantity', 'revenue'):
            raise DataError('Рейтинг сортируется по quantity или revenue.')
        refresh_error = await self.refresh_today(start, end) if refresh else None
        result = await asyncio.to_thread(self._summary, start, end, window, direction, selected, top, sort,
                                         include_non_dishes, refresh_error)
        # Без подключения iiko пустой день — «загрузить неоткуда», а не «ещё грузится».
        result['configured'] = self.sync is not None
        return result

    def _summary(self, start, end, window, direction, selected, top, sort, include_non_dishes, refresh_error):
        now = self.clock()
        today = now.date()
        history_start = self.settings.sales_history_start
        states = self.store.states(start, end)
        days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        missing = [day for day in days if day not in states]
        partial = [day for day in days if day in states and (day >= today or not states[day]['closed'])]
        lines = self.store.lines(start, end, ('order_id', 'opened_at', 'closed_at', 'direction',
                                              'dish_name', 'dish_type', 'quantity', 'gross', 'revenue',
                                              'cost'))
        if selected is not None:
            lines = [line for line in lines if line['direction'] in selected]
        untimed = 0
        if window is not None:
            field = 'closed_at' if window['basis'] == 'close' else 'opened_at'
            selected = []
            for line in lines:
                if not line[field]:
                    untimed += 1
                elif window['start'] <= minute_of(line[field]) < window['end']:
                    selected.append(line)
            lines = selected
        warnings = []
        if missing:
            before = [day for day in missing if day < history_start]
            warnings.append('Нет загруженных данных за ' + ', '.join(day.strftime('%d.%m') for day in missing[:10])
                            + (f' и ещё {len(missing) - 10}' if len(missing) > 10 else '')
                            + ' — эти дни в итоги не вошли.')
            if before:
                warnings.append(f'История продаж в базе начинается с {history_start:%d.%m.%Y}.')
        if today in states and start <= today <= end:
            warnings.append('Сегодня учтены только закрытые чеки на '
                            f'{states[today]["fetched_at"].astimezone(TZ):%H:%M}; открытые столы iiko не отдаёт.')
            if window is not None and minute_of(now.isoformat()) < window['end']:
                warnings.append(f'Окно {window["label"]} сегодня ещё не закончилось.')
        stale = [day for day in partial if day < today]
        if stale:
            warnings.append('Ещё не перечитаны после закрытия смены: '
                            + ', '.join(day.strftime('%d.%m') for day in stale) + '.')
        mismatched = [day for day in days if day in states and states[day]['status'] != 'ok']
        if mismatched:
            warnings.append('Сверка с итогом iiko не сошлась за '
                            + ', '.join(day.strftime('%d.%m') for day in mismatched) + '.')
        if untimed:
            warnings.append(f'Строк без времени чека: {untimed}; в окно часов они не попали.')
        if refresh_error:
            warnings.append(refresh_error)
        covered = [day for day in days if day in states]
        updated = max((states[day]['fetched_at'] for day in covered), default=None)
        result = dict(
            source='iiko OLAP SALES · история чеков в базе панели',
            timezone=str(TZ), period=dict(start=start.isoformat(), end=end.isoformat()),
            window=window and {key: window[key] for key in ('kind', 'label', 'basis', 'time')},
            lunch_hours=f'{clock_text(self.lunch[0])}–{clock_text(self.lunch[1])}',
            direction=direction, history_start=history_start.isoformat(),
            complete=not missing and not partial, days=len(days), days_with_data=len(covered),
            missing_days=[day.isoformat() for day in missing],
            partial=bool(partial), partial_days=[day.isoformat() for day in partial],
            updated_at=updated.astimezone(TZ).isoformat(timespec='seconds') if updated else None,
            refresh_error=refresh_error, warnings=warnings)
        if not covered:
            # Данных нет вовсе: итоги неизвестны, а не равны нулю.
            result.update(totals=None, by_direction={}, by_hour=[], by_day=[], top_items=[],
                          excluded_items={})
            return result
        result['totals'] = self._totals(lines)
        result['by_direction'] = {
            name: self._totals([line for line in lines if line['direction'] == name])
            for name in sorted({line['direction'] for line in lines})}
        field = 'opened_at' if window and window['basis'] == 'open' else 'closed_at'
        hours = defaultdict(list)
        for line in lines:
            if line[field]:
                hours[minute_of(line[field]) // 60].append(line)
        result['by_hour'] = [dict(hour=f'{hour:02d}:00', **self._totals(items, short=True))
                             for hour, items in sorted(hours.items())]
        per_day = defaultdict(list)
        for line in lines:
            per_day[line['day']].append(line)
        result['by_day'] = [dict(day=day.isoformat(), **self._totals(per_day.get(day, []), short=True),
                                 partial=day in partial, status=states[day]['status'],
                                 updated_at=states[day]['fetched_at'].astimezone(TZ).isoformat(timespec='seconds'))
                            for day in covered]
        result['top_items'], result['excluded_items'], checked = self._top(lines, top, sort, include_non_dishes)
        result['top_sort'] = sort
        result['top_scope'] = 'all' if include_non_dishes else 'ready_dishes'
        result['catalog_checked'] = checked
        if not checked and not include_non_dishes:
            warnings.append('Справочник меню не загружен: блюда отобраны по типу позиции iiko.')
        return result

    @staticmethod
    def _totals(lines, *, short=False):
        revenue = sum((line['revenue'] for line in lines), Decimal(0))
        by_order = defaultdict(Decimal)
        for line in lines:
            by_order[line['order_id']] += line['revenue']
        receipts = sum(1 for amount in by_order.values() if amount > 0)
        result = dict(revenue=str(revenue), receipts=receipts,
                      average_check=whole(revenue / receipts) if receipts else None)
        if short:
            return result
        gross = sum((line['gross'] for line in lines), Decimal(0))
        costs = [line['cost'] for line in lines if line['cost'] is not None]
        return dict(result, orders=len(by_order), gross=str(gross), discount=str(gross - revenue),
                    quantity=str(sum((line['quantity'] for line in lines), Decimal(0))),
                    cost=str(sum(costs, Decimal(0)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)),
                    cost_complete=len(costs) == len(lines))

    def _top(self, lines, top, sort, include_non_dishes):
        catalog = self._catalog()
        items = {}
        for line in lines:
            item = items.setdefault(line['dish_name'], dict(name=line['dish_name'], quantity=Decimal(0),
                                                            revenue=Decimal(0), types=set()))
            item['quantity'] += line['quantity']
            item['revenue'] += line['revenue']
            item['types'].add(line['dish_type'])
        ranked, excluded = [], {}
        for item in items.values():
            item_class = catalog.classify(item['name'])
            # Карточки нет в недельном справочнике (новое блюдо или справочник
            # не загружен): решает тип позиции в самом отчёте продаж.
            keep = include_non_dishes or item_class in READY or (
                item_class == 'unknown' and item['types'] == {DISH_TYPE})
            if not keep:
                bucket = excluded.setdefault(item_class, dict(items=0, revenue=Decimal(0)))
                bucket['items'] += 1
                bucket['revenue'] += item['revenue']
                continue
            ranked.append(dict(name=item['name'], quantity=str(item['quantity']), revenue=str(item['revenue']),
                               item_class=item_class, _sort=(item[sort], item['revenue'])))
        ranked.sort(key=lambda row: row['_sort'], reverse=True)
        for row in ranked:
            del row['_sort']
        return (ranked[:top], {name: dict(items=bucket['items'], revenue=str(bucket['revenue']))
                               for name, bucket in sorted(excluded.items())}, catalog.available)

    # ── Сверка дня ──────────────────────────────────────────────────────────

    async def reconcile(self, day):
        return await asyncio.to_thread(self._reconcile, day)

    def _reconcile(self, day):
        """Наша база против контрольного отчёта iiko (при загрузке) и снимка кассы."""
        state = self.store.state(day)
        if state is None:
            return dict(day=day.isoformat(), status='missing', differences=['День ещё не загружен.'])
        result = dict(day=day.isoformat(), status=state['status'], closed=state['closed'],
                      updated_at=state['fetched_at'].astimezone(TZ).isoformat(timespec='seconds'),
                      revisions=state['revisions'], revenue=str(state['revenue']),
                      iiko_revenue=money(state['iiko_revenue']), **state['reconciliation'])
        archive = self._cashier()
        snapshot = archive.get(day) if archive is not None else None
        if snapshot is None or snapshot.revenue_breakdown is None:
            result['cashier'] = None
            return result
        lines = state['reconciliation'].get('lines', {})
        breakdown = snapshot.revenue_breakdown
        paid_retro = sum((order['paid'] or Decimal(0) for order in self.store.orders(day, day)
                          if order['direction'] == 'retro'), Decimal(0))
        compared, differences = {}, []
        for name, value in (('retro', breakdown.retro), ('school', breakdown.school),
                            ('banquet', breakdown.bekhruz_banquet)):
            ours = Decimal(lines.get(name, {}).get('revenue', '0'))
            compared[name] = dict(ours=str(ours), cashier=str(value), difference=str(ours - value))
            if abs(ours - value) > TOLERANCE:
                differences.append(f'{DIRECTION_NAMES[name]}: в базе {ours}, у кассира {value}')
        if snapshot.revenue is not None and abs(paid_retro - snapshot.revenue) > TOLERANCE:
            differences.append(f'Оплаты Retro в базе {paid_retro}, выручка кассира {snapshot.revenue}')
        fetched = snapshot.fetched_at.astimezone(TZ)
        result['cashier'] = dict(
            fetched_at=fetched.isoformat(timespec='seconds'), directions=compared,
            paid_retro=str(paid_retro), cashier_revenue=money(snapshot.revenue),
            status='mismatch' if differences else 'ok', differences=differences,
            # Снимки читались в разное время: за открытый день разница — это
            # чеки, закрытые между двумя чтениями, а не ошибка.
            comparable=fetched >= archive_boundary(day) and state['closed'])
        return result

    # ── Состояние синхронизации ─────────────────────────────────────────────

    async def status(self):
        return await asyncio.to_thread(self._status)

    def _status(self):
        today = self.clock().date()
        start = self.settings.sales_history_start
        states = self.store.states(start, today) if start <= today else {}
        missing = []
        cursor = start
        while cursor <= today:
            if cursor not in states:
                missing.append(cursor.isoformat())
            cursor += timedelta(days=1)
        sync = self.sync
        return dict(history_start=start.isoformat(), coverage=self.store.coverage(),
                    lunch_hours=f'{clock_text(self.lunch[0])}–{clock_text(self.lunch[1])}',
                    configured=sync is not None, running=sorted(day.isoformat() for day in sync.jobs) if sync else [],
                    missing_days=missing, missing_count=len(missing),
                    mismatch_days=[day.isoformat() for day, state in states.items() if state['status'] != 'ok'],
                    today=(dict(updated_at=states[today]['fetched_at'].astimezone(TZ).isoformat(timespec='seconds'),
                                revenue=str(states[today]['revenue']), orders=states[today]['orders'])
                           if today in states else None),
                    runs=self.store.runs(10), failures=self.store.runs(5, status='failed'))
