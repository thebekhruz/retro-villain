"""Отправка карточки менеджера в Hikvision (ТЗ 09.10, М-02 п. 5, М-03).

Порядок такой, чтобы повтор никогда не заводил второго человека:
1. номер для устройства уже выдан и записан в карточку (roster.manager_create);
2. сначала ищем этот номер на устройстве: есть и имя наше — отправка уже
   дошла (ответ потерялся при обрыве связи), второй раз не создаём;
   под номером чужой человек — выдаём новый номер выше занятых;
3. создаём и проверяем поиском: успех — только когда устройство показало
   человека под нашим номером. Тогда номер становится привязкой, и проходы
   по нему сами попадают в карточку.
Любой сбой оставляет карточку с причиной; «Отправить ещё раз» идёт по тому
же пути.
"""

import asyncio

from retro.integrations.hikvision import HikvisionError
from retro.logging_config import log_safe_failure
from retro.modules.accountant.names import device_name_matches
from retro.modules.cashier.service import today_tashkent


async def push_to_hikvision(state, employee_id: int, *, by: str | None = None,
                            request_id: str = '-') -> None:
    locks = state.manager_push_locks
    # Две отправки одной карточки (двойной тап, две вкладки) идут по очереди:
    # вторая увидит итог первой и ничего не создаст.
    lock = locks.setdefault(employee_id, asyncio.Lock())
    async with lock:
        roster = state.accountant_roster
        card = await asyncio.to_thread(roster.manager_card, employee_id)
        if card is None or card['hikvision_id'] or not card['hikvision_employee_no']:
            return
        client = state.hikvision_writer
        if client is None:
            await asyncio.to_thread(roster.record_hikvision_attempt, employee_id,
                                    'pending', 'not_configured')
            return
        number, name = card['hikvision_employee_no'], card['name']
        try:
            person = await client.find_person(number)
            if person is not None and not device_name_matches(person.name, name):
                people = await client.fetch_people()
                number = await asyncio.to_thread(
                    roster.reassign_employee_no, employee_id,
                    {item.employee_no for item in people} | {number}, by=by)
                person = None
            if person is None:
                await client.create_person(number, name, valid_from=today_tashkent())
                person = await client.find_person(number)
                if person is None or not device_name_matches(person.name, name):
                    raise HikvisionError('not_confirmed')
            linked = await asyncio.to_thread(roster.confirm_hikvision_number, employee_id, number, by=by)
            if linked:
                # Проходы, пришедшие до подтверждения, сразу становятся его входами.
                await asyncio.to_thread(state.attendance_store.reconcile_links, {number: employee_id})
        except HikvisionError as error:
            await asyncio.to_thread(roster.record_hikvision_attempt, employee_id, 'error', error.code)
        except Exception as error:
            log_safe_failure('manager-cabinet', error, operation='push-hikvision', request_id=request_id)
            await asyncio.to_thread(roster.record_hikvision_attempt, employee_id, 'error', 'internal')
