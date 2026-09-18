# Accountant Demo Finance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a usable local demo of the Retro Milliy finance module from the approved `ЗП` roster, with attendance flags, confirmed payroll, partial payments, carried debt, other expenses, procurement advances and funding scenarios.

**Architecture:** Import the user workbook into a separate SQLite demo database, never into source control or the cashier database. A pure daily attendance/payroll calculator reads roster rows and deterministic simulated events; a transactional ledger stores confirmed accruals and real-in-demo cash movements. FastAPI exposes the demo state and mutations; the accountant page renders the grouped roster and financial actions.

**Tech Stack:** Python 3.11, FastAPI, SQLite, openpyxl for reading the source workbook, vanilla HTML/CSS/JS, pytest.

**Spec:** `docs/superpowers/specs/2026-09-17-accountant-payroll-finance-design.md`

## Global Constraints

- This is a local `ДЕМО`; simulated attendance and finance movements never change `build/cashier.sqlite3`, cashier exports or real `К передаче`.
- Source roster is only worksheet `ЗП`, rows 5–78. It contains 60 named employees, 51 rates and 9 missing rates; original names and rates never enter git.
- Yesterday in `Asia/Tashkent` is the initial workday. Late means first entry after `10:00:00`; lateness never reduces the daily rate.
- Missing source data is distinct from numeric zero. A missing rate prevents accrual; source outage is not absence.
- One unlinked-day override maximum per employee, with approver and reason. Confirming the same day twice cannot duplicate debt.
- Available cash changes on confirmed receipt or actual disbursement; accrual alone changes only debt. Existing cashier automatic 350,000 UZS stays unallocated.
- No real Hikvision integration, no automatic group closure, no personal roster in static assets or repository.

---

### Task 1: Import and locally store the approved roster

**Files:**
- Create: `retro/modules/accountant/roster.py`
- Create: `scripts/import_accountant_roster.py`
- Test: `tests/test_accountant_roster.py`

**Interfaces:**
- Produces: `RosterStore(path: Path)`, `RosterStore.import_xlsx(path: Path) -> dict`, `RosterStore.list() -> list[Employee]`, `RosterStore.update(employee_id: int, *, rate: str | None, group_name: str, reason: str) -> Employee`.
- `Employee` contains `id`, `source_row`, `name`, `role`, `group_name`, `rate: Decimal | None`, `hikvision_id: str | None`.

- [ ] **Step 1: Write failing tests** for importing only named rows of `ЗП`, 8 normalized groups, blank rate versus zero, stable source-row IDs and no accidental reimport overwrite of edited rates. Generate a tiny temporary workbook in tests, not a committed copy of employee data.

```python
book = Workbook(); sheet = book.active; sheet.title = 'ЗП'
sheet['A5'], sheet['B5'], sheet['C5'], sheet['D5'] = 1, 'Тест Повар', 'повар миллий', 250000
sheet['A6'], sheet['B6'], sheet['C6'], sheet['D6'] = 2, 'Тест Уборка', 'техперсонал', None
assert [(x.group_name, x.rate) for x in store.list()] == [('Кухня', Decimal('250000')), ('Уборка', None)]
```

- [ ] **Step 2: Run** `PYTHONPATH=. build/venv/bin/pytest -q tests/test_accountant_roster.py` and observe failures.
- [ ] **Step 3: Implement** mapping and import transaction. Use `source_row` as unique source identity, preserve edited records on repeat import, and reject an absent `ЗП` sheet or duplicate source row.

```python
GROUPS = {'менеджер':'Управление', 'хостес':'Встреча гостей', 'официант':'Обслуживание зала',
          'ранер':'Обслуживание зала', 'бармен':'Бар', 'няня':'Присмотр за детьми',
          'техперсонал':'Уборка', 'охрана':'Охрана'}
def group_for(role: str) -> str:
    value = ' '.join(role.casefold().split())
    return 'Кухня' if value.startswith('повар') or value == 'кондитер' else GROUPS[value]
```

- [ ] **Step 4: Run** targeted tests and verify the real workbook yields 60 names, 51 known rates, 9 missing rates without printing names or copying the file.
- [ ] **Step 5: Review** the import transaction and no-PII-in-git rule; commit only code/tests if the worktree permits an isolated commit.

### Task 2: Deterministic demo attendance, daily payroll and one-day exception

**Files:**
- Create: `retro/modules/accountant/payroll.py`
- Modify: `retro/modules/accountant/attendance.py`
- Test: `tests/test_accountant_payroll.py`

**Interfaces:**
- Consumes: `Employee` from Task 1.
- Produces: `demo_attendance(day: date, employees: list[Employee]) -> list[AttendanceRow]`, `draft_payroll(day: date, employees: list[Employee], exceptions: set[int]) -> list[PayrollRow]`.
- `PayrollRow` includes employee ID, group, status, first entry time or `None`, rate or `None`, payable or `None`, and explanation.

- [ ] **Step 1: Write failing tests** for `10:00:00`/`10:00:01`, full pay when late, zero when missing, `None` when rate absent or link missing, a one-day exception, and stable events for the same date.

```python
assert compute_pay(Decimal('270000'), 'late', exception=False) == Decimal('270000')
assert compute_pay(Decimal('270000'), 'missing', exception=False) == Decimal('0')
assert compute_pay(None, 'on_time', exception=False) is None
assert compute_pay(Decimal('270000'), 'unlinked', exception=False) is None
```

- [ ] **Step 2: Run** `PYTHONPATH=. build/venv/bin/pytest -q tests/test_accountant_payroll.py` and confirm red.
- [ ] **Step 3: Implement** deterministic synthetic statuses using day and internal employee ID (never a real device ID). Keep `demo=true` in every API result. Existing `is_late` remains the 10:00 boundary function.
- [ ] **Step 4: Run** targeted tests; review that a missing device connection cannot be interpreted as a real absence.
- [ ] **Step 5: Commit** only Task 2 files if isolated staging is safe.

### Task 3: Transactional finance ledger and carried salary debt

**Files:**
- Create: `retro/modules/accountant/ledger.py`
- Test: `tests/test_accountant_ledger.py`

**Interfaces:**
- Produces: `FinanceStore(path: Path)` with `grant_exception(employee_id, day, reason, approver)`, `confirm_payroll(day, rows, approver)`, `confirm_transfer(cashier_day, received_day, amount)`, `add_opening(day, amount, note)`, `pay_salary(accrual_id, paid_day, amount)`, `add_expense(day, description, amount)`, `give_procurement(day, recipient, purpose, amount)`, `summary(day)`.
- All money values are `Decimal`; IDs and unique references prevent double confirmations.

- [ ] **Step 1: Write failing tests** for repeat payroll confirmation, rate snapshots, one exception lifetime, partial payout on a later day, overpayment/insufficient-funds rejection, unique cashier transfer, other expense, and a separately categorized Шох advance.

```python
store.confirm_payroll(workday, [row], 'Руководитель финансов')
store.confirm_payroll(workday, [row], 'Руководитель финансов')
assert store.summary(workday)['accrued'] == Decimal('270000')
store.confirm_transfer(workday, next_day, Decimal('200000'))
store.pay_salary(accrual_id, next_day, Decimal('100000'))
assert store.summary(next_day)['salary_debt'] == Decimal('170000')
assert store.summary(next_day)['cash_balance'] == Decimal('100000')
```

- [ ] **Step 2: Run** `PYTHONPATH=. build/venv/bin/pytest -q tests/test_accountant_ledger.py` and confirm red.
- [ ] **Step 3: Implement** SQLite transactions with uniqueness on `(work_day, employee_id)` accruals and `(type, reference)` cash receipts. Validate positive finite amounts, paid dates not before work dates, cumulative cash and debt at the chosen date. Record descriptive journal entries rather than editing balances in place.
- [ ] **Step 4: Run** targeted tests, including close/reopen of SQLite and concurrent/idempotent confirmation.
- [ ] **Step 5: Commit** only Task 3 files if isolated staging is safe.

### Task 4: Accountant API and app wiring

**Files:**
- Modify: `retro/app.py`
- Modify: `retro/modules/accountant/routes.py`
- Test: `tests/test_accountant_api.py`

**Interfaces:**
- `GET /api/accountant/day?date=YYYY-MM-DD` returns `{demo:true, date, employees, groups, payroll, ledger, scenarios}`.
- `POST /api/accountant/exceptions`, `/payroll/confirm`, `/transfers`, `/opening`, `/salary-payments`, `/expenses`, `/procurement` call the Task 3 store. Body types use Pydantic and day bounds; routes return 422 on business-rule violations and 409 on conflicting repeats.

- [ ] **Step 1: Write failing API tests** using a temporary demo database and synthetic employees. Verify yesterday default, future-date rejection, grouped records, no cashier DB writes, confirmed/paid/debt totals and all mutation endpoints.

```python
response = client.get('/api/accountant/day', params={'date': '2026-09-16'})
assert response.status_code == 200
assert response.json()['demo'] is True
assert client.get('/api/cashier/expenses', params={'date': '2026-09-16'}).json()['total'] == '350000'
```

- [ ] **Step 2: Run** `PYTHONPATH=. build/venv/bin/pytest -q tests/test_accountant_api.py` and confirm red.
- [ ] **Step 3: Wire** separate `build/accountant-demo.sqlite3` through `create_app` and expose the typed endpoints. Do not call live Hikvision. Cashier data is read-only when presenting a transfer estimate; posting a finance receipt always needs a finance action.
- [ ] **Step 4: Run** targeted and full API tests, check no secrets or personal employee list in response cache headers or static files.
- [ ] **Step 5: Commit** only Task 4 files if isolated staging is safe.

### Task 5: Mobile-first accountant interface and end-to-end verification

**Files:**
- Modify: `retro/static/accountant.html`
- Modify: `retro/static/accountant.js`
- Modify: `retro/static/style.css`
- Modify: `README.md`
- Test: `tests/test_accountant_api.py`

**Interfaces:**
- Consumes Task 4 JSON and endpoints; presents selected day, grouped employee states, accrued/paid/debt/cash cards, actions, journal and future-shift group scenarios. Existing empty attendance XLSX button must clearly state its remaining limitation until the real source is connected.

- [ ] **Step 1: Extend failing page/API tests** for visible `ДЕМО` status, default yesterday, eight group labels, disabled/unavailable real-Hikvision wording and action labels.

```python
page = client.get('/accountant').text
assert 'Зарплата к выплате' in page and 'Выдать Шоху' in page
assert 'ДЕМО' in page
```

- [ ] **Step 2: Run** the targeted test and confirm red.
- [ ] **Step 3: Replace** hard-coded sample cards with JS-rendered employee cards and finance panels. Use `textContent` for names, reason and descriptions; forms send JSON to Task 4 endpoints and refresh the selected day after success. Mark demo figures and debt clearly, with no cash mutation on payroll confirmation.
- [ ] **Step 4: Verify** 390px and desktop layouts, keyboard focus, `node --check` for both accountant and cashier JS, full pytest suite, `git diff --check`, and live local HTTP interactions on port 8011. Keep existing cashier salary and report behavior unchanged.
- [ ] **Step 5: Update** README with local import command, demo isolation, cash/debt rules and no-public-tunnel warning; commit only Task 5 files if isolated staging is safe.
