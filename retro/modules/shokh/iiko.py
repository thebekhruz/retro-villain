"""iikoWeb incoming invoices, using the same authenticated client as reports.

Contract verified against Retro's documents/nomenclature applications (9.9.1).
Reads may retry authorization; creation is NEVER automatically retried.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from time import monotonic
from urllib.parse import quote

import httpx

from retro.modules.cashier.service import DataError, TZ

TYPE = 'INCOMING_INVOICE'
POST_PERMISSION = 'DOCUMENT_BUNDLE_INCOMING_INVOICE_POST'


class Rejected(DataError):
    """iiko explicitly rejected the document before saving it."""


def name(value):
    if isinstance(value, str):
        return value
    return (value or {}).get('customValue') or ''


def money(value):
    from decimal import InvalidOperation
    try:
        result = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise DataError('iiko вернул некорректную денежную сумму.') from None


class ProcurementIiko:
    def __init__(self, client):
        self.client = client
        self._catalog = None
        self._until = 0
        self._lock = asyncio.Lock()

    async def read(self, path, *, params=None, body=None):
        try:
            async with self.client._client() as http:
                token = http.headers.get('Authorization')
                for attempt in range(2):
                    response = (await http.get(path, params=params) if body is None else
                                await http.post(path, json=body))
                    if response.status_code not in (401, 403) or attempt:
                        break
                    await self.client._authorize(http, rejected_token=token)
                if response.status_code in (401, 403):
                    raise DataError('iiko отклонил доступ к закупкам. Проверьте права учётной записи.')
                if not response.is_success:
                    raise DataError(f'Не удалось загрузить закупки из iiko (HTTP {response.status_code}).')
                data = response.json()
                if isinstance(data, dict):
                    if data.get('error') or data.get('formValidationError'):
                        raise DataError('iiko отклонил запрос справочника закупок.')
                    return data.get('data', data)
                if isinstance(data, list):
                    return data
                raise ValueError()
        except (httpx.HTTPError, ValueError, TypeError):
            raise DataError('Не удалось связаться с iiko. Повторите загрузку справочников.') from None

    async def catalog(self, *, fresh=False):
        async with self._lock:
            if not fresh and self._catalog is not None and monotonic() < self._until:
                return self._catalog
            if not self.client.settings.configured:
                raise DataError('Подключение iiko не настроено. Обратитесь к администратору.')
            store = str(self.client.settings.store_id)
            # Bounded concurrency: independent small dictionaries, no report jobs.
            storages, suppliers = await asyncio.gather(
                self.read('/api/documents/storage/list', params={'store': store}),
                self.read('/api/documents/suppliers/list', params={'store': store}))
            units, taxes = await asyncio.gather(
                self.read('/api/entities/list-of-type', params={'type': 'MeasureUnit', 'includeDeleted': 'false'}),
                self.read('/api/entities/list-of-type', params={'type': 'TaxCategory', 'includeDeleted': 'false'}))
            products, permissions = await asyncio.gather(
                self.read('/api/productV3/list', body={'types': ['GOODS'], 'properties': [
                    'id', 'name', 'num', 'mainUnit', 'deleted', 'type', 'taxCategory']}),
                self.read('/api/permissions/my'))
            settings = await self.read('/api/documents/config/store-settings', params={'storeId': store})
            if not all(isinstance(x, list) for x in (storages, units, taxes, products, permissions)) or not isinstance(suppliers, dict):
                raise DataError('iiko вернул неполный справочник закупок.')
            unit_map = {u['id']: u for u in units if not u.get('isDeleted')}
            tax_map = {t['id']: t for t in taxes if not t.get('isDeleted')}
            items = []
            for p in products:
                if p.get('deleted') or p.get('type') != 'GOODS' or not name(p.get('name')):
                    continue
                u = unit_map.get(p.get('mainUnit'))
                tax_id = p.get('taxCategory')
                if u is None or (tax_id and tax_id not in tax_map):
                    continue
                vat = Decimal(str(tax_map[tax_id]['vatPercent'])) if tax_id else Decimal(0)
                if not vat.is_finite() or not 0 <= vat <= 100:
                    raise DataError('iiko вернул некорректную ставку НДС.')
                items.append(dict(id=p['id'], item=name(p['name']), code=p.get('num', ''),
                                  unit=u['name'], unit_id=u['id'], vat_percent=str(vat)))
            result = dict(source='iiko', items=sorted(items, key=lambda x: x['item'].casefold()),
                          suppliers=[dict(id=s['id'], name=s['name']) for s in suppliers.values()
                                     if not s.get('deleted') and not s.get('isRepresentsStore')],
                          storages=[dict(id=s['id'], name=s['name']) for s in storages
                                    if not s.get('deleted') and s.get('storageType') == 'INVENTORY_ASSETS'],
                          can_create=POST_PERMISSION in permissions,
                          updated_at=datetime.now(TZ).isoformat(), settings=settings)
            if not result['items'] or not result['suppliers'] or not result['storages']:
                raise DataError('В iiko не хватает товаров, поставщиков или складов для закупа.')
            self._catalog, self._until = result, monotonic() + 300
            return result

    def invoice(self, catalog, *, key, day, at, product, supplier, storage, quantity, price):
        quantity, price = Decimal(quantity), Decimal(price)
        total = money(quantity * price)
        vat = Decimal(product['vat_percent'])
        net_price = money(price * 100 / (100 + vat))
        net_total = money(total * 100 / (100 + vat))
        times = catalog['settings'].get('docSettings', {}).get('defaultDocumentTimes', [])
        incoming = at
        setting = next((t for t in times if t.get('documentType') == TYPE), None)
        if setting and setting.get('timeType') == 'SPECIFIC_TIME':
            minutes = int(setting['minutes'])
            if not 0 <= minutes < 1440:
                raise DataError('Некорректное время приходных накладных в iiko.')
            incoming = at.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
        incoming = incoming.replace(year=day.year, month=day.month, day=day.day)
        marker = 'Retro Shokh ' + key
        return dict(type=TYPE, documentNumber='', status='PROCESSED',
                    dateIncoming=incoming.isoformat(), comment=marker, conception=None,
                    store=str(self.client.settings.store_id), supplier=supplier['id'], storage=storage['id'],
                    invoice='', invoiceDate=day.isoformat(), incomingDocumentNumber='',
                    transportInvoiceNumber='', externalOrderNumber=marker, fromExternalService=False,
                    items=[dict(id=None, product=product['id'], name=product['item'], num=product['code'],
                                type='GOODS', storage=storage['id'], containerId=product['unit_id'],
                                count=float(quantity), amount=float(quantity), actualAmount=float(quantity),
                                price=float(price), priceWithoutNds=float(net_price),
                                sum=float(total), sumWithoutNds=float(net_total), ndsPercent=float(vat))])

    async def create(self, payload):
        # Authentication happens before the one and only mutation. Transport
        # failure, 5xx, warning or malformed success is an uncertain result.
        async with self.client._client() as http:
            response = await http.post('/api/documents/create', json=payload)
        try:
            data = response.json()
        except ValueError:
            raise DataError('Ответ iiko не подтверждён. Проверьте статус покупки.') from None
        result = data.get('data') if isinstance(data, dict) else None
        has_id = isinstance(result, dict) and bool(result.get('id'))
        if not has_id and (response.status_code in (400, 401, 403, 404, 405, 415, 422) or
                (response.is_success and isinstance(data, dict)
                 and (data.get('error') or data.get('formValidationError')))):
            detail = data.get('errorMessage') if isinstance(data, dict) else None
            raise Rejected((detail or 'iiko отклонил накладную. Проверьте права и реквизиты.')[:500])
        if not response.is_success or not isinstance(data, dict):
            raise DataError('iiko не подтвердил запись. Повторная отправка заблокирована до сверки.')
        if not isinstance(result, dict) or not result.get('id'):
            raise DataError('iiko не подтвердил номер накладной. Проверьте статус покупки.')
        return dict(id=str(result['id']), number=str(result.get('documentNumber') or ''))

    async def document(self, document_id):
        return await self.read('/api/documents/get/' + quote(document_id, safe=''), params={'type': TYPE})

    async def find(self, payload):
        day = datetime.fromisoformat(payload['dateIncoming']).date()
        found = await self.read('/api/documents/list', params={
            'store': payload['store'], 'type': TYPE, 'dateFrom': day.isoformat(),
            'dateTo': (day + timedelta(days=1)).isoformat()})
        if not isinstance(found, dict) or not isinstance(found.get('documents'), list):
            raise DataError('iiko не подтвердил список накладных.')
        matches = [d for d in found['documents'] if d.get('comment') == payload['comment']]
        if len(matches) > 1:
            raise DataError('В iiko найдено несколько накладных закупа. Нужна проверка бухгалтера.')
        return await self.document(matches[0]['id']) if matches else None

    @staticmethod
    def verify(document, payload):
        if not isinstance(document, dict) or document.get('type') != TYPE or document.get('status') != 'PROCESSED':
            raise DataError('Накладная в iiko ещё не проведена. Проверьте её статус.')
        items = document.get('items') or []
        expected = payload['items'][0]
        if (str(document.get('store')) != payload['store'] or document.get('supplier') != payload['supplier']
                or document.get('comment') != payload['comment'] or len(items) != 1):
            raise DataError('Реквизиты накладной iiko не совпали с покупкой.')
        row = items[0]
        if (row.get('product') != expected['product'] or row.get('storage') != expected['storage']
                or row.get('containerId') != expected['containerId']
                or str(row.get('amount')) in ('None', 'NaN', 'Infinity')
                or Decimal(str(row.get('amount'))) != Decimal(str(expected['amount']))
                or money(row.get('price')) != money(expected['price'])
                or money(row.get('sum')) != money(expected['sum'])
                or money(document.get('sum')) != money(expected['sum'])):
            raise DataError('Количество или сумма в iiko не совпали с покупкой. Нужна сверка.')
        return dict(id=str(document['id']), number=str(document.get('documentNumber') or ''))
