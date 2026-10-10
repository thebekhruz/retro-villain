"""Номера телефонов для входа по SMS (ТЗ 09.10, М-05).

Модуль-лист: его читает config.py при разборе окружения, поэтому здесь
только разбор строк — без настроек и базы. Та же нормализация стоит на
экране входа (static/login-logic.js): «90 123 45 67», «(90) 123-45-67»,
«998901234567» и «+998 90 123 45 67» — один номер, +998901234567.
"""

import re

# Каким ролям открыт вход по номеру. Официант добавится сюда, когда появится
# его роль (следующий этап ТЗ); бухгалтер, кассир, директор и учредитель
# входят по логину и паролю, как раньше.
PHONE_ROLES = frozenset({'manager'})
LOCAL_DIGITS = 9  # после +998: код оператора и номер


def local_digits(raw) -> str | None:
    """Цифры номера после +998 или None, если номер не узбекский.

    Плюс или больше девяти цифр — значит, код страны набран, и он обязан быть
    998. Без плюса и до девяти цифр — местный номер без кода страны."""
    text = str(raw or '').strip()
    digits = re.sub(r'\D', '', text)
    if digits.startswith('00998'):
        digits = digits[2:]
    if text.startswith('+') or len(digits) > LOCAL_DIGITS:
        return digits[3:] if digits.startswith('998') else None
    return digits


def normalize_phone(raw) -> str | None:
    """«+998 (90) 123-45-67» → «+998901234567»; неполный или чужой номер — None."""
    digits = local_digits(raw)
    if digits is None or len(digits) != LOCAL_DIGITS:
        return None
    return '+998' + digits


def display_phone(phone: str) -> str:
    """+998901234567 → «+998 90 123 45 67» — так номер показывают на экране."""
    digits = phone[4:]
    return f'+998 {digits[:2]} {digits[2:5]} {digits[5:7]} {digits[7:9]}'


def parse_phone_users(value: str, panel_users: dict[str, tuple[str, str]]) -> dict[str, str]:
    """DASHBOARD_PHONE_USERS: «+998901234567=логин;+998 90 765 43 21=логин2».

    Номер привязывается к уже заведённой учётной записи DASHBOARD_PANEL_USERS
    с ролью, которой открыт вход по номеру. Один номер — одна учётная запись;
    у одной учётной записи может быть несколько номеров (рабочий и личный)."""
    if not value.strip():
        return {}
    result: dict[str, str] = {}
    for raw_entry in value.split(';'):
        if not raw_entry.strip():
            continue
        if raw_entry.count('=') != 1:
            raise ValueError('DASHBOARD_PHONE_USERS должен содержать пары «+998XXXXXXXXX=логин».')
        raw_phone, login = (part.strip() for part in raw_entry.split('='))
        phone = normalize_phone(raw_phone)
        if phone is None:
            raise ValueError(f'DASHBOARD_PHONE_USERS: «{raw_phone}» — не номер вида +998XXXXXXXXX.')
        if phone in result:
            raise ValueError(f'DASHBOARD_PHONE_USERS: номер {phone} указан дважды.')
        user = panel_users.get(login)
        if user is None or user[1] not in PHONE_ROLES:
            raise ValueError(f'DASHBOARD_PHONE_USERS: «{login}» нет среди менеджеров DASHBOARD_PANEL_USERS.')
        result[phone] = login
    return result
