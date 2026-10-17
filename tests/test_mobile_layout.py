"""Панель открывают с телефона в зале, а не только с ноутбука бухгалтера."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'retro' / 'static'
# Страница входа в этот список не входит: она открывается до входа, и всё,
# что она грузит, должно быть в списке публичных путей приложения. Своей
# вёрстки у неё одна карточка по центру, правила рабочей области ей не нужны.
PAGES = ['index.html', 'accountant.html', 'employees.html', 'payroll.html', 'salary-day.html',
         'shoh-balance.html',
         'director.html', 'founder.html', 'shokh.html',
         'director-app.html', 'founder-cabinet.html']


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


def test_forecast_bars_are_measured_against_their_own_track():
    """17.10: все семь столбиков «Средней выручки» выходили одной высоты.
    Столбик стоял в колонке рядом с подписями, и flex ужимал все высокие до
    остатка места. Теперь процент считается от дорожки, которая забирает всё,
    что осталось от подписей, а сумма висит над столбиком, не отнимая высоты."""
    css = (STATIC / 'founder-cabinet.css').read_text(encoding='utf-8')
    script = (STATIC / 'founder-cabinet.js').read_text(encoding='utf-8')
    assert re.search(r'\.fo-fc-track\{[^}]*flex:1 1 0[^}]*min-height:0', css)
    assert re.search(r'\.fo-forecast i>span\{[^}]*position:absolute[^}]*bottom:100%', css)
    render = script[script.index('function renderForecast'):]
    render = render[:render.index('\n  }\n')]
    assert "node('div', 'fo-fc-track')" in render and 'track.append(bar)' in render
    assert 'bar.append(' in render  # сумма внутри столбика, а не соседом в колонке
