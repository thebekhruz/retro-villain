from datetime import date

from retro.app import create_app
from retro.config import Settings
from retro.runtime import resolve_data_dir


def test_explicit_data_dir_wins_and_is_owner_only(tmp_path):
    target = tmp_path / 'shared'

    assert resolve_data_dir(str(target), {}) == target.resolve()
    assert target.stat().st_mode & 0o777 == 0o700


def test_xdg_data_dir_is_used_without_explicit_setting(tmp_path):
    expected = tmp_path / 'retro-villain'

    assert resolve_data_dir('', {'XDG_DATA_HOME': str(tmp_path)}) == expected
    assert expected.stat().st_mode & 0o777 == 0o700


def test_app_uses_shared_database_names(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))

    assert app.state.expenses.path == tmp_path / 'cashier.sqlite3'
    assert app.state.accountant_finance.path == tmp_path / 'accountant.sqlite3'
    assert app.state.director_store.path == tmp_path / 'director.sqlite3'
    assert app.state.founder_chat_store.path == tmp_path / 'founder.sqlite3'


def test_new_database_files_are_owner_only(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))

    app.state.expenses.list(date(2026, 9, 19))

    assert (tmp_path / 'cashier.sqlite3').stat().st_mode & 0o777 == 0o600
