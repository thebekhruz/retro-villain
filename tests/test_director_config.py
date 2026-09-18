import pytest

from retro.config import Settings


def test_director_config_reads_claude_and_category_map(monkeypatch):
    monkeypatch.setenv('CLAUDE_API_KEY', 'key')
    monkeypatch.setenv('CLAUDE_MODEL', 'claude-test')
    monkeypatch.setenv('IIKO_DIRECTOR_CATEGORIES',
                       'Основное меню=menu;Десерты=dessert;Напитки=drink')

    settings = Settings.from_env()

    assert settings.claude_configured is True
    assert settings.director_categories == {
        'Основное меню': 'menu', 'Десерты': 'dessert', 'Напитки': 'drink',
    }


@pytest.mark.parametrize('value', ['Меню=', 'Меню=unknown', 'Меню=menu;Меню=dessert', 'Меню'])
def test_director_category_config_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv('IIKO_DIRECTOR_CATEGORIES', value)

    with pytest.raises(ValueError, match='IIKO_DIRECTOR_CATEGORIES'):
        Settings.from_env()


def test_director_categories_may_be_unset_before_director_goes_live(monkeypatch):
    monkeypatch.setenv('IIKO_DIRECTOR_CATEGORIES', '')

    assert Settings.from_env().director_categories == {}


def test_director_excluded_groups_are_parsed(monkeypatch):
    monkeypatch.setenv('IIKO_DIRECTOR_EXCLUDED_GROUPS', 'Контейнеры;ДОСТАВКА ЯНДЕКС')

    assert Settings.from_env().director_excluded_groups == {'Контейнеры', 'ДОСТАВКА ЯНДЕКС'}
