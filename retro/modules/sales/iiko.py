"""Один день продаж из OLAP iiko — построчно, плюс два контрольных отчёта.

Что и почему (живой зонд 17.10, подробности в docs/sales-history.md):

* строки — заказ × блюдо × официант с временем открытия и закрытия чека.
  Себестоимость `ProductCostBase.ProductCost` — итог строки, не цена порции;
* виды операций по заказу (оплата, зачёт предоплаты, «без оплаты») — отдельным
  отчётом: в строке продажи измерение OperationType делит количество долями и
  повторяет себестоимость, поэтому в строки его не берём;
* контроль — тот же день по кассе и отделению, без заказов и блюд. Итог строк
  обязан с ним совпасть: это сверка нашей базы с iiko.

Отчёты идут строго по одному и мимо кэша отчётов: история пишется в базу один
раз за прогон и должна видеть свежий iiko, а аналитике и кассиру остаются
свободные слоты (`IikoClient._analytics_slots`).
"""
import hashlib
import json
from datetime import datetime
from decimal import Decimal

import httpx

from retro.config import IIKO_ORIGIN
from retro.integrations.iiko import olap_leaves, olap_range_body
from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import DataError, TZ, cell, number
from retro.modules.founder.models import OPERATION_KINDS

LINE_GROUPS = ('UniqOrderId.Id', 'OrderNum', 'OpenTime', 'CloseTime', 'CashRegisterName',
               'RestaurantSection', 'WaiterName', 'DishId', 'DishName', 'DishGroup', 'DishType',
               'Storned')
LINE_FIELDS = ('DishAmountInt', 'DishSumInt', 'DishDiscountSumInt', 'ProductCostBase.ProductCost',
               'DishReturnSum')
OPERATION_GROUPS = ('UniqOrderId.Id', 'OperationType')
OPERATION_FIELDS = ('DishDiscountSumInt',)
CONTROL_GROUPS = ('CashRegisterName', 'RestaurantSection')
CONTROL_FIELDS = ('DishDiscountSumInt', 'DishSumInt', 'DishAmountInt', 'UniqOrderId.OrdersCount')
# «Сторнирован» в iiko приходит словом локали ru_RU («Да»/«Нет»).
STORNED = frozenset({'Да', 'Yes', 'TRUE', 'true'})


def text(value):
    return '' if value is None else str(value).strip()


def moment(value, what):
    """Время iiko приходит без пояса: это часы ресторана, то есть Ташкент."""
    if value in (None, ''):
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        raise DataError(f'iiko вернул некорректное время {what} чека.') from None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TZ)
    return stamp.astimezone(TZ).isoformat()


def optional_number(row, index):
    """Себестоимость iiko иногда не знает: это «неизвестно», а не ноль."""
    value = row.get(f'field{index}')
    value = value.get('value') if isinstance(value, dict) else None
    return None if value is None else number(value)


def line_key(values):
    """Ключ строки — все её измерения. Повтор ключа в одном ответе — брак отчёта."""
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, default=str).encode()).hexdigest()[:32]


def lines_from_olap(rows):
    lines, seen = [], set()
    for values, row in olap_leaves(rows, len(LINE_GROUPS), 'продажи по строкам'):
        (order_id, order_num, opened, closed, register, section, waiter,
         dish_id, dish_name, dish_group, dish_type, storned) = values
        if not text(order_id):
            raise DataError('iiko вернул продажу без идентификатора заказа.')
        key = line_key(values)
        if key in seen:
            raise DataError('iiko вернул повторяющиеся строки продаж.')
        seen.add(key)
        quantity, gross, revenue = (number(cell(row, len(LINE_GROUPS) + index)) for index in range(3))
        cost = optional_number(row, len(LINE_GROUPS) + 3)
        returned = optional_number(row, len(LINE_GROUPS) + 4)
        lines.append(dict(
            line_key=key, order_id=text(order_id), order_num=text(order_num),
            opened_at=moment(opened, 'открытия'), closed_at=moment(closed, 'закрытия'),
            register=text(register), section=text(section), waiter=text(waiter),
            dish_id=text(dish_id), dish_name=text(dish_name), dish_group=text(dish_group),
            dish_type=text(dish_type), storned=text(storned) in STORNED,
            quantity=quantity, gross=gross, revenue=revenue, discount=gross - revenue,
            cost=cost, returned=returned or Decimal(0)))
    return lines


def operations_from_olap(rows):
    """Заказ → суммы по видам операций: paid — оплата, prepaid — зачёт аванса."""
    result = {}
    for (order_id, operation), row in olap_leaves(rows, len(OPERATION_GROUPS), 'операции заказов'):
        kind = OPERATION_KINDS.get(text(operation))
        kind = {'paid': 'paid', 'prepaid': 'prepaid'}.get(kind, 'other')
        totals = result.setdefault(text(order_id), dict(paid=Decimal(0), prepaid=Decimal(0), other=Decimal(0)))
        totals[kind] += number(cell(row, len(OPERATION_GROUPS)))
    return result


def control_from_olap(rows):
    """Касса × отделение → выручка, сумма до скидки, количество и чеки iiko."""
    result = []
    for (register, section), row in olap_leaves(rows, len(CONTROL_GROUPS), 'контроль продаж'):
        revenue, gross, quantity, orders = (number(cell(row, len(CONTROL_GROUPS) + index))
                                            for index in range(4))
        result.append(dict(register=text(register), section=text(section), revenue=revenue,
                           gross=gross, quantity=quantity, orders=int(orders)))
    return result


def revision_of(lines, operations, control):
    """Хэш разобранного ответа: тот же день iiko — тот же хэш, порядок строк не важен."""
    payload = dict(
        lines=sorted((dict(line, **{key: str(line[key]) for key in ('quantity', 'gross', 'revenue',
                                                                      'discount', 'cost', 'returned')})
                      for line in lines), key=lambda line: line['line_key']),
        operations={order: {kind: str(value) for kind, value in totals.items()}
                    for order, totals in sorted(operations.items())},
        control=sorted((dict(item, **{key: str(item[key]) for key in ('revenue', 'gross', 'quantity')})
                        for item in control), key=lambda item: (item['register'], item['section'])))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SalesIiko:
    """Источник истории продаж. `iiko` — клиент панели или функция, которая его отдаёт:
    тесты подменяют `app.state.iiko` уже после сборки приложения."""

    def __init__(self, iiko):
        self._iiko = iiko

    @property
    def iiko(self):
        return self._iiko() if callable(self._iiko) else self._iiko

    async def load_day(self, day):
        iiko = self.iiko
        settings = iiko.settings
        if not settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        if settings.base_url != IIKO_ORIGIN:
            raise DataError('Разрешён только сервер Retro Milliy.')
        try:
            async with iiko._client() as client:
                answers = []
                # Последовательно: истории продаж положен один слот iiko.
                for groups, fields in ((CONTROL_GROUPS, CONTROL_FIELDS), (LINE_GROUPS, LINE_FIELDS),
                                       (OPERATION_GROUPS, OPERATION_FIELDS)):
                    answers.append(await iiko._fetch_olap(client, olap_range_body(
                        settings.store_id, day, day, list(groups), list(fields))))
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_sales_day')
            raise DataError('Не удалось связаться с iiko. Повторим загрузку продаж позже.') from None
        control, lines, operations = (control_from_olap(answers[0]), lines_from_olap(answers[1]),
                                      operations_from_olap(answers[2]))
        return dict(day=day, lines=lines, operations=operations, control=control,
                    revision=revision_of(lines, operations, control))
