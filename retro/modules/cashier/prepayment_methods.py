"""Incoming noncash advances from shift operations, never redeemed sale payments."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .service import DataError, TZ, number


PENDING_NOTE = ('Смена ещё открыта. iiko пока не отдаёт предоплаты по способам оплаты. '
                'Посмотрите после закрытия смены или завтра — выберите этот день и обновите отчёт.')
CASH_NOTE = ('Наличные предоплаты и «Инкасса QR» показаны вместе: '
             'iiko не отдаёт их отдельную разбивку в этом отчёте.')
SCOPE_NOTE = 'Предоплаты относятся ко всей кассе Retro, включая банкетное отделение.'


def breakdown_from_shifts(day, details):
    entries, seen = [], set()
    pending = False
    for response in details:
        data = response.get('data') if isinstance(response, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get('shift'), dict):
            raise DataError('iiko не вернул детализацию смены.')
        shift = data['shift']
        if shift.get('cashRegNumber') != 1 or str(shift.get('openDate', ''))[:10] != day.isoformat():
            raise DataError('iiko вернул детализацию другой кассы или дня.')
        transactions = data.get('transactions')
        if transactions is None:
            if shift.get('isOpen') is True or shift.get('sessionStatus') == 'OPEN':
                pending = True
                continue
            raise DataError('iiko пока не отдал операции закрытой смены. Обновите отчёт позже.')
        if not isinstance(transactions, dict) or not isinstance(transactions.get('cashlessRecords'), list):
            raise DataError('iiko вернул неполную детализацию предоплат.')
        decoration = response.get('decoration')
        names = decoration.get('paymentTypes') if isinstance(decoration, dict) else None
        if not isinstance(names, dict):
            raise DataError('iiko не вернул названия способов оплаты.')
        for row in transactions['cashlessRecords']:
            info = row.get('info') if isinstance(row, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get('type'), str):
                raise DataError('iiko вернул некорректную операцию смены.')
            if info['type'] != 'PREPAY':
                continue
            identifier = info.get('id')
            name = names.get(info.get('paymentTypeId'))
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise DataError('iiko вернул повторную или неполную предоплату.')
            if not isinstance(name, str) or not name.strip() or name == 'UNKNOWN':
                raise DataError('iiko не указал способ оплаты предоплаты.')
            amount = number(info.get('sum'))
            if amount <= 0:
                raise DataError('iiko вернул некорректную сумму внесения предоплаты.')
            try:
                stamp = info['creationDate']
                if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                    raise ValueError
                received_at = datetime.fromtimestamp(stamp, TZ).isoformat()
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                raise DataError('iiko не указал время внесения предоплаты.') from None
            seen.add(identifier)
            entries.append(dict(id=identifier, received_at=received_at, amount=str(amount),
                                payment_method=name, shift_number=shift.get('sessionNumber')))
    # Never present a partial day's breakdown as complete if one shift is unavailable.
    if pending:
        return dict(status='pending', note=PENDING_NOTE, payments=[], total=None)
    groups = defaultdict(list)
    for entry in sorted(entries, key=lambda entry: (entry['received_at'], entry['id'])):
        groups[entry['payment_method']].append(entry)
    payments = [dict(name=name, entries=rows,
                     amount=str(sum((Decimal(row['amount']) for row in rows), Decimal(0))))
                for name, rows in sorted(groups.items())]
    return dict(status='ready', payments=payments,
                total=str(sum((Decimal(row['amount']) for row in entries), Decimal(0))),
                note=CASH_NOTE, scope_note=SCOPE_NOTE)
