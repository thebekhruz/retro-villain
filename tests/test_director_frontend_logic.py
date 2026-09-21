import json
import subprocess


def run_node(expression):
    script = (
        "const logic=require('./retro/static/director-logic.js');"
        f"console.log(JSON.stringify({expression}));"
    )
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


GROUP = ("{"
         "'Плов':{quantity:'100',revenue:'1000000',cost:'400000',gross_profit:'600000',margin_percent:'60'},"
         "'Лагман':{quantity:'40',revenue:'400000',cost:'340000',gross_profit:'60000',margin_percent:'15'},"
         "'Чай':{quantity:'500',revenue:'50000',cost:'5000',gross_profit:'45000',margin_percent:'90'},"
         "'Хлеб':{quantity:'3',revenue:'0',cost:'0',gross_profit:'0',margin_percent:null}"
         "}")


def test_totals_sum_the_group_and_skip_margin_without_revenue():
    result = run_node(f"logic.totals({GROUP})")
    assert result['revenue'] == 1450000
    assert result['cost'] == 745000
    assert result['profit'] == 705000
    assert result['positions'] == 4
    assert round(result['margin'], 2) == 48.62


def test_totals_of_an_empty_group_report_no_margin_instead_of_zero():
    result = run_node("logic.totals({})")
    assert result == {'quantity': 0, 'revenue': 0, 'cost': 0, 'profit': 0, 'positions': 0, 'margin': None}


def test_position_without_revenue_has_no_margin():
    result = run_node(f"logic.entries({GROUP}).map(row=>[row.name,row.margin])")
    assert ['Хлеб', None] in result


def test_margin_sort_puts_the_worst_first_and_keeps_marginless_at_the_end():
    result = run_node(f"logic.rank({GROUP},'margin').map(row=>row.name)")
    assert result == ['Лагман', 'Плов', 'Чай', 'Хлеб']


def test_revenue_sort_is_the_default_for_an_unknown_key():
    result = run_node(f"logic.rank({GROUP},'нет такого').map(row=>row.name)")
    assert result == ['Плов', 'Лагман', 'Чай', 'Хлеб']


def test_limit_cuts_the_list_after_sorting():
    result = run_node(f"logic.rank({GROUP},'quantity',2).map(row=>row.name)")
    assert result == ['Чай', 'Плов']


def test_locomotives_rank_by_money_earned_not_by_percent():
    # У «Чая» маржа 90%, самая высокая в группе, но прибыли он приносит
    # меньше «Лагмана» с его пятнадцатью процентами — и стоит ниже.
    result = run_node(f"logic.locomotives({GROUP},3).map(row=>row.name)")
    assert result == ['Плов', 'Лагман', 'Чай']


def test_drains_skip_positions_too_small_to_matter():
    # Доля «Чая» в выручке около 3%: при пороге в 10% он выпадает, хотя
    # позиций с маржой ниже него в группе нет. Порядок — от худшей маржи.
    result = run_node(f"logic.drains({GROUP},3,0.1).map(row=>row.name)")
    assert result == ['Лагман', 'Плов']


def test_drains_of_an_empty_group_are_empty():
    assert run_node("logic.drains({},3)") == []


def test_search_matches_case_insensitively_and_keeps_order():
    result = run_node(f"logic.search(logic.rank({GROUP},'revenue'),'ЛА').map(row=>row.name)")
    assert result == ['Лагман']


def test_share_is_clamped_and_zero_without_revenue():
    result = run_node("[logic.shareOf({revenue:50},{revenue:200}),"
                      "logic.shareOf({revenue:50},{revenue:0}),"
                      "logic.shareOf({revenue:400},{revenue:200})]")
    assert result == [0.25, 0, 1]


def test_amount_treats_empty_and_broken_values_as_zero():
    result = run_node("[logic.amount(null),logic.amount(''),logic.amount('нет'),logic.amount('12.5')]")
    assert result == [0, 0, 0, 12.5]


def test_waiters_are_sorted_by_revenue():
    result = run_node("logic.waiters({"
                      "'Азиз':{quantity:'10',revenue:'100',cost:'40',gross_profit:'60'},"
                      "'Дилноза':{quantity:'20',revenue:'300',cost:'100',gross_profit:'200'}"
                      "}).map(row=>row.name)")
    assert result == ['Дилноза', 'Азиз']


def test_period_label_hides_the_month_and_year_repeated_at_both_ends():
    assert run_node("logic.periodLabel('2026-09-09','2026-09-18')") == '9 — 18 сентября 2026'


def test_period_label_keeps_both_months_when_the_period_crosses_one():
    assert run_node("logic.periodLabel('2026-08-28','2026-09-06')") == '28 августа — 6 сентября 2026'


def test_period_label_keeps_both_years_at_the_turn_of_the_year():
    assert run_node("logic.periodLabel('2025-12-28','2026-01-06')") == '28 декабря 2025 — 6 января 2026'


def test_period_label_falls_back_to_a_dash_on_broken_input():
    result = run_node("[logic.periodLabel('','2026-01-06'),logic.periodLabel('2026-13-01','2026-13-10')]")
    assert result == ['—', '—']


def test_plural_picks_the_russian_form_by_the_last_digits():
    result = run_node("[1,2,4,5,11,14,21,22,25,111].map(n=>"
                      "logic.plural(n,'позиция','позиции','позиций'))")
    assert result == ['позиция', 'позиции', 'позиции', 'позиций', 'позиций', 'позиций',
                      'позиция', 'позиции', 'позиций', 'позиций']


def test_day_label_spells_the_month_and_falls_back_on_garbage():
    result = run_node("[logic.dayLabel('2026-09-19'),logic.dayLabel('2026-13-19'),logic.dayLabel('')]")
    assert result == ['19 сентября 2026', '—', '—']
