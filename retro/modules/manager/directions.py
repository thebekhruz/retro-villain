"""Направления менеджеров и учётная запись менеджера.

Модуль-лист: его читает config.py при разборе окружения, поэтому здесь
только константы и разбор строк — без импорта настроек и базы.

Направление — то, за что отвечает менеджер (ТЗ 09.10, М-01): кухня,
уборка, зал. В реестре бухгалтера людей делят группы («Обслуживание зала»,
«Встреча гостей», «Бар»…), поэтому направление — набор групп. Группы те же,
что в roster.group_for: новый сотрудник встаёт в ту группу, где его ждут
«Сотрудники» и «Зарплата · день».
"""

from dataclasses import dataclass

DIRECTIONS: dict[str, tuple[str, ...]] = {
    'Кухня': ('Кухня',),
    'Зал': ('Обслуживание зала', 'Встреча гостей', 'Бар', 'Присмотр за детьми'),
    'Уборка': ('Уборка',),
}

# Подсказки в поле «Должность»: к ним добавляются должности, которые уже
# есть в реестре у групп направления.
DIRECTION_ROLES: dict[str, tuple[str, ...]] = {
    'Кухня': ('Повар', 'Кондитер'),
    'Зал': ('Официант', 'Ранер', 'Хостес', 'Бармен', 'Няня'),
    'Уборка': ('Техперсонал',),
}

EMPLOYMENT_TYPES = {'shift': 'Сменный', 'temporary': 'Временный'}


@dataclass(frozen=True)
class ManagerAccount:
    """Кто работает в кабинете: логин, роль панели и его направления.

    Точка расширения для входа по телефону (T-433): номер находит логин, а
    роль и направления берутся отсюда же — кабинет об этом не знает."""
    login: str
    role: str
    directions: tuple[str, ...]

    def owns(self, direction: str) -> bool:
        return direction in self.directions


def parse_manager_directions(value: str, panel_users: dict[str, tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    """DASHBOARD_MANAGER_DIRECTIONS: «логин=Кухня,Уборка;логин2=Зал».

    Логин должен быть менеджером из DASHBOARD_PANEL_USERS. Менеджер без
    строки здесь получает все направления (один менеджер на весь ресторан)."""
    if not value.strip():
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for raw_entry in value.split(';'):
        if not raw_entry.strip():
            continue
        if raw_entry.count('=') != 1:
            raise ValueError('DASHBOARD_MANAGER_DIRECTIONS должен содержать пары «логин=Направление,Направление».')
        login, raw_directions = (part.strip() for part in raw_entry.split('='))
        directions = tuple(item.strip() for item in raw_directions.split(',') if item.strip())
        user = panel_users.get(login)
        if user is None or user[1] != 'manager':
            raise ValueError(f'DASHBOARD_MANAGER_DIRECTIONS: «{login}» нет среди менеджеров DASHBOARD_PANEL_USERS.')
        unknown = [item for item in directions if item not in DIRECTIONS]
        if not directions or unknown or len(set(directions)) != len(directions) or login in result:
            raise ValueError('DASHBOARD_MANAGER_DIRECTIONS: направления — ' + ', '.join(DIRECTIONS) + '.')
        result[login] = directions
    return result


def group_in_direction(direction: str, role_group: str | None) -> str | None:
    """Группа нового сотрудника: по должности, если она из этого направления;
    незнакомая должность («Тандырщик») — первая группа направления. None —
    должность из чужого направления (официант у менеджера кухни)."""
    groups = DIRECTIONS[direction]
    if role_group is None:
        return groups[0]
    return role_group if role_group in groups else None


def direction_of_group(group: str | None) -> str | None:
    for name, groups in DIRECTIONS.items():
        if group in groups:
            return name
    return None
