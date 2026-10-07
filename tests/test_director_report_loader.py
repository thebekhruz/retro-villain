"""«Сформировать отчёт» у директора: загрузчик приложения принимает период."""

import asyncio
from datetime import date

from legacy_app import create_app
from retro.config import Settings


def test_app_director_loader_accepts_period(tmp_path, monkeypatch):
    app = create_app(Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     director_db_path=tmp_path / 'director.sqlite3')
    seen = {}

    async def fake_load(state, method, *args, **kwargs):
        seen.update(method=method, args=args, kwargs=kwargs)
        return 'snapshot'

    monkeypatch.setattr('retro.app.load_iiko', fake_load)
    loader = app.state.director_service.loader
    result = asyncio.run(loader(date(2026, 9, 29), start=date(2026, 9, 1), end=date(2026, 9, 28)))
    assert result == 'snapshot'
    assert seen['method'] == 'load_director_report'
    assert seen['kwargs']['start'] == date(2026, 9, 1) and seen['kwargs']['end'] == date(2026, 9, 28)
