# Director Mobile Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the third, mobile-first director module that turns a verified ten-day iiko sales snapshot into retained Claude recommendations and a downloadable PDF.

**Architecture:** A focused `director` module owns normalized sales metrics, immutable SQLite reports, report PDF generation, and HTTP routes. `IikoClient` gains a read-only ten-day detail query; it returns a typed snapshot only after category, revenue-direction, and completeness validation. The server calculates every metric before a minimal aggregate is sent to Claude, then persists the validated result and its exact PDF.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, httpx, SQLite, ReportLab 5.0.1, static HTML/CSS/JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-09-18-director-mobile-analytics-design.md`

## Global Constraints

- Work only in `projects/retro-app`; preserve the unrelated dirty accountant files and `.superpowers/` directory.
- Use `Asia/Tashkent`; a generated report covers exactly the ten completed calendar days before today.
- iiko is read-only, uses only the configured Retro Milliy origin, and keeps the existing deleted/void/non-business exclusions.
- Include only iiko categories configured as `menu`, `dessert`, or `drink`; reject unknown or missing categories rather than guessing.
- Retro and Oxbridge form the cash total; Yandex is a payment channel that may overlap either direction and is never added again to that total.
- Report generation fails atomically: no zero substitution, no partial report, no mutable historical report.
- Claude receives only aggregate business metrics; no iiko credentials, guest data, raw checks, or order identifiers leave the server.
- Hikvision and dangerous-operation panels must say `Интеграция ожидается` / `Будет добавлено`; never render fictitious counts or an empty list as a clean audit.
- PDF is the only v1 download format and is served only from an already persisted report.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `retro/config.py` | Validate and expose Claude and iiko category configuration without logging secrets. |
| `retro/integrations/iiko.py` | Fetch and validate daily detailed OLAP rows, then compose a ten-day `DirectorSnapshot`. |
| `retro/integrations/claude.py` | Call the Anthropic Messages API with a safe aggregate and parse validated JSON. |
| `retro/modules/director/models.py` | Immutable domain types, direction/category rules, metric calculations, JSON serialization. |
| `retro/modules/director/store.py` | SQLite schema and immutable report persistence. |
| `retro/modules/director/pdf.py` | Render the exact persisted report to a ReportLab PDF. |
| `retro/modules/director/service.py` | Orchestrate snapshot → Claude → PDF → one atomic stored report. |
| `retro/modules/director/routes.py` | Director API routes and status-to-HTTP translation. |
| `retro/static/director.html`, `director.css`, `director.js` | Accessible 320 px-first director UI and history/report interactions. |
| `tests/test_director_metrics.py` | Pure business-rule and aggregation coverage. |
| `tests/test_director_iiko.py` | iiko OLAP contract, field completeness, direction/category validation. |
| `tests/test_director_claude.py` | Claude request privacy and response-schema coverage. |
| `tests/test_director_store_pdf.py` | Immutability, BLOB PDF, checksum, and PDF content coverage. |
| `tests/test_director_api.py` | Route, locking, auth, UI serving, and end-to-end fixture coverage. |

### Task 1: Configuration and dependency boundary

**Files:**
- Modify: `requirements.txt`
- Modify: `config.env.example`
- Modify: `retro/config.py`
- Test: `tests/test_director_api.py`

**Interfaces:**
- Produces `Settings.claude_configured: bool`, `Settings.claude_api_key`, `Settings.claude_model`, and `Settings.director_categories: dict[str, str]`.

- [ ] **Step 1: Write the failing configuration and app-state tests**

```python
def test_director_config_requires_key_model_and_category_map(monkeypatch):
    monkeypatch.setenv('CLAUDE_API_KEY', 'key')
    monkeypatch.setenv('CLAUDE_MODEL', 'claude-test')
    monkeypatch.setenv('IIKO_DIRECTOR_CATEGORIES',
                       'Основное меню=menu;Десерты=dessert;Напитки=drink')
    settings = Settings.from_env()
    assert settings.claude_configured is True
    assert settings.director_categories['Десерты'] == 'dessert'

```

- [ ] **Step 2: Run the tests to verify the failures**

Run: `pytest tests/test_director_api.py -q`

Expected: FAIL because `Settings` does not expose the director values.

- [ ] **Step 3: Add the minimal configuration wiring**

Add exactly `reportlab==5.0.1` to `requirements.txt`. Add blank `CLAUDE_API_KEY`, `CLAUDE_MODEL`, and a documented `IIKO_DIRECTOR_CATEGORIES=Основное меню=menu;Десерты=dessert;Напитки=drink` to `config.env.example`.

Parse the category variable into a non-empty mapping whose values are exactly `menu`, `dessert`, or `drink`; reject blank names, repeated names, malformed pairs, and other values with `ValueError`. Store secret values with `repr=False`. Do not instantiate the director service before its dependencies exist; Task 6 adds its injected database and transport arguments.

- [ ] **Step 4: Run the configuration tests**

Run: `pytest tests/test_director_api.py -q`

Expected: PASS for configuration parsing.

- [ ] **Step 5: Commit the boundary**

```bash
git add requirements.txt config.env.example retro/config.py tests/test_director_api.py
git commit -m "feat: configure director analytics module"
```

### Task 2: Typed sales rows, directions, and deterministic metrics

**Files:**
- Create: `retro/modules/director/__init__.py`
- Create: `retro/modules/director/models.py`
- Test: `tests/test_director_metrics.py`

**Interfaces:**
- Produces `SalesRow(day: date, register: str, section: str, payment_type: str, item: str, category: str, quantity: Decimal, revenue: Decimal, cost: Decimal, waiter: str, order_id: str)`.
- Produces `build_snapshot(rows: Iterable[SalesRow], categories: Mapping[str, str]) -> DirectorSnapshot` and `completed_period(today: date) -> tuple[date, date]`.
- Produces `DirectorSnapshot.metrics`, with total cash, overlapping Yandex channel, per-direction item metrics, and waiter metrics.

- [ ] **Step 1: Write the failing metric tests**

```python
def test_yandex_overlaps_cash_direction_without_double_counting():
    snapshot = build_snapshot([sale('Kassa-FiscalBox1', 'Ресторан', 'Яндекс Еда',
                                   'Плов', 'Основное меню', '2', '200000', '80000', 'Олег', 'o-1')], CATEGORIES)
    assert snapshot.cash_total == Decimal('200000')
    assert snapshot.yandex_revenue == Decimal('200000')
    assert snapshot.item_metrics['all']['Плов'].margin_percent == Decimal('60.00')

def test_unknown_category_and_missing_waiter_are_rejected():
    with pytest.raises(DataError, match='категория'):
        build_snapshot([sale(category='Сувениры')], CATEGORIES)
    with pytest.raises(DataError, match='официант'):
        build_snapshot([sale(waiter='')], CATEGORIES)
```

- [ ] **Step 2: Run the test file to verify it fails**

Run: `pytest tests/test_director_metrics.py -q`

Expected: FAIL because the director package and types do not exist.

- [ ] **Step 3: Implement the pure model layer**

Define constants from the existing cashier rules: `RETRO_REGISTER`, `SCHOOL_REGISTER`, `BANQUET_SECTION`, and normalized `Яндех Еда → Яндекс Еда`. Classify `Kassa-FiscalBox1` outside the banquet as `retro`, `GL-Kassa-Oksbrich` as `oxbridge`, and reject every other register or a new `бехруз` section. Calculate `gross_profit = revenue - cost` and percentage rounded to `Decimal('0.01')`; represent zero-revenue percentage as `None`, never `0`.

Group item and waiter metrics into `all`, `retro`, `oxbridge`, and `yandex`. Keep Yandex as a channel grouping and calculate `cash_total` from only Retro + Oxbridge. Require ten distinct dates, each inside one inclusive supplied period, non-negative finite quantities/cost/revenue, nonblank names, and unique `(order_id, item, waiter)` input rows.

- [ ] **Step 4: Run focused and full pure tests**

Run: `pytest tests/test_director_metrics.py tests/test_revenue_scope.py -q`

Expected: PASS, including the existing cashier three-way classification.

- [ ] **Step 5: Commit the metric layer**

```bash
git add retro/modules/director/__init__.py retro/modules/director/models.py tests/test_director_metrics.py
git commit -m "feat: calculate director menu and waiter metrics"
```

### Task 3: iiko detail query and ten-day snapshot

**Files:**
- Modify: `retro/integrations/iiko.py`
- Test: `tests/test_director_iiko.py`

**Interfaces:**
- Consumes `Settings.director_categories` and Task 2 `SalesRow`, `DirectorSnapshot`, `completed_period`.
- Produces `async IikoClient.load_director_report(today: date) -> DirectorSnapshot`.

- [ ] **Step 1: Write the failing HTTP contract tests**

```python
async def test_director_query_requests_detail_dimensions_for_each_completed_day(source):
    snapshot = await source.load_director_report(date(2026, 9, 18))
    assert snapshot.period_start == date(2026, 9, 8)
    assert snapshot.period_end == date(2026, 9, 17)
    assert all(body['includeVoidTransactions'] is False for body in source.request_bodies)
    assert {'CashRegisterName', 'RestaurantSection', 'PayTypes', 'DishName',
            'DishCategory', 'WaiterName', 'UniqOrderId'} <= set(source.request_bodies[0]['groupFields'])

async def test_director_query_rejects_incomplete_iiko_detail(source):
    source.remove_field('WaiterName')
    with pytest.raises(DataError, match='официант'):
        await source.load_director_report(date(2026, 9, 18))
```

- [ ] **Step 2: Run the iiko detail tests to verify they fail**

Run: `pytest tests/test_director_iiko.py -q`

Expected: FAIL because `load_director_report` is absent.

- [ ] **Step 3: Add the query and typed row adapter**

Refactor `_olap` to accept `date_from` and `date_to` while retaining the existing single-day `load` behavior. Add a private `_director_rows(client, day)` that sends one `SALES` OLAP request per completed day with groups `CashRegisterName`, `RestaurantSection`, `PayTypes`, `DishName`, `DishCategory`, `WaiterName`, `UniqOrderId` and fields `DishAmountInt`, `DishDiscountSumInt`, `DishCostSum`. Translate each returned grouped row into `SalesRow`; any unexpected nested shape, missing field, nonfinite amount, unknown direction, unknown category, or missing waiter raises `DataError`.

Authenticate once per ten-day load, retain the existing safe errors for authentication/network/poll expiry, and call `build_snapshot` only after exactly ten day snapshots are present. Do not write raw iiko response bodies to disk or logs.

- [ ] **Step 4: Run iiko and existing integration tests**

Run: `pytest tests/test_director_iiko.py tests/test_api.py tests/test_revenue_scope.py -q`

Expected: PASS. Existing cashier queries still use their original fields and result shape.

- [ ] **Step 5: Commit the iiko extension**

```bash
git add retro/integrations/iiko.py tests/test_director_iiko.py
git commit -m "feat: load verified iiko director snapshot"
```

### Task 4: Claude boundary and validated recommendation JSON

**Files:**
- Create: `retro/integrations/claude.py`
- Test: `tests/test_director_claude.py`

**Interfaces:**
- Consumes `DirectorSnapshot` from Task 2 and `Settings.claude_api_key`, `Settings.claude_model`.
- Produces `async ClaudeClient.analyze(snapshot: DirectorSnapshot) -> DirectorAnalysis`.
- Produces `DirectorAnalysis(summary: str, problems: tuple[Problem, ...])`, where `Problem` has `subject`, `direction`, `metrics`, `reason`, `priority`, and `action`.

- [ ] **Step 1: Write failing privacy and schema tests**

```python
async def test_claude_receives_only_aggregate_and_returns_validated_actions(client, snapshot):
    analysis = await client.analyze(snapshot)
    payload = client.transport.last_json
    text = json.dumps(payload, ensure_ascii=False)
    assert 'order_id' not in text and 'o-1' not in text and 'secret' not in text
    assert analysis.problems[0].action == 'promote'

async def test_claude_rejects_more_than_ten_or_unknown_action(client, snapshot):
    client.transport.reply({'summary': 'x', 'problems': [{'action': 'fire'}]})
    with pytest.raises(DataError, match='Claude'):
        await client.analyze(snapshot)
```

- [ ] **Step 2: Run the Claude tests to verify failure**

Run: `pytest tests/test_director_claude.py -q`

Expected: FAIL because `ClaudeClient` and `DirectorAnalysis` are absent.

- [ ] **Step 3: Implement one bounded Messages API client**

Use existing `httpx.AsyncClient` to POST `https://api.anthropic.com/v1/messages` with `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`, the configured model, `max_tokens: 2400`, and a non-streaming Russian system prompt. Request a single JSON object in a fenced-free response. The request body contains only serialized aggregate item/waiter totals and the report period.

Parse the assistant text as JSON and validate: nonempty summary at most 900 characters; 1–10 unique problems; `direction` in `all|retro|oxbridge|yandex`; `priority` in `high|medium|low`; `action` in `remove|replace|promote|review`; nonempty subject/reason; and numeric values only for supplied metrics. Convert every upstream timeout, non-2xx response, malformed content block, and schema failure to a safe `DataError` that does not expose Anthropic response text.

- [ ] **Step 4: Run Claude tests**

Run: `pytest tests/test_director_claude.py -q`

Expected: PASS, including absence of forbidden raw data from outbound payloads.

- [ ] **Step 5: Commit the Claude adapter**

```bash
git add retro/integrations/claude.py retro/modules/director/models.py tests/test_director_claude.py
git commit -m "feat: add validated Claude menu recommendations"
```

### Task 5: Immutable report storage and deterministic PDF

**Files:**
- Create: `retro/modules/director/store.py`
- Create: `retro/modules/director/pdf.py`
- Test: `tests/test_director_store_pdf.py`

**Interfaces:**
- Consumes Task 2 `DirectorSnapshot` and Task 4 `DirectorAnalysis`.
- Produces `DirectorReportStore.create(snapshot, analysis, pdf: bytes) -> StoredReport`, `list() -> list[StoredReport]`, `get(report_id: str) -> StoredReport`, `get_pdf(report_id: str) -> bytes`.
- Produces `render_report_pdf(snapshot: DirectorSnapshot, analysis: DirectorAnalysis, created_at: datetime) -> bytes`.

- [ ] **Step 1: Write failing store/PDF tests**

```python
def test_saved_report_keeps_exact_snapshot_pdf_and_checksum(tmp_path, snapshot, analysis):
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    pdf = render_report_pdf(snapshot, analysis, FIXED_TIME)
    saved = store.create(snapshot, analysis, pdf, FIXED_TIME)
    assert store.get(saved.id).snapshot_json == snapshot.json()
    assert store.get_pdf(saved.id) == pdf
    assert hashlib.sha256(pdf).hexdigest() == saved.pdf_sha256

def test_report_rows_cannot_be_updated_or_deleted(tmp_path, snapshot, analysis):
    store = DirectorReportStore(tmp_path / 'director.sqlite3')
    report = store.create(snapshot, analysis, b'%PDF-1.4', FIXED_TIME)
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute('DELETE FROM director_reports WHERE id=?', (report.id,))
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `pytest tests/test_director_store_pdf.py -q`

Expected: FAIL because the director store and PDF renderer do not exist.

- [ ] **Step 3: Implement append-only SQLite and PDF rendering**

Create `director_reports` with `id TEXT PRIMARY KEY`, `created_at TEXT NOT NULL`, `period_start TEXT NOT NULL`, `period_end TEXT NOT NULL`, `snapshot_json TEXT NOT NULL`, `analysis_json TEXT NOT NULL`, `pdf_sha256 TEXT NOT NULL`, and `pdf BLOB NOT NULL`. Enable foreign keys, use a transaction for each insert, and install SQLite triggers rejecting `UPDATE` and `DELETE`. Serialize decimals as strings and JSON with stable sorting.

Render an A4 Russian-language ReportLab document from the supplied immutable values: title, period, generated time, source iiko, total cash, Retro/Oxbridge/Yandex cards, top-10 table, waiter table, methodology, and the statement that Claude recommendations require director judgment. Register a bundled DejaVu Sans font if available; otherwise make a missing-font startup error explicit instead of producing unreadable Cyrillic. Escape every variable before adding a paragraph.

- [ ] **Step 4: Run persistence/PDF tests**

Run: `pytest tests/test_director_store_pdf.py -q`

Expected: PASS, and the test confirms a PDF starts with `%PDF-` and contains a nonempty immutable BLOB.

- [ ] **Step 5: Commit storage and PDF**

```bash
git add retro/modules/director/store.py retro/modules/director/pdf.py tests/test_director_store_pdf.py
git commit -m "feat: persist director reports with PDF"
```

### Task 6: Report orchestration and HTTP API

**Files:**
- Create: `retro/modules/director/service.py`
- Create: `retro/modules/director/routes.py`
- Modify: `retro/app.py`
- Test: `tests/test_director_api.py`

**Interfaces:**
- Consumes `IikoClient.load_director_report`, `ClaudeClient.analyze`, `DirectorReportStore`, `render_report_pdf`.
- Produces `DirectorService.generate(today: date) -> StoredReport`, application state `director_store`, `director_service`, `director_lock: asyncio.Lock`, and endpoints defined in the spec.

- [ ] **Step 1: Write failing endpoint and atomicity tests**

```python
def test_generate_list_get_and_download_director_report(app, client):
    created = client.post('/api/director/reports')
    assert created.status_code == 201
    report_id = created.json()['id']
    assert client.get('/api/director/reports').json()['reports'][0]['id'] == report_id
    assert client.get(f'/api/director/reports/{report_id}/pdf').headers['content-type'].startswith('application/pdf')

def test_claude_failure_persists_no_report(app, client):
    app.state.claude.transport.fail_timeout = True
    assert client.post('/api/director/reports').status_code == 503
    assert client.get('/api/director/reports').json()['reports'] == []
```

- [ ] **Step 2: Run the API tests to verify failure**

Run: `pytest tests/test_director_api.py -q`

Expected: FAIL because director endpoints do not exist.

- [ ] **Step 3: Implement orchestration and routes**

Extend `create_app(..., director_db_path=None, claude_transport=None)` to initialize `DirectorReportStore`, `ClaudeClient`, `DirectorService`, and `asyncio.Lock` on application state, allowing each test to use an isolated SQLite file and mock Claude transport. `DirectorService.generate` first rejects absent iiko or Claude configuration with an actionable `DataError`; then loads the verified snapshot, obtains validated analysis, renders PDF, and inserts once. Do not insert before all three stages have succeeded. `POST /api/director/reports` acquires `app.state.director_lock` non-blockingly and returns `429` with `Другой анализ уже формируется.` if locked; wrap iiko and Claude calls with 120-second limits and return `503`/`504` safe errors.

Implement `GET /api/director/today` with the existing iiko lock and direction totals, `GET /api/director/reports`, `GET /api/director/reports/{id}`, and PDF `GET` with a UUID path pattern. Return `404` for unknown IDs. Use `Content-Disposition: attachment; filename="Retro-director-YYYY-MM-DD.pdf"`, never regenerate a PDF on download. Register `/director` and `director_router` in `create_app`, list module `director` as available in `/api/config`, and keep all existing middleware protections.

- [ ] **Step 4: Run API and security regression tests**

Run: `pytest tests/test_director_api.py tests/test_api.py -q`

Expected: PASS. Network clients without configured Basic credentials still receive `401`/`403`.

- [ ] **Step 5: Commit routes and orchestration**

```bash
git add retro/modules/director/service.py retro/modules/director/routes.py retro/app.py tests/test_director_api.py
git commit -m "feat: expose director report API"
```

### Task 7: Mobile-first director interface

**Files:**
- Create: `retro/static/director.html`
- Create: `retro/static/director.css`
- Create: `retro/static/director.js`
- Modify: `retro/static/index.html`
- Modify: `retro/static/accountant.html`
- Modify: `retro/static/style.css`
- Test: `tests/test_director_api.py`

**Interfaces:**
- Consumes `/api/director/today`, `/api/director/reports`, `/api/director/reports/{id}`, and PDF download URLs from Task 6.
- Produces the `/director` mobile navigation and report-history interaction.

- [ ] **Step 1: Write UI-serving and required-copy tests**

```python
def test_director_page_has_mobile_actions_and_honest_waiting_states(client):
    page = client.get('/director').text
    assert 'Сформировать отчёт' in page
    assert 'Интеграция ожидается' in page
    assert 'Будет добавлено' in page
    assert 'viewport' in page
```

- [ ] **Step 2: Run the UI test to verify failure**

Run: `pytest tests/test_director_api.py::test_director_page_has_mobile_actions_and_honest_waiting_states -q`

Expected: FAIL because the director static document is absent.

- [ ] **Step 3: Build the screen without unsafe HTML insertion**

Create semantic `main`, labelled sections, live status region, and buttons with 44 px minimum touch targets. Start with the report action card; then render cash totals, two status-only team cards, a dangerous-operation status card, latest report, and history. Include `director.css` after shared `style.css`; use a single column under 720 px and no horizontal overflow at 320 px.

In `director.js`, use `textContent`, `replaceChildren`, `Intl.NumberFormat('ru-RU')`, abort stale fetches, disable generation while in flight, and reload history after success. Display API failures in `role="alert"`; no direct `innerHTML`, no secret/config endpoints, and no client-side computation of business metrics. Add a Director navigation link to both existing module pages and mark the current module with `aria-current="page"`.

- [ ] **Step 4: Run the UI/API test and manual narrow-screen check**

Run: `pytest tests/test_director_api.py -q`

Expected: PASS.

Run: `build/venv/bin/python -m uvicorn retro.app:app --host 127.0.0.1 --port 8010 --no-proxy-headers`

Expected: `/director` loads locally; use browser responsive mode at 320 px to verify no horizontal overflow and all actions remain visible.

- [ ] **Step 5: Commit the interface**

```bash
git add retro/static/director.html retro/static/director.css retro/static/director.js retro/static/index.html retro/static/accountant.html retro/static/style.css tests/test_director_api.py
git commit -m "feat: add mobile director dashboard"
```

### Task 8: Full verification and operational documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-09-18-director-mobile-analytics-design.md`
- Test: all `tests/test_director_*.py`

**Interfaces:**
- Consumes the complete module from Tasks 1–7.
- Produces setup and backup instructions for a deployer.

- [ ] **Step 1: Write the final regression test for the full fixture path**

```python
def test_director_fixture_flow_is_immutable(client):
    created = client.post('/api/director/reports').json()
    first = client.get(f"/api/director/reports/{created['id']}").json()
    pdf = client.get(f"/api/director/reports/{created['id']}/pdf").content
    assert first['period'] == {'start': '2026-09-08', 'end': '2026-09-17'}
    assert hashlib.sha256(pdf).hexdigest() == first['pdf_sha256']
```

- [ ] **Step 2: Run the test to verify its expected first failure or validate the complete flow**

Run: `pytest tests/test_director_api.py::test_director_fixture_flow_is_immutable -q`

Expected: PASS after Tasks 1–7; if it fails, repair the specific contract before documentation.

- [ ] **Step 3: Document setup, backup, and known v1 boundaries**

Add README instructions for `CLAUDE_API_KEY`, `CLAUDE_MODEL`, `IIKO_DIRECTOR_CATEGORIES`, database backup of `build/director.sqlite3`, report generation behavior, and the requirement to keep external access protected. State explicitly that Hikvision and dangerous-operation integration are not yet connected, and that Yandex is an overlapping channel rather than a third cash-total summand. Update the spec only if an implementation-forced wording correction is needed; do not expand scope.

- [ ] **Step 4: Run complete verification**

Run: `pytest -q`

Expected: PASS with no skips introduced by this module.

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intended director/README/spec changes are staged or committed, while pre-existing accountant modifications remain untouched.

- [ ] **Step 5: Commit documentation and verification artifact**

```bash
git add README.md docs/superpowers/specs/2026-09-18-director-mobile-analytics-design.md tests/test_director_api.py
git commit -m "docs: document director analytics operations"
```
