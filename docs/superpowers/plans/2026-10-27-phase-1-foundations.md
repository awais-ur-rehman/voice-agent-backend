# Phase 1 foundations implementation plan

**Goal:** Complete the approved Phase 1 staff API and PostgreSQL acceptance criteria, then stop for owner testing.

**Spec:** `docs/superpowers/specs/2026-10-27-phase-1-foundations-design.md` (owner approved).

**Architecture:** FastAPI synchronous SQLAlchemy/psycopg transactions, PostgreSQL 16 constraints/RLS, Alembic, Pydantic contracts. Keep Phase 0 evaluation independent; no frontend/runtime/Phase 2 work.

## Constraints and review focus

Python 3.12; stable locked dependencies; no real credentials/provider calls in tests; no existing volume deletion. Tenant identity comes from validated auth, application DB role cannot bypass RLS. Reservations include buffers. DST folds/gaps, rollback after failed reschedule, refresh reuse revocation and patient daily-limit races need real coverage. Preserve synthetic Phase 0 fixtures and tests. Phase 1 stays unmerged.

## Task 1: Toolchain and deterministic scheduling

Files: `pyproject.toml`, `uv.lock`, `scheduler/config.py`, `scheduler/domain.py`, `tests_api/test_domain.py`.
Interfaces: typed `Window`, `Reservation`, `ScheduleRule`, `DateException`; `candidate_slots(day, timezone, rules, exceptions, duration, before, after, busy, now, notice, horizon)` returns timezone-aware UTC starts.

- [x] Write domain tests for weekly intervals/breaks, exception precedence, grid, buffers, adjacent reservations, min notice/horizon, DST gaps/folds and naive timestamp rejection. Run red.
- [x] Implement pure scheduling and validated settings; install/lock compatible dependencies with upstream license records. Run `uv run pytest tests_api/test_domain.py` (expected all pass) and retain Phase 0 checks. Commit.

## Task 2: PostgreSQL and authentication

Files: `scheduler/models.py`, `scheduler/db.py`, `scheduler/security.py`, `scheduler/auth.py`, `scheduler/main.py`, `scheduler/errors.py`, `migrations/`, `alembic.ini`, `scripts/init_db.py`, `tests_api/conftest.py`, `tests_api/test_auth.py`, `tests_api/test_database.py`.
Interfaces: application factory `create_app(settings=None, clock=None)`; request transaction/session dependency; verified `Principal`; frozen initial migration. Dedicated app role and privileged login lookup; all other tenant reads/writes use transaction-local RLS context.

- [x] Write failing auth/RLS/constraint tests against isolated PostgreSQL: role safety, missing/wrong tenant, cross-tenant FK, hashed passwords, invalid/expired JWT, role changes, refresh rotation/reuse/logout.
- [x] Implement schema, migration, guarded test DB setup, auth contracts and endpoints. Verify upgrade/downgrade/upgrade on disposable DB, and `uv run pytest tests_api/test_auth.py tests_api/test_database.py`. Commit with passing evidence.

## Task 3: Clinic configuration, patients and slots

Files: `scheduler/contracts.py`, `scheduler/catalog.py`, `scheduler/slots.py`, `tests_api/test_catalog.py`.
Interfaces: role-gated clinic/provider/service/association/rule/exception CRUD, safe patient contracts/deduplication, bounded GET /slots using Task 1 scheduling and current tenant data.

- [x] Write failing API tests for roles, CRUD/archive, configuration validation, normalized phones, duplicate patients, cross-tenant IDs, exception changes and bounded slot search.
- [x] Implement typed routes/services with explicit transaction and error boundaries; validate all query/mutation fields. Run `uv run pytest tests_api/test_catalog.py` and existing API/domain tests. Commit.

## Task 4: Appointment transactions and races

Files: `scheduler/appointments.py`, `tests_api/test_appointments.py`.
Interfaces: confirmed booking/reschedule/cancel with Idempotency-Key; consistent patient/provider locking; exact successful replay; hash mismatch conflict; audit records; bounded conflict alternatives.

- [x] Write failing tests for booking/replay/mismatch, unconfirmed fields, policy limits, cancellation, successful/failed atomic reschedule, concurrent identical keys, same-slot races, cross-provider patient daily limits and database exclusion without API locks.
- [x] Implement transactions and snapshots, preserving old appointment on any failed reschedule. Run `uv run pytest tests_api` (all tests expected pass). Commit.

## Task 5: Reproducible owner environment and verification

Files: `Dockerfile`, `compose.yaml`, `.env.api.example`, `scripts/dev_setup.py`, `scheduler/bootstrap.py`, `.github/workflows/ci.yml`, `docs/licenses.md`, `docs/phase-1-owner-checkpoint.md`, `README.md`.
Interfaces: generated private .env.api; persistent dev and ephemeral test DB; local owner provisioning; synthetic demo data; OpenAPI; owner setup/test commands.

- [x] Add Compose/bootstrap/smoke tests; verify from a fresh DB and build pinned images. Ensure migrations and app use different roles and startup rejects unsafe roles.
- [x] Run Ruff, mypy, all API/domain/database tests, 63 Phase 0 offline tests and CI. Review branch independently and fix important findings with regressions.
- [x] Create focused draft PR into develop, attach it, open local /docs, document test results/limitations and exact manual steps, leave testable service ready. Stop before Phase 2 and before Phase 1 merge.

Use inline execution. Track per-task commands/results in this plan's ignored execution ledger; resume from committed tasks after compaction. Routine choices follow the approved design; document material deviations rather than silently changing scope.

## Completion evidence

Owner-approved scope implemented; 48 Phase 1 tests and 63 retained offline tests pass, with Ruff/mypy and real Docker/HTTP lifecycle/race verification. Independent review findings reproduced and fixed. See `docs/phase-1-results.md`. Phase 1 is left in draft PR #2 for owner testing; Phase 2 has not started.
