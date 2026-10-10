"""Тёзки при добавлении сотрудника у бухгалтера (ТЗ 09.10, М-04).

Карточки заводит только бухгалтер (решение 10.10): перед добавлением она
видит возможных тёзок в реестре и решает сама, тот же это человек или
другой. Сами карточки не объединяются. Гоняется на SQLite и Postgres.
"""
import pytest

from test_accountant_design_parity import any_db  # noqa: F401 — фикстура

from retro.modules.accountant.names import similar_names

URL = '/api/accountant/employees'
MONTHLY = '/api/accountant/monthly-employees'


def shift(c, name, role='хостес', group='Встреча гостей', **extra):
    return c.post(URL, json=dict(name=name, role=role, rate='150000', group=group, **extra))


@pytest.mark.parametrize('first, second, same', [
    ('Баходиров Ихтиёр', 'Баходиров Ихтиер', True),          # ё = е
    ('Jahongir Karimov', 'Каримов Жахонгир', True),          # латиница, другой порядок
    ('Kарамат', 'Карамат', True),                            # латинская K внутри кириллицы
    ('Карамат', 'Каримова Карамат', True),                   # одно имя целиком в другом
    ('Қодиров Ўткир', "Kodirov O'tkir", True),               # узбекские буквы
    ('Каримов Жахонгир', 'Каримов Бобур', False),            # однофамильцы — не тёзки
    ('Карамат', 'Каримов Жахонгир', False),
])
def test_similar_names(first, second, same):
    assert similar_names(first, second) is same


def test_namesake_is_asked_and_nothing_is_written_until_confirmed(any_db):  # noqa: F811
    c = any_db
    first = shift(c, 'Каримова Карамат', employment_type='temporary', work_from='2026-10-08', work_to='2026-10-10')
    assert first.status_code == 201, first.text
    before = len(c.app.state.accountant_roster.list())

    # Тот же человек, набранный иначе: вопрос, а не новая карточка.
    asked = shift(c, 'Карамат Каримова')
    assert asked.status_code == 409, asked.text
    body = asked.json()
    assert body['detail'] == 'Похожие уже есть в реестре. Это тот же человек?'
    assert [(m['kind'], m['name'], m['role'], m['group'], m['temporary'], m['work_period'])
            for m in body['matches']] == [('shift', 'Каримова Карамат', 'хостес', 'Встреча гостей', True, '08.10–10.10')]
    assert len(c.app.state.accountant_roster.list()) == before

    # «Это другой человек» — добавляем.
    confirmed = shift(c, 'Карамат Каримова', confirm_new=True)
    assert confirmed.status_code == 201, confirmed.text
    assert len(c.app.state.accountant_roster.list()) == before + 1


def test_monthly_and_shift_lists_see_each_other(any_db):  # noqa: F811
    c = any_db
    monthly = c.post(MONTHLY, json=dict(name='Баходиров Ихтиер', role='Менеджер', salary='5000000'))
    assert monthly.status_code == 201, monthly.text
    # Сменного с именем окладника — тоже спрашиваем.
    asked = shift(c, 'Баходиров Ихтиёр', role='менеджер', group='Управление')
    assert asked.status_code == 409
    assert [(m['kind'], m['group']) for m in asked.json()['matches']] == [('monthly', 'На окладе')]
    # И наоборот: окладника с именем сменного.
    assert shift(c, 'Абдулганиева Сельвина').status_code == 201
    asked = c.post(MONTHLY, json=dict(name='Сельвина Абдулганиева', role='Хостес', salary='4000000'))
    assert asked.status_code == 409
    assert [m['kind'] for m in asked.json()['matches']] == ['shift']
    assert c.post(MONTHLY, json=dict(name='Сельвина Абдулганиева', role='Хостес', salary='4000000',
                                     confirm_new=True)).status_code == 201


def test_no_question_without_namesakes_and_latin_is_still_refused(any_db):  # noqa: F811
    c = any_db
    assert shift(c, 'Каримов Жахонгир', role='менеджер', group='Управление').status_code == 201
    # Однофамилец — не тёзка: добавляется сразу.
    assert shift(c, 'Каримов Бобур', role='официант', group='Обслуживание зала').status_code == 201
    # Латиница — отказ по кириллице, а не вопрос о тёзке.
    latin = shift(c, 'Karimov Jahongir', role='менеджер', group='Управление')
    assert latin.status_code == 422
    assert 'латиниц' in latin.json()['detail']
