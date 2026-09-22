"""Шапка панели: выход на экран входа и переключение языка.

Кнопку выхода и переключатель языка легко потерять при правке одной
страницы из пяти — поэтому их наличие проверяем на всех сразу.
"""

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'retro' / 'static'
DASHBOARDS = ('index.html', 'accountant.html', 'employees.html',
              'director.html', 'founder.html')


def markup(page: str) -> str:
    return (STATIC / page).read_text(encoding='utf-8')


def test_every_dashboard_page_has_a_way_out_and_a_language_switch():
    for page in DASHBOARDS:
        text = markup(page)
        assert 'id="logout"' in text, page
        assert 'data-lang="ru"' in text and 'data-lang="uz"' in text, page
        assert '/static/i18n.js' in text, page
        # Словарь обязан грузиться раньше самого переключателя, иначе первый
        # показ узбекского уходит в пустой словарь.
        assert text.index('/static/i18n-uz.js') < text.index('/static/i18n.js'), page


def test_login_page_offers_the_language_switch_too():
    # Человек выбирает язык до входа, иначе экран входа остаётся русским.
    text = markup('login.html')
    assert 'data-lang="uz"' in text
    assert '/static/i18n.js' in text


def test_logout_leads_to_the_login_screen():
    assert "navigate('/login')" in (STATIC / 'logout.js').read_text(encoding='utf-8')


def test_the_panel_does_not_name_the_model_vendor_on_screen():
    for page in DASHBOARDS + ('login.html',):
        assert 'Claude' not in markup(page), page
    assert 'Claude' not in (STATIC / 'founder-chat.js').read_text(encoding='utf-8')
