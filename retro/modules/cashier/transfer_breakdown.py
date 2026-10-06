"""Receipt-level payments for iiko's combined Click/Payme payment type."""
from datetime import datetime
from decimal import Decimal

from .service import BANQUET_SECTION, RETRO_REGISTER, TZ, DataError, number


PAYMENT_NAME = 'Click/Payme Безналичный перевод'
GROUPS = ('CardTypeName', 'CardType', 'PaymentTransaction.Id', 'UniqOrderId.Id',
          'OrderNum', 'CloseTime', 'OrderComment')
NOTE = ('В iiko Click, Payme и безналичный перевод записаны одним типом оплаты. '
        'Ниже — платежи по чекам.')


def report_filters():
    return [
        dict(field='CashRegisterName', filterType='value_list',
             valueList=[RETRO_REGISTER], inclusiveList=True),
        dict(field='RestaurantSection', filterType='value_list',
             valueList=[BANQUET_SECTION], inclusiveList=False),
        dict(field='OperationType', filterType='value_list',
             valueList=['PAYMENT'], inclusiveList=True),
        dict(field='PayTypes', filterType='value_list',
             valueList=[PAYMENT_NAME], inclusiveList=True),
    ]


def breakdown_from_olap(day, rows):
    """Count leaf metrics only: parent totals repeat the same payments.

    The filtered payment amount can be smaller than the whole receipt on a
    mixed-payment order. CardTypeName in Retro repeats the combined payment
    name; neither that field nor a comment establishes Click versus Payme.
    """
    if not isinstance(rows, list):
        raise DataError('iiko не вернул детализацию переводов.')
    result, seen = [], set()

    def walk(row, parents):
        if not isinstance(row, dict):
            raise DataError('iiko вернул некорректную строку перевода.')
        values = dict(parents)
        for index in range(len(GROUPS)):
            key = f'field{index}'
            if key in row:
                if not isinstance(row[key], dict) or 'value' not in row[key]:
                    raise DataError('iiko вернул неполную детализацию переводов.')
                values[index] = row[key]['value']
        if 'children' in row:
            if not isinstance(row['children'], list) or not row['children']:
                raise DataError('iiko вернул неполную детализацию переводов.')
            for child in row['children']:
                walk(child, values)
            return
        metric = row.get(f'field{len(GROUPS)}')
        if (len(values) != len(GROUPS) or not isinstance(metric, dict)
                or 'value' not in metric):
            raise DataError('iiko вернул неполную детализацию переводов.')
        _, _, transaction_id, order_id, order, received_at, comment = (
            values[index] for index in range(len(GROUPS)))
        if not all(isinstance(value, str) and value for value in (transaction_id, order_id)):
            raise DataError('iiko не указал идентификатор перевода.')
        identifier = f'{transaction_id}:{order_id}'
        if identifier in seen:
            raise DataError('iiko вернул повторяющиеся строки переводов.')
        seen.add(identifier)
        if order is not None and (isinstance(order, bool) or not isinstance(order, (str, int))):
            raise DataError('iiko вернул некорректный номер чека.')
        if comment is not None and not isinstance(comment, str):
            raise DataError('iiko вернул некорректный комментарий перевода.')
        try:
            timestamp = datetime.fromisoformat(received_at)
        except (TypeError, ValueError):
            raise DataError('iiko не указал время закрытия чека.') from None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=TZ)
        result.append(dict(id=identifier, order_number=str(order) if order is not None else None,
                           received_at=timestamp.astimezone(TZ).isoformat(),
                           amount=str(number(metric['value'])), comment=comment or ''))

    for row in rows:
        walk(row, {})
    return dict(date=day.isoformat(), payment_name=PAYMENT_NAME,
                total=str(sum((Decimal(row['amount']) for row in result), Decimal(0))),
                provider_split_known=False, note=NOTE,
                rows=sorted(result, key=lambda item: (item['received_at'], item['id'])))
