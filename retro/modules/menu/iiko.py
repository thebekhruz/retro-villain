"""Номенклатура iiko: всё меню одним чтением, без отчётов OLAP.

Зачем отдельный источник. До этого модуля названия блюд приходили только из
отчёта продаж, поэтому позиции, которых за период никто не заказал, для панели
не существовали — а убирать из меню надо именно их. Здесь читается справочник:
карточки блюд и товаров с группой, единицей и ценой, независимо от продаж.

Контракт тот же, что у закупа (`modules/shokh/iiko.py`), и проверен по тем же
приложениям iikoWeb: `/api/productV3/list` отдаёт карточки, а
`/api/entities/list-of-type` — справочники групп, категорий и единиц. Здесь
только чтения, поэтому повтор запроса безопасен по построению.
"""
import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx

from retro.modules.cashier.service import DataError, TZ

# Что считаем меню. `GOODS` — это вся закупочная номенклатура (в том числе
# помидоры со склада), но напитки и алкоголь в iiko продаются именно товарами,
# поэтому тип берём целиком, а «в меню это или склад» решает признак позиции.
MENU_TYPES = ('DISH', 'GOODS', 'MODIFIER', 'SERVICE')
# Минимальный набор типов на случай, если расширенный запрос iiko не принял.
FALLBACK_TYPES = ('DISH', 'GOODS')
PROPERTIES = ('id', 'name', 'num', 'code', 'parent', 'category', 'type',
              'mainUnit', 'deleted', 'sizePrices')
# Без цен и групп, зато заведомо принимаемый набор: лучше справочник без цен,
# чем пустая таблица. Прогон запишет это в примечание, а не промолчит.
FALLBACK_PROPERTIES = ('id', 'name', 'num', 'type', 'mainUnit', 'deleted', 'parent')
# Дерево групп в iiko неглубокое; ограничение спасает от цикла в данных.
MAX_GROUP_DEPTH = 12


def text(value) -> str:
    """Имя из карточки iiko: иногда строка, иногда `{'customValue': ...}`."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get('customValue') or '').strip()
    return ''


def money_or_none(value):
    """Цена карточки как наши деньги — текстом, до копеек. Мусор → None.

    Одна странная карточка не должна валить недельный прогон: такие позиции
    просто остаются без цены, а их число попадает в примечание прогона.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).replace(',', '.').strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not amount.is_finite() or amount < 0 or amount > 1_000_000_000:
        return None
    return str(amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def price_and_menu_flag(product):
    """Цена и признак «в меню» из `sizePrices`, как их отдаёт карточка iiko.

    У блюда столько цен, сколько у него размеров, и каждая помечена признаком
    «входит в меню». Берём самую большую из входящих в меню — это цена, которую
    видит гость; если в меню не входит ни одна, берём максимум просто чтобы
    цена была известна, но признак остаётся отрицательным.
    """
    sizes = product.get('sizePrices')
    found = []
    if isinstance(sizes, list):
        for size in sizes:
            block = size.get('price') if isinstance(size, dict) else None
            if not isinstance(block, dict):
                continue
            value = money_or_none(block.get('currentPrice'))
            if value is None:
                continue
            found.append((bool(block.get('isIncludedInMenu')), Decimal(value)))
    if found:
        in_menu = any(flag for flag, _ in found)
        chosen = max(value for flag, value in found if flag or not in_menu)
        return str(chosen), in_menu
    # Расширенных свойств в ответе нет (см. FALLBACK_PROPERTIES) — тогда и
    # признака «в меню» мы не знаем. Врать «не в меню» нельзя: это разные вещи.
    for key in ('price', 'defaultSalePrice', 'currentPrice'):
        value = money_or_none(product.get(key))
        if value is not None:
            return value, None
    return None, None


def group_paths(groups):
    """id группы → путь вида «Кухня/Горячие блюда». Цикл в данных не зацикливает."""
    names = {}
    parents = {}
    for group in groups:
        if not isinstance(group, dict) or not group.get('id'):
            continue
        names[group['id']] = text(group.get('name'))
        parent = group.get('parent') or group.get('parentId') or ''
        parents[group['id']] = parent if isinstance(parent, str) else ''
    result = {}
    for group_id in names:
        parts, current, seen = [], group_id, set()
        while current and current in names and current not in seen and len(parts) < MAX_GROUP_DEPTH:
            seen.add(current)
            if names[current]:
                parts.append(names[current])
            current = parents.get(current, '')
        result[group_id] = '/'.join(reversed(parts))
    return result


def entity_names(entities):
    """id → имя для плоских справочников iiko (единицы, категории)."""
    return {item['id']: text(item.get('name'))
            for item in entities
            if isinstance(item, dict) and item.get('id') and not item.get('isDeleted')
            and not item.get('deleted')}


class MenuIiko:
    def __init__(self, client):
        self.client = client

    async def read(self, path, *, params=None, body=None):
        """Одно чтение справочника тем же авторизованным клиентом, что отчёты."""
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
                    raise DataError('iiko отклонил доступ к номенклатуре. Проверьте права учётной записи.')
                if not response.is_success:
                    raise DataError(f'Не удалось загрузить меню из iiko (HTTP {response.status_code}).')
                data = response.json()
                if isinstance(data, dict):
                    if data.get('error') or data.get('formValidationError'):
                        raise DataError('iiko отклонил запрос номенклатуры.')
                    return data.get('data', data)
                if isinstance(data, list):
                    return data
                raise ValueError()
        except (httpx.HTTPError, ValueError, TypeError):
            raise DataError('Не удалось связаться с iiko. Повторите загрузку меню.') from None

    async def _products(self, notes):
        """Карточки меню. Расширенный запрос, при отказе — заведомо принимаемый."""
        try:
            products = await self.read('/api/productV3/list', body={
                'types': list(MENU_TYPES), 'properties': list(PROPERTIES)})
        except DataError as error:
            notes.append(f'расширенный запрос номенклатуры не прошёл ({error}), меню без цен')
            products = await self.read('/api/productV3/list', body={
                'types': list(FALLBACK_TYPES), 'properties': list(FALLBACK_PROPERTIES)})
        if not isinstance(products, list) or not products:
            raise DataError('iiko вернул пустую номенклатуру. Повторите загрузку меню.')
        return products

    async def _dictionary(self, entity_type, notes, what):
        """Справочник, без которого меню всё равно полезно: отказ — примечание."""
        try:
            data = await self.read('/api/entities/list-of-type',
                                   params={'type': entity_type, 'includeDeleted': 'false'})
        except DataError as error:
            notes.append(f'{what} не прочитаны ({error})')
            return []
        if not isinstance(data, list):
            notes.append(f'{what} пришли в неизвестном виде')
            return []
        return data

    async def nomenclature(self):
        """Всё меню iiko одним снимком: позиции + чем прогон был неполон."""
        if not self.client.settings.configured:
            raise DataError('Подключение iiko не настроено. Обратитесь к администратору.')
        notes = []
        products = await self._products(notes)
        groups, categories, units = await asyncio.gather(
            self._dictionary('ProductGroup', notes, 'группы меню'),
            self._dictionary('ProductCategory', notes, 'категории блюд'),
            self._dictionary('MeasureUnit', notes, 'единицы измерения'))
        paths = group_paths(groups)
        category_names, unit_names = entity_names(categories), entity_names(units)
        items, unpriced = [], 0
        for product in products:
            if not isinstance(product, dict) or not product.get('id'):
                continue
            name = text(product.get('name'))
            if not name:
                continue
            price, in_menu = price_and_menu_flag(product)
            if price is None:
                unpriced += 1
            group_id = product.get('parent') or ''
            items.append(dict(
                product_id=str(product['id']),
                code=str(product.get('num') or product.get('code') or '').strip(),
                name=name,
                kind=str(product.get('type') or '').strip().upper(),
                group_id=group_id if isinstance(group_id, str) else '',
                group_path=paths.get(group_id, '') if isinstance(group_id, str) else '',
                category=category_names.get(product.get('category'), ''),
                unit=unit_names.get(product.get('mainUnit'), ''),
                price=price, in_menu=in_menu, deleted=bool(product.get('deleted'))))
        if not items:
            raise DataError('iiko не вернул ни одной позиции меню.')
        if unpriced:
            notes.append(f'без цены {unpriced} из {len(items)} позиций')
        items.sort(key=lambda item: (item['group_path'].casefold(), item['name'].casefold()))
        return dict(source='iiko · номенклатура', read_at=datetime.now(TZ).isoformat(),
                    items=items, note='; '.join(notes))
