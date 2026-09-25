"""Панель открывают с телефона в зале, а не только с ноутбука бухгалтера."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'retro' / 'static'
# Страница входа в этот список не входит: она открывается до входа, и всё,
# что она грузит, должно быть в списке публичных путей приложения. Своей
# вёрстки у неё одна карточка по центру, правила рабочей области ей не нужны.
PAGES = ['index.html', 'accountant.html', 'employees.html', 'payroll.html',
         'director.html', 'founder.html']


def stylesheets(page: str) -> list[str]:
    markup = (STATIC / page).read_text(encoding='utf-8')
    return re.findall(r'<link rel="stylesheet" href="/static/([^"]+)"', markup)


def test_every_page_loads_the_mobile_sheet_last():
    # Лист телефона перекрывает частные правила модулей, поэтому обязан идти
    # последним: подключённый раньше, он проигрывает по порядку каскада.
    for page in PAGES:
        sheets = stylesheets(page)
        assert 'mobile.css' in sheets, page
        assert sheets[-1] == 'mobile.css', (page, sheets)


def test_tap_targets_are_not_smaller_than_a_finger():
    rules = (STATIC / 'mobile.css').read_text(encoding='utf-8')
    for selector in ('.logout-button', '.icon-button', 'input[type=date]'):
        assert selector in rules, selector
    assert 'min-height:44px' in rules


def test_rows_of_chips_scroll_instead_of_stretching_the_page():
    # Семь быстрых дней в строку растягивали кассу до 668 точек при экране
    # в 390: страница уезжала вбок целиком.
    rules = (STATIC / 'mobile.css').read_text(encoding='utf-8')
    assert 'overflow-x:auto' in rules
    assert '.quick-days' in rules


def test_login_page_loads_only_publicly_allowed_files():
    """До входа доступны только перечисленные в приложении пути. Лишняя
    ссылка на закрытый файл даёт 401 прямо на форме входа."""
    import re
    app = (Path(__file__).resolve().parent.parent / 'retro' / 'app.py').read_text(encoding='utf-8')
    block = re.search(r"PUBLIC_PATHS = \{([^}]*)\}", app, re.S).group(1)
    allowed = set(re.findall(r"'([^']+)'", block))
    markup = (STATIC / 'login.html').read_text(encoding='utf-8')
    for asset in re.findall(r'(?:href|src)="(/static/[^"]+)"', markup):
        assert asset in allowed, asset
