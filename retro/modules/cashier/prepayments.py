"""Incoming advances from iiko's transaction report, not redeemed sales payments."""
import hashlib
import json
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from .service import BANQUET_SECTION, RETRO_REGISTER, DataError, Prepayment, TZ, number


GROUPS = ('Session.CashRegister', 'Session.RestaurantSection',
          'DateSecondary.DateTimeTyped', 'OrderNum', 'Account.Name',
          'Contr-Account.Name', 'Counteragent.Name', 'Comment')
PREPAYMENT_ACCOUNT = 'Предоплата за заказы'
# Аванс картой через терминал iiko проводит на эквайринг; переводы (Click/Payme)
# и наличные — одинаково через расчёты с сотрудниками, их различает только смена.
ACQUIRING_ACCOUNT = 'Денежные средства, эквайринг'
CARD_METHOD = 'Банковская карта'
CASH_METHOD = 'Наличные'


def report_body(store_id, day):
    return report_body_range(store_id, day, day)


def report_body_range(store_id, start, end, orders=None):
    """Авансы за период. orders — только эти заказы (в TRANSACTIONS номер — строкой)."""
    body = dict(
        storeIds=[store_id], olapType='TRANSACTIONS', categoryFields=[],
        groupFields=list(GROUPS), dataFields=['Sum.Incoming', 'Sum.Outgoing'],
        calculatedFields=[], includeVoidTransactions=False,
        includeNonBusinessPaymentTypes=False,
        filters=[
            dict(field='DateTime.OperDayFilter', filterType='date_range',
                 dateFrom=start.isoformat(), dateTo=end.isoformat(),
                 includeLeft=True, includeRight=True, inclusiveList=True),
            dict(field='TransactionType', filterType='value_list',
                 valueList=['PREPAY'], inclusiveList=True),
            dict(field='Session.CashRegister', filterType='value_list',
                 valueList=[RETRO_REGISTER], inclusiveList=True),
        ])
    if orders:
        body['filters'].append(dict(field='OrderNum', filterType='value_list',
                                    valueList=[str(order) for order in orders], inclusiveList=True))
    return body


def prepayments_from_olap(rows):
    """Use only the credit of the advance liability: debit is the same money.

    iiko leaves section blank on some open orders. Preserve those advances of
    the Retro register, but exclude explicitly assigned banquet/school rows.
    The employee clearing account occurs for both cash and card advances, so
    neither it nor the cashier's name can establish a payment method/guest.
    """
    if not isinstance(rows, list):
        raise DataError('iiko не вернул список предоплат.')
    result = []

    def walk(row, parents):
        if not isinstance(row, dict):
            raise DataError('iiko вернул некорректную строку предоплаты.')
        # Subtotals belong to their parent, never to a missing leaf amount.
        values = {index: value for index, value in parents.items() if index < len(GROUPS)}
        for index in range(len(GROUPS) + 2):
            key = f'field{index}'
            if key in row:
                if not isinstance(row[key], dict) or 'value' not in row[key]:
                    raise DataError('iiko вернул неполную строку предоплаты.')
                values[index] = row[key]['value']
        if 'children' in row:
            if not isinstance(row['children'], list) or not row['children']:
                raise DataError('iiko вернул неполную детализацию предоплат.')
            for child in row['children']:
                walk(child, values)
            return
        if any(index not in values for index in range(len(GROUPS) + 2)):
            raise DataError('iiko вернул неполную детализацию предоплат.')
        register, section, received_at, order, account, contra, _, comment = (
            values[index] for index in range(len(GROUPS)))
        if register != RETRO_REGISTER:
            return
        if section is not None and not isinstance(section, str):
            raise DataError('iiko не указал корректное отделение предоплаты.')
        if section == BANQUET_SECTION or (section and 'бехруз' in section.casefold()):
            return
        if account != PREPAYMENT_ACCOUNT:
            return
        incoming, outgoing = number(values[8]), number(values[9])
        if incoming != 0 or outgoing <= 0:
            raise DataError('iiko вернул неоднозначную сумму внесения предоплаты.')
        try:
            timestamp = datetime.fromisoformat(received_at)
        except (TypeError, ValueError):
            raise DataError('iiko не указал время внесения предоплаты.') from None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=TZ)
        timestamp = timestamp.astimezone(TZ).isoformat()
        if comment is not None and not isinstance(comment, str):
            raise DataError('iiko вернул некорректное описание предоплаты.')
        if order is not None and not isinstance(order, (str, int)):
            raise DataError('iiko вернул некорректный номер заказа предоплаты.')
        identifier = hashlib.sha256(json.dumps(
            [timestamp, order, section, str(outgoing), comment],
            ensure_ascii=False).encode()).hexdigest()
        result.append(Prepayment(id=identifier, received_at=timestamp,
                                 amount=outgoing, order_number=str(order) if order is not None else None,
                                 payment_method=CARD_METHOD if contra == ACQUIRING_ACCOUNT else None,
                                 comment=comment or ''))

    for row in rows:
        walk(row, {})
    return tuple(sorted(result, key=lambda entry: (entry.received_at, entry.id)))


def shift_window(shift):
    """Когда смена кассы Retro была открыта: (начало, конец); у открытой конца нет."""
    def moment(value):
        stamp = datetime.fromisoformat(value)
        return stamp.replace(tzinfo=TZ) if stamp.tzinfo is None else stamp.astimezone(TZ)
    try:
        start = moment(shift['openDate'])
        end = None if shift.get('isOpen') or not shift.get('closeDate') else moment(shift['closeDate'])
    except (KeyError, TypeError, ValueError):
        raise DataError('iiko не указал время кассовой смены.') from None
    if end is not None and end < start:
        raise DataError('iiko вернул кассовую смену, закрытую раньше открытия.')
    return start, end


def shift_identifier(shift):
    try:
        return str(UUID(shift['id']))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DataError('iiko вернул кассовую смену без идентификатора.') from None


def cashless_prepays_from_shift(details):
    """Безналичные авансы смены: [(секунда внесения, сумма, тип оплаты или None)].

    Наличных авансов в детализации смены нет вовсе — только карты и переводы.
    Нет детализации — смена не прочитана, и по ней ничего выводить нельзя."""
    data = details.get('data') if isinstance(details, dict) else None
    transactions = data.get('transactions') if isinstance(data, dict) else None
    records = transactions.get('cashlessRecords') if isinstance(transactions, dict) else None
    types = (details.get('decoration') or {}).get('paymentTypes') if isinstance(details, dict) else None
    if not isinstance(records, list) or not isinstance(types, dict):
        raise DataError('iiko не отдал детализацию кассовой смены.')
    result = []
    for record in records:
        info = record.get('info') if isinstance(record, dict) else None
        if not isinstance(info, dict):
            raise DataError('iiko вернул неполную запись кассовой смены.')
        if info.get('type') != 'PREPAY':
            continue
        created = info.get('creationDate')
        if not isinstance(created, (int, float)) or isinstance(created, bool):
            raise DataError('iiko не указал время безналичного аванса.')
        amount = number(info.get('sum'))
        if amount <= 0:
            raise DataError('iiko вернул неоднозначную сумму безналичного аванса.')
        name = types.get(info.get('paymentTypeId'))
        if isinstance(name, dict):
            name = name.get('name')
        name = name.strip() if isinstance(name, str) and name.strip() and name != 'UNKNOWN' else None
        result.append((int(created), amount, name))
    return result


def assign_methods(entries, records, windows):
    """Способ оплаты каждого аванса из реестра.

    1. Безналичная запись смены с той же секундой и суммой — её тип оплаты.
    2. Проводка на эквайринг без записи в смене — «Банковская карта».
    3. Ни того, ни другого, а смена, в которую внесён аванс, прочитана целиком, —
       наличные: iiko не пишет наличные авансы в детализацию смены (сверено
       с кассиром 13.10.2026 на шести авансах 07–11.10).
    4. Иначе способ неизвестен: смену не прочитали — наличные не выдумываем."""
    by_key = defaultdict(list)
    for seconds, amount, name in records:
        by_key[(seconds, amount)].append(name)
    result = []
    for entry in entries:
        received = datetime.fromisoformat(entry.received_at)
        names = by_key.get((int(received.timestamp()), entry.amount))
        if names:
            # Безналичный — точно не наличные; имя только если оно однозначно.
            method = names[0] if names[0] and len(set(names)) == 1 else entry.payment_method
        elif entry.payment_method:
            method = entry.payment_method
        elif any(start <= received and (end is None or received <= end) for start, end in windows):
            method = CASH_METHOD
        else:
            method = None
        result.append(replace(entry, payment_method=method))
    return tuple(result)
