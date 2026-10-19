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


def test_panel_users_are_read_from_environment(monkeypatch):
    monkeypatch.setenv(
        'DASHBOARD_PANEL_USERS',
        'cashier:test-password:cashier;accountant:test-password:accountant;director:test-password:director;founder:test-password:founder')

    assert Settings.from_env().dashboard_panel_users == {
        'cashier': ('test-password', 'cashier'),
        'accountant': ('test-password', 'accountant'),
        'director': ('test-password', 'director'),
        'founder': ('test-password', 'founder'),
    }


def test_panel_users_accept_optional_admin_role(monkeypatch):
    monkeypatch.setenv(
        'DASHBOARD_PANEL_USERS',
        'cashier:test-password:cashier;accountant:test-password:accountant;director:test-password:director;founder:test-password:founder;admin:test-password:admin')

    try:
        panel_users = Settings.from_env().dashboard_panel_users
    except ValueError:
        panel_users = {}

    assert panel_users == {
        'cashier': ('test-password', 'cashier'),
        'accountant': ('test-password', 'accountant'),
        'director': ('test-password', 'director'),
        'founder': ('test-password', 'founder'),
        'admin': ('test-password', 'admin'),
    }


def test_panel_users_allow_multiple_founder_accounts(monkeypatch):
    monkeypatch.setenv(
        'DASHBOARD_PANEL_USERS',
        'cashier:test-password:cashier;accountant:test-password:accountant;'
        'director:test-password:director;founder-one:test-password:founder;'
        'founder-two:other-password:founder')

    panel_users = Settings.from_env().dashboard_panel_users

    assert panel_users['founder-one'][1] == 'founder'
    assert panel_users['founder-two'][1] == 'founder'


@pytest.mark.parametrize('value', [
    'cashier:test-password',
    'cashier:test-password:cashier',
    'cashier:test-password:unknown',
    'cashier:test-password:cashier;cashier:other-password:founder',
    'cashier::cashier',
])
def test_panel_user_config_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv('DASHBOARD_PANEL_USERS', value)

    with pytest.raises(ValueError, match='DASHBOARD_PANEL_USERS'):
        Settings.from_env()


def test_director_price_tiers_default_and_custom(monkeypatch):
    monkeypatch.delenv('DIRECTOR_PRICE_TIERS', raising=False)
    assert Settings.from_env().director_price_tiers == (30000, 45000)
    monkeypatch.setenv('DIRECTOR_PRICE_TIERS', '')
    assert Settings.from_env().director_price_tiers == (30000, 45000)
    monkeypatch.setenv('DIRECTOR_PRICE_TIERS', '25 000, 40000,60000')
    assert Settings.from_env().director_price_tiers == (25000, 40000, 60000)


@pytest.mark.parametrize('value', ['45000,30000', '30000,30000', '0,30000', '30к', '1,2,3,4,5,6', ','])
def test_director_price_tiers_reject_invalid_values(monkeypatch, value):
    monkeypatch.setenv('DIRECTOR_PRICE_TIERS', value)

    with pytest.raises(ValueError, match='DIRECTOR_PRICE_TIERS'):
        Settings.from_env()
