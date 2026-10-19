import json
import subprocess


def run_node(expression):
    script = (
        "const logic=require('./retro/static/founder-logic.js');"
        f"console.log(JSON.stringify({expression}));"
    )
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_revenue_paths_keep_each_direction_separate_and_scale_from_zero():
    result = run_node("logic.revenuePaths(["
                      "{values:{retro:'100',school:'40'}},"
                      "{values:{retro:'200',school:'80'}}"
                      "],['retro','school'],200,100)")
    assert result == {
        'retro': '0,50 200,0',
        'school': '0,80 200,60',
    }


def test_request_gate_rejects_late_response():
    result = run_node("(()=>{const gate=logic.requestGate();"
                      "const first=gate.next();gate.invalidate();"
                      "const second=gate.next();"
                      "return [gate.isCurrent(first),gate.isCurrent(second)]})()")
    assert result == [False, True]


def test_completed_periods_end_yesterday_in_tashkent():
    result = run_node("logic.quickPeriod('30','2026-11-19')")
    assert result == {'start': '2026-10-20', 'end': '2026-11-18'}


def test_weekday_uses_calendar_date_without_local_timezone_shift():
    result = run_node("['2026-09-20','2026-09-21','2026-09-22'].map(logic.weekday)")
    assert result == ['воскресенье', 'понедельник', 'вторник']


def test_hover_selects_nearest_revenue_group_and_clamps_to_chart_edges():
    result = run_node(
        "[-20,149,151,420].map(x=>logic.nearestRevenueIndex(x,300,4))"
    )
    assert result == [0, 1, 2, 3]


def test_payment_lines_sum_each_method_across_selected_directions_per_period():
    result = run_node(
        "logic.paymentLineSeries(["
        "{start:'2026-09-01',end:'2026-09-01',incomplete:false,directions:{"
        "retro:{Cash:100,Card:50},school:{Cash:25,Card:0}}},"
        "{start:'2026-09-02',end:'2026-09-02',incomplete:true,directions:{"
        "retro:{Cash:50,Card:20},school:{Cash:25,Card:80}}}"
        "],['retro','school'],['Cash','Card'])"
    )
    assert result == [
        {
            'start': '2026-09-01',
            'end': '2026-09-01',
            'incomplete': False,
            'values': {'Cash': 125, 'Card': 50},
        },
        {
            'start': '2026-09-02',
            'end': '2026-09-02',
            'incomplete': True,
            'values': {'Cash': 75, 'Card': 100},
        },
    ]


def days(start, count, values):
    """Ряд сервера по дням: start — ISO-дата, values(index) — суммы направлений."""
    return ("(()=>{const rows=[];for(let i=0;i<%d;i+=1){const d=new Date(Date.parse('%sT00:00:00Z')+i*864e5)"
            ".toISOString().slice(0,10);rows.push({start:d,end:d,incomplete:false,values:%s})}return rows})()"
            % (count, start, values))


def test_revenue_bars_stack_directions_and_keep_day_bars_up_to_sixty_points():
    # ТЗ 16.10, п. 5.1: столбец = Retro + Школа + Банкет, без пересечений.
    result = run_node("logic.revenueBars(" + days('2026-09-17', 30, "{retro:'100',school:'40',banquet:i===3?'25':'0'}")
                      + ",['retro','school','banquet'])")
    assert result['unit'] == 'day' and result['regrouped'] is False
    assert len(result['bars']) == 30
    assert result['bars'][3]['values'] == {'retro': 100, 'school': 40, 'banquet': 25}
    assert result['bars'][3]['total'] == 165
    assert not any(bar['partial'] for bar in result['bars'])


def test_long_day_series_is_regrouped_into_weeks_with_partial_edges_marked():
    # 90 дней: 17.07 (пятница) — 14.10 (среда). Края недель обрезаны периодом.
    result = run_node("logic.revenueBars(" + days('2026-07-17', 90, "{retro:'10',school:'1'}")
                      + ",['retro','school'],{granularity:'day'})")
    bars = result['bars']
    assert result['unit'] == 'week' and result['regrouped'] is True
    assert (bars[0]['start'], bars[0]['end'], bars[0]['days'], bars[0]['partial']) == (
        '2026-07-17', '2026-07-19', 3, True)
    assert (bars[1]['start'], bars[1]['end'], bars[1]['days'], bars[1]['partial']) == (
        '2026-07-20', '2026-07-26', 7, False)
    assert (bars[-1]['start'], bars[-1]['end'], bars[-1]['days'], bars[-1]['partial']) == (
        '2026-10-12', '2026-10-14', 3, True)
    # Сумма корзин равна сумме дней — ничего не потеряно и не задвоено.
    assert sum(bar['total'] for bar in bars) == 90 * 11
    assert bars[1]['values'] == {'retro': 70, 'school': 7}


def test_server_week_and_month_buckets_are_flagged_when_incomplete():
    result = run_node(
        "[logic.revenueBars([{start:'2026-09-29',end:'2026-10-04',values:{retro:1}},"
        "{start:'2026-10-05',end:'2026-10-11',values:{retro:1}}],['retro'],{granularity:'week'}).bars.map(b=>[b.days,b.partial]),"
        "logic.revenueBars([{start:'2026-09-17',end:'2026-09-30',values:{retro:1}},"
        "{start:'2026-10-01',end:'2026-10-31',values:{retro:1}}],['retro'],{granularity:'month'}).bars.map(b=>[b.days,b.full,b.partial])]")
    assert result == [[[6, True], [7, False]], [[14, 30, True], [31, 31, False]]]


def test_bar_stack_scale_and_hover_follow_the_bar_slot():
    result = run_node(
        "(()=>{const bar={values:{retro:100,school:-20,banquet:30}};"
        "return [logic.barStack(bar,['retro','school','banquet']),"
        "logic.barScale([bar],['retro','school','banquet']),"
        "[0,74.9,75,299,400,-3].map(x=>logic.barIndexAt(x,300,4)),"
        "[18400000,2100,58000000,1,0].map(logic.niceCeil)]})()")
    stack, scale, indexes, ceilings = result
    assert stack == [
        {'direction': 'retro', 'from': 0, 'to': 100},
        {'direction': 'school', 'from': -20, 'to': 0},
        {'direction': 'banquet', 'from': 100, 'to': 130},
    ]
    assert scale == {'max': 160, 'min': -20}
    # Каждый столбец владеет полосой width/count: подсказка — над его серединой.
    assert indexes == [0, 0, 1, 3, 3, 0]
    assert ceilings == [20000000, 2400, 60000000, 1, 1]


def test_filters_round_trip_through_the_page_address():
    # ТЗ 16.10, п. 5.4: фильтры в адресе страницы, перезагрузка их не сбрасывает.
    result = run_node(
        "[logic.filtersToQuery({start:'2026-09-01',end:'2026-09-30',granularity:'week',directions:['school','retro'],period:null}),"
        "logic.filtersFromQuery('?start=2026-09-01&end=2026-09-30&granularity=week&directions=retro,school','2026-10-17'),"
        "logic.filtersToQuery({start:'x',end:'y',granularity:'day',directions:['retro','school','banquet'],period:'7'}),"
        "logic.filtersFromQuery('?period=7','2026-10-17')]")
    assert result[0] == 'start=2026-09-01&end=2026-09-30&granularity=week&directions=retro,school'
    assert result[1] == {'start': '2026-09-01', 'end': '2026-09-30', 'granularity': 'week',
                         'directions': ['retro', 'school'], 'period': None}
    assert result[2] == 'period=7'
    # Быстрый период пересчитывается от сегодняшнего дня, а не берётся из старой ссылки.
    assert result[3] == {'start': '2026-10-10', 'end': '2026-10-16', 'granularity': 'day',
                         'directions': ['retro', 'school', 'banquet'], 'period': '7'}


def test_broken_or_future_address_falls_back_to_thirty_days():
    result = run_node(
        "['?start=2026-10-20&end=2026-10-21','?start=2026-02-30&end=2026-03-01',"
        "'?start=2026-10-10&end=2026-10-01','?granularity=hour&directions=bar',''].map("
        "search=>logic.filtersFromQuery(search,'2026-10-17'))")
    for filters in result:
        assert (filters['start'], filters['end'], filters['period']) == ('2026-09-17', '2026-10-16', '30')
        assert filters['granularity'] == 'day'
        assert filters['directions'] == ['retro', 'school', 'banquet']
