"""Выручка по кассам iiko (ТЗ «Выручка, оплаты и предоплаты», 08.10.2026).

Группы и типы оплат берутся из iiko (`PayTypes.Group` → `PayTypes`), а не из
списка в коде: новый тип в iiko появляется на экране сам. Полный счёт по типу —
оплаты продаж (OperationType PAYMENT) плюс зачтённые при закрытии заказа
предоплаты (OperationType PREPAY): так iiko сам делит деньги заказа, у
которого был аванс. Реальная касса дня = полные счета − зачтённые предоплаты.
"""
from collections import OrderedDict
from decimal import Decimal

from .service import (CASH_GROUP_HINT, PAYMENT_ALIASES, DataError, PayTypeAmount, PaymentGroup,
                      RedeemedOrder, cell, number)

NO_PAYMENT = '(без оплаты)'


def pay_leaves(rows, depth):
    """Листья дерева grouped-table: [(значение уровня 0, …, уровня depth-1, сумма)].

    Родитель несёт значение своего уровня и подытог, лист — свой уровень и свою
    сумму; значения детей перекрывают родительские."""
    if not isinstance(rows, list):
        raise DataError('iiko вернул некорректный отчёт по оплатам.')
    leaves = []

    def walk(row, values):
        if not isinstance(row, dict):
            raise DataError('iiko вернул некорректную строку отчёта по оплатам.')
        values = dict(values)
        for key, item in row.items():
            if key.startswith('field'):
                if not isinstance(item, dict) or 'value' not in item:
                    raise DataError('iiko вернул неполный отчёт по оплатам.')
                values[int(key[5:])] = item['value']
        children = row.get('children')
        if children is not None:
            if not isinstance(children, list) or not children:
                raise DataError('iiko вернул неполную детализацию оплат.')
            for child in children:
                walk(child, values)
            return
        if any(index not in values for index in range(depth + 1)):
            raise DataError('iiko вернул неполную детализацию оплат.')
        leaves.append(tuple(values[index] for index in range(depth)) + (number(values[depth]),))

    for row in rows:
        walk(row, {})
    return leaves


def _named(group, name, amount):
    """Лист с группой и типом. «(без оплаты)» с нулём iiko отдаёт всегда — пропускаем."""
    if name == NO_PAYMENT:
        if amount:
            raise DataError('iiko вернул сумму без типа оплаты. Проверьте отчёт за день.')
        return None
    if not isinstance(group, str) or not group.strip() or not isinstance(name, str) or not name.strip():
        if amount:
            raise DataError('iiko вернул оплату без группы или типа.')
        return None
    return group.strip(), PAYMENT_ALIASES.get(name, name).strip()


def flat_payment_rows(paid_leaves):
    """Строки «тип → сумма» для build_snapshot из дерева «группа → тип»."""
    amounts = OrderedDict()
    for group, name, amount in paid_leaves:
        named = _named(group, name, amount)
        if named is None:
            continue
        amounts[named[1]] = amounts.get(named[1], Decimal(0)) + amount
    return [{'field0': {'value': name}, 'field1': {'value': amount}} for name, amount in amounts.items()]


def build_payment_groups(paid_leaves, redeemed_leaves, dictionary_leaves=()):
    """Группы касс iiko: типы с оплатами, зачтёнными предоплатами и нулевые из справочника.

    Порядок постоянный, чтобы сверка шла глазами по одним местам: группа с
    наличными «Демо» первой, остальные по алфавиту; типы внутри — по алфавиту."""
    slots = OrderedDict()

    def slot(named):
        group, name = named
        return slots.setdefault(group, OrderedDict()).setdefault(name, [Decimal(0), Decimal(0)])

    for group, name, amount in paid_leaves:
        named = _named(group, name, amount)
        if named:
            slot(named)[0] += amount
    for _order, group, name, amount in redeemed_leaves:
        named = _named(group, name, amount)
        if named:
            slot(named)[1] += amount
    for group, name, _amount in dictionary_leaves:
        named = _named(group, name, Decimal(0))
        if named:
            slot(named)

    def group_key(name):
        return (0 if any(CASH_GROUP_HINT in item for item in slots[name]) else 1, name.casefold())

    return tuple(
        PaymentGroup(group, tuple(PayTypeAmount(name, *slots[group][name])
                                  for name in sorted(slots[group], key=str.casefold)))
        for group in sorted(slots, key=group_key))


def redeemed_orders(redeemed_leaves):
    """Заказы, закрытые в этот день с зачётом предоплаты: номер, сумма зачёта, чем был аванс."""
    orders = OrderedDict()
    for order, group, name, amount in redeemed_leaves:
        if order is None or isinstance(order, bool) or not isinstance(order, (str, int)):
            if amount:
                raise DataError('iiko вернул зачёт предоплаты без номера заказа.')
            continue
        named = _named(group, name, amount)
        entry = orders.setdefault(str(order), [Decimal(0), []])
        entry[0] += amount
        if named and named[1] not in entry[1] and amount:
            entry[1].append(named[1])
    return tuple(RedeemedOrder(order, total, tuple(methods))
                 for order, (total, methods) in orders.items() if total)


def full_totals(day, rows):
    """Полные счета дня: (количество чеков, сумма) по оплатам и зачтённым предоплатам."""
    if not isinstance(rows, list):
        raise DataError('iiko вернул некорректный итог дня.')
    if not rows:
        return 0, Decimal(0)
    if len(rows) > 1:
        raise DataError('Вместо итога за один день iiko вернул несколько строк.')
    if cell(rows[0], 0) != day.isoformat():
        raise DataError('Дата ответа iiko не совпадает с выбранным днём.')
    count, total = number(cell(rows[0], 1)), number(cell(rows[0], 2))
    if count < 0 or count != count.to_integral_value():
        raise DataError('iiko вернул некорректное количество чеков.')
    return int(count), total
