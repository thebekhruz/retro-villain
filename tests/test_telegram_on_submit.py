"""T-484: «Передать бухгалтеру» и «Сохранить и сдать отчёт» отправляют отчёт в Telegram."""
from decimal import Decimal

from test_accountant_month_close import SEP_30, client
from test_cashier_design_parity import DAY, c, hand_over, make_app, till_day  # noqa: F401 — фикстуры
from legacy_boundary import legacy_boundary  # noqa: F401 — фикстура


class Recorder:
    """Вместо бота: что и за какой день попросили отправить."""

    def __init__(self):
        self.calls = []

    async def notify(self, kind, day):
        self.calls.append((kind, day))

    async def close(self):
        pass


def test_cashier_handover_sends_the_cashier_report(c):
    c.app.state.telegram_report = recorder = Recorder()
    snapshot = till_day(c)
    refused = hand_over(c, snapshot, '999999')
    assert refused.status_code == 409 and recorder.calls == []  # не передано — не отправляем
    assert hand_over(c, snapshot, '850000').status_code == 201
    assert recorder.calls == [('cashier', DAY)]


def test_accountant_day_report_sends_finance_of_the_day(tmp_path, monkeypatch, legacy_boundary):
    with client(tmp_path, monkeypatch) as c:
        c.app.state.telegram_report = recorder = Recorder()
        finance = c.app.state.accountant_finance
        finance.record_handover(SEP_30, Decimal('1000000'), source='cashier')
        refused = c.post('/api/accountant/day-report', json={'date': SEP_30.isoformat()})
        assert refused.status_code == 422 and recorder.calls == []
        assert c.post('/api/accountant/handover/confirm', json={
            'date': SEP_30.isoformat(), 'amount': '1000000'}).status_code == 200
        assert c.post('/api/accountant/day-report', json={'date': SEP_30.isoformat()}).status_code == 200
        assert recorder.calls == [('accountant', SEP_30)]


def test_without_bot_buttons_work_as_before(c):
    assert c.app.state.telegram_report is None
    assert hand_over(c, till_day(c), '850000').status_code == 201
