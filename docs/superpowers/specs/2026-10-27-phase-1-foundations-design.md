# Phase 1 foundations design

## Goal and acceptance

Implement v0.1 Phase 1, with v0.2 overrides: a runnable FastAPI/Python 3.12 API, PostgreSQL 16 migrations, authentication, tenant isolation, scheduling configuration, deterministic slot calculation and appointment lifecycle APIs. Completion requires actual PostgreSQL tests proving tenant isolation, booking/rescheduling/cancellation, idempotency and no double booking under concurrent requests. Stop for owner testing; no Phase 2 work or automatic release merge.

The owner explicitly authorized Phase 1 after the CPU-only Phase 0 checkpoint. Voice quality/GPU evidence and unrestricted model reliability remain open and do not get relabelled as passed. Phase 1 uses synthetic data and requires no NIM calls.

## Scope and approaches

The governing specs already select FastAPI and PostgreSQL. Use a modular monolith with synchronous SQLAlchemy 2/psycopg transactions and Alembic migrations. This keeps transaction ownership explicit and allows ordinary FastAPI worker threads to serve concurrent requests. An async ORM adds complexity without a demonstrated need; SQLite cannot prove PostgreSQL RLS/exclusion behavior. No microservices are needed for these foundations.

Keep the existing `evaluation/` tooling independent. Add `scheduler/` for the API/domain/data implementation, `migrations/`, `tests_api/`, development/test Compose services and scripts. Keep the separate frontend repository unchanged: the React dashboard is Phase 4. Swagger/OpenAPI provides the Phase 1 manual API testing surface.

No agent runtime, LLM tool endpoints, patient-verification sessions, dialogue/task stack, bilingual relative-date resolver, speech normalizers, voice workers, KB, analytics dashboard, paid services, public deployment or GPU dependency is introduced. Staff APIs act on authenticated staff permissions; they are not exposed to an LLM. Phase 2 must add the separate patient/session guards before agent access.

## Data and database boundaries

Phase 1 tables: tenants, users, refresh tokens, providers, services, provider-service associations, availability rules, date exceptions, patients, appointments, idempotency records and mutation audit records. UUIDs identify rows. All tenant-owned tables carry tenant_id and composite foreign keys prevent cross-tenant relationships. Users have one tenant and a globally unique normalized login email in this MVP; multi-tenant memberships/invitations are future scope.

Enable and FORCE RLS on tenant-owned tables. Policies use a transaction-local app.tenant_id set from a validated access token, never from a request body/header/query. Use a dedicated non-owner application role with NOSUPERUSER/NOBYPASSRLS; migration credentials remain separate. Refuse an unsafe application DB role on startup. Tests run as that application role and verify missing/wrong context and raw cross-tenant writes as well as HTTP reads.

Before authentication, a narrowly scoped SECURITY DEFINER function locates an active user by normalized email. It has a fixed search_path, no dynamic SQL, explicit execution grants, and no general tenant-data access interface. Its privileged result stays inside authentication; password hashes never appear in API responses or logs. Refresh tokens carry a tenant locator plus random secret; the locator alone authorizes nothing.

Database exclusion constraints are the final double-booking authority. Protect each provider's half-open occupied time range for booked rows with GiST/btree_gist. Occupied ranges include before/after buffers. Appointment duration/buffer values are snapshots; a database trigger computes occupied boundaries so direct inserts cannot omit the reservation range. Constraint/check/FK tests exercise PostgreSQL itself, not just an application lock.

Reschedule atomically cancels the old appointment and inserts a linked replacement. A conflict or policy error rolls back everything and leaves the old appointment booked. Cancellation is a status transition, not row deletion. Staff cannot change appointment status or tenant ownership through arbitrary PATCH fields.

## Authentication and permissions

Use Argon2 password hashes, short-lived signed JWT access tokens (15 minutes, pinned algorithm/issuer/audience, exp/iat/sub/tenant claims), and hashed opaque refresh tokens (seven days). Rotate refresh tokens transactionally. Reuse revokes the token family; the revocation must commit even when the refresh response is unauthorized. Logout revokes the refresh family; issued access tokens retain their short expiry. Recheck user active status/role from the database on authenticated requests.

Provision the first clinic/owner through a local administrative CLI; no public tenant-registration endpoint or hardcoded credential. Owners/admins configure scheduling; staff may read configuration and manage patients/appointments. Owners may provision admins/staff; admins may provision staff only. Tenant identity and role cannot be supplied as authority by clients. Unknown email and incorrect password return the same authentication error.

## Scheduling behavior

Use absolute timezone-aware timestamps for Phase 1 booking inputs; store UTC and evaluate clinic-local policies using ZoneInfo. Reject naive timestamps. Default clinic timezone Asia/Karachi, currency PKR, phone normalization +92 through phonenumbers. Relative Urdu/English date resolution remains Phase 2.

Weekly provider rules use ISO weekdays and local start/end plus slot-minute steps. Breaks are separate open intervals. Closed date exceptions override weekly/custom windows; custom hours replace weekly windows on matching dates. Date ranges are inclusive for exceptions and search. Merge adjoining open windows, deduplicate overlapping candidate starts, and exclude reservations intersecting booked occupied ranges. Generate starts on the rule grid; require the service duration and both buffers to fit an open interval. DST gaps produce no nonexistent local starts; folds may produce two distinct UTC starts. Treat durations as elapsed minutes.

Only active providers offering an active service have slots. Availability APIs bound date range to 31 days and result size; booking validation checks the chosen date without depending on a truncated search page. Policies are configurable: minimum notice, booking horizon, cancellation window and per-patient daily appointment limit. Builder defaults, not previously owner-specified values: two hours' notice, ninety-day horizon, twenty-four-hour cancellation window, three appointments per patient per day. The owner can edit these through clinic settings for testing.

Serialize appointment mutations for a patient and provider with consistent row-lock ordering, and recheck availability/policies inside the transaction. Patient serialization protects the daily limit across different providers. The exclusion constraint independently protects slot races. Configuration edits and appointment snapshots have explicit transaction boundaries; historical appointments keep their original duration/buffer values.

Every booking, reschedule and cancellation requires an Idempotency-Key and confirmed critical details. Bind the key to tenant, operation and a canonical request hash. Replays of successful identical requests return the original result; reuse with different input returns a conflict. Concurrent identical requests create one appointment. Failed operations roll back without reserving a successful idempotency result. Persist mutation audit metadata without duplicating patient-identifying payloads.

Staff bookings do not require a voice soft hold. Valkey holds and short-lived slot caches belong with the Phase 2 runtime/tool boundary; Phase 1 computes authoritative slots directly. This avoids a second source of truth before a consumer needs it.

## API surface

- Health/liveness and DB readiness, with versioned OpenAPI contracts.
- POST /auth/login, /auth/refresh, /auth/logout; GET /auth/me.
- GET/PATCH /clinic for policies/timezone; owner/admin user provisioning/listing.
- Providers and services CRUD/archive; explicit provider-service associations.
- Availability rules and closed/custom-hours date exceptions CRUD.
- Patient creation, search, retrieval/update and appointment history; safe deduplication on normalized phone plus name. Patient merge is deferred to the patient-management/dashboard phase rather than silently rewriting clinical ownership now.
- GET /slots by service, optional provider, date range and bounded limit.
- GET/POST /appointments; POST /appointments/{id}/reschedule and /cancel.

Use typed Pydantic contracts, forbid extra mutation fields, bounded pagination/search and uniform error codes. Cross-tenant identifiers appear as not found. Slot conflicts are 409 with bounded alternatives; invalid input/policy failures have stable error codes. Do not claim bilingual spoken confirmations before the Phase 2 code templates/normalizers exist; Phase 1 returns typed staff API data, not model-facing tool envelopes.

## Operations and reproducibility

Docker Compose runs an API and PostgreSQL for local development, plus an isolated ephemeral PostgreSQL test service/profile. Bind development ports to loopback. Preserve the existing backend/.env NIM file; generate a separate ignored .env.api with random local DB/JWT secrets and a tracked placeholder example. Never log passwords, tokens, raw bodies, query strings or patient-identifying transcripts. Structured request logs contain a request ID, route template, status and timing. Avoid raw SQL exception parameters in error responses/logs.

Use uv/pyproject with a committed lockfile, stable compatible direct dependencies and reviewed upstream licenses. Preserve the existing hash-locked Phase 0 runner path. Add Ruff, type checks, pytest domain/API/integration tests and CI against PostgreSQL 16. Pin container image digests after resolving the official tags. No destructive commands against existing external databases/volumes. Test cleanup requires an explicitly dedicated *_test database and separate credentials.

## Verification and owner checkpoint

Tests must cover login/expired or invalid JWTs, refresh rotation/reuse/logout, role denial, RLS with missing/wrong tenant, cross-tenant identifiers and FKs, weekly rules/breaks/exceptions, buffers, timezone/DST gaps/folds, notice/horizon/daily limits, unconfirmed mutations, idempotent replay/mismatch/concurrency, parallel same-slot booking, adjacent nonoverlapping appointments, failed reschedule rollback, cancellation/rebooking, and DB constraints bypassing the HTTP layer.

Run migration upgrade on a fresh database and verify a downgrade/upgrade cycle on the disposable test database. Run all retained Phase 0 offline tests; no real .env or provider calls in tests. Review the whole branch before the checkpoint. Deliver commands for setup, seeding two synthetic clinics, API login/configuration, finding slots, booking, replay, rescheduling, cancelling and confirming cross-tenant denial. Leave the local testable service running if available, open /docs, create a focused draft Phase 1 PR into develop, and stop for owner acceptance.

## Git integration

Phase 1 uses feat/phase-1-foundations in the requested backend checkout. The current baseline includes accepted Phase 0 commits so evaluation tooling is preserved. Merge the accepted Phase 0 draft PR into develop through GitHub before publishing Phase 1 against develop; do not merge Phase 1 or release main before owner testing. If the owner wants Phase 0 unmerged, retain it as a documented prerequisite and keep Phase 1 unmerged as well.
