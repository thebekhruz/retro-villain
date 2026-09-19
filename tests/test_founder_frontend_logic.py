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
                      "const first=gate.next();const second=gate.next();"
                      "return [gate.isCurrent(first),gate.isCurrent(second)]})()")
    assert result == [False, True]


def test_completed_periods_end_yesterday_in_tashkent():
    result = run_node("logic.quickPeriod('30','2026-09-19')")
    assert result == {'start': '2026-08-20', 'end': '2026-09-18'}
