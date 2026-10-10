"""Отправка сотрудника и его лица в Hikvision из кабинета менеджера
(ТЗ 09.10, М-02 п. 5, М-03).

Порядок такой, чтобы повтор никогда не заводил второго человека:
1. номер для устройства выдаётся и записывается в карточку ДО отправки
   (roster.reserve_employee_no). Если человек с этим именем уже есть на
   устройстве и ни к кому не привязан — берём его номер;
2. сначала ищем этот номер на устройстве: есть и имя наше — отправка уже
   дошла (ответ потерялся при обрыве связи), второй раз не создаём;
   под номером чужой человек — выдаём новый номер выше занятых;
3. создаём и проверяем поиском: успех — только когда устройство показало
   человека под нашим номером. Тогда номер становится привязкой, и проходы
   по нему сами попадают в карточку;
4. есть фото и его лицо ещё не на устройстве — отправляем лицо под тем же
   номером (FPID = employeeNo). Сбой лица не отменяет человека: он
   «Добавлен», лицо — «ошибка» со своей причиной.
Любой сбой оставляет карточку с причиной; «Отправить ещё раз» идёт по тому
же пути: уже добавленного человека не создаёт, а дошлёт только лицо.
"""

import asyncio

from retro.integrations.hikvision import HikvisionError
from retro.logging_config import log_safe_failure
from retro.modules.accountant.names import device_name_matches
from retro.modules.accountant.roster import DeviceNamesakes
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
        # Без фото не отправляем; ручную отметку турникет не видит.
        if card is None or card['manual_attendance'] or not card['photo_updated_at']:
            return
        client = state.hikvision_writer
        # Человек уже на устройстве — повтор досылает только лицо.
        if not card['hikvision_id']:
            if client is None:
                await asyncio.to_thread(roster.record_hikvision_attempt, employee_id,
                                        'pending', 'not_configured')
                return
            if not await _push_person(state, client, card, by=by, request_id=request_id):
                return
            card = await asyncio.to_thread(roster.manager_card, employee_id)
        if card is None or not card['photo_updated_at'] or card['face_state'] == 'sent':
            return
        if client is None:
            await asyncio.to_thread(roster.record_face, employee_id, 'pending', 'not_configured',
                                    version=card['photo_updated_at'])
            return
        await _push_face(roster, client, employee_id, card['hikvision_id'], by=by, request_id=request_id)


async def _push_person(state, client, card: dict, *, by: str | None, request_id: str) -> bool:
    """Человек на устройство. True — устройство подтвердило, номер привязан."""
    roster, employee_id = state.accountant_roster, card['id']
    number, name = card['hikvision_employee_no'], card['name']
    try:
        if not number:
            people = await client.fetch_people()
            number = await asyncio.to_thread(roster.reserve_employee_no, employee_id, people)
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
        return linked
    except HikvisionError as error:
        await asyncio.to_thread(roster.record_hikvision_attempt, employee_id, 'error', error.code)
    except DeviceNamesakes:
        await asyncio.to_thread(roster.record_hikvision_attempt, employee_id, 'error', 'namesakes')
    except Exception as error:
        log_safe_failure('manager-cabinet', error, operation='push-hikvision', request_id=request_id)
        await asyncio.to_thread(roster.record_hikvision_attempt, employee_id, 'error', 'internal')
    return False


async def _push_face(roster, client, employee_id: int, number: str, *, by: str | None,
                     request_id: str) -> None:
    """Лицо под номером человека. Итог пишется только для того снимка, что
    ушёл: новое фото, загруженное во время отправки, по-прежнему ждёт."""
    photo = await asyncio.to_thread(roster.photo, employee_id)
    if photo is None:
        return
    content, mime, version = photo
    try:
        await client.upload_face(number, content, mime=mime)
    except HikvisionError as error:
        await asyncio.to_thread(roster.record_face, employee_id, 'error', error.code, version=version)
        return
    except Exception as error:
        log_safe_failure('manager-cabinet', error, operation='push-hikvision-face', request_id=request_id)
        await asyncio.to_thread(roster.record_face, employee_id, 'error', 'internal', version=version)
        return
    await asyncio.to_thread(roster.record_face, employee_id, 'sent', None, version=version, by=by)
