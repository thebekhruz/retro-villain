# Hikvision First Entry Integration Design

**Date:** 2026-09-21  
**Task:** T-348  
**Status:** Approved in conversation; awaiting written-spec review  
**PM spec:** `PM/Retro/Specs/hikvision-attendance.md`

## Purpose and scope

Retro replaces its deterministic attendance preview with read-only data from one Hikvision entrance terminal. It records only the first confirmed employee entry per Tashkent calendar day. It does not collect exits, captured images, reference faces, or access-control writes.

The accountant, director, and entrance XLSX use the same stored first-entry records. An unavailable or incompletely covered source is never represented as employee absence.

## Architecture

The integration is split into independent units:

1. `HikvisionConfig` validates one root HTTP(S) device URL, username, password, source name, polling interval, timeouts, and initial backfill. Credentials stay in `build/.env` or process environment and are excluded from representations and logs.
2. `HikvisionDigestTransport` implements Hikvision-compatible Digest Auth for POST requests with replayable bodies and bounded responses. It follows the proven Parent App behavior instead of relying on an automatic client retry that can lose the request body.
3. `HikvisionJsonParser` converts paginated `UserInfo/Search?format=json` and `AcsEvent?format=json` responses into typed people and pass events. Missing optional fields, malformed timestamps, and pagination status are handled explicitly.
4. `HikvisionLinkService` performs a one-time exact normalized-name match and writes the unique Hikvision `employeeNo` to the existing unique `accountant_employees.hikvision_id`. Normalization trims, collapses whitespace, and compares case-insensitively. It does not transliterate or fuzzy-match. Both the Retro name and device name must be unique within their normalized value.
5. `AttendanceStore` owns device event deduplication, poll state/coverage, and first-entry persistence in the existing accountant SQLite database.
6. `HikvisionPoller` obtains people when linkage is requested and polls `AcsEvent` in the background. It advances its persistent cursor only after every page succeeds.
7. `AttendanceService` maps stored data plus coverage into `on_time`, `late`, `missing`, `unlinked`, or `unavailable` and is the only attendance source used by accountant routes, director routes, payroll draft calculation, and XLSX export.

The ingest boundary accepts typed events independently of ISAPI transport. A future second device or protected push adapter can call the same ingest method without changing attendance consumers.

## Device protocol

People are read with `POST /ISAPI/AccessControl/UserInfo/Search`. Events are read with `POST /ISAPI/AccessControl/AcsEvent`; only successful pass events with `major=5` and `minor=75` are eligible. The event contract requires a non-empty `employeeNoString`, `serialNo`, and offset-aware `time` (or a device-local time explicitly interpreted as `Asia/Tashkent`).

The client paginates until the device reports no more records. Response byte count, JSON nesting/size, page count, and per-request duration are bounded so a public device cannot exhaust the Retro process.

No `pictureURL` is fetched and no photo field is persisted.

## Persistence and idempotency

The accountant SQLite database gains three focused tables:

- `hikvision_events(source, serial_no, employee_no, occurred_at, received_at)` with unique `(source, serial_no)`. This is minimal metadata only and supports audit/dedup; no raw XML or image bytes.
- `hikvision_first_entries(work_day, employee_id, employee_no, occurred_at, source, serial_no)` with unique `(work_day, employee_id)`. An upsert retains the minimum `occurred_at`, so delayed older events correct the first entry.
- `hikvision_sync_state(source, cursor_at, covered_from, covered_through, last_attempt_at, last_success_at, last_error_code)` with one row per source. Human-safe error codes are stored instead of exception bodies that might contain URLs or challenge details.

SQLite transactions insert the event and update the first entry atomically. Unknown or unlinked `employeeNo` events may be retained as minimal dedup metadata, but never create an employee entry.

## Polling and coverage

On an empty state, the poller begins at the start of the previous Tashkent day. On subsequent runs it resumes from the last fully successful cursor with a small overlap. A page or parse failure leaves the cursor unchanged, so the next run replays the interval safely.

Coverage advances only after complete pagination. Attendance for a day is `missing` only if the stored coverage proves the source was queried for the whole relevant day. Otherwise employees with no entry are `unavailable`. For the current day, a linked employee without an entry is not treated as a final absence while the workday is in progress.

The poller starts and stops through FastAPI lifespan. It catches source failures, applies bounded backoff, records a safe health state, and never prevents login, iiko, finance, or existing dashboards from starting.

## Attendance and payroll behavior

The existing 10:00 boundary is retained: exactly 10:00:00 is on time; anything later is late. `draft_payroll` no longer generates attendance internally. It receives real attendance rows from `AttendanceService`, keeping pay calculation separate from transport and storage.

`unavailable` never maps to zero payable. `missing` can map to the existing zero-shift rule only for a fully covered completed day. `unlinked` retains the existing one-day manual-exception path. Confirmed financial records are not retroactively rewritten by a later device event; reconciliation requires an explicit finance action.

API payloads include `demo=false`, a source-health object, coverage information appropriate for the requested day, and employee statuses. They do not expose the device URL, username, credential state, raw JSON, or serial numbers. Accountant and director endpoints use the same service snapshot; the director response continues to omit pay and rate fields.

## Security

The public device port must be allowlisted to the Retro server address before continuous polling. HTTPS is preferred. Plain HTTP with Digest Auth is supported only as an explicit infrastructure risk because Digest protects the password exchange but does not encrypt response metadata.

Configuration rejects credentials in the URL, query strings, fragments, non-HTTP(S) schemes, invalid intervals, and unbounded timeouts. Secrets use non-revealing `repr` behavior and never enter exceptions returned to clients. Logs use source aliases and safe error categories.

The integration is read-only. It never calls door-control, face-library mutation, or user mutation endpoints.

## Error handling and observability

Errors are categorized as `not_configured`, `network`, `timeout`, `unauthorized`, `invalid_response`, or `device_error`. The UI displays actionable Russian states without treating them as zero attendance. The last successful poll time is visible to authorized dashboard users.

Partial pagination is not success. A malformed individual event is skipped and counted only when the enclosing response is structurally valid; a malformed JSON response page fails the poll and preserves the cursor.

## Testing

TDD covers:

- configuration validation and secret redaction;
- Digest challenge parsing and replayable POST bodies;
- UserInfo and AcsEvent JSON contracts, pagination, and limits;
- exact normalized linking, duplicate names, existing bindings, and conflicts;
- event deduplication and earliest-entry upsert across restarts;
- cursor advancement only after full success and initial backfill;
- Tashkent day boundaries and the 10:00:00 threshold;
- `missing` versus `unavailable` coverage rules and payroll safety;
- accountant, director, export, and authorization regressions;
- FastAPI lifespan startup/shutdown with a fake poller.

The live read-only acceptance run loads people, reviews the proposed links before or as they are committed, observes one real pass, and confirms it appears in both dashboards and XLSX within 60 seconds. Credentials and raw responses are not captured in test artifacts.

## Operations

Document placeholder variables in `config.env.example`, but put real values only in `build/.env`. The runbook includes network allowlisting, read-only connectivity verification, person-link report review, enabling the poller, health verification, and rollback by disabling the integration without deleting stored attendance.

The server currently running on port 8014 is a development preview. Live-device credentials will be installed only after the written spec is approved and the public port is restricted.
