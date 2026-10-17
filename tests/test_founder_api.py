from datetime import date

from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import Settings
from retro.modules.cashier.service import DataError
from retro.integrations.broadcasts import BroadcastConflict


class FounderIiko:
    def __init__(self, result=None, error=None):
        self.result = result or {'ok': True}
        self.error = error
        self.calls = []

    async def load_founder_analytics(self, start, end, granularity, directions):
        self.calls.append((start, end, granularity, directions))
        if self.error:
            raise self.error
        return self.result


class FounderBookings:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def load(self, start, end):
        self.calls.append((start, end))
        if self.error:
            raise self.error
        return self.result


class FounderBroadcasts:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def audience(self):
        if self.error:
            raise self.error
        return {'subscribers': 2, 'profiles': 9, 'recipients': [
            {'id': 'A' * 32, 'name': 'Азиза', 'phone': '••• 12 34'},
            {'id': 'B' * 32, 'name': 'Бехруз', 'phone': '••• 56 78'},
        ]}

    async def start(self, operation_id, text, recipient_ids, author=None):
        self.calls.append((operation_id, text, recipient_ids, author))
        if self.error:
            raise self.error
        return {'id': operation_id, 'status': 'queued', 'audience': len(recipient_ids),
                'sent': 0, 'blocked': 0, 'failed': 0}

    async def status(self, operation_id):
        self.calls.append(('status', operation_id))
        if self.error:
            raise self.error
        return {'id': operation_id, 'status': 'completed', 'audience': 7,
                'sent': 6, 'blocked': 1, 'failed': 0}

    async def list(self, kind='all', cursor=None, limit=20):
        self.calls.append(('list', kind, cursor))
        if self.error:
            raise self.error
        return {'supported': True, 'jobs': [], 'total': 0, 'next_cursor': None}

    async def deliveries(self, job_id, statuses=None, offset=0, limit=100):
        self.calls.append(('deliveries', job_id, statuses, offset))
        if self.error:
            raise self.error
        return {'job_id': job_id, 'recipients_known': True, 'total': 0, 'recipients': [], 'next_offset': None}

    async def resume(self, job_id, author=None):
        self.calls.append(('resume', job_id, author))
        if self.error:
            raise self.error
        return {'job': {'id': job_id, 'status': 'queued'}, 'started': True}


def client_with(source):
    app = create_app(Settings())
    app.state.iiko = source
    return TestClient(app, client=('127.0.0.1', 50000))


def test_founder_api_parses_inclusive_period_granularity_and_directions():
    source = FounderIiko({'period': {'start': '2026-09-01', 'end': '2026-09-08'}})
    with client_with(source) as client:
        response = client.get('/api/founder/analytics', params={
            'start': '2026-09-01', 'end': '2026-09-08',
            'granularity': 'week', 'directions': 'retro,banquet',
        })

    assert response.status_code == 200
    assert response.json()['period']['end'] == '2026-09-08'
    assert source.calls == [
        (date(2026, 9, 1), date(2026, 9, 8), 'week', ('retro', 'banquet'))]


def test_founder_api_combines_cashier_and_accountant_expenses_without_mutating_pnl(tmp_path, monkeypatch):
    # Формула расходов на архивных сентябрьских данных; граница 13.10 — в
    # test_founder_api_has_no_dashboard_expenses_before_accounting_start.
    from retro.modules.founder import routes
    monkeypatch.setattr(routes, 'ACCOUNTING_START', date(2026, 9, 1))
    source = FounderIiko({'pnl': {'net_profit': '1000'}, 'scope_note': 'iiko.'})
    app = create_app(
        Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
        accountant_db_path=tmp_path / 'accountant.sqlite3')
    app.state.iiko = source
    app.state.expenses.add(date(2026, 9, 1), 'Расход кассы', '125.50')
    app.state.expenses.add(date(2026, 9, 9), 'Вне периода', '999')
    app.state.accountant_finance.add_opening(date(2026, 9, 1), '1000', 'Остаток')
    app.state.accountant_finance.add_expense(
        date(2026, 9, 2), 'admin_it', 'Сервис', '200')

    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/founder/analytics', params={
            'start': '2026-09-01', 'end': '2026-09-08'})

    assert response.status_code == 200
    assert response.json()['pnl']['net_profit'] == '1000'
    assert response.json()['dashboard_expenses'] == {
        'cashier': '125.50', 'accountant_other': '200',
        'accountant_salary': '0', 'accountant': '200', 'total': '325.50'}
    assert response.json()['net_profit_after_dashboard_expenses'] == '674.50'


def test_founder_api_has_no_dashboard_expenses_before_accounting_start(tmp_path, monkeypatch):
    """Период с днями до 13.10: расходов кассы и бухгалтерии за них нет — не 0, а None (T-486)."""
    from retro.modules.founder import routes
    monkeypatch.setattr(routes, 'today_tashkent', lambda: date(2026, 10, 16))
    source = FounderIiko({'pnl': {'net_profit': '1000'}, 'warnings': []})
    app = create_app(
        Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
        accountant_db_path=tmp_path / 'accountant.sqlite3')
    app.state.iiko = source
    app.state.expenses.add(date(2026, 10, 14), 'Расход кассы', '125.50')

    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/founder/analytics', params={
            'start': '2026-09-16', 'end': '2026-10-15'})

    assert response.status_code == 200
    data = response.json()
    assert source.calls[0][:2] == (date(2026, 9, 16), date(2026, 10, 15))
    assert data['pnl']['net_profit'] == '1000'
    assert data['dashboard_expenses'] is None
    assert data['net_profit_after_dashboard_expenses'] is None
    assert any('13.10.2026' in warning for warning in data['warnings'])


def test_founder_api_rejects_future_oversized_and_unknown_filters():
    source = FounderIiko()
    with client_with(source) as client:
        assert client.get('/api/founder/analytics?start=2026-01-01&end=2027-01-02').status_code == 422
        assert client.get('/api/founder/analytics?start=2099-01-01&end=2099-01-02').status_code == 422
        assert client.get('/api/founder/analytics?start=2026-09-01&end=2026-09-08&directions=retro,other').status_code == 422
        assert client.get('/api/founder/analytics?start=2026-09-08&end=2026-09-01').status_code == 422
    assert source.calls == []


def test_founder_api_does_not_replace_iiko_error_with_zeroes():
    source = FounderIiko(error=DataError('iiko недоступен'))
    with client_with(source) as client:
        response = client.get('/api/founder/analytics', params={
            'start': '2026-09-01', 'end': '2026-09-08'})

    assert response.status_code == 503
    assert response.json() == {'detail': 'iiko недоступен'}


def test_founder_page_and_module_are_exposed():
    source = FounderIiko()
    with client_with(source) as client:
        page = client.get('/founder')
        config = client.get('/api/config').json()

    assert page.status_code == 200
    assert 'Учредитель' in page.text
    # Ищем модуль по id, а не по месту в списке: меню пополняется новыми
    # модулями, и позиция «учредителя» к смыслу проверки не относится.
    # Причины отказа больше нет (T-383): закрытые модули сервер не отдаёт вовсе.
    founder_module = next(item for item in config['modules'] if item['id'] == 'founder')
    assert founder_module == {'id': 'founder', 'name': 'Учредитель',
                              'path': '/founder', 'available': True}


def test_founder_booking_api_applies_period_and_granularity_without_directions():
    coverage = {'history_started_at': '2026-09-01T08:00:00.000Z',
                'historical_data_complete': False}
    empty = {'bookings': 0, 'guests': 0, 'unknown_guest_bookings': 0}
    submitted = {'bookings': 2, 'guests': 5, 'unknown_guest_bookings': 1}
    source = FounderBookings({
        'submitted': {
            'coverage': coverage, 'excluded_missing_date': 0, 'totals': submitted,
            'by_date': [{'value': '2026-09-01', **submitted}],
            'by_source': [{'value': 'direct', 'label': 'direct', **submitted}],
        },
        'cancelled': {
            'coverage': coverage, 'excluded_missing_date': 0, 'totals': empty,
            'by_date': [], 'by_source': [],
        },
    })
    app = create_app(Settings())
    app.state.bookings = source

    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/founder/bookings', params={
            'start': '2026-09-01', 'end': '2026-09-08', 'granularity': 'week'})

    assert response.status_code == 200
    assert response.json()['totals'] == {
        'bookings': 2, 'guests': 5, 'unknown_guest_bookings': 1, 'cancelled': 0, 'all': 2}
    assert response.json()['series'][0]['values'] == {
        'bookings': 2, 'guests': 5, 'cancelled': 0}
    assert source.calls == [(date(2026, 9, 1), date(2026, 9, 8))]


class GuestIiko:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.calls = []

    async def load_guest_counts(self, start, end):
        self.calls.append((start, end))
        if self.error:
            raise self.error
        return self.rows


def test_founder_guests_api_counts_retro_checks_by_period_before_accounting_start():
    """ТЗ 16.10, п. 5.3: гости по чекам iiko рядом с бронями. Это живое
    чтение iiko — граница учёта 13.10 к нему не относится."""
    iiko = GuestIiko([
        {'day': '2026-09-01', 'direction': 'retro', 'guests': 40, 'orders': 25},
        {'day': '2026-09-01', 'direction': 'school', 'guests': 70, 'orders': 70},
        {'day': '2026-09-07', 'direction': 'retro', 'guests': 12, 'orders': 9},
    ])
    app = create_app(Settings())
    app.state.iiko = iiko
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/founder/guests', params={
            'start': '2026-09-01', 'end': '2026-09-08', 'granularity': 'week'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['totals'] == {'retro': {'guests': 52, 'checks': 34}, 'school': {'guests': 70, 'checks': 70}}
    assert [(row['start'], row['end'], row['values']) for row in body['series']] == [
        ('2026-09-01', '2026-09-06', {'guests': 40, 'checks': 25}),
        ('2026-09-07', '2026-09-08', {'guests': 12, 'checks': 9})]
    assert iiko.calls == [(date(2026, 9, 1), date(2026, 9, 8))]


def test_founder_guests_api_reports_iiko_failure_as_unavailable():
    app = create_app(Settings())
    app.state.iiko = GuestIiko(error=DataError('iiko недоступен'))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/founder/guests?start=2026-09-01&end=2026-09-08')
    assert response.status_code == 503
    assert response.json()['detail'] == 'iiko недоступен'


def test_booking_api_error_is_local_and_does_not_replace_iiko_data():
    app = create_app(Settings())
    app.state.iiko = FounderIiko({'revenue_series': [{'ok': True}]})
    app.state.bookings = FounderBookings(error=DataError('бот недоступен'))

    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        booking_response = client.get(
            '/api/founder/bookings?start=2026-09-01&end=2026-09-08')
        iiko_response = client.get(
            '/api/founder/analytics?start=2026-09-01&end=2026-09-08')

    assert booking_response.status_code == 503
    assert booking_response.json() == {'detail': 'бот недоступен'}
    assert iiko_response.status_code == 200
    assert iiko_response.json() == {'revenue_series': [{'ok': True}]}


def test_founder_broadcast_preview_start_and_status_are_server_side():
    app = create_app(Settings())
    source = FounderBroadcasts()
    app.state.broadcasts = source
    operation_id = '12345678-1234-4123-8123-123456789012'
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        audience = client.get('/api/founder/broadcast/audience')
        started = client.post('/api/founder/broadcast', json={
            'operation_id': operation_id, 'text': '  Новое меню  ',
            'recipient_ids': ['B' * 32]})
        status = client.get('/api/founder/broadcast/' + operation_id)
    assert audience.json()['recipients'][1]['name'] == 'Бехруз'
    assert started.status_code == 202
    assert status.json()['sent'] == 6
    # Без входа (стенд с петли) автор — логин «local»; с входом — имя учётной записи.
    assert source.calls == [
        (operation_id, 'Новое меню', ['B' * 32], 'local'), ('status', operation_id)]


def test_founder_broadcast_rejects_empty_text_and_reports_parallel_run():
    app = create_app(Settings())
    source = FounderBroadcasts(error=BroadcastConflict('Другая рассылка уже выполняется.'))
    app.state.broadcasts = source
    operation_id = '12345678-1234-4123-8123-123456789012'
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        assert client.post('/api/founder/broadcast', json={
            'operation_id': operation_id, 'text': '   ',
            'recipient_ids': ['A' * 32]}).status_code == 422
        conflict = client.post('/api/founder/broadcast', json={
            'operation_id': operation_id, 'text': 'Текст',
            'recipient_ids': ['A' * 32]})
    assert conflict.status_code == 409
    assert conflict.json() == {'detail': 'Другая рассылка уже выполняется.'}


def test_founder_expense_detail_counts_monthly_salaries_as_salary(tmp_path, monkeypatch):
    """Оклады частями — строки «Зарплаты» журнала: в разбивке они «зарплата», а не «прочие»."""
    # Формула расходов на архивных сентябрьских данных; граница 13.10 — в
    # test_founder_api_has_no_dashboard_expenses_before_accounting_start.
    from retro.modules.founder import routes
    monkeypatch.setattr(routes, 'ACCOUNTING_START', date(2026, 9, 1))
    source = FounderIiko({'pnl': {'net_profit': '5000'}, 'scope_note': ''})
    app = create_app(
        Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
        accountant_db_path=tmp_path / 'accountant.sqlite3')
    app.state.iiko = source
    app.state.accountant_finance.add_opening(date(2026, 9, 1), '10000', 'Остаток')
    app.state.accountant_finance.add_expense(date(2026, 9, 2), 'salary_monthly', 'Оклад', '1500')
    app.state.accountant_finance.add_expense(date(2026, 9, 2), 'admin_it', 'Сервис', '200')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        data = client.get('/api/founder/analytics', params={
            'start': '2026-09-01', 'end': '2026-09-08'}).json()
    assert data['dashboard_expenses'] == {
        'cashier': '0', 'accountant_other': '200', 'accountant_salary': '1500',
        'accountant': '1700', 'total': '1700'}


def test_founder_broadcast_history_routes_pass_filters_and_author(monkeypatch):
    """Журнал рассылок (ТЗ 16.10, п. 5.3): вид, страница, фильтр получателей, досылка от имени вошедшего."""
    from types import SimpleNamespace
    app = create_app(Settings())
    source = FounderBroadcasts()
    app.state.broadcasts = source
    monkeypatch.setattr(app.state.accounts, 'get', lambda login: SimpleNamespace(label='Карина'))
    job_id = 'job_restart_1234567890'
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        assert client.get('/api/founder/broadcast/jobs').json()['supported'] is True
        assert client.get('/api/founder/broadcast/jobs', params={
            'kind': 'auto', 'cursor': 'eyJhIjoiYiJ9'}).status_code == 200
        # Статус — один маршрут для формы и журнала: по id задачи бота (T-505).
        assert client.get('/api/founder/broadcast/' + job_id).json()['status'] == 'completed'
        assert client.get(f'/api/founder/broadcast/jobs/{job_id}/deliveries', params={
            'status': 'queued', 'offset': 100}).status_code == 200
        assert client.get(f'/api/founder/broadcast/jobs/{job_id}/deliveries').status_code == 200
        resumed = client.post(f'/api/founder/broadcast/jobs/{job_id}/resume')
        client.post('/api/founder/broadcast', json={
            'operation_id': '12345678-1234-4123-8123-123456789012', 'text': 'Плов',
            'recipient_ids': ['A' * 32]})
        # Плохие параметры до бота не доходят.
        assert client.get('/api/founder/broadcast/jobs', params={'kind': 'sms'}).status_code == 422
        assert client.get('/api/founder/broadcast/jobs', params={'cursor': 'a b'}).status_code == 422
        assert client.get('/api/founder/broadcast/jobs/short/deliveries').status_code == 422
        assert client.get(f'/api/founder/broadcast/jobs/{job_id}/deliveries',
                          params={'status': 'read'}).status_code == 422
        assert client.post('/api/founder/broadcast/jobs/short/resume').status_code == 422
    assert resumed.json() == {'job': {'id': job_id, 'status': 'queued'}, 'started': True}
    assert source.calls == [
        ('list', 'all', None), ('list', 'auto', 'eyJhIjoiYiJ9'), ('status', job_id),
        ('deliveries', job_id, ('pending', 'sending'), 100), ('deliveries', job_id, None, 0),
        ('resume', job_id, 'Карина'),
        ('12345678-1234-4123-8123-123456789012', 'Плов', ['A' * 32], 'Карина'),
    ]


def test_founder_broadcast_history_maps_bot_errors():
    from retro.integrations.broadcasts import BroadcastNotFound
    app = create_app(Settings())
    job_id = 'job_restart_1234567890'
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        app.state.broadcasts = FounderBroadcasts(error=BroadcastConflict('Другая рассылка уже выполняется.'))
        conflict = client.post(f'/api/founder/broadcast/jobs/{job_id}/resume')
        app.state.broadcasts = FounderBroadcasts(error=BroadcastNotFound('Рассылка не найдена.'))
        missing = client.get(f'/api/founder/broadcast/jobs/{job_id}/deliveries')
        app.state.broadcasts = FounderBroadcasts(error=DataError('Рассылка через booking-бот ещё не настроена.'))
        unconfigured = client.get('/api/founder/broadcast/jobs')
    assert (conflict.status_code, conflict.json()) == (409, {'detail': 'Другая рассылка уже выполняется.'})
    assert missing.status_code == 404
    assert (unconfigured.status_code, unconfigured.json()) == (
        503, {'detail': 'Рассылка через booking-бот ещё не настроена.'})


def test_founder_broadcast_history_against_previous_bot_keeps_the_form_working():
    """Бот без журнала: история «не поддерживается», запуск рассылки работает как раньше."""
    import httpx
    posts = []

    def handler(request):
        if request.method == 'GET' and request.url.path == '/admin/broadcast/jobs':
            return httpx.Response(405, json={'error': 'method_not_allowed'})
        posts.append(request)
        return httpx.Response(202, json={'id': '12345678-1234-4123-8123-123456789012', 'status': 'queued',
                                         'audience': 1, 'sent': 0, 'blocked': 0, 'failed': 0})

    settings = Settings(booking_api_url='https://booking.example.test', booking_api_token='read-secret',
                        booking_broadcast_token='write-secret')
    app = create_app(settings, broadcast_transport=httpx.MockTransport(handler))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        history = client.get('/api/founder/broadcast/jobs')
        started = client.post('/api/founder/broadcast', json={
            'operation_id': '12345678-1234-4123-8123-123456789012', 'text': 'Плов',
            'recipient_ids': ['A' * 32]})
    assert history.json() == {'supported': False, 'jobs': [], 'total': 0, 'next_cursor': None}
    assert started.status_code == 202 and started.json()['pending'] == 1
    assert len(posts) == 1


def test_founder_broadcast_history_fills_old_bot_jobs_from_panel_journal(tmp_path):
    """Задания, которые бот запомнил до своего журнала, без текста и автора:
    если они ушли через панель, подпись берётся из журнала панели (T-505)."""
    bare = {'id': 'job_' + 'x' * 20, 'status': 'completed', 'audience': 3, 'sent': 3, 'blocked': 0,
            'failed': 0, 'pending': 0, 'source': None, 'manual': None, 'author': None, 'text': None,
            'recipients_known': False, 'resumable': False, 'resume_count': 0}
    other = dict(bare, id='job_' + 'y' * 20)

    class Bot(FounderBroadcasts):
        async def list(self, kind='all', cursor=None, limit=20):
            return {'supported': True, 'jobs': [dict(bare), dict(other)], 'total': 2, 'next_cursor': None}

    app = create_app(Settings(data_dir=tmp_path))
    app.state.broadcasts = Bot()
    app.state.broadcast_journal.record('12345678-1234-4123-8123-123456789012', bare, text='Плов в субботу',
                                       recipients=3, sent_by='Карина', at='2026-10-17T14:05:00+05:00')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        first, second = client.get('/api/founder/broadcast/jobs').json()['jobs']
        resumed = client.post('/api/founder/broadcast/jobs/' + bare['id'] + '/resume')
        [entry] = app.state.broadcast_journal.list()
    assert (first['source'], first['manual'], first['text'], first['author']) == (
        'dashboard', True, 'Плов в субботу', 'Карина')
    # Не из панели — остаётся как есть: «Источник не записан».
    assert (second['source'], second['text'], second['author']) == (None, None, None)
    # Досылка освежает итог в журнале панели.
    assert resumed.status_code == 200 and entry['status'] == 'queued'
