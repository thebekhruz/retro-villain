"""ПК-отчёт директора без дублей (ТЗ 16.10, п. 3.3, T-501): одна выручка,
«Как считается» вместо подписи-перечня, одна заметка к таблице."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'retro' / 'static'
MARKUP = (STATIC / 'director.html').read_text(encoding='utf-8')
SCRIPT = (STATIC / 'director.js').read_text(encoding='utf-8')


def test_every_element_the_script_fills_is_on_the_page():
    """Удалённая плитка, на которую скрипт ещё пишет, роняет всю отрисовку
    отчёта: `$('…')` вернёт null на первой же цифре."""
    used = set(re.findall(r"\$\('([\w-]+)'\)", SCRIPT))
    skeleton = re.search(r"\[([^\]]+)\]\s*\.forEach\(id => \$\(id\)\.replaceChildren\(skel\(\)\)\)", SCRIPT)
    dashes = re.search(r"dashSkeletons\(\[([^\]]+)\]\)", SCRIPT)
    used |= set(re.findall(r"'([\w-]+)'", skeleton.group(1) + dashes.group(1)))
    assert {'revenue', 'summary-profit', 'cost-total'} <= used
    missing = sorted(name for name in used if f'id="{name}"' not in MARKUP)
    assert missing == []


def test_one_revenue_on_the_page():
    """Выручка — одна, по меню. Продажи до исключений, «вне банкетной
    выборки» и Яндекс — только в «Как считается», без второй «Выручки»."""
    assert 'id="summary-revenue"' not in MARKUP and 'id="cash-note"' not in MARKUP
    assert "$('revenue').textContent = money.format(Math.round(all.revenue))" in SCRIPT
    assert 'snapshot.cash_total' not in SCRIPT.split('function renderRevenueNotes')[0]
    assert '<p>Выручка</p>' not in MARKUP
    hint = re.search(r'<details id="revenue-hint" class="calc-hint" hidden><summary>Как считается</summary>'
                     r'<div id="revenue-notes"></div></details>', MARKUP)
    assert hint is not None


def test_margin_card_has_no_second_profit_and_names_the_formula():
    assert 'Валовая прибыль' not in MARKUP and 'gross-profit' not in MARKUP
    assert 'Прибыль итоговая к выручке, после расхода без выручки' in MARKUP


def test_profit_tiles_carry_numbers_and_the_table_carries_one_note():
    tiles = re.search(r'<section class="profit-summary".*?</section>', MARKUP, re.S).group(0)
    assert '<small>' not in tiles
    assert re.findall(r'<p>([^<]+)</p>', tiles) == ['Прибыль продаж', 'Расход без выручки', 'Прибыль итоговая']
    menu = re.search(r'<section class="panel menu-panel".*?</section>', MARKUP, re.S).group(0)
    notes = re.findall(r'<p class="panel-note[^"]*">([^<]+)</p>', menu)
    assert len(notes) == 1
    assert 'Прибыль продаж: выручка минус себестоимость проданного.' in notes[0]
    assert 'Цена: средняя цена продажи после скидок.' in notes[0]
    waiters = re.search(r'<details class="fold">.*?</details>', MARKUP, re.S).group(0)
    assert re.findall(r'<p class="panel-note">([^<]+)</p>', waiters) == [
        'Выручка засчитывается официанту, открывшему заказ.']


def test_table_footer_repeats_revenue_only_for_a_narrowed_list():
    """Без поиска и цены сумма под таблицей равна плитке над ней."""
    assert "(filtered ? ' · выручка ' + sums(shownRevenue) : '')" in SCRIPT
    assert 'const filtered = Boolean(view.query.trim() || view.price);' in SCRIPT


def test_school_is_named_like_the_rest_of_the_panel_and_decor_is_trimmed():
    visible = re.sub(r'<!--.*?-->', '', MARKUP, flags=re.S)
    assert 'Oxbridge' not in visible
    assert visible.count('Retro в школе') == 2
    assert 'Меню под контролем' not in visible and 'Источник: iikoWeb' not in visible
    # Кикер и номер модуля — общий дизайн панелей, остаются.
    assert '<p class="eyebrow">РЕШЕНИЯ ПО РЕСТОРАНУ</p>' in visible
    assert '<div class="module-badge"><span>03</span>' in visible
