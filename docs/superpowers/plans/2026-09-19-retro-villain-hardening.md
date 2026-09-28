# Retro Villain Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one tested integration branch that fixes the confirmed financial, security, persistence, UI, and operations defects without touching the currently running worktrees or source databases.

**Architecture:** Start from the T-347 superset and add small isolated services for runtime paths, database maintenance, request security, and audit recording. Port only the required accountant behavior from the dirty checkout, enforce all financial invariants in SQLite transactions, and make frontend state transitions explicit and testable.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, httpx, openpyxl, ReportLab, vanilla HTML/CSS/JavaScript, pytest, Node.js syntax tests, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-19-retro-villain-hardening-design.md`

## Global Constraints

- Work only in `/home/mvzmysun/Projects/PM/projects/retro-app-hardening` on `codex/retro-villain-hardening`.
- Do not edit, stop, or repoint the servers running from T-346 and T-347.
- Do not modify or delete any existing database under another worktree.
- No personal salary seed rows or live secrets may enter Git.
- Every production behavior change follows red-green-refactor; observe each new regression test fail before implementation.
- Paid cash may not become negative; excess expense value is recorded as debt.
- Preserve existing API shapes unless the unsafe behavior must be rejected.
- Use `apply_patch` for hand-written file changes.
- Run the focused test after each green step and the full suite before each task commit.

---

### Task 0: Isolated environment and green baseline

**Files:**
- No tracked files.
- Create outside Git: `build/venv/`.

**Interfaces:**
- Produces the worktree-local Python executable used by every later task: `build/venv/bin/python`.

- [ ] **Step 1: Confirm isolation and branch**

Run: `git branch --show-current && git status --short`

Expected: branch `codex/retro-villain-hardening`; no changes except the committed specification and plan history.

- [ ] **Step 2: Create the local environment**

Run: `python3 -m venv build/venv`

Run: `build/venv/bin/pip install -r requirements-dev.txt`

Expected: dependency installation exits 0; `build/` remains ignored.

- [ ] **Step 3: Verify the inherited baseline**

Run: `build/venv/bin/python -m pytest -q`

Expected: `158 passed` with no failures. The two existing upstream deprecation warnings are recorded as baseline noise and must not increase.

### Task 1: Shared runtime data directory

**Files:**
- Create: `retro/runtime.py`
- Modify: `retro/config.py`
- Modify: `retro/app.py`
- Modify: `config.env.example`
- Test: `tests/test_runtime_paths.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `resolve_data_dir(explicit: str = "", environ: Mapping[str, str] | None = None) -> Path`.
- Produces: `secure_directory(path: Path) -> Path` and `secure_file(path: Path) -> Path`.
- Extends: `Settings.data_dir: Path` and `Settings.report_retention: int`.
- `create_app()` consumes `settings.data_dir` when individual test database paths are not injected.

- [ ] **Step 1: Add failing path-resolution and application-path tests**

```python
def test_explicit_data_dir_wins_and_is_owner_only(tmp_path):
    target = tmp_path / 'shared'
    assert resolve_data_dir(str(target), {}) == target.resolve()
    assert target.stat().st_mode & 0o777 == 0o700


def test_xdg_data_dir_is_used_without_explicit_setting(tmp_path):
    expected = tmp_path / 'retro-villain'
    assert resolve_data_dir('', {'XDG_DATA_HOME': str(tmp_path)}) == expected


def test_app_uses_shared_database_names(tmp_path):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    assert app.state.expenses.path == tmp_path / 'cashier.sqlite3'
    assert app.state.accountant_finance.path == tmp_path / 'accountant.sqlite3'
    assert app.state.director_store.path == tmp_path / 'director.sqlite3'


def test_new_database_files_are_owner_only(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    app.state.expenses.list(DAY)
    assert (tmp_path / 'cashier.sqlite3').stat().st_mode & 0o777 == 0o600
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_runtime_paths.py tests/test_api.py::test_app_uses_shared_database_names -v`

Expected: collection or assertion failure because `retro.runtime`, `Settings.data_dir`, and shared paths do not exist.

- [ ] **Step 3: Implement deterministic data-path resolution**

```python
def resolve_data_dir(explicit='', environ=None):
    values = os.environ if environ is None else environ
    if explicit:
        path = Path(explicit).expanduser()
    elif values.get('XDG_DATA_HOME'):
        path = Path(values['XDG_DATA_HOME']) / 'retro-villain'
    else:
        path = Path.home() / '.local' / 'share' / 'retro-villain'
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path
```

Add `RETRO_DATA_DIR` and an integer `DIRECTOR_REPORT_RETENTION` with a bounded positive default. Construct all three SQLite stores from `settings.data_dir` in `create_app()` while retaining explicit test path parameters. Every store applies `secure_file()` immediately after SQLite creates its file.

- [ ] **Step 4: Run focused and full tests**

Run: `build/venv/bin/python -m pytest tests/test_runtime_paths.py tests/test_api.py -v`

Run: `build/venv/bin/python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add retro/runtime.py retro/config.py retro/app.py config.env.example tests/test_runtime_paths.py tests/test_api.py
git commit -m "fix: centralize runtime data paths"
```

### Task 2: Verified backup, restore, and legacy-data migration

**Files:**
- Create: `retro/maintenance.py`
- Create: `retro/schema.py`
- Create: `scripts/retro_data.py`
- Modify: `README.md`
- Test: `tests/test_maintenance.py`

**Interfaces:**
- Produces: `backup_databases(sources: Mapping[str, Path], backup_root: Path, now: datetime) -> Path`.
- Produces: `verify_database(path: Path) -> DatabaseCheck`.
- Produces: `install_verified_backup(backup_dir: Path, destination: Path, replace: bool = False) -> list[Path]`.
- Produces: `migrate_schema(connection: sqlite3.Connection, database_kind: str) -> int` backed by `retro_schema_version`.
- CLI commands: `backup`, `verify`, `migrate`, and `restore --destination`.

- [ ] **Step 1: Add failing backup and refusal tests**

```python
def test_backup_uses_sqlite_api_and_writes_verified_manifest(tmp_path):
    source = sqlite_file(tmp_path / 'source.sqlite3', 'CREATE TABLE values_table(value TEXT)', ('ok',))
    result = backup_databases({'cashier.sqlite3': source}, tmp_path / 'backups', FIXED_NOW)
    manifest = json.loads((result / 'manifest.json').read_text())
    assert manifest['databases'][0]['integrity'] == 'ok'
    assert manifest['databases'][0]['sha256'] == file_sha256(result / 'cashier.sqlite3')


def test_install_refuses_nonempty_destination_without_replace(tmp_path):
    backup = verified_backup(tmp_path)
    destination = tmp_path / 'data'
    destination.mkdir()
    (destination / 'cashier.sqlite3').write_bytes(b'existing')
    with pytest.raises(MaintenanceError, match='уже существует'):
        install_verified_backup(backup, destination)


def test_legacy_schema_migrates_once_without_partial_numeric_rows(tmp_path):
    database = legacy_accountant_database(tmp_path / 'accountant.sqlite3')
    with sqlite3.connect(database) as connection:
        assert migrate_schema(connection, 'accountant') == CURRENT_ACCOUNTANT_SCHEMA
        assert migrate_schema(connection, 'accountant') == CURRENT_ACCOUNTANT_SCHEMA
        assert connection.execute('SELECT version FROM retro_schema_version').fetchone()[0] == CURRENT_ACCOUNTANT_SCHEMA
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_maintenance.py -v`

Expected: import failure because the maintenance service does not exist.

- [ ] **Step 3: Implement verified, atomic maintenance operations**

Use `sqlite3.Connection.backup()` into a temporary file in the target filesystem, run `PRAGMA integrity_check`, migrate a verified copy through numbered schema transactions, fsync the file, rename atomically, and write a manifest containing filename, byte size, SHA-256, schema version, integrity result, and UTC timestamp. `migrate` maps legacy `accountant-demo.sqlite3` to `accountant.sqlite3`. A row containing invalid numeric text raises `MaintenanceError` with table and row ID before installation. Never remove the source.

```python
with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as source_db:
    with sqlite3.connect(temporary) as target_db:
        source_db.backup(target_db)
if verify_database(temporary).integrity != 'ok':
    raise MaintenanceError(f'Проверка {source.name} не пройдена.')
os.replace(temporary, destination)
```

- [ ] **Step 4: Verify CLI dry-run semantics**

Run: `build/venv/bin/python scripts/retro_data.py verify --source /home/mvzmysun/Projects/PM/projects/retro-app/build/cashier.sqlite3`

Expected: exit 0 with filename, size, hash, and `integrity=ok`; no row contents printed.

Run: `build/venv/bin/python -m pytest tests/test_maintenance.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add retro/maintenance.py retro/schema.py scripts/retro_data.py tests/test_maintenance.py README.md
git commit -m "feat: add verified database maintenance"
```

### Task 3: Financial payment and edit invariants

**Files:**
- Modify: `retro/modules/accountant/ledger.py`
- Modify: `retro/modules/accountant/routes.py`
- Test: `tests/test_accountant_ledger.py`
- Test: `tests/test_accountant_api.py`

**Interfaces:**
- Changes: `update_salary_payment(payment_id: int, day: date, amount) -> None` verifies the stored payment day and sums every other payment by `id != payment_id`.
- Changes: `update_movement(movement_id: int, day: date, item_code: str, note: str, amount) -> None` rejects a mismatched stored day.
- Preserves: `record_debt()` as the path for the unpaid portion of an expense.

- [ ] **Step 1: Add failing overpayment, date-mismatch, and paid-cash tests**

```python
def test_editing_second_partial_salary_payment_cannot_overpay_accrual(tmp_path):
    store, accrual_id = confirmed_accrual(tmp_path, amount='100')
    store.pay_salary(accrual_id, DAY, '60', cashier_amount=Decimal('500'))
    second = store.pay_salary(accrual_id, DAY, '30', cashier_amount=Decimal('500'))
    with pytest.raises(LedgerError, match='начислен'):
        store.update_salary_payment(second, DAY, '70')


def test_movement_update_requires_stored_day(tmp_path):
    store = funded_store(tmp_path, DAY, amount='1000')
    movement = store.add_expense(DAY, 'admin_other', 'Бумага', '100', cashier_amount=Decimal('1000'))
    with pytest.raises(LedgerError, match='Дата операции'):
        store.update_movement(movement, NEXT_DAY, 'admin_other', 'Бумага', '100')


def test_paid_expense_above_cash_is_rejected_and_partial_expense_is_debt(tmp_path):
    store = funded_store(tmp_path, DAY, amount='100')
    with pytest.raises(LedgerError, match='недостаточно'):
        store.add_expense(DAY, 'admin_other', 'Расход', '101', cashier_amount=Decimal('100'))
    debt = store.record_debt(DAY, 'admin_other', 'Расход', '150', '100', cashier_amount=Decimal('100'))
    assert store.debt_summary(DAY)['manual_debts'][0]['id'] == debt
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_accountant_ledger.py -k 'partial_salary or stored_day or above_cash' -v`

Expected: failures reproduce the overpayment and date-trust defects.

- [ ] **Step 3: Correct SQL identity checks and enforce stored dates**

Replace the nested subquery with a direct exclusion:

```python
paid_elsewhere = sum((Decimal(row[0]) for row in connection.execute(
    'SELECT amount FROM accountant_salary_payments WHERE accrual_id=? AND id!=?',
    (accrual_id, payment_id))), Decimal(0))
```

Read `paid_day` or `day` with the target row and reject mismatch before any update. Route errors through the existing safe `LedgerError` mapping. Keep negative paid cash prohibited and direct partial expenses through `record_debt()`.

- [ ] **Step 4: Run focused and full tests**

Run: `build/venv/bin/python -m pytest tests/test_accountant_ledger.py tests/test_accountant_api.py -v`

Run: `build/venv/bin/python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add retro/modules/accountant/ledger.py retro/modules/accountant/routes.py tests/test_accountant_ledger.py tests/test_accountant_api.py
git commit -m "fix: enforce financial payment invariants"
```

### Task 4: Safe operation deletion and financial audit trail

**Files:**
- Create: `retro/modules/accountant/audit.py`
- Modify: `retro/modules/accountant/ledger.py`
- Modify: `retro/modules/accountant/routes.py`
- Test: `tests/test_accountant_audit.py`
- Test: `tests/test_accountant_reserves.py`

**Interfaces:**
- Produces: `record_audit(connection, entity_type: str, entity_id: int | str, action: str, before: dict | None, after: dict | None) -> None`.
- Produces: `delete_operation(operation_type: str, operation_id: int, day: date) -> None`.
- Adds append-only table `accountant_finance_audit`.

- [ ] **Step 1: Add failing deletion and audit tests**

```python
def test_deleting_salary_payment_removes_only_salary_payment(tmp_path):
    store, salary_id, debt_payment_id = store_with_colliding_payment_ids(tmp_path)
    store.delete_operation('salary_payment', salary_id, DAY)
    assert store.salary_payment(salary_id) is None
    assert store.debt_payment(debt_payment_id) is not None


def test_deleting_transfer_cannot_make_later_reserve_negative(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.reserve_entry(DAY, 'dividends', 'opening', '0', 'Начало')
    transfer = store.reserve_entry(DAY, 'dividends', 'transfer', '300', 'Сейф', cashier_amount=Decimal('1000'))
    store.reserve_entry(NEXT_DAY, 'dividends', 'withdrawal', '200', 'Выдача')
    with pytest.raises(LedgerError, match='последующ'):
        store.delete_operation('reserve_transfer', transfer, DAY)


def test_financial_mutation_records_before_and_after(tmp_path):
    store = funded_store(tmp_path, DAY, amount='1000')
    movement = store.add_expense(DAY, 'admin_other', 'Бумага', '100', cashier_amount=Decimal('1000'))
    store.update_movement(movement, DAY, 'admin_other', 'Бумага', '90')
    row = store.audit_entries(entity_type='movement', entity_id=movement)[-1]
    assert row['action'] == 'update'
    assert row['before']['amount'] == '100'
    assert row['after']['amount'] == '90'
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_accountant_audit.py tests/test_accountant_reserves.py -v`

Expected: failures because audit storage and safe deletion do not exist.

- [ ] **Step 3: Implement transactional deletion and append-only auditing**

For `salary_payment`, delete from `accountant_salary_payments` by its own ID. For a debt payment, delete its `accountant_debt_payments` row and linked `accountant_movements` row in the same transaction. For a reserve transfer, tentatively delete, recompute every later cutoff balance, and roll back if any becomes negative. Record before/after JSON inside the same transaction. Route every financial create, update, payment, and deletion through the same audit helper; read-only calculations do not emit audit rows.

```sql
CREATE TABLE IF NOT EXISTS accountant_finance_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    changed_at TEXT NOT NULL
)
```

- [ ] **Step 4: Run focused and full tests**

Run: `build/venv/bin/python -m pytest tests/test_accountant_audit.py tests/test_accountant_reserves.py tests/test_accountant_ledger.py -v`

Run: `build/venv/bin/python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add retro/modules/accountant/audit.py retro/modules/accountant/ledger.py retro/modules/accountant/routes.py tests/test_accountant_audit.py tests/test_accountant_reserves.py
git commit -m "fix: make financial mutations auditable"
```

### Task 5: Monthly payroll without source-code personal data

**Files:**
- Modify: `retro/modules/accountant/roster.py`
- Modify: `retro/modules/accountant/routes.py`
- Modify: `retro/static/employees.html`
- Modify: `retro/static/employees.js`
- Modify: `retro/static/style.css`
- Create: `scripts/import_monthly_payroll.py`
- Test: `tests/test_accountant_roster.py`
- Test: `tests/test_accountant_api.py`

**Interfaces:**
- Produces: `parse_money(value, *, allow_zero=True) -> Decimal` for all monthly fields.
- Produces: `RosterStore.list_monthly()`, `add_monthly()`, `update_monthly()`, and `delete_monthly()` without automatic reseeding.
- Produces: import CLI accepting a local XLSX/CSV path and the shared accountant database path.

- [ ] **Step 1: Add failing validation and empty-roster tests**

```python
@pytest.mark.parametrize('field,value', [
    ('card', 'bad'), ('cash', 'NaN'), ('advances', 'Infinity'),
    ('remaining', '-1'), ('salary', '1.001'),
])
def test_monthly_money_fields_reject_invalid_values(tmp_path, field, value):
    store = RosterStore(tmp_path / 'accountant.sqlite3')
    payload = dict(name='Сотрудник', role='Роль', salary='100', schedule='', card='0', cash='0', advances='0', remaining='0')
    payload[field] = value
    with pytest.raises(ValueError):
        store.add_monthly(**payload)
    assert store.list_monthly() == []


def test_deleting_last_monthly_employee_keeps_roster_empty(tmp_path):
    store = RosterStore(tmp_path / 'accountant.sqlite3')
    employee = store.add_monthly(name='Сотрудник', role='Роль', salary='100')
    store.delete_monthly(employee.id)
    assert store.list_monthly() == []
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_accountant_roster.py -k monthly -v`

Expected: failures because the T-347 base has no monthly payroll API.

- [ ] **Step 3: Port the monthly schema with complete validation and explicit import**

Create the table without embedded people. Validate `salary`, `card`, `cash`, `advances`, and `remaining` before opening the write transaction. Commit only canonical decimal strings. The import script reads user-supplied local data and upserts by an explicit external key or rejects ambiguous duplicate names; it never ships rows in source.

```python
money = {name: parse_money(value, allow_zero=True) for name, value in {
    'salary': salary, 'card': card, 'cash': cash,
    'advances': advances, 'remaining': remaining,
}.items()}
```

- [ ] **Step 4: Add explicit accessible edit buttons and run tests**

Render a `<button type="button" class="edit-monthly">Изменить</button>` for each monthly row. Bind click and Enter/Space through the native button behavior; do not require `dblclick`.

Run: `build/venv/bin/python -m pytest tests/test_accountant_roster.py tests/test_accountant_api.py -v`

Run: `find retro/static -name '*.js' -print0 | xargs -0 -n1 node --check`

Expected: all tests and syntax checks pass.

- [ ] **Step 5: Commit**

```bash
git add retro/modules/accountant/roster.py retro/modules/accountant/routes.py retro/static/employees.html retro/static/employees.js retro/static/style.css scripts/import_monthly_payroll.py tests/test_accountant_roster.py tests/test_accountant_api.py
git commit -m "feat: add validated monthly payroll records"
```

### Task 6: Request-boundary and CSRF hardening

**Files:**
- Create: `retro/security.py`
- Modify: `retro/config.py`
- Modify: `retro/app.py`
- Modify: `retro/modules/director/routes.py`
- Test: `tests/test_security.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `client_address(request: Request, settings: Settings) -> ip_address`.
- Produces: `is_finance_path(path: str) -> bool` including accountant and director attendance.
- Produces: `validate_mutation_origin(request: Request) -> None`.
- Adds explicit `TRUSTED_PROXY_NETWORK` setting; forwarded headers are ignored unless the direct peer belongs to it.

- [ ] **Step 1: Add failing Host, origin, and proxy tests**

```python
def test_external_peer_cannot_forge_local_host(finance_app):
    with TestClient(finance_app, client=('203.0.113.5', 50000)) as client:
        response = client.get('/api/accountant/day', headers={'host': 'localhost'}, auth=('viewer', 'secret'))
    assert response.status_code == 403


def test_cross_origin_report_generation_is_rejected(local_director_app):
    with TestClient(local_director_app, client=('127.0.0.1', 50000)) as client:
        response = client.post('/api/director/reports', headers={'origin': 'https://attacker.example'})
    assert response.status_code == 403


def test_untrusted_peer_cannot_supply_forwarded_client(local_app):
    with TestClient(local_app, client=('203.0.113.5', 50000)) as client:
        response = client.get('/api/config', headers={'x-forwarded-for': '127.0.0.1'})
    assert response.status_code == 403
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_security.py -v`

Expected: forged Host and cross-origin report POST are accepted before the fix.

- [ ] **Step 3: Implement peer-address authorization and same-origin mutation checks**

Use `request.client.host` for direct connections. Accept one forwarded address only when the peer belongs to `TRUSTED_PROXY_NETWORK`. Reject malformed or multi-hop values. For `POST`, `PUT`, `PATCH`, and `DELETE`, compare parsed scheme/host/port of `Origin` to the effective request origin. Reject `Origin: null` and cross-origin values.

```python
if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
    origin = request.headers.get('origin')
    if origin is not None and normalized_origin(origin) != effective_origin(request, settings):
        return JSONResponse({'detail': 'Запрос с другого источника отклонён.'}, 403)
```

Return only identity/role/status from director attendance, never rate or payable.

- [ ] **Step 4: Run security and full tests**

Run: `build/venv/bin/python -m pytest tests/test_security.py tests/test_api.py tests/test_director_api.py -v`

Run: `build/venv/bin/python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add retro/security.py retro/config.py retro/app.py retro/modules/director/routes.py tests/test_security.py tests/test_api.py tests/test_director_api.py
git commit -m "fix: enforce trusted request boundaries"
```

### Task 7: Director data correctness and bounded report storage

**Files:**
- Modify: `retro/integrations/gemini.py`
- Modify: `retro/modules/director/models.py`
- Modify: `retro/modules/director/store.py`
- Modify: `retro/modules/director/service.py`
- Modify: `retro/modules/director/routes.py`
- Test: `tests/test_director.py`
- Test: `tests/test_director_api.py`
- Test: `tests/test_director_store.py`

**Interfaces:**
- `GeminiClient.analyze()` authenticates with `x-goog-api-key`.
- `build_snapshot()` raises `DataError` for any included category absent from the configured map.
- `DirectorReportStore.create_or_replace(snapshot, analysis, pdf, created_at, retention) -> str` enforces one period record and retention.
- `DirectorReportStore.list_metadata() -> list[dict]` omits full snapshots and PDF bodies.

- [ ] **Step 1: Add failing key-header and category tests**

```python
def test_gemini_key_is_header_not_query(settings):
    def handler(request):
        assert request.headers['x-goog-api-key'] == 'secret'
        assert 'key=' not in str(request.url)
        return httpx.Response(200, json=VALID_GEMINI_RESPONSE)
    asyncio.run(GeminiClient(settings, transport=httpx.MockTransport(handler)).analyze(snapshot()))


def test_unmapped_director_category_fails_closed():
    with pytest.raises(DataError, match='категор'):
        build_snapshot([director_row(category='Упаковка')], {'Основное меню': 'menu'}, set(), PERIOD)
```

- [ ] **Step 2: Add failing duplicate-period and retention tests**

```python
def test_same_period_replaces_without_duplicate(tmp_path):
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    first = store.create_or_replace(snapshot('2026-09-01', '2026-09-10'), analysis('one'), b'one', CREATED, 10)
    second = store.create_or_replace(snapshot('2026-09-01', '2026-09-10'), analysis('two'), b'two', CREATED, 10)
    assert second == first
    assert len(store.list_metadata()) == 1
    assert store.get(first)['analysis']['summary'] == 'two'


def test_report_retention_keeps_newest_periods(tmp_path):
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    for index in range(3):
        store.create_or_replace(snapshot_for(index), analysis(str(index)), str(index).encode(), created(index), 2)
    assert [row['analysis_summary'] for row in store.list_metadata()] == ['2', '1']
```

- [ ] **Step 3: Run focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_director.py tests/test_director_store.py -v`

Expected: URL-key, category-default, duplicate, and unbounded-retention assertions fail.

- [ ] **Step 4: Implement header authentication, fail-closed categories, unique periods, and metadata listing**

Add a unique index on `(period_start, period_end)`. Upsert report content inside `BEGIN IMMEDIATE`, then prune IDs outside the newest retention window. Return metadata with `id`, timestamps, period, `analysis_summary`, and `pdf_sha256` only.

Run: `build/venv/bin/python -m pytest tests/test_director.py tests/test_director_api.py tests/test_director_store.py -v`

Expected: all director tests pass.

- [ ] **Step 5: Commit**

```bash
git add retro/integrations/gemini.py retro/modules/director/models.py retro/modules/director/store.py retro/modules/director/service.py retro/modules/director/routes.py tests/test_director.py tests/test_director_api.py tests/test_director_store.py
git commit -m "fix: harden director reports"
```

### Task 8: Cashier and dashboard frontend correctness

**Files:**
- Modify: `retro/static/app.js`
- Modify: `retro/static/accountant.js`
- Modify: `retro/static/director.js`
- Modify: `retro/static/founder.js`
- Modify: `retro/static/founder.html`
- Modify: `retro/static/founder.css`
- Modify: `retro/static/employees.js`
- Modify: `retro/static/employees.html`
- Create: `retro/static/frontend-state.js`
- Create: `tests/js/frontend-state.test.mjs`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces pure JS helpers exported through `globalThis.RetroState` for response parsing and request-state decisions.
- Founder renders an accessible series table under the charts.
- All async loaders release busy state for the current request in `finally`.
- Employee, monthly-payroll, handover, and journal rows expose visible native edit/delete buttons; double-click is optional only.

- [ ] **Step 1: Add failing JavaScript state tests**

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
import '../../retro/static/frontend-state.js';

test('successful JSON response returns its amount', async () => {
  const response = new Response(JSON.stringify({amount:'125'}), {status:200});
  assert.equal((await globalThis.RetroState.responseJson(response)).amount, '125');
});

test('failed founder request clears previous analytics', () => {
  assert.deepEqual(globalThis.RetroState.analyticsAfterFailure({totals:{retro:'1'}}), null);
});

test('busy state clears only for current request', () => {
  assert.equal(globalThis.RetroState.shouldReleaseBusy(4, 4), true);
  assert.equal(globalThis.RetroState.shouldReleaseBusy(3, 4), false);
});
```

- [ ] **Step 2: Run JS tests and verify RED**

Run: `node --test tests/js/frontend-state.test.mjs`

Expected: module-not-found failure because `frontend-state.js` is absent.

- [ ] **Step 3: Implement response parsing and robust async state**

Create `retro/static/frontend-state.js`, include it before page scripts, and use it from USD GET/POST. Clear founder iiko results immediately when a load begins and retain an explicit error state on failure. Move accountant busy cleanup into `finally` guarded by its request sequence. Catch director list refresh errors separately from successful report creation.

```javascript
async function responseJson(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить запрос.');
  return data;
}
```

- [ ] **Step 4: Add accessible time-series fallback and verify**

Render a table with period, Retro, school, banquet, selected total, each payment method, bookings, guests, and cancellations. Keep it visually compact but available to screen readers and keyboard users. Explicitly format director timestamps with `timeZone: 'Asia/Tashkent'`. Render native `button` controls labelled `Изменить` and `Удалить` for editable accountant and employee rows and reuse the guarded mutation APIs; no operation may require a pointer-only double click.

Run: `node --test tests/js/frontend-state.test.mjs`

Run: `find retro/static -name '*.js' -print0 | xargs -0 -n1 node --check`

Run: `build/venv/bin/python -m pytest tests/test_api.py tests/test_founder_api.py -v`

Expected: all checks pass.

- [ ] **Step 5: Commit**

```bash
git add retro/static/app.js retro/static/accountant.js retro/static/director.js retro/static/founder.js retro/static/founder.html retro/static/founder.css retro/static/employees.js retro/static/employees.html retro/static/frontend-state.js tests/js/frontend-state.test.mjs tests/test_api.py
git commit -m "fix: make dashboard state recoverable"
```

### Task 9: Persisted cashier salary policy

**Files:**
- Modify: `retro/modules/cashier/expenses.py`
- Modify: `retro/modules/cashier/routes.py`
- Modify: `retro/static/index.html`
- Modify: `retro/static/app.js`
- Modify: `scripts/retro_data.py`
- Test: `tests/test_expenses.py`
- Test: `tests/test_expenses_api.py`

**Interfaces:**
- Removes implicit `DAILY_SALARY` insertion from `ExpenseStore.list()`.
- Adds explicit migration command `seed-cashier-expense --from YYYY-MM-DD --to YYYY-MM-DD --description Зарплата --amount 350000` with duplicate detection.
- Cashier day API includes `expense_policy_configured: bool` until the historical policy is explicitly installed.

- [ ] **Step 1: Add failing non-synthetic history tests**

```python
def test_empty_day_has_no_implicit_salary(tmp_path):
    store = ExpenseStore(tmp_path / 'cashier.sqlite3')
    assert store.list(DAY) == []
    assert store.total(DAY) == Decimal('0')


def test_salary_seed_is_explicit_and_idempotent(tmp_path):
    database = tmp_path / 'cashier.sqlite3'
    first = seed_cashier_expense(database, DAY, NEXT_DAY, 'Зарплата', Decimal('350000'))
    second = seed_cashier_expense(database, DAY, NEXT_DAY, 'Зарплата', Decimal('350000'))
    assert first.inserted == 2
    assert second.inserted == 0
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_expenses.py -k 'implicit or explicit' -v`

Expected: empty day still contains the synthetic salary and the seed API is absent.

- [ ] **Step 3: Remove runtime synthesis and implement explicit seeding**

Delete the implicit return branch that prepends `Expense(None, ..., automatic=True)`. Add a unique operation key or deterministic duplicate query for explicit seed rows. Show a warning when no policy marker exists; do not invent historical rows automatically.

- [ ] **Step 4: Run focused and full tests**

Run: `build/venv/bin/python -m pytest tests/test_expenses.py tests/test_expenses_api.py -v`

Run: `build/venv/bin/python -m pytest -q`

Expected: all tests pass with explicit expenses only.

- [ ] **Step 5: Commit**

```bash
git add retro/modules/cashier/expenses.py retro/modules/cashier/routes.py retro/static/index.html retro/static/app.js scripts/retro_data.py tests/test_expenses.py tests/test_expenses_api.py
git commit -m "fix: persist cashier salary expenses explicitly"
```

### Task 10: Logging, configuration, file safety, and CI

**Files:**
- Create: `retro/logging_config.py`
- Create: `scripts/check_runtime_permissions.py`
- Create: `scripts/check_tracked_secrets.py`
- Create: `.github/workflows/ci.yml`
- Modify: `.gitignore`
- Modify: `config.env.example`
- Modify: `README.md`
- Modify: `requirements-dev.txt`
- Modify: `retro/integrations/iiko.py`
- Modify: `retro/integrations/bookings.py`
- Modify: `retro/integrations/cbu.py`
- Modify: `retro/modules/cashier/routes.py`
- Modify: `retro/modules/accountant/routes.py`
- Modify: `retro/modules/director/routes.py`
- Modify: `retro/modules/founder/routes.py`
- Test: `tests/test_logging_safety.py`
- Test: `tests/test_runtime_permissions.py`

**Interfaces:**
- Produces: `configure_logging()` with structured safe context.
- Produces: `check_runtime_permissions(paths: Iterable[Path]) -> list[PermissionIssue]` without reading file contents.
- CI runs pytest, compileall, Node syntax/tests, `git diff --check`, and a tracked-secret filename/content guard.

- [ ] **Step 1: Add failing redaction and permission tests**

```python
def test_logged_upstream_error_excludes_secrets(caplog):
    log_upstream_failure('gemini', RuntimeError('failed'), request_id='req-1')
    text = caplog.text
    assert 'req-1' in text
    assert 'GEMINI_API_KEY' not in text
    assert 'Authorization' not in text


def test_permission_check_flags_group_or_world_readable_secret(tmp_path):
    secret = tmp_path / '.env'
    secret.write_text('KEY=secret')
    secret.chmod(0o644)
    assert check_runtime_permissions([secret])[0].expected_mode == 0o600
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `build/venv/bin/python -m pytest tests/test_logging_safety.py tests/test_runtime_permissions.py -v`

Expected: imports fail because logging and permission diagnostics do not exist.

- [ ] **Step 3: Implement safe logging and diagnostics**

Log adapter name, operation, duration, request ID, and exception class. Never log request headers, credentials, query strings containing secrets, personal records, or upstream bodies. The permission tool reports path, actual mode, and expected mode only. It exits nonzero when `.env` is broader than `0600`, data directories broader than `0700`, or SQLite/backup files broader than `0600`.

- [ ] **Step 4: Add CI and configuration documentation**

Use a pinned Python setup action and install `requirements-dev.txt`. CI commands:

```yaml
- run: python -m pytest -q
- run: python -m compileall -q retro scripts
- run: find retro/static -name '*.js' -print0 | xargs -0 -n1 node --check
- run: node --test tests/js/*.test.mjs
- run: git diff --check
- run: python scripts/check_tracked_secrets.py
```

Update ignore rules for `.env`, `.env.*` except `config.env.example`, `.venv/`, `*.sqlite3`, backup directories, PDFs, and generated reports. Document permanent service setup, health verification, backup schedule, restore rehearsal, shared data directory, authentication scope, trusted proxy behavior, and all environment keys.

- [ ] **Step 5: Run all operations checks and commit**

Run: `build/venv/bin/python -m pytest -q`

Run: `python3 -m compileall -q retro scripts`

Run: `find retro/static -name '*.js' -print0 | xargs -0 -n1 node --check`

Run: `node --test tests/js/*.test.mjs`

Run: `git diff --check`

Expected: all commands exit 0.

```bash
git add retro/logging_config.py scripts/check_runtime_permissions.py .github/workflows/ci.yml .gitignore config.env.example README.md requirements-dev.txt retro tests scripts/check_tracked_secrets.py
git commit -m "chore: add operational safety gates"
```

### Task 11: Full verification and real-data migration rehearsal

**Files:**
- Modify only if verification exposes a defect, and then return to the appropriate earlier TDD task.
- Create outside Git: a temporary rehearsal directory from `mktemp -d`.

**Interfaces:**
- Consumes all preceding tasks.
- Produces evidence that code, backup, migration, restore, pages, and data integrity work together.

- [ ] **Step 1: Run the full automated verification**

Run:

```bash
build/venv/bin/python -m pytest -q
python3 -m compileall -q retro scripts
find retro/static -name '*.js' -print0 | xargs -0 -n1 node --check
node --test tests/js/*.test.mjs
git diff --check
```

Expected: every command exits 0 with no failing tests.

- [ ] **Step 2: Rehearse backup and migration into a temporary destination**

Create a temporary directory with `mktemp -d`. Run `scripts/retro_data.py backup` against the real source paths in read-only mode, migrate the backup into the temporary destination, and run `verify` on all three resulting databases. Do not point the application or servers at this directory.

Expected: source hashes and mtimes remain unchanged; copied cashier/accountant databases report `integrity=ok`; the new director database reports `integrity=ok` and zero reports.

- [ ] **Step 3: Start an isolated smoke-test server**

Run the application on an unused loopback port with `RETRO_DATA_DIR` set to the rehearsal directory and no external binding. Verify `/`, `/accountant`, `/accountant/employees`, `/director`, `/founder`, and `/api/config` return 200 from loopback. Verify an external TestClient with forged Host receives 403 for finance paths.

Expected: all five pages render, no source database changes, and no unexpected traceback in logs.

- [ ] **Step 4: Request independent code review**

Provide the reviewer with the spec, plan, base commit `ec36683`, current HEAD, test output, and the explicit instruction to look for financial corruption, secret exposure, unsafe migrations, and divergence from T-347 behavior.

Expected: no unresolved Critical or Important findings.

- [ ] **Step 5: Prepare deployment and migration handoff**

Document the exact verified commands for backup, migration, permission correction, service restart, health check, and rollback. Do not execute the real migration, stop the live servers, push, merge, or deploy until the user explicitly approves that operational step.
