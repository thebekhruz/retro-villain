"""KPI официантов: правила чека, целевые позиции и расчёт премии. Без iiko и без базы.

Схема (KPI-Waiters/03-scheme.md): премия платится из прироста валовой маржи по
целевым позициям над собственной базой официанта (его частота на 100 чеков за
предыдущие недели). Фонд — доля прироста, часть лично, часть в командный пул.
Считается только чек зала, оплаченный, без удалений, без ручной льготы, без
безналичных «Счет Шефа / Дегустация / Комплимент» и целиком на одном официанте.
"""
import math
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
    dict(key='ezogelin', title='Эзогелин (суп)', group='блюдо', target100=None, manual=False, margin=32280,
         dishes=[('1999177a-715c-4d87-bd66-52da61a4985f', 'Суп Эзогелин')]),
    dict(key='mushroom', title='Грибной суп со сливками', group='блюдо', target100=None, manual=False, margin=34295,
         dishes=[('c1901c76-06b5-4fc2-b372-87ac9761dd29', 'Суп Грибной со сливками')]),
    dict(key='pide', title='Пиде ассорти', group='блюдо', target100=None, manual=False, margin=49379,
         dishes=[('c552b370-4af0-47b6-b359-f151e469be57', 'Пиде Ассорти'), ('a91a0b9e-5f73-4cb9-a067-9cc9b26b995c', 'Пиде Ассорти')]),
    dict(key='beetfeta', title='Салат со свеклой, фетой и апельсином', group='блюдо', target100=None, manual=False, margin=46713,
         dishes=[('69f033f4-00e0-40e5-8477-48c9799d986f', 'Салат со свеклой, фетой и апельсинами')]),
    dict(key='teas', title='Фирменные чаи (чайник)', group='напиток', target100=None, manual=False, margin=35720,
         dishes=[('04a2ce6c-eb6c-4795-b313-da6ea3b65292', 'ЧАЙ Жасминовый'), ('2882fedd-8f9a-4c1e-8b52-aa2deb54fd46', 'ЧАЙ Имбирный'),
                 ('3f5343c4-84c6-4d76-8d97-d4ab2b99f0e1', 'ЧАЙ Фруктовый'), ('d7dc4e7e-1b3b-4f84-b593-f2357f111b13', 'ЧАЙ Цитрусовый')]),
    dict(key='kompot', title='Компот 1 л', group='напиток', target100=None, manual=False, margin=39236,
         dishes=[('cb564d3c-4ec5-4717-a44f-08faeb51ce42', 'КОМПОТ Фруктовый графин 1л')]),
    dict(key='milkshake', title='Милкшейк банановый', group='напиток', target100=None, manual=False, margin=36106,
         dishes=[('43463d65-f0ca-453d-a6b5-a9583e126335', 'КОКТЕЙЛЬ " Милк Шейк"банановый')]),
    dict(key='fondant', title='Шоколадный фондан', group='десерт', target100=None, manual=False, margin=26288,
         dishes=[('fe42af72-f3db-4af2-a751-9019a351dc0d', 'ДЕСЕРТ ШОКОЛАДНЫЙ ФОНДАН')]),
    dict(key='katmir', title='Катмир кассиб', group='десерт', target100=None, manual=False, margin=38805,
         dishes=[('509ba748-ada6-4d18-b4f4-5ba19300f0ab', 'ДЕСЕРТ КАТМИР КАССИБ')]),
    dict(key='rulka', title='Рулька с пюре', group='новинка', target100=None, manual=False, margin=90753,
         dishes=[('b658af09-e4fa-4aa9-a571-030b70814c9e', 'Рулька с пюре')]),
    dict(key='dumgaza', title='Думгаза с картофелем', group='новинка', target100=None, manual=False, margin=85895,
         dishes=[('a402ceea-ff77-45ff-8675-be4b07aa75bd', 'Думгаза кебаб')]),
    dict(key='sudak', title='Судак жареный (0,5 кг или 1 кг)', group='новинка', target100=None, manual=False, margin=80364,
         dishes=[('05acdae3-bdb2-4042-8cc0-a6f7434cdce3', 'Судак жареный филе 0,5'), ('622fad41-351a-4912-96f5-1bfdc1d62496', 'Судак жареный филе 1 кг')]),
]
# Фонд 40 % от прироста сверх порога шума 1,5 млн в неделю (решение Amir 17.10, T-490).
DEFAULT_SCHEME = dict(share=0.40, noise_week=1_500_000, cap=1_000_000, min_checks=20, min_shifts=3, baseline_weeks=4,
                      netting=True)
# Схема до T-490: без порога шума, доля 25 % по умолчанию. Такие сохранённые настройки поднимаются до новых дефолтов.
LEGACY_SHARE = 0.25
# Прежние поля схемы (личная часть 70 %, своя база от N чеков) — читаются из старых настроек и отбрасываются (T-468).
LEGACY_SCHEME_KEYS = ('personal', 'team', 'baseline_min_checks')
MAX_TARGETS = 20
# Цель на 100 чеков по умолчанию (решение Amir 17.10, T-490; до этого +30 %, T-469): база зала + 70 %, но не меньше
# чем база + 0,75, с округлением вверх до 0,5. Ручная цель директора действует, только если она выше правила.
TARGET_GROWTH = 1.7
TARGET_MIN_STEP = 0.75


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


def upgrade_scheme(scheme: dict) -> dict:
    """Сохранённая схема до T-490 (без порога шума): порог по умолчанию, доля 25 % → 40 %. Ручная доля остаётся."""
    if not isinstance(scheme, dict) or 'noise_week' in scheme:
        return scheme
    upgraded = dict(scheme, noise_week=DEFAULT_SCHEME['noise_week'])
    if abs(float(upgraded.get('share', LEGACY_SHARE)) - LEGACY_SHARE) < 1e-9:
        upgraded['share'] = DEFAULT_SCHEME['share']
    return upgraded


def normalize_scheme(raw) -> dict:
    if not isinstance(raw, dict):
        raise KpiError('Настройки схемы должны быть объектом.')
    scheme = dict(DEFAULT_SCHEME)
    try:
        share = float(raw.get('share', scheme['share']))
    except (TypeError, ValueError):
        raise KpiError('Поле share: нужно число.') from None
    if not 0 <= share <= 1:
        raise KpiError('Поле share: доля от 0 до 1.')
    if share > 0.5:
        raise KpiError('Доля фонда больше половины прироста: ресторан перестаёт выигрывать.')
    scheme['share'] = share
    for name in ('noise_week', 'cap', 'min_checks', 'min_shifts', 'baseline_weeks'):
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
        manual = bool(item.get('manual')) and item.get('target100') not in (None, '')
        try:
            target100 = float(item['target100']) if manual else None
            margin = float(item.get('margin')) if item.get('margin') not in (None, '') else None
        except (TypeError, ValueError):
            raise KpiError(f'Позиция «{title}»: цель и маржа — числа.') from None
        if target100 is not None and not 0 < target100 <= 100:
            raise KpiError(f'Позиция «{title}»: цель на 100 чеков больше 0 и не больше 100.')
        if margin is not None and margin <= 0:
            raise KpiError(f'Позиция «{title}»: маржа на порцию должна быть больше нуля.')
        targets.append(dict(key=key, title=title, group=group, target100=target100, manual=manual, margin=margin,
                            dishes=dishes))
    return targets


def auto_target(base100: float) -> float:
    """Цель на 100 чеков по правилу: max(база × 1,7; база + 0,75), вверх до 0,5. База 0 → 1."""
    raw = max(base100 * TARGET_GROWTH, base100 + TARGET_MIN_STEP)
    return math.ceil(round(raw * 2, 6)) / 2


def effective_targets(targets, hall_base) -> list:
    """Цели периода от базы зала: правило или ручная цель директора, если она выше правила."""
    out = []
    for target in targets:
        auto = auto_target(hall_base.rate(target['key']))
        manual = target.get('target100') if target.get('manual') else None
        if manual is not None and manual > auto:
            out.append(dict(target, target100=manual, target_auto=auto, target_source='manual'))
        else:
            out.append(dict(target, target100=auto, target_auto=auto, target_source='base'))
    return out


def default_settings() -> dict:
    return dict(scheme=dict(DEFAULT_SCHEME),
                targets=[dict(item, dishes=[tuple(dish) for dish in item['dishes']]) for item in DEFAULT_TARGETS])


def compute(now: dict, base: dict, targets, scheme, *, days_total=7, days_passed=None) -> dict:
    """Период целиком: командный прирост, фонд и доли (T-468, решение Amir 15.10).

    Прирост считается по залу: его частота каждой целевой позиции на 100 чеков за период против средней
    частоты зала за базовые недели, умноженная на чеки периода и опорную маржу. Фонд = доля прироста,
    делится между участниками (порог чеков и смен) пропорционально числу их чеков; потолок задан на
    неделю и растёт с длиной периода. Личных баз нет: позиции у человека показываются против базы зала."""
    hall_base = base.get(HALL, Stats())
    hall_now = now.get(HALL, Stats())
    targets = effective_targets(targets, hall_base)
    cap = scheme['cap'] * max(1, round(days_total / 7))
    margins = {target['key']: float(target.get('margin') or 0) for target in targets}
    # Ожидаемые чеки и цель периода в штуках: средние чеки базовых недель, приведённые к длине периода.
    base_days = max(len(hall_base.days), 1)
    expected_checks = hall_base.checks / base_days * days_total if hall_base.checks else hall_now.checks
    passed = days_total if days_passed is None else max(0, min(days_passed, days_total))
    progress, team_uplift = [], 0.0
    for target in targets:
        key = target['key']
        before, current = hall_base.rate(key), hall_now.rate(key)
        gain = (current - before) / 100 * hall_now.checks * margins[key]
        team_uplift += gain if scheme['netting'] else max(gain, 0.0)
        goal = target['target100'] / 100 * expected_checks
        sold_qty = hall_now.quantity[key]
        expected_today = goal * passed / days_total if days_total else goal
        progress.append(dict(key=key, title=target['title'], group=target['group'], margin=margins[key],
                             base=round(before, 2), now=round(current, 2), target=target['target100'],
                             target_source=target['target_source'], target_auto=target['target_auto'],
                             sold=round(sold_qty), goal_qty=round(goal, 1), remaining=max(0, math.ceil(goal - sold_qty)),
                             lag=round(expected_today - sold_qty, 1), uplift=round(gain)))
    # «В приоритете сегодня»: 2–3 позиции с самым большим отставанием от темпа.
    progress.sort(key=lambda item: (-item['lag'], -item['remaining'], item['title']))
    behind = [item for item in progress if item['lag'] > 0]
    for index, item in enumerate(progress):
        item['push'] = item['lag'] > 0 and index < (3 if len(behind) >= 3 else len(behind))
    # Порог шума (T-490): прирост ниже порога периода — обычные колебания недели, он целиком остаётся ресторану.
    threshold = scheme.get('noise_week', 0) * days_total / 7
    fund_total = scheme['share'] * max(team_uplift - threshold, 0.0)
    rows = []
    for waiter, stats in now.items():
        if waiter == HALL:
            continue
        eligible = stats.checks >= scheme['min_checks'] and len(stats.days) >= scheme['min_shifts']
        items = []
        for target in targets:
            key = target['key']
            items.append(dict(key=key, title=target['title'], group=target['group'], base=round(hall_base.rate(key), 2),
                              now=round(stats.rate(key), 2), target=target['target100'], quantity=stats.quantity[key],
                              margin=margins[key],
                              by_day={day: counter[key] for day, counter in stats.by_day.items() if counter[key]}))
        rows.append(dict(waiter=waiter, checks=stats.checks, shifts=len(stats.days), eligible=eligible, items=items,
                         sold=round(sum(stats.quantity[target['key']] for target in targets)),
                         checks_by_day=dict(stats.checks_by_day)))
    eligible_rows = [row for row in rows if row['eligible']]
    team_checks = sum(row['checks'] for row in eligible_rows)
    for row in rows:
        if not row['eligible'] or not team_checks:
            row.update(share=0.0, premium=0)
            continue
        share = row['checks'] / team_checks
        row.update(share=round(share, 4), premium=round(min(fund_total * share, cap)))
    rows.sort(key=lambda row: (not row['eligible'], -row['premium'], -row['sold'], -row['checks'], row['waiter']))
    fund = sum(row['premium'] for row in rows)
    goal_margin = sum(max(target['target100'] - hall_base.rate(target['key']), 0) / 100 * expected_checks
                      * margins[target['key']] for target in targets)
    # Метка порога на кольце (порции): с какой доли цели прирост маржи переходит порог шума.
    base_qty = sum(hall_base.rate(target['key']) / 100 * expected_checks for target in targets)
    goal_qty_total = sum(item['goal_qty'] for item in progress)
    threshold_mark = threshold_position(base_qty, goal_qty_total, threshold, goal_margin)
    return dict(rows=rows, team_uplift=round(team_uplift), net_uplift=round(team_uplift), fund=round(fund),
                fund_total=round(fund_total), restaurant_net=round(team_uplift - fund), participants=len(eligible_rows),
                waiters=len(rows), team_checks=team_checks,
                sold=round(sum(item['sold'] for item in progress)), goal_qty=round(sum(item['goal_qty'] for item in progress), 1),
                expected_checks=round(expected_checks, 1), targets_progress=progress,
                hall=dict(checks=hall_now.checks, base_checks=hall_base.checks, days=len(hall_now.days),
                          sold_by_day={day: round(sum(counter[t['key']] for t in targets)) for day, counter in sorted(hall_now.by_day.items())},
                          rates={t['key']: dict(base=round(hall_base.rate(t['key']), 2), now=round(hall_now.rate(t['key']), 2),
                                                target=t['target100']) for t in targets}),
                goal=round(goal_margin), goal_uplift=round(goal_margin), cap=cap, targets=targets,
                threshold=round(threshold), threshold_mark=threshold_mark, base_qty=round(base_qty, 1),
                to_threshold=round(max(threshold - team_uplift, 0.0)), above_threshold=team_uplift > threshold)


def threshold_position(base_qty: float, goal_qty: float, threshold: float, goal_uplift: float):
    """Доля кольца (0–1), с которой начисляется премия: базовые порции плюс доля пути к цели, равная
    порогу шума от прироста при выходе на цель. None — цели или прироста при цели нет."""
    if goal_qty <= 0 or goal_uplift <= 0:
        return None
    mark = (base_qty + threshold / goal_uplift * (goal_qty - base_qty)) / goal_qty
    return round(min(max(mark, 0.0), 1.0), 4)


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
