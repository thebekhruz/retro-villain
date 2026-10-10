"""Public accounting dates. Internal historical records remain available for audit."""
import asyncio
from datetime import date, timedelta

from fastapi import HTTPException, Request
from pydantic import TypeAdapter, ValidationError
from retro.accounting_period import ACCOUNTING_START

DATE_FIELDS = {'date', 'day', 'start', 'end', 'first', 'last', 'paid_day',
               'handover_date', 'received_date', 'cashier_date', 'work_day'}
DATE_ADAPTER = TypeAdapter(date)
MESSAGE = f'Учёт доступен с {ACCOUNTING_START:%d.%m.%Y}. Более ранние даты недоступны.'


def check_dates(values):
    for key, value in values:
        if key == 'month' and isinstance(value, str):
            try:
                selected = date.fromisoformat(value + '-01')
            except ValueError:
                continue  # The endpoint handles malformed input.
            if selected < ACCOUNTING_START.replace(day=1):
                raise HTTPException(422, MESSAGE)
        elif key in DATE_FIELDS:
            try:
                selected = DATE_ADAPTER.validate_python(value)
            except ValidationError:
                continue
            # A first-day receipt may refer to the preceding cashier shift.
            if key == 'cashier_date':
                minimum = ACCOUNTING_START - timedelta(days=1)
            else:
                minimum = ACCOUNTING_START
            if selected < minimum:
                raise HTTPException(422, MESSAGE)


async def require_accounting_dates(request: Request):
    if not request.url.path.startswith('/api/'):
        return
    check_dates(request.query_params.multi_items())
    check_dates(request.path_params.items())
    if request.url.path.startswith('/api/shokh/'):
        for field, getter in (('trip_id', request.app.state.shokh.trip),
                              ('purchase_id', request.app.state.shokh.purchase)):
            raw = request.path_params.get(field)
            if raw is not None:
                try:
                    identifier = int(raw)
                except ValueError:
                    continue
                record = await asyncio.to_thread(getter, identifier)
                if record:
                    check_dates([('day', record['day'])])
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and 'application/json' in request.headers.get('content-type', ''):
        try:
            body = await request.json()
        except ValueError:
            return
        if isinstance(body, dict):
            check_dates(body.items())
    if request.method in {'POST', 'PUT', 'PATCH'} and any(kind in request.headers.get('content-type', '') for kind in ('multipart/form-data', 'application/x-www-form-urlencoded')):
        form = await request.form()
        check_dates(form.multi_items())
