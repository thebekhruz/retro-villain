"""Durable procurement outbox. A claimed send can only be reconciled, never replayed."""
import asyncio
import hashlib
import json
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from retro.modules.cashier.service import DataError, TZ
from .iiko import Rejected
from .store import ShokhError


class ProcurementSync:
    def __init__(self, store, source):
        self.store, self.source = store, source
        with closing(store._open()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS shokh_iiko_operations (
                    operation_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    purchase_id INTEGER NOT NULL UNIQUE REFERENCES shokh_purchases(id),
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    document_id TEXT,
                    document_number TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );
            ''')
            # Покупки, записанные до колонки iiko_refs: поставщик, склад и товар
            # берём из их накладной. По ним работают подстановка поставщика и
            # склада по точке и «Часто покупаете» по id товара iiko.
            missing = c.execute('SELECT p.id, o.payload FROM shokh_purchases p '
                                'JOIN shokh_iiko_operations o ON o.purchase_id = p.id '
                                'WHERE p.iiko_refs IS NULL').fetchall()
            for purchase_id, payload in missing:
                refs = self.refs_from_payload(payload)
                if refs:
                    c.execute('UPDATE shokh_purchases SET iiko_refs = ? WHERE id = ?',
                              (json.dumps(refs, ensure_ascii=False), purchase_id))
            c.commit()

    @staticmethod
    def refs_from_payload(payload):
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
            refs = dict(supplier_id=data['supplier'], storage_id=data['storage'])
            items = data.get('items') or []
            if items and items[0].get('product'):
                refs['product_id'] = items[0]['product']
            return refs
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            return None

    def operation(self, key=None, purchase_id=None):
        with closing(self.store._open()) as c:
            row = c.execute('SELECT operation_id,fingerprint,purchase_id,payload,status,document_id,'
                            'document_number,error,updated_at FROM shokh_iiko_operations WHERE '
                            + ('operation_id=?' if key is not None else 'purchase_id=?'),
                            (key if key is not None else purchase_id,)).fetchone()
        return dict(zip(('key','fingerprint','purchase_id','payload','status','document_id',
                         'document_number','error','updated_at'), row)) if row else None

    def point_defaults(self, limit=300):
        """Поставщик и склад прошлой покупки по каждой точке — см. store.point_defaults."""
        return self.store.point_defaults(limit)

    def decorate(self, rows):
        if not rows:
            return rows
        ids = [r['id'] for r in rows]
        with closing(self.store._open()) as c:
            found = c.execute('SELECT purchase_id,status,document_id,document_number,error '
                              'FROM shokh_iiko_operations WHERE purchase_id IN ('
                              + ','.join('?' for _ in ids) + ')', ids).fetchall()
        by_id = {r[0]: dict(status=r[1], document_id=r[2], number=r[3], error=r[4]) for r in found}
        # Товар не из справочника iiko: накладной нет, её проводит бухгалтер руками.
        return [dict(r, iiko=by_id.get(r['id'], dict(status='manual' if r.get('off_catalog') else 'legacy')))
                for r in rows]

    def prepare(self, key, fingerprint, payload, day, at, fields):
        with closing(self.store._open()) as c:
            c.execute('BEGIN IMMEDIATE')
            # ON CONFLICT is the cross-worker guard on PostgreSQL. A second
            # request can race between the initial lookup and the insert; the
            # transaction rollback also discards its provisional purchase.
            existing = c.execute('SELECT fingerprint,purchase_id FROM shokh_iiko_operations '
                                 'WHERE operation_id=?', (key,)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise ShokhError('Этот ключ уже использован для другой покупки.')
                return existing[1]
            purchase_id = self.store.add_purchase(day, at, **fields, _connection=c, iiko_unit=True,
                                                  refs=self.refs_from_payload(payload))
            inserted = c.execute('INSERT INTO shokh_iiko_operations '
                      '(operation_id,fingerprint,purchase_id,payload,status,updated_at) VALUES (?,?,?,?,?,?) '
                      'ON CONFLICT(operation_id) DO NOTHING',
                      (key, fingerprint, purchase_id, json.dumps(payload, ensure_ascii=False),
                       'pending', at.isoformat()))
            if inserted.rowcount == 0:
                c.rollback()
                existing = self.operation(key=key)
                if existing['fingerprint'] != fingerprint:
                    raise ShokhError('Этот ключ уже использован для другой покупки.')
                return existing['purchase_id']
            c.commit()
        return purchase_id

    def claim(self, purchase_id, expected='pending'):
        with closing(self.store._open()) as c, c:
            result = c.execute("UPDATE shokh_iiko_operations SET status='sending',error=NULL,updated_at=? "
                               'WHERE purchase_id=? AND status=?',
                               (datetime.now(TZ).isoformat(), purchase_id, expected))
            return result.rowcount == 1

    def update(self, purchase_id, status, *, result=None, error=None):
        result = result or {}
        with closing(self.store._open()) as c, c:
            # A late failure must not overwrite a successful concurrent read.
            c.execute('UPDATE shokh_iiko_operations SET status=?,document_id=COALESCE(?,document_id),'
                      'document_number=COALESCE(?,document_number),error=?,updated_at=? '
                      "WHERE purchase_id=? AND status!='synced'",
                      (status, result.get('id'), result.get('number'), error,
                       datetime.now(TZ).isoformat(), purchase_id))

    async def send(self, purchase_id, *, retry=False):
        operation = await asyncio.to_thread(self.operation, purchase_id=purchase_id)
        expected = 'rejected' if retry and operation and operation['status'] == 'rejected' else 'pending'
        if not await asyncio.to_thread(self.claim, purchase_id, expected):
            return await self.reconcile(purchase_id)
        operation = await asyncio.to_thread(self.operation, purchase_id=purchase_id)
        payload = json.loads(operation['payload'])
        try:
            result = await self.source.create(payload)
            await asyncio.to_thread(self.update, purchase_id, 'uncertain', result=result)
            document = await self.source.document(result['id'])
            verified = self.source.verify(document, payload)
            await asyncio.to_thread(self.update, purchase_id, 'synced', result=verified)
        except Rejected as error:
            await asyncio.to_thread(self.update, purchase_id, 'rejected', error=str(error))
        except Exception:
            # Don't expose upstream bodies, credentials, SQL or transport URLs.
            await asyncio.to_thread(self.update, purchase_id, 'uncertain',
                error='Ответ iiko не подтверждён. Нажмите «Проверить iiko»; повторная отправка заблокирована.')
        return await asyncio.to_thread(self.result, purchase_id)

    async def reconcile(self, purchase_id):
        operation = await asyncio.to_thread(self.operation, purchase_id=purchase_id)
        if not operation:
            raise ShokhError('Покупка не связана с iiko.')
        if operation['status'] not in ('sending', 'uncertain'):
            return await asyncio.to_thread(self.result, purchase_id)
        payload = json.loads(operation['payload'])
        try:
            document = (await self.source.document(operation['document_id']) if operation['document_id']
                        else await self.source.find(payload))
            if document is not None:
                result = self.source.verify(document, payload)
                await asyncio.to_thread(self.update, purchase_id, 'synced', result=result)
        except (DataError, ValueError, TypeError, KeyError, ArithmeticError):
            pass  # The durable uncertain state remains visible; absence isn't permission to resend.
        return await asyncio.to_thread(self.result, purchase_id)

    def result(self, purchase_id):
        return self.decorate([self.store.purchase(purchase_id)])[0]

    async def purchase(self, *, key, day, at, product_id, supplier_id, storage_id, unit_id,
                       point, quantity, price, trip_id=None, photo=None, photo_type=None):
        try:
            key = str(UUID(key))
        except (ValueError, TypeError, AttributeError):
            raise ShokhError('Не указан корректный ключ покупки. Обновите страницу.') from None
        submitted = dict(day=day.isoformat(), product=product_id, supplier=supplier_id,
                         storage=storage_id, unit=unit_id, point=point, quantity=quantity, price=price,
                         trip_id=trip_id, photo_type=photo_type,
                         photo_hash=hashlib.sha256(photo or b'').hexdigest())
        fingerprint = hashlib.sha256(json.dumps(submitted, sort_keys=True).encode()).hexdigest()
        existing = await asyncio.to_thread(self.operation, key=key)
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise ShokhError('Этот ключ уже использован для другой покупки.')
            return await self.send(existing['purchase_id'])
        catalog = await self.source.catalog(fresh=True)
        if not catalog['can_create']:
            raise ShokhError('У подключения iiko нет права проводить приходные накладные.')
        def select(rows, id_, title):
            row = next((r for r in rows if r['id'] == id_), None)
            if row is None:
                raise ShokhError(f'Выберите {title} из актуального справочника iiko.')
            return row
        product = select(catalog['items'], product_id, 'товар')
        supplier = select(catalog['suppliers'], supplier_id, 'поставщика')
        storage = select(catalog['storages'], storage_id, 'склад')
        if unit_id != product['unit_id']:
            raise ShokhError('Единица товара изменилась. Выберите товар заново.')
        from .store import _money, _text
        count = _money(quantity, name='количество', quantum='0.001')
        unit_price = _money(price, name='цену')
        if count != Decimal(str(quantity).replace(',', '.')) or unit_price != Decimal(str(price).replace(',', '.')):
            raise ShokhError('Укажите до трёх знаков после запятой в количестве и до двух в цене.')
        quantity, price = str(count), str(unit_price)
        if Decimal(quantity) * Decimal(price) > 1_000_000_000:
            raise ShokhError('Слишком большая сумма покупки.')
        point = _text(point or supplier['name'], name='точку закупа', limit=80)
        payload = self.source.invoice(catalog, key=key, day=day, at=at, product=product,
                                      supplier=supplier, storage=storage, quantity=quantity, price=price)
        purchase_id = await asyncio.to_thread(self.prepare, key, fingerprint, payload, day, at,
            dict(point=point, item=product['item'], unit=product['unit'], quantity=quantity,
                 price=price, trip_id=trip_id, photo=photo, photo_type=photo_type))
        return await self.send(purchase_id)
