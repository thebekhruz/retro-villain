# Hikvision First Entry Integration Implementation Plan

**Goal:** Connect one Hikvision entrance terminal to Retro, link employees once by exact normalized name, and use the earliest real `employeeNo` event per Tashkent day across accountant, director, payroll draft, and XLSX.

**Architecture:** A read-only ISAPI adapter produces typed people and events. Focused SQLite storage owns deduplication, coverage, and first-entry state; an attendance service converts that state into business statuses. A lifespan-managed poller is isolated from the web process so a device failure cannot stop Retro.

**Tech Stack:** Python 3.14, FastAPI, httpx, SQLite, pytest, vanilla JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-21-hikvision-first-entry-design.md`

## Global constraints

- One entrance device only; direct ISAPI; no HikCentral or external webhook.
- Save only first entry per employee/day; never fetch or store photos or exits.
- Initial exact normalized-name binding; later event identity only by `employeeNo`.
- Never turn incomplete source coverage into `missing` or zero payable.
- Real credentials live only in `build/.env`; tests and docs use placeholders.
- Device interaction is read-only and bounded by timeout, response-size, and pagination limits.

### Task 1: Configuration, Digest transport, and ISAPI parsing

**Files:**
- Modify `retro/config.py`
- Create `retro/integrations/hikvision.py`
- Create `tests/test_hikvision_config.py`
- Create `tests/test_hikvision_isapi.py`

1. Add failing configuration tests for a fully configured device, all-or-none credentials, URL rejection (credentials/query/fragment/non-HTTP), interval/timeout ranges, and password redaction from `repr`.
2. Add `HikvisionConfig` to `Settings` with `HIKVISION_URL`, `HIKVISION_USER`, `HIKVISION_PASSWORD`, `HIKVISION_SOURCE`, `HIKVISION_POLL_SECONDS`, `HIKVISION_TIMEOUT_SECONDS`, and `HIKVISION_VERIFY_TLS`. Absence of all three connection values disables the integration; partial configuration fails fast.
3. Add failing pure-unit tests for Digest challenge parsing/header generation, including `qop="auth,auth-int"`, request targets containing `?format=json`, nonce count, and opaque.
4. Implement the Digest helper and a transport that first probes `GET /ISAPI/System/deviceInfo`, then sends replayable JSON POST bytes with explicit `Content-Length`. On 401 it accepts one refreshed challenge and retries once. Map failures to safe codes without including URL or credentials.
5. Add failing parser/pagination tests for `UserInfoSearch` and `AcsEvent`: `MORE`, `numOfMatches`, `major=5`, `minor=75`, non-empty `employeeNoString`/`serialNo`, valid event time, malformed page, and maximum pages/response size.
6. Implement typed `HikvisionPerson`, `HikvisionEvent`, `fetch_people()`, and `fetch_events(from_at, through_at)` without requesting `pictureURL`.
7. Run `pytest tests/test_hikvision_config.py tests/test_hikvision_isapi.py -q` and commit `T-348: add safe Hikvision ISAPI client`.

### Task 2: Linkage, persistence, and attendance service

**Files:**
- Create `retro/modules/accountant/hikvision.py`
- Modify `retro/modules/accountant/roster.py`
- Modify `retro/modules/accountant/payroll.py`
- Create `tests/test_hikvision_attendance.py`
- Modify `tests/test_accountant_roster.py`
- Modify `tests/test_accountant_payroll.py`

1. Add failing tests for normalization (trim, casefold, collapsed whitespace), two-sided uniqueness, already-linked employees, duplicate Retro names, duplicate Hikvision names, empty values, and unique-ID conflicts.
2. Add `RosterStore.link_hikvision_people(people)` that writes only empty `hikvision_id` fields, never overwrites a binding, and returns counts for linked/already-linked/ambiguous/unmatched.
3. Add failing SQLite tests for schema creation, minimal event dedup by `(source, serial_no)`, earliest-entry upsert by `(work_day, employee_id)`, unknown employee numbers, timezone conversion, and persistent sync state.
4. Implement `AttendanceStore` in the accountant database with `hikvision_events`, `hikvision_first_entries`, and `hikvision_sync_state`. Event insert and earliest-entry upsert share one transaction.
5. Add failing service tests for `on_time`, `late`, `unlinked`, `missing`, and `unavailable`; exact 10:00 is on time; current or incompletely covered days do not become missing.
6. Implement `AttendanceService.snapshot(day, roster, now)` and change `draft_payroll` to consume explicit attendance rows. Add `unavailable` handling to `compute_pay` so it never returns zero.
7. Run the focused tests and commit `T-348: persist and classify first employee entries`.

### Task 3: Recoverable poller and application lifecycle

**Files:**
- Create `retro/integrations/hikvision_poller.py`
- Modify `retro/app.py`
- Create `tests/test_hikvision_poller.py`

1. Add failing tests for initial backfill from previous Tashkent midnight, resume with overlap, cursor advancement only after complete fetch, failure state recording, retry after failure, and clean stop.
2. Implement `HikvisionPoller.run_once()`: record attempt; fetch/link people when unlinked roster rows exist; fetch events; ingest; then atomically record coverage/cursor/success. Categorize failures and keep the old cursor.
3. Add a bounded asynchronous loop with immediate first run, configured interval, exponential retry capped at the regular interval, and cooperative cancellation.
4. Wire it through FastAPI lifespan. `create_app` accepts an injected Hikvision client/poller for tests; unconfigured mode creates no network task. Shutdown closes the client.
5. Run poller/lifecycle tests plus security tests and commit `T-348: run Hikvision polling without blocking Retro`.

### Task 4: Replace demo attendance in APIs, payroll, and export

**Files:**
- Modify `retro/modules/accountant/routes.py`
- Modify `retro/modules/director/routes.py`
- Modify `retro/modules/accountant/attendance.py`
- Modify `retro/modules/accountant/employee_export.py`
- Modify `tests/test_accountant_api.py`
- Modify `tests/test_attendance.py`
- Modify `tests/test_security.py`

1. Add failing API tests proving accountant and director return the same stored entries and health state, director omits pay/rate, and unconfigured/unavailable does not claim absence.
2. Replace every route-level `draft_payroll` call with attendance from the shared service. Preserve explicit exceptions and confirmed-ledger behavior.
3. Return `demo=false` for configured live attendance and include only safe health fields: status, last success, covered range, and requested-day completeness.
4. Feed real first entries into both entrance and employee XLSX exports; filenames and text lose the `DEMO` suffix only for live data.
5. Verify auth boundaries and local-only finance protection remain unchanged.
6. Run API/export/security tests and commit `T-348: expose real Hikvision attendance safely`.

### Task 5: UI states, operations, and full verification

**Files:**
- Modify `retro/static/accountant.html`
- Modify `retro/static/accountant.js`
- Modify `retro/static/director.html`
- Modify `retro/static/director.js`
- Modify `config.env.example`
- Modify `README.md`
- Modify or create relevant JS/frontend tests

1. Add failing frontend assertions for live, not-configured, unavailable, stale, and incomplete states; ensure no state renders unavailable people as absent.
2. Replace hard-coded demo labels with API health copy and show last successful poll. Keep the employee/late lists mobile-safe and accessible.
3. Document placeholder environment variables, public-port IP allowlisting, HTTPS preference, read-only linkage, health verification, rollback by disabling the config, and a no-secret live-test checklist.
4. Run all Python tests, JS tests, `compileall`, `node --check`, secret scanning, and `git diff --check`.
5. With credentials present locally and allowlisting confirmed, run a read-only device check: device info, person count/link report, event fetch, and one observed first entry within 60 seconds. Do not capture raw payloads.
6. Update task evidence, commit `T-348: finish Hikvision attendance integration`, and push only the T-348 branches after the complete verification is green.
