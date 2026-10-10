"""Единый алфавит данных (ТЗ 09.10, М-04): имена, должности и направления —
кириллицей.

Раньше в реестр попадало что угодно: «Karimov Jahongir» из Hikvision,
«Карамат» с латинской «а» внутри, набранной на телефоне с английской
раскладкой. Внешне такие имена не отличить, а для поиска, сверки с
Hikvision и ведомости это разные люди — так и появляются две карточки на
одного человека.

Здесь две вещи:
* проверка нового ввода: только кириллица (с узбекскими ў қ ғ ҳ и ё),
  пробел и дефис; латинская буква — отказ с названием буквы;
* сверка имени с тем, что вернуло устройство Hikvision.

Старые имена в базе не проверяются и не переписываются: проверяется только
то, что вводят сейчас.
"""

CYRILLIC_LETTERS = frozenset(
    'абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ'
    'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯЎҚҒҲ')

# Латинские буквы, которые на экране не отличить от кириллических.
HOMOGLYPHS = {
    'a': 'а', 'e': 'е', 'o': 'о', 'p': 'р', 'c': 'с', 'x': 'х', 'y': 'у',
    'A': 'А', 'B': 'В', 'E': 'Е', 'K': 'К', 'M': 'М', 'H': 'Н', 'O': 'О',
    'P': 'Р', 'C': 'С', 'T': 'Т', 'X': 'Х',
}

# Что кроме букв можно в поле. В имени — только пробел и дефис; в должности
# бывают «Повар 2-го цеха» и «Официант (банкет)».
NAME_EXTRA = frozenset(' -')
ROLE_EXTRA = frozenset(' -.,()/№0123456789')

FIELDS = {
    # поле: (именительный, «набрано/набрана», предложный, что можно кроме букв)
    'name': ('Имя', 'набрано', 'имени', NAME_EXTRA, 'пробел и дефис'),
    'role': ('Должность', 'набрана', 'должности', ROLE_EXTRA, 'цифры, пробел, дефис и скобки'),
    'direction': ('Направление', 'набрано', 'направлении', NAME_EXTRA, 'пробел и дефис'),
}
LIMITS = {'name': 160, 'role': 80, 'direction': 80}
EMPTY = {'name': 'Укажите имя сотрудника.', 'role': 'Укажите должность.',
         'direction': 'Выберите направление.'}


def collapse_spaces(value) -> str:
    return ' '.join(str(value or '').split())


def is_latin_letter(char: str) -> bool:
    return char.isascii() and char.isalpha()


def cyrillic_text(value, field: str = 'name') -> str:
    """Проверить новый ввод и вернуть его с нормализованными пробелами.

    Ошибка — ValueError с текстом для человека: какая буква не та и чем её
    заменить. Сообщения те же, что в manager-logic.js (проверка на экране)."""
    nominative, typed, prepositional, extra, extra_text = FIELDS[field]
    text = collapse_spaces(value)
    if not text:
        raise ValueError(EMPTY[field])
    if len(text) > LIMITS[field]:
        raise ValueError(f'{nominative} длиннее {LIMITS[field]} знаков.')
    if not any(char in CYRILLIC_LETTERS for char in text):
        if any(is_latin_letter(char) for char in text):
            raise ValueError(f'{nominative} {typed} латиницей — наберите кириллицей.')
        raise ValueError(f'{nominative} без букв — наберите кириллицей.')
    for char in text:
        if char in CYRILLIC_LETTERS or char in extra:
            continue
        if is_latin_letter(char):
            twin = HOMOGLYPHS.get(char)
            if twin:
                raise ValueError(f'В {prepositional} «{text}» латинская «{char}» вместо '
                                 f'кириллической «{twin}» — наберите кириллицей.')
            raise ValueError(f'В {prepositional} «{text}» латинская буква «{char}» — '
                             f'наберите кириллицей.')
        if char.isalpha():
            raise ValueError(f'В {prepositional} «{text}» буква «{char}» не из русской '
                             f'или узбекской кириллицы.')
        raise ValueError(f'В {prepositional} «{text}» недопустимый знак «{char}»: можно '
                         f'буквы кириллицы, {extra_text}.')
    return text


def person_name(value) -> str:
    return cyrillic_text(value, 'name')


def role_name(value) -> str:
    return cyrillic_text(value, 'role')


# ── Сверка с устройством ────────────────────────────────────────────────────

def plain(value) -> str:
    return collapse_spaces(value).lower().replace('ё', 'е')


def device_name_matches(device_name, our_name) -> bool:
    """Под номером на устройстве — наш человек? Имя сравниваем без регистра
    и лишних пробелов; устройство может обрезать длинное имя — тогда его
    начало. Имени устройство не вернуло — сравнивать не с чем, считаем нашим:
    номер выдан нами и выше всех известных."""
    device, ours = plain(device_name), plain(our_name)
    if not device:
        return True
    return device == ours or (len(device) >= 8 and ours.startswith(device))
