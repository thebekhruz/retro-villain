"""Единый алфавит данных (ТЗ 09.10, М-04): имена, должности и направления —
кириллицей.

Раньше в реестр попадало что угодно: «Karimov Jahongir» из Hikvision,
«Карамат» с латинской «а» внутри, набранной на телефоне с английской
раскладкой. Внешне такие имена не отличить, а для поиска, сверки с
Hikvision и ведомости это разные люди — так и появляются две карточки на
одного человека.

Здесь три вещи:
* проверка нового ввода: только кириллица (с узбекскими ў қ ғ ҳ и ё),
  пробел и дефис; латинская буква — отказ с названием буквы;
* ключ поиска: латиница находит кириллицу (Alijon → Алижон), как в поиске
  «Сотрудников» (employees-logic.js, latinKey) — правила одни и те же;
* ключ сверки: возможные совпадения перед созданием карточки. Совпадение —
  только подсказка: тёзок не объединяем, решает человек.

Старые имена в базе не проверяются и не переписываются: проверяется только
то, что вводят сейчас.
"""

import re

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


# ── Поиск и сверка ──────────────────────────────────────────────────────────
# Тот же ключ, что latinKey в employees-logic.js: кириллица переводится в
# латиницу по узбекским правилам, а варианты записи сводятся к одному
# (kh = x, zh = j, ye = e). Тогда «Ixtiyor», «Ихтиёр» и «Ихтиер» сходятся.

_CYR = {'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ж': 'j', 'з': 'z', 'и': 'i',
        'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's',
        'т': 't', 'у': 'u', 'ф': 'f', 'х': 'x', 'ц': 's', 'ч': 'ch', 'ш': 'sh', 'щ': 'sh', 'ъ': '',
        'ы': 'i', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya', 'ў': 'o', 'қ': 'q', 'ғ': 'g', 'ҳ': 'x'}
_APOSTROPHES = re.compile(r"[ʻʼ’‘'`]")
_LONE_H = re.compile(r'(^|[^sc])h')


def plain(value) -> str:
    return collapse_spaces(value).lower().replace('ё', 'е')


def latin_key(value) -> str:
    text = collapse_spaces(value).lower().replace('ё', 'yo')
    text = ''.join(_CYR.get(char, char) for char in text)
    text = _APOSTROPHES.sub('', text)
    text = text.replace('kh', 'x').replace('zh', 'j').replace('ts', 's')
    text = _LONE_H.sub(r'\1x', text)
    return text.replace('ye', 'e').replace('w', 'v')


def matches_query(query, *fields) -> bool:
    """Поиск как в «Сотрудниках»: без регистра, ё = е, латиница находит кириллицу."""
    wanted = plain(query)
    if not wanted:
        return True
    wanted_latin = latin_key(wanted)
    return any(wanted in plain(field) or (wanted_latin and wanted_latin in latin_key(field))
               for field in fields)


def _unmixed(word: str) -> str:
    """Слово с кириллицей, в которое затесались латинские двойники, — целиком
    кириллицей: «Kарамат» и «Карамат» — одно имя."""
    if any(char in CYRILLIC_LETTERS for char in word):
        return ''.join(HOMOGLYPHS.get(char, char) for char in word)
    return word


_O_APOSTROPHE = re.compile(r"[oO][ʻʼ’‘'`]")


def _match_key(word: str) -> str:
    # Сверка грубее поиска: так одно имя пишут по-разному. «Ихтиёр» =
    # «Ихтиер» (ё = е, yo = e), «Қодиров» = «Кодиров» (қ = к), «Ўткир» =
    # «Уткир» = «O'tkir» (по-русски ў пишут как у).
    word = _O_APOSTROPHE.sub('u', word.replace('ў', 'у').replace('Ў', 'У'))
    return latin_key(word).replace('yo', 'e').replace('q', 'k')


def match_words(value) -> frozenset[str]:
    words = (_unmixed(word) for word in collapse_spaces(value).split())
    return frozenset(filter(None, (_match_key(word) for word in words)))


def similar_names(first, second) -> bool:
    """Возможно, это один человек: слова одного имени целиком есть в другом,
    порядок не важен («Каримов Жахонгир» ~ «Jahongir Karimov» ~ «Каримов
    Жахонгир Алишерович»). Это повод спросить, а не объединить."""
    left, right = match_words(first), match_words(second)
    if not left or not right:
        return False
    return left <= right or right <= left


def device_name_matches(device_name, our_name) -> bool:
    """Под номером на устройстве — наш человек? Имя сравниваем без регистра
    и лишних пробелов; устройство может обрезать длинное имя — тогда его
    начало. Имени устройство не вернуло — сравнивать не с чем, считаем нашим:
    номер выдан нами и выше всех известных."""
    device, ours = plain(device_name), plain(our_name)
    if not device:
        return True
    return device == ours or (len(device) >= 8 and ours.startswith(device))
