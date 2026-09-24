"""Контрол периода: расчёт дат и проверка границ на стороне панели."""

import json
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'retro' / 'static'
PAGES = ('index.html', 'accountant.html', 'director.html')


def run_node(expression):
    script = ("const period=require('./retro/static/period.js');"
              f"console.log(JSON.stringify({expression}));")
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_presets_end_yesterday_and_never_include_the_open_day():
    assert run_node("period.presetRange('2026-09-24','10')") == {'start': '2026-09-14', 'end': '2026-09-23'}
    assert run_node("period.presetRange('2026-09-24','yesterday')") == {'start': '2026-09-23', 'end': '2026-09-23'}
    # Первое число: «этот месяц» не может начаться позже своего конца.
    assert run_node("period.presetRange('2026-09-01','month')") == {'start': '2026-08-31', 'end': '2026-08-31'}


def test_month_presets_cover_whole_calendar_months():
    assert run_node("period.presetRange('2026-09-24','month')") == {'start': '2026-09-01', 'end': '2026-09-23'}
    assert run_node("period.presetRange('2026-09-24','prev-month')") == {'start': '2026-08-01', 'end': '2026-08-31'}


def test_period_label_drops_the_month_and_year_it_repeats():
    assert run_node("period.label('2026-09-13','2026-09-22')") == '13 — 22 сентября 2026'
    assert run_node("period.label('2026-08-30','2026-09-02')") == '30 августа — 2 сентября 2026'
    assert run_node("period.label('2025-12-30','2026-01-02')") == '30 декабря 2025 — 2 января 2026'
    assert run_node("period.label('2026-09-22','2026-09-22')") == '22 сентября 2026'


def test_the_panel_refuses_the_same_ranges_as_the_server():
    assert run_node("period.check('2026-09-14','2026-09-23','2026-09-24')") == ''
    assert 'не закрыт' in run_node("period.check('2026-09-14','2026-09-24','2026-09-24')")
    assert 'позже' in run_node("period.check('2026-09-20','2026-09-14','2026-09-24')")
    assert '62' in run_node("period.check('2026-01-01','2026-09-01','2026-09-24')")
    assert run_node("period.daysBetween('2026-09-14','2026-09-23')") == 10


def test_every_working_page_mounts_the_shared_period_control():
    # Три модуля читают одни и те же цифры; выбор периода на них обязан
    # выглядеть и считаться одинаково, иначе сверять отчёты нечем.
    for page in PAGES:
        markup = (STATIC / page).read_text(encoding='utf-8')
        assert '/static/period.js' in markup, page
        assert 'id="period-host"' in markup, page


def test_the_excel_table_belongs_to_the_accountant_only():
    """Таблица «как в Excel» заведена для бухгалтера по образцу его книги.
    У кассира и директора вид остался прежним — там цифры читают иначе."""
    assert 'sheet-table' in (STATIC / 'accountant.html').read_text(encoding='utf-8')
    for page in ('index.html', 'director.html'):
        assert 'sheet-table' not in (STATIC / page).read_text(encoding='utf-8'), page
    rules = (STATIC / 'style.css').read_text(encoding='utf-8')
    for selector in ('.sheet-table tr.is-total', '.sheet-table tr.is-grand', '.sheet-table tr.is-section'):
        assert selector in rules, selector


def test_no_page_promises_a_module_that_does_not_exist():
    # Подпись «Модуль 03 · Позже» в сайдбаре обещала то, чего в панели нет.
    for page in PAGES + ('founder.html', 'employees.html'):
        assert 'class="coming"' not in (STATIC / page).read_text(encoding='utf-8'), page
