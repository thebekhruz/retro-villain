"""Incoming advances from iiko's transaction report, not redeemed sales payments."""
import hashlib
import json
from datetime import datetime

from .service import BANQUET_SECTION, RETRO_REGISTER, DataError, Prepayment, TZ, number


GROUPS = ('Session.CashRegister', 'Session.RestaurantSection',
          'DateSecondary.DateTimeTyped', 'OrderNum', 'Account.Name',
          'Contr-Account.Name', 'Counteragent.Name', 'Comment')
PREPAYMENT_ACCOUNT = 'Предоплата за заказы'


def report_body(store_id, day):
    return dict(
        storeIds=[store_id], olapType='TRANSACTIONS', categoryFields=[],
        groupFields=list(GROUPS), dataFields=['Sum.Incoming', 'Sum.Outgoing'],
        calculatedFields=[], includeVoidTransactions=False,
        includeNonBusinessPaymentTypes=False,
        filters=[
            dict(field='DateTime.OperDayFilter', filterType='date_range',
                 dateFrom=day.isoformat(), dateTo=day.isoformat(),
                 includeLeft=True, includeRight=True, inclusiveList=True),
            dict(field='TransactionType', filterType='value_list',
                 valueList=['PREPAY'], inclusiveList=True),
            dict(field='Session.CashRegister', filterType='value_list',
                 valueList=[RETRO_REGISTER], inclusiveList=True),
        ])


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
        register, section, received_at, order, account, _, _, comment = (
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
                                 comment=comment or ''))

    for row in rows:
        walk(row, {})
    return tuple(sorted(result, key=lambda entry: (entry.received_at, entry.id)))
