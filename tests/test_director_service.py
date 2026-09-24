import asyncio
from datetime import date
from types import SimpleNamespace

from retro.modules.director.service import DirectorService
from retro.modules.director.store import DirectorReportStore


class IikoStub:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def load_director_report(self, today):
        return SimpleNamespace(json=lambda: dict(self.snapshot))


class ClaudeStub:
    def __init__(self):
        self.calls = 0

    async def analyze(self, snapshot):
        self.calls += 1
        return {'summary': 'Краткий вывод.', 'problems': []}


def snapshot(cash_total='1000'):
    return {
        'period_start': '2026-09-08',
        'period_end': '2026-09-17',
        'cash_total': cash_total,
        'yandex_revenue': '0',
        'item_metrics': {},
        'waiter_metrics': {},
    }


def test_same_snapshot_rerenders_locally_without_second_claude_call(tmp_path):
    iiko = IikoStub(snapshot())
    claude = ClaudeStub()
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    service = DirectorService(iiko, claude, store)

    first = asyncio.run(service.generate(date(2026, 9, 18)))
    second = asyncio.run(service.generate(date(2026, 9, 18)))

    assert first['id'] != second['id']
    assert claude.calls == 1
    assert store.get_pdf(first['id']).startswith(b'%PDF-')


def test_changed_snapshot_requests_fresh_analysis(tmp_path):
    iiko = IikoStub(snapshot())
    claude = ClaudeStub()
    service = DirectorService(iiko, claude, DirectorReportStore(tmp_path / 'director.sqlite3'))

    asyncio.run(service.generate(date(2026, 9, 18)))
    iiko.snapshot = snapshot('2000')
    asyncio.run(service.generate(date(2026, 9, 18)))

    assert claude.calls == 2
