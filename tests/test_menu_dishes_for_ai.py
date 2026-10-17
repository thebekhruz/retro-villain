"""ТЗ 16.10, 2.3 и 3.1: AI называет блюдом только готовую позицию меню (T-487)."""
import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import Settings
from retro.integrations.claude import compact_analysis_input
from retro.modules.cashier.service import TZ
from retro.modules.director.tools import DirectorChatTools
from retro.modules.founder.tools import FounderChatTools
from retro.modules.menu.catalog import MenuCatalog

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=TZ)


def card(product_id, name, kind, *, category='', deleted=False):
    return dict(product_id=product_id, name=name, kind=kind, category=category, deleted=deleted)


# Как в настоящей номенклатуре Retro: бутылки в зале — DISH, склад — GOODS,
# «Мед» заведён и блюдом, и товаром, компоненты сетов — модификаторы.
CARDS = [
    card('1', 'Лагман Уйгурский', 'DISH'),
    card('2', 'Пиво Жатецкий Гусь', 'DISH'),
    card('3', 'Пиво Жатецкий Гусь', 'GOODS'),
    card('4', 'Вино КРАСНОЕ Азалия  сухое 0,750 л', 'DISH', category='алкоголь'),
    card('5', 'Мед', 'DISH'),
    card('6', 'Мед', 'GOODS'),
    card('7', 'Листья салата', 'GOODS'),
    card('8', 'Зелень Кинза', 'GOODS'),
    card('9', 'Салат Ачичук сет', 'MODIFIER'),
    card('10', 'Аренда зала', 'SERVICE'),
    card('11', 'Тесто слоёное', 'PREPARED'),
    card('12', 'Долма', 'DISH', deleted=True),
    card('13', 'Пиде с Сыром и Медом', 'DISH'),
]


def test_classes_follow_iiko_type_and_exclusion_rules():
    catalog = MenuCatalog(CARDS)
    assert catalog.classify('Лагман Уйгурский') == 'dish'
    assert catalog.classify('лагман  уйгурский') == 'dish'  # регистр и пробелы
    assert catalog.classify('Пиво Жатецкий Гусь') == 'drink'
    assert catalog.classify('Вино КРАСНОЕ Азалия  сухое 0,750 л') == 'drink'
    # Мёд — правило исключения ТЗ 3.1, хотя в iiko есть карточка DISH.
    assert catalog.classify('Мед') == 'ingredient'
    assert catalog.classify('Мёд') == 'ingredient'
    assert catalog.classify('Листья салата') == 'ingredient'
    assert catalog.classify('Зелень Кинза') == 'stock'
    assert catalog.classify('Салат Ачичук сет') == 'set_component'
    assert catalog.classify('Аренда зала') == 'service'
    assert catalog.classify('Тесто слоёное') == 'semi_finished'
    assert catalog.classify('Долма') == 'dish'  # удалённое блюдо в старых продажах
    assert catalog.classify('Пиде с Сыром и Медом') == 'dish'
    assert catalog.classify('Новое блюдо') == 'unknown'


def test_ready_menu_lists_only_live_dishes_and_drinks():
    result = MenuCatalog(CARDS).ready(limit=50)
    names = {row['name'] for row in result['items']}
    assert names == {'Лагман Уйгурский', 'Пиво Жатецкий Гусь',
                     'Вино КРАСНОЕ Азалия  сухое 0,750 л', 'Пиде с Сыром и Медом'}
    assert result['counts']['stock'] == 1 and result['counts']['ingredient'] == 2
    assert MenuCatalog(CARDS).ready(query='пиво', limit=50)['total_ready'] == 1
    limited = MenuCatalog(CARDS).ready(limit=1)
    assert limited['returned'] == 1 and limited['truncated'] is True


def test_empty_catalog_excludes_nothing():
    catalog = MenuCatalog([])
    assert not catalog.available
    assert catalog.exceptions(['Мед', 'Лагман']) == {'Мед': 'ingredient', 'Лагман': 'unknown'}


def metric(quantity, revenue, gross):
    return {'quantity': str(quantity), 'revenue': str(revenue), 'cost': str(revenue - gross),
            'gross_profit': str(gross), 'margin_percent': str(round(gross / revenue * 100, 2))
            if revenue else None, 'breakdown': {}}


def test_report_analysis_never_proposes_ingredients_or_set_parts():
    snapshot = {'item_metrics': {'retro': {
        'Мед': metric(3, 24000, -6000),
        'Салат Ачичук сет': metric(191, 0, -50000),
        'Лагман Уйгурский': metric(40, 1600000, -10000),
    }}}
    classes = MenuCatalog(CARDS).exceptions(snapshot['item_metrics']['retro'])
    data = compact_analysis_input(snapshot, classes)
    assert [item['subject'] for item in data['review_candidates']] == ['Лагман Уйгурский']
    assert data['not_menu_dishes_excluded'] == ['Мед', 'Салат Ачичук сет']
    # Без справочника анализ прежний: ничего не прячем.
    assert len(compact_analysis_input(snapshot)['review_candidates']) == 3


@pytest.fixture
def app(tmp_path):
    application = create_app(Settings(data_dir=tmp_path))
    application.state.menu_store.save(CARDS, at=NOW)
    return application


def test_menu_catalog_tool_for_founder_and_director(app):
    with TestClient(app):
        founder = asyncio.run(FounderChatTools(app).execute('get_menu_catalog', {'limit': 10}))
        director = asyncio.run(DirectorChatTools(app).execute(
            'get_menu_catalog', {'limit': 10, 'query': 'лагман'}))
    assert 'Мед' not in {row['name'] for row in founder['items']}
    assert founder['total_ready'] == 4
    assert [row['name'] for row in director['items']] == ['Лагман Уйгурский']


class Snapshot:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class SalesIiko:
    async def load_director_report(self, today, **kwargs):
        return Snapshot({'item_metrics': {'all': {
            'Лагман Уйгурский': metric(40, 1600000, 900000), 'Мед': metric(3, 24000, 18000)}}})

    async def load_sales_details(self, start, end, dimensions, *, limit, **kwargs):
        return {'rows': [{'dimensions': {'DishName': 'Мед'}, 'quantity': '3'},
                         {'dimensions': {'DishName': 'Лагман Уйгурский'}, 'quantity': '40'}],
                'warnings': []}


def test_sales_given_to_ai_carry_item_classes(app):
    app.state.iiko = SalesIiko()
    with TestClient(app):
        tools = DirectorChatTools(app)
        dashboard = asyncio.run(tools.execute('get_director_dashboard', {'as_of': '2026-10-15'}))
        details = asyncio.run(tools.execute('get_iiko_sales_details', {
            'start': '2026-10-13', 'end': '2026-10-15', 'dimensions': ['DishName'], 'limit': 10}))
    assert dashboard['item_classes'] == {'Мед': 'ingredient'}
    assert dashboard['item_classes_checked'] is True
    assert [row['item_class'] for row in details['rows']] == ['ingredient', 'dish']
