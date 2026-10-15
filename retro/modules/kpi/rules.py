"""KPI официантов: правила чека, целевые позиции и расчёт премии. Без iiko и без базы.

Схема (KPI-Waiters/03-scheme.md): премия платится из прироста валовой маржи по
целевым позициям над собственной базой официанта (его частота на 100 чеков за
предыдущие недели). Фонд — доля прироста, часть лично, часть в командный пул.
Считается только чек зала, оплаченный, без удалений, без ручной льготы, без
безналичных «Счет Шефа / Дегустация / Комплимент» и целиком на одном официанте.
"""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

SCHOOL_REGISTER = 'GL-Kassa-Oksbrich'
BANQUET_SECTION = 'Бехруз (Свадьба)'
TAKEAWAY_SECTION, DELIVERY_SECTION, SCHOOL_SECTION = 'Вынос', 'Доставка', 'Зал'
YANDEX_PAY = frozenset({'Яндех Еда'})
UZUM_PAY = frozenset({'я Узум Тезкор'})
FREE_PAYMENT_TYPES = frozenset({'Счет Шефа', 'Дегустация', 'Комплимент'})
SERVICE_LOGIN = re.compile(r'(?i)касса|менеджер|счет шефа|свадьба')
BANQUET_NAME = re.compile(r'(?i)свадьба|бехруз|новый год|(^|\| )1 ')
AGGREGATOR_NAME = re.compile(r'(?i)яндекс|(^|\| )узум')
RENT_NAME = re.compile(r'(?i)(^|\| )аренда зала')

REASONS = {
    'school': 'школьный буфет', 'banquet': 'свадьба или банкет', 'yandex': 'Яндекс', 'uzum': 'Узум',
    'rent': 'аренда зала', 'takeaway': 'вынос', 'delivery': 'доставка', 'order_deleted': 'чек удалён',
    'deleted_lines': 'удалённые позиции', 'discount': 'ручная льгота', 'free': 'без оплаты',
    'service_login': 'служебный логин', 'multi_waiter': 'перенос между официантами', 'empty': 'нулевой чек',
}
TARGET_GROUPS = ('блюдо', 'напиток', 'десерт', 'новинка')

# Дефолты пилота 19–25.10.2026 (KPI-Waiters/02-targets.md): карточки iiko, цель на 100 чеков зала и
# опорная маржа на порцию за 17.08–11.10.2026. Директор меняет всё это в настройках периода.
PILOT_START = date(2026, 10, 19)
DEFAULT_TARGETS = [
    dict(key='ezogelin', title='Эзогелин (суп)', group='блюдо', target100=4.0, margin=32280,
         dishes=[('1999177a-715c-4d87-bd66-52da61a4985f', 'Суп Эзогелин')]),
    dict(key='mushroom', title='Грибной суп со сливками', group='блюдо', target100=3.5, margin=34295,
         dishes=[('c1901c76-06b5-4fc2-b372-87ac9761dd29', 'Суп Грибной со сливками')]),
    dict(key='pide', title='Пиде ассорти', group='блюдо', target100=3.0, margin=49379,
         dishes=[('c552b370-4af0-47b6-b359-f151e469be57', 'Пиде Ассорти'), ('a91a0b9e-5f73-4cb9-a067-9cc9b26b995c', 'Пиде Ассорти')]),
    dict(key='beetfeta', title='Салат со свеклой, фетой и апельсином', group='блюдо', target100=2.5, margin=46713,
         dishes=[('69f033f4-00e0-40e5-8477-48c9799d986f', 'Салат со свеклой, фетой и апельсинами')]),
    dict(key='teas', title='Фирменные чаи (чайник)', group='напиток', target100=5.0, margin=35720,
         dishes=[('04a2ce6c-eb6c-4795-b313-da6ea3b65292', 'ЧАЙ Жасминовый'), ('2882fedd-8f9a-4c1e-8b52-aa2deb54fd46', 'ЧАЙ Имбирный'),
                 ('3f5343c4-84c6-4d76-8d97-d4ab2b99f0e1', 'ЧАЙ Фруктовый'), ('d7dc4e7e-1b3b-4f84-b593-f2357f111b13', 'ЧАЙ Цитрусовый')]),
    dict(key='kompot', title='Компот 1 л', group='напиток', target100=7.0, margin=39236,
         dishes=[('cb564d3c-4ec5-4717-a44f-08faeb51ce42', 'КОМПОТ Фруктовый графин 1л')]),
    dict(key='milkshake', title='Милкшейк банановый', group='напиток', target100=2.0, margin=36106,
         dishes=[('43463d65-f0ca-453d-a6b5-a9583e126335', 'КОКТЕЙЛЬ " Милк Шейк"банановый')]),
    dict(key='fondant', title='Шоколадный фондан', group='десерт', target100=3.0, margin=26288,
         dishes=[('fe42af72-f3db-4af2-a751-9019a351dc0d', 'ДЕСЕРТ ШОКОЛАДНЫЙ ФОНДАН')]),
    dict(key='katmir', title='Катмир кассиб', group='десерт', target100=1.5, margin=38805,
         dishes=[('509ba748-ada6-4d18-b4f4-5ba19300f0ab', 'ДЕСЕРТ КАТМИР КАССИБ')]),
    dict(key='rulka', title='Рулька с пюре', group='новинка', target100=1.5, margin=90753,
         dishes=[('b658af09-e4fa-4aa9-a571-030b70814c9e', 'Рулька с пюре')]),
    dict(key='dumgaza', title='Думгаза с картофелем', group='новинка', target100=1.5, margin=85895,
         dishes=[('a402ceea-ff77-45ff-8675-be4b07aa75bd', 'Думгаза кебаб')]),
    dict(key='sudak', title='Судак жареный (0,5 кг или 1 кг)', group='новинка', target100=2.0, margin=80364,
         dishes=[('05acdae3-bdb2-4042-8cc0-a6f7434cdce3', 'Судак жареный филе 0,5'), ('622fad41-351a-4912-96f5-1bfdc1d62496', 'Судак жареный филе 1 кг')]),
]
DEFAULT_SCHEME = dict(share=0.25, personal=0.7, team=0.3, cap=1_000_000, min_checks=20, min_shifts=3,
                      baseline_weeks=4, baseline_min_checks=40, netting=True)
MAX_TARGETS = 20


class KpiError(ValueError):
    pass


@dataclass(frozen=True)
class Line:
    dish_id: str
    name: str
    quantity: float
    revenue: float
    cost: float


@dataclass
class Order:
    id: str
    day: date
    register: str
    section: str
    waiters: frozenset
    lines: tuple
    discount_types: frozenset = frozenset()
    noncash_types: frozenset = frozenset()
    pay_types: frozenset = frozenset()
    deleted_lines: bool = False
    order_deleted: bool = False

    @property
    def waiter(self) -> str:
        return next(iter(self.waiters)) if len(self.waiters) == 1 else '|'.join(sorted(self.waiters))

    @property
    def paid_lines(self):
        return [line for line in self.lines if line.revenue > 0]


def channel(order: Order) -> str:
    """Канал чека по правилам книги меню-инжиниринга (questions.md §6–8)."""
    joined = ' | '.join(line.name.lower() for line in order.lines)
    if order.register == SCHOOL_REGISTER or order.section == SCHOOL_SECTION or re.search(r'(^|\| )школа', joined):
        return 'school'
    if order.section == BANQUET_SECTION or BANQUET_NAME.search(joined):
        return 'banquet'
    if 'яндекс' in joined or order.pay_types & YANDEX_PAY:
        return 'yandex'
    if re.search(r'(^|\| )узум', joined) or order.pay_types & UZUM_PAY:
        return 'uzum'
    if RENT_NAME.search(joined):
        return 'rent'
    if order.section == DELIVERY_SECTION:
        return 'delivery'
    if order.section == TAKEAWAY_SECTION:
        return 'takeaway'
    return 'hall'


def exclusion_reasons(order: Order) -> list:
    reasons = []
    kind = channel(order)
    if kind != 'hall':
        reasons.append(kind)
    if order.order_deleted:
        reasons.append('order_deleted')
    if order.deleted_lines:
        reasons.append('deleted_lines')
    if order.discount_types:
        reasons.append('discount')
    if order.noncash_types & FREE_PAYMENT_TYPES:
        reasons.append('free')
    if len(order.waiters) > 1:
        reasons.append('multi_waiter')
    elif any(SERVICE_LOGIN.search(name or '') for name in order.waiters):
        reasons.append('service_login')
    if not order.paid_lines:
        reasons.append('empty')
    return reasons


@dataclass
class Stats:
    checks: int = 0
    days: set = field(default_factory=set)
    quantity: Counter = field(default_factory=Counter)        # key → порций
    by_day: dict = field(default_factory=lambda: defaultdict(Counter))  # day → key → порций
    checks_by_day: Counter = field(default_factory=Counter)

    def rate(self, key) -> float:
        return self.quantity[key] * 100 / self.checks if self.checks else 0.0


HALL = '__hall__'


def aggregate(orders, targets, waiters=None) -> dict:
    """Официант → Stats по включённым чекам зала; HALL — весь зал вместе.

    `waiters` — логины, отмеченные директором как официанты зала (опт-ин, T-466): чеки остальных
    логинов в KPI не идут вовсе, ни в рейтинг, ни в итоги зала. None — без фильтра (тесты правил)."""
    dish_key = {dish_id: target['key'] for target in targets for dish_id, _name in target['dishes']}
    stats = defaultdict(Stats)
    for order in orders:
        if exclusion_reasons(order):
            continue
        if waiters is not None and order.waiter not in waiters:
            continue
        for who in (order.waiter, HALL):
            item = stats[who]
            item.checks += 1
            item.days.add(order.day)
            item.checks_by_day[order.day.isoformat()] += 1
            for line in order.paid_lines:
                key = dish_key.get(line.dish_id)
                if key:
                    item.quantity[key] += line.quantity
                    item.by_day[order.day.isoformat()][key] += line.quantity
    return stats


def login_counts(orders) -> Counter:
    """Логин → включённых чеков зала, без фильтра по отметке: список для карточек в настройке периода."""
    counts = Counter()
    for order in orders:
        if not exclusion_reasons(order):
            counts[order.waiter] += 1
    return counts


def normalize_scheme(raw) -> dict:
    if not isinstance(raw, dict):
        raise KpiError('Настройки схемы должны быть объектом.')
    scheme = dict(DEFAULT_SCHEME)
    for name in ('share', 'personal', 'team'):
        value = raw.get(name, scheme[name])
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise KpiError(f'Поле {name}: нужно число.') from None
        if not 0 <= value <= 1:
            raise KpiError(f'Поле {name}: доля от 0 до 1.')
        scheme[name] = value
    if scheme['share'] > 0.5:
        raise KpiError('Доля фонда больше половины прироста: ресторан перестаёт выигрывать.')
    if abs(scheme['personal'] + scheme['team'] - 1) > 1e-6:
        raise KpiError('Личная и командная части вместе должны давать 100 %.')
    for name in ('cap', 'min_checks', 'min_shifts', 'baseline_weeks', 'baseline_min_checks'):
        value = raw.get(name, scheme[name])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or int(value) != value:
            raise KpiError(f'Поле {name}: целое число не меньше нуля.')
        scheme[name] = int(value)
    if not 1 <= scheme['baseline_weeks'] <= 12:
        raise KpiError('База: от 1 до 12 недель.')
    scheme['netting'] = bool(raw.get('netting', scheme['netting']))
    return scheme


def normalize_targets(raw) -> list:
    if not isinstance(raw, list) or not raw:
        raise KpiError('Нужна хотя бы одна целевая позиция.')
    if len(raw) > MAX_TARGETS:
        raise KpiError(f'Не больше {MAX_TARGETS} целевых позиций: официант столько не удержит.')
    targets, keys, dish_ids = [], set(), set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise KpiError('Позиция должна быть объектом.')
        title = str(item.get('title') or '').strip()
        if not title:
            raise KpiError(f'Позиция {index}: нет названия.')
        key = str(item.get('key') or f't{index}').strip()
        if key in keys:
            raise KpiError(f'Позиция «{title}»: ключ повторяется.')
        keys.add(key)
        group = item.get('group') or 'блюдо'
        if group not in TARGET_GROUPS:
            raise KpiError(f'Позиция «{title}»: группа из списка {", ".join(TARGET_GROUPS)}.')
        dishes = []
        for dish in item.get('dishes') or ():
            if isinstance(dish, dict):
                dish = (dish.get('id'), dish.get('name'))
            if not isinstance(dish, (list, tuple)) or len(dish) != 2 or not dish[0]:
                raise KpiError(f'Позиция «{title}»: карточка iiko без кода.')
            dish_id, name = str(dish[0]), str(dish[1] or '')
            if dish_id in dish_ids:
                raise KpiError(f'Карточка «{name}» уже в другой позиции.')
            dish_ids.add(dish_id)
            dishes.append((dish_id, name))
        if not dishes:
            raise KpiError(f'Позиция «{title}»: выберите хотя бы одну карточку iiko.')
        try:
            target100 = float(item.get('target100', 0))
            margin = float(item.get('margin')) if item.get('margin') not in (None, '') else None
        except (TypeError, ValueError):
            raise KpiError(f'Позиция «{title}»: цель и маржа — числа.') from None
        if target100 < 0 or target100 > 100:
            raise KpiError(f'Позиция «{title}»: цель на 100 чеков от 0 до 100.')
        if margin is not None and margin <= 0:
            raise KpiError(f'Позиция «{title}»: маржа на порцию должна быть больше нуля.')
        targets.append(dict(key=key, title=title, group=group, target100=target100, margin=margin, dishes=dishes))
    return targets


def default_settings() -> dict:
    return dict(scheme=dict(DEFAULT_SCHEME),
                targets=[dict(item, dishes=[tuple(dish) for dish in item['dishes']]) for item in DEFAULT_TARGETS])


def compute(now: dict, base: dict, targets, scheme, *, days_total=7) -> dict:
    """Рейтинг периода: прирост маржи, участие, премия на человека, итоги зала.

    `now`/`base` — результат aggregate() за период и за базовое окно. Потолок
    премии задан на неделю; на более длинном периоде он растёт пропорционально."""
    hall_base = base.get(HALL, Stats())
    hall_now = now.get(HALL, Stats())
    cap = scheme['cap'] * max(1, round(days_total / 7))
    margins = {target['key']: float(target.get('margin') or 0) for target in targets}
    rows = []
    for waiter, stats in now.items():
        if waiter == HALL:
            continue
        own = base.get(waiter)
        own_base = own is not None and own.checks >= scheme['baseline_min_checks']
        source = own if own_base else hall_base
        eligible = stats.checks >= scheme['min_checks'] and len(stats.days) >= scheme['min_shifts']
        items, uplift = [], 0.0
        for target in targets:
            key = target['key']
            before, current = source.rate(key), stats.rate(key)
            gain = (current - before) / 100 * stats.checks * margins[key]
            uplift += gain if scheme['netting'] else max(gain, 0.0)
            items.append(dict(key=key, title=target['title'], group=target['group'], base=round(before, 2),
                              now=round(current, 2), target=target['target100'], quantity=stats.quantity[key],
                              uplift=round(gain), margin=margins[key],
                              by_day={day: counter[key] for day, counter in stats.by_day.items() if counter[key]}))
        rows.append(dict(waiter=waiter, checks=stats.checks, shifts=len(stats.days), eligible=eligible,
                         own_base=own_base, base_checks=own.checks if own else 0,
                         uplift=round(uplift), uplift_positive=round(max(uplift, 0.0)), items=items,
                         checks_by_day=dict(stats.checks_by_day)))
    eligible_rows = [row for row in rows if row['eligible']]
    team_uplift = sum(row['uplift_positive'] for row in eligible_rows)
    team_checks = sum(row['checks'] for row in eligible_rows)
    for row in rows:
        if not row['eligible']:
            row.update(personal=0, team=0, premium=0)
            continue
        personal = scheme['share'] * scheme['personal'] * row['uplift_positive']
        team = scheme['share'] * scheme['team'] * team_uplift * row['checks'] / team_checks if team_checks else 0.0
        row.update(personal=round(personal), team=round(team), premium=round(min(personal + team, cap)))
    rows.sort(key=lambda row: (not row['eligible'], -row['uplift'], row['waiter']))
    net_uplift = sum(row['uplift'] for row in eligible_rows)
    fund = sum(row['premium'] for row in rows)
    # Цель периода: сколько маржи даст зал, если выйдет на все цели при том же числе чеков в день.
    base_days = max(len(hall_base.days), 1)
    expected_checks = hall_base.checks / base_days * days_total if hall_base.checks else hall_now.checks
    goal = sum(max(target['target100'] - hall_base.rate(target['key']), 0) / 100 * expected_checks
               * margins[target['key']] for target in targets)
    return dict(rows=rows, team_uplift=round(team_uplift), net_uplift=round(net_uplift), fund=round(fund),
                restaurant_net=round(net_uplift - fund), participants=len(eligible_rows), waiters=len(rows),
                hall=dict(checks=hall_now.checks, base_checks=hall_base.checks, days=len(hall_now.days),
                          rates={t['key']: dict(base=round(hall_base.rate(t['key']), 2), now=round(hall_now.rate(t['key']), 2),
                                                target=t['target100']) for t in targets}),
                goal=round(goal), cap=cap)


def week_bounds(day: date):
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def month_bounds(day: date):
    start = day.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


def next_bounds(kind: str, end: date):
    first = end + timedelta(days=1)
    return week_bounds(first) if kind == 'week' else month_bounds(first)


def baseline_window(start: date, weeks: int):
    return start - timedelta(days=7 * weeks), start - timedelta(days=1)


def money(value) -> str:
    return str(Decimal(int(round(value))))
