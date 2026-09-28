"""Опыт, уровни, серия дней и задания.

Всё выводится из записанных покупок и поездок — отдельного состояния нет.
Так цифры нельзя расстроить с журналом: удалили покупку, и опыт пересчитался
сам. Пороги и награды лежат здесь константами, а не в базе.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

# Награды за одну покупку.
XP_PURCHASE = 30
XP_PHOTO = 10
XP_FAIR_PRICE = 10
# Бонус за быстрый закуп: цель — пятнадцать минут на поездку.
XP_FAST_TRIP = 50
FAST_TRIP_MINUTES = 15

LEVELS = (
    (0, 'Новичок закупа'),
    (300, 'Знает базар'),
    (900, 'Держит цену'),
    (2000, 'Мастер закупа'),
    (4000, 'Легенда базара'),
)


def purchase_xp(purchase: dict) -> dict:
    """Опыт за покупку: за саму запись, за фото и за цену не выше обычной."""
    parts = [('Покупка', XP_PURCHASE)]
    if purchase.get('has_photo'):
        parts.append(('Фото', XP_PHOTO))
    # Без истории цену сравнивать не с чем — премию не выдаём и не отнимаем.
    if purchase.get('usual_price') is not None and not purchase.get('price_above_usual'):
        parts.append(('Цена в норме', XP_FAIR_PRICE))
    return dict(total=sum(value for _, value in parts),
                parts=[dict(label=label, xp=value) for label, value in parts])


def trip_minutes(trip: dict) -> float | None:
    if not trip.get('started_at') or not trip.get('finished_at'):
        return None
    started = datetime.fromisoformat(trip['started_at'])
    finished = datetime.fromisoformat(trip['finished_at'])
    return max(0.0, (finished - started).total_seconds() / 60)


def trip_bonus(trip: dict) -> int:
    minutes = trip_minutes(trip)
    if minutes is None or minutes > FAST_TRIP_MINUTES:
        return 0
    return XP_FAST_TRIP


def total_xp(purchases: list[dict], trips: list[dict]) -> int:
    return (sum(purchase_xp(item)['total'] for item in purchases)
            + sum(trip_bonus(trip) for trip in trips))


def level_for(xp: int) -> dict:
    """Уровень, его название и сколько осталось до следующего."""
    index = 0
    for position, (threshold, _) in enumerate(LEVELS):
        if xp >= threshold:
            index = position
    threshold, title = LEVELS[index]
    following = LEVELS[index + 1] if index + 1 < len(LEVELS) else None
    if following is None:
        return dict(level=index + 1, title=title, xp=xp, floor=threshold,
                    next_at=None, to_next=0, progress=1.0)
    span = following[0] - threshold
    return dict(level=index + 1, title=title, xp=xp, floor=threshold,
                next_at=following[0], to_next=following[0] - xp,
                progress=round((xp - threshold) / span, 4) if span else 1.0)


def streak(days: set, today: date) -> int:
    """Сколько дней подряд был закуп, считая от сегодня или от вчера.

    Прерывать серию в середине дня, когда закуп ещё не начался, неправильно —
    поэтому отсчёт допускается и от вчерашнего дня.
    """
    if not days:
        return 0
    start = today if today in days else today - timedelta(days=1)
    if start not in days:
        return 0
    length, cursor = 0, start
    while cursor in days:
        length += 1
        cursor -= timedelta(days=1)
    return length


def quests(purchases: list[dict], trips: list[dict]) -> list[dict]:
    """Задания на сегодня: по три покупки, все с фото и закуп за 15 минут."""
    done = len(purchases)
    with_photo = sum(1 for item in purchases if item.get('has_photo'))
    minutes = [value for value in (trip_minutes(trip) for trip in trips) if value is not None]
    fastest = min(minutes) if minutes else None
    return [
        dict(key='three', title='Записать три покупки', done=min(done, 3), target=3,
             complete=done >= 3, xp=XP_PURCHASE),
        dict(key='photos', title='Каждую покупку с фото', done=with_photo, target=max(done, 1),
             complete=done > 0 and with_photo == done, xp=XP_PHOTO),
        dict(key='fast', title=f'Закуп за {FAST_TRIP_MINUTES} минут',
             done=1 if fastest is not None and fastest <= FAST_TRIP_MINUTES else 0, target=1,
             complete=fastest is not None and fastest <= FAST_TRIP_MINUTES, xp=XP_FAST_TRIP),
    ]


def week_marks(days: set, today: date) -> list[dict]:
    """Последние семь дней: был закуп или нет."""
    result = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        result.append(dict(day=day.isoformat(), weekday=day.isoweekday(),
                           active=day in days, today=day == today))
    return result


def spent(purchases: list[dict]) -> Decimal:
    return sum((Decimal(item['total']) for item in purchases), Decimal(0))
