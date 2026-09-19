# Retro Villain Hardening Design

**Date:** 2026-09-19

**Status:** Approved in chat

## Goal

Turn the current T-347 superset into one safe integration branch that preserves the existing cashier, accountant, director, founder, and booking functionality while fixing the confirmed financial-integrity, access-control, persistence, UI, and operations defects found in the repository audit.

## Scope and baseline

The implementation starts from commit `ec36683` (`task/T-347-booking-dashboard`) in an isolated branch. The running T-346 and T-347 worktrees and the dirty `codex/cashier-dashboard` checkout remain untouched.

The dirty accountant work is not copied wholesale. Its user-facing monthly-payroll and journal-editing behavior is ported selectively, with new tests and corrected invariants. Existing personal salary seed rows must not be committed to source code.

This hardening covers:

- cashier expenses, receipts, USD rates, and USD balances;
- accountant employees, accruals, salary payments, debts, movements, reserves, handovers, and monthly payroll;
- director reports, attendance, and Gemini analysis;
- founder iiko analytics and booking aggregates;
- local persistence, backup, restore verification, authentication boundaries, mutation protection, UI recovery, CI, and configuration documentation.

It does not add new restaurant features, connect real Hikvision attendance, merge PM tasks, deploy publicly, or delete any existing database.

## Chosen architecture

### Integration strategy

Use T-347 as the only implementation base because it contains the complete director, founder, and booking stack. Port required accountant changes as small tested commits. Do not patch the four existing branches independently.

Each defect is implemented with a red-green-refactor cycle. Financial fixes land before UI and operations work. The full suite must pass after every independently reviewable task.

### Shared data directory

Runtime data moves out of the checkout. `Settings` exposes a resolved `data_dir`:

1. `RETRO_DATA_DIR`, when explicitly configured;
2. otherwise `${XDG_DATA_HOME}/retro-villain`;
3. otherwise `~/.local/share/retro-villain`.

The application stores these files there:

- `cashier.sqlite3`;
- `accountant.sqlite3`;
- `director.sqlite3`.

Tests continue to inject temporary paths. Source assets and `build/.env` remain checkout-local; databases do not.

The migration command is explicit and idempotent. It accepts source paths, creates a timestamped backup directory, copies each source database through SQLite's backup API, runs `PRAGMA integrity_check` on the copies, and atomically installs only verified files. It refuses to overwrite a non-empty destination unless an explicit replacement option is supplied. Source databases and manual backups are never deleted.

For the first migration on this machine:

- cashier source: `projects/retro-app/build/cashier.sqlite3`;
- accountant source: `projects/retro-app/build/accountant-demo.sqlite3`;
- director destination: a new empty database, because all audited director databases contain zero reports.

Migration execution is a separate operational step after code verification. The implementation may prepare and dry-run it, but must not stop or repoint the currently running servers without an explicit deployment step.

### Financial invariants

An actually paid amount may never make available cash negative on its payment date or any already-known later date. If an expense total exceeds available cash, the unpaid portion is represented by `accountant_debts`; it is not represented as negative cash.

The following rules are enforced in the store, not only in the UI:

- salary payments cannot exceed their accrual across any number of partial payments or edits;
- expense and debt payments cannot exceed available cash;
- reserve withdrawals cannot exceed the reserve balance;
- a reserve transfer cannot be edited or deleted if doing so makes any later reserve balance negative;
- updates validate against the stored operation date; a client-supplied date must match it;
- every money field is a finite decimal within the existing upper bound and precision rules;
- deleting a salary payment operates only on `accountant_salary_payments`;
- linked debt payments and movements are updated or removed transactionally;
- a deliberately emptied monthly roster stays empty and is not silently reseeded.

The monthly employee table is initialized through an explicit import or migration path. Personal names and salary amounts are data, not Python constants. Existing dirty-checkout seed values may be imported from the current local database, but are not placed in Git.

Financial mutations gain an audit trail recording entity type, entity ID, action, timestamp, and before/after JSON. Audit rows are append-only through the application API. Existing roster audit data remains supported.

The synthetic cashier salary of `350000` is replaced by an explicit persisted operation during migration/configuration. Historical reports must not depend on a mutable code constant. Until that explicit entry exists, the application shows a configuration warning rather than silently inventing the expense.

### Access control and mutation protection

Local-only decisions use the peer address (`request.client.host`), never the `Host` header. The accountant pages/APIs and director attendance endpoint share the same local-finance boundary.

If a reverse proxy or tunnel is introduced, it is not trusted merely because the proxy connects from loopback. Public exposure requires configured dashboard authentication and an explicit trusted-proxy configuration. Network allowlists apply to the resolved trusted client address only when that proxy is explicitly configured.

All state-changing routes require same-origin validation. Requests with an `Origin` must match the request origin; browser form submissions without an acceptable origin are rejected for mutation endpoints. JSON mutations additionally retain their existing content-type/body validation. The no-body director report POST is covered.

The existing single dashboard credential remains the minimum authentication mechanism for this hardening pass. Role-based access is not introduced, but documentation must state that the credential grants all dashboard capabilities permitted by the network boundary.

Director attendance must not return pay rates or payable amounts outside the protected finance boundary. The director UI receives only the fields it displays: employee identity, role, and attendance status.

Gemini authentication uses the `x-goog-api-key` header instead of a URL query parameter. Server logs never print credentials, upstream response bodies, or authorization headers.

### Director reports and category safety

Director category mapping is fail-closed. Every included iiko category must be explicitly mapped to `menu`, `dessert`, or `drink`; excluded groups remain excluded. Unknown categories stop report generation with a safe error instead of defaulting to menu.

Only one stored report is allowed for a given period. Repeating generation for the same period returns or replaces the existing period record according to a single transactional operation, avoiding duplicate Gemini charges caused by repeated clicks.

Report storage has a configurable retention count with a conservative default. Pruning happens only after a new report has been stored successfully. The report list endpoint returns metadata and the short analysis summary, not every full snapshot. Full report data remains available from the detail endpoint.

### UI behavior

- USD balance GET and POST responses are parsed as JSON before reading `amount`.
- Founder analytics clear or mark prior iiko results stale at the start of every load. Failed requests cannot leave old metrics under new filters.
- Accountant busy state is released in `finally` for the current request generation.
- Employee, monthly-payroll, handover, and operation editing use visible focusable buttons and work with keyboard and touch input. Double-click may remain an optional shortcut, not the only control.
- Director report-list failures produce a visible retryable error and do not misreport an already-created report as failed.
- Founder time-series data has an accessible non-pointer representation, such as a compact table or list, while preserving the visual charts.
- Displayed timestamps explicitly use `Asia/Tashkent`.

### Operations and configuration

`config.env.example` documents Gemini, booking, `RETRO_DATA_DIR`, report retention, authentication, network, and trusted-proxy settings using placeholder values only. Root `.env`, virtual environments, database files, backups, and generated reports are ignored defensively.

The application creates new secret/data files with owner-only permissions where it controls creation. Documentation and a diagnostic command report unsafe existing permissions without printing contents.

Backup tooling uses SQLite's online backup API, writes into a timestamped directory, verifies every copied database, and produces a small manifest containing filenames, sizes, hashes, schema versions, and creation time. Restore defaults to verification-only and requires an explicit destination for installation.

Application logging records operation name, safe identifiers, duration, and exception class for iiko, Gemini, booking, SQLite, report generation, backup, and migration failures. Logs exclude secrets, personal row payloads, and upstream bodies.

A GitHub Actions workflow runs the supported Python version, installs `requirements-dev.txt`, executes the full pytest suite, performs Python compile checks and JavaScript syntax checks, and fails on tracked secret-like environment files. Dependency vulnerability scanning is included only if it can be pinned and run deterministically without making ordinary local tests depend on the network.

## Error handling and compatibility

Database mutations use explicit transactions. Expected validation conflicts return safe 4xx errors. SQLite lock, integrity, upstream, and unexpected errors are logged server-side with a request correlation ID and returned as generic 5xx responses without leaking internals.

Existing valid databases migrate in place through explicit schema migrations recorded in a schema-version table. Migrations are idempotent and tested against copies of legacy schemas. Invalid numeric rows are reported with table and row ID and block migration rather than crashing normal page reads after partial commit.

No current source database is deleted or modified during backup/migration preparation. Existing API shapes remain compatible except where a previously unsafe mutation is now rejected.

## Test strategy

Backend regression tests cover:

- external peer plus forged local Host;
- trusted and untrusted proxy behavior;
- same-origin and cross-origin mutations;
- multiple partial salary payments followed by edits;
- update-date mismatches;
- salary, debt-payment, movement, and reserve-transfer deletion;
- reserve history after backdated changes;
- nonnumeric, NaN, infinite, negative, over-precision, and oversized money values;
- deleting the last monthly employee;
- audit creation and immutability;
- fail-closed director categories;
- duplicate-period report generation and retention;
- shared data-directory resolution;
- backup, integrity verification, migration refusal, and restore verification.

Frontend regression coverage uses small JavaScript tests for pure state/rendering helpers and FastAPI integration tests for returned pages/APIs. It covers USD JSON parsing, stale founder results, busy-state recovery, accessible edit controls, director-list errors, accessible chart data, and Tashkent timestamps.

Final verification includes the full pytest suite, `compileall`, `node --check`, `git diff --check`, SQLite integrity checks on generated migration fixtures, and a local smoke test of all five pages.

## Acceptance criteria

- All confirmed P1/P2 defects from the 2026-09-19 audit have a regression test and fix or are explicitly documented as an operational limitation.
- The full suite is green with no known failing financial invariant.
- No operation can overpay an accrual, corrupt a numeric row, delete an unrelated payment, or create a negative paid-cash balance.
- Forging `Host` cannot cross the finance boundary, and cross-origin pages cannot trigger mutations.
- One configured data directory serves every checkout and deployment instance.
- A verified backup exists before the first real-data migration; restoration is tested without replacing the source.
- No personal salary seed data or live secret is added to Git.
- Existing running servers remain untouched until a separately confirmed deployment/migration step.
