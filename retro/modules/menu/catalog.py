"""Готовые позиции меню против ингредиентов: что AI может называть блюдом.

ТЗ 16.10 (2.3, 3.1): AI директора и учредителя советовал продавать мёд и
листья салата как блюда. Отчёт продаж iiko (`DishName`) не отличает блюдо от
добавки, компонента сета или товара, а закупки Шоха — это сырьё. Поэтому класс
позиции берём из сохранённой номенклатуры iiko (`menu_items`, раз в неделю):

* `DISH` — готовая позиция меню (блюдо или напиток: бутылки в зале тоже `DISH`);
* `GOODS` — склад: сырьё, ингредиенты, закупочные и хозяйственные товары;
* `PREPARED` — полуфабрикат; `MODIFIER` — компонент сета или добавка;
  `SERVICE` — услуга (аренда зала, доставка, бой посуды).

Плюс ручные исключения `NOT_DISHES`: позиции, которые в iiko заведены блюдом,
но по сути ингредиент. Названия, которых нет в номенклатуре, остаются
`unknown` — их не прячем, а помечаем непроверенными.
"""
from collections import Counter, defaultdict

CLASSES = {
    'dish': 'готовое блюдо меню',
    'drink': 'напиток меню',
    'ingredient': 'ингредиент или сырьё — не блюдо (правило исключения)',
    'stock': 'сырьё, ингредиент или закупочный товар склада — не блюдо',
    'semi_finished': 'полуфабрикат — не блюдо',
    'set_component': 'компонент сета или добавка — отдельно гостю не продаётся',
    'service': 'услуга — не блюдо',
    'unknown': 'нет в номенклатуре iiko — тип не проверен',
}
READY = frozenset({'dish', 'drink'})
NOT_READY = frozenset({'ingredient', 'stock', 'semi_finished', 'set_component', 'service'})
# Правила исключения ТЗ 3.1: в iiko это «блюдо», а гостю продаётся как
# добавка. Расширять по слову заказчика; сравнение без регистра и «ё».
NOT_DISHES = frozenset({'мед', 'листья салата', 'лист салата'})
DRINK_CATEGORIES = frozenset({'алкоголь'})
DRINK_PREFIXES = ('напиток', 'пиво', 'вино', 'водка', 'виски', 'коньяк', 'шампанское',
                  'ликер', 'текила', 'сок', 'лимонад', 'коктейль', 'компот',
                  'морс', 'чай', 'кофе', 'капучино', 'латте', 'американо', 'эспрессо',
                  'мин.вода', 'вода', 'фанта', 'спрайт', 'айран')
KIND_CLASSES = (('DISH', 'dish'), ('PREPARED', 'semi_finished'), ('GOODS', 'stock'),
                ('MODIFIER', 'set_component'), ('SERVICE', 'service'))


def key(name):
    return ' '.join(str(name or '').casefold().replace('ё', 'е').split())


def is_drink(name, category):
    if key(category) in DRINK_CATEGORIES:
        return True
    words = key(name)
    return any(words == prefix or words.startswith(prefix + ' ') for prefix in DRINK_PREFIXES)


class MenuCatalog:
    """Индекс номенклатуры по названию. Пустой каталог ничего не исключает."""

    def __init__(self, items, *, synced_at=None):
        self.synced_at = synced_at
        live, deleted = defaultdict(list), defaultdict(list)
        for item in items:
            (deleted if item.get('deleted') else live)[key(item.get('name'))].append(item)
        # Удалённая карточка решает, только если живой с тем же названием нет:
        # блюдо могли пересоздать, а старая карточка осталась в iiko.
        self._index = {name: live.get(name) or deleted[name] for name in {*live, *deleted}}

    @classmethod
    def from_store(cls, store):
        state = store.state()
        return cls(store.items(scope='all'), synced_at=(state or {}).get('last_success_at'))

    @property
    def available(self):
        return bool(self._index)

    def classify(self, name):
        name_key = key(name)
        if name_key in NOT_DISHES:
            return 'ingredient'
        cards = self._index.get(name_key)
        if not cards:
            return 'unknown'
        kinds = {card.get('kind') for card in cards}
        for kind, item_class in KIND_CLASSES:
            if kind in kinds:
                if item_class == 'dish' and any(is_drink(card.get('name'), card.get('category'))
                                                for card in cards if card.get('kind') == 'DISH'):
                    return 'drink'
                return item_class
        return 'unknown'

    def exceptions(self, names):
        """Только позиции, которые НЕ готовые блюда: ответ остаётся коротким."""
        result = {}
        for name in names:
            item_class = self.classify(name)
            if item_class not in READY:
                result[name] = item_class
        return result

    def ready(self, *, query='', limit=200):
        """Готовые позиции меню (живые карточки) — справочник для AI."""
        query_key = key(query)
        rows, counts = [], Counter()
        for name_key, cards in sorted(self._index.items()):
            card = cards[0]
            item_class = self.classify(card.get('name'))
            if card.get('deleted'):
                continue
            counts[item_class] += 1
            if item_class in READY and (not query_key or query_key in name_key):
                rows.append({'name': card.get('name'), 'class': item_class,
                             'category': card.get('category') or ''})
        return {
            'source': 'iiko · номенклатура (снимок раз в неделю)',
            'synced_at': self.synced_at,
            'available': self.available,
            'classes': CLASSES,
            'not_dishes_rule': sorted(NOT_DISHES),
            'counts': dict(counts),
            'items': rows[:limit],
            'returned': min(len(rows), limit),
            'total_ready': len(rows),
            'truncated': len(rows) > limit,
        }
