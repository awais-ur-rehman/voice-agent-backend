# Phase 1 owner checkpoint

Phase 1 implements staff-facing API foundations. The separate React dashboard is Phase 4. Test through Swagger at http://127.0.0.1:8000/docs or the commands below. Phase 2 agent/session tools, patient verification, dialogue state, relative-date resolution and voice integration have not started. These staff bearer credentials must never be handed to an LLM.

## Start or restart locally

From the backend directory:

```bash
uv sync --locked --all-groups
python3 scripts/dev_setup.py
docker compose --env-file .env.api up --build -d api
uv run python -m scheduler.bootstrap --demo
uv run python scripts/smoke_phase1.py
```

Docker Desktop must be running. `dev_setup.py` creates `.env.api` only when absent, with generated DB/JWT secrets and mode 0600. It preserves the existing Phase 0 `.env`. Compose starts PostgreSQL, initializes the non-owner application role, runs migrations with separate credentials, then starts the API. Ports are loopback-only: API 8000, development PostgreSQL 55432. Development data is persistent. Do not remove the volume to restart or update.

Demo accounts and identifiers are in the ignored private file `.local/phase1-demo-credentials.json`, mode 0600. Open it locally; do not paste its passwords or tokens into chat/issues. Each synthetic clinic has a different owner password, one provider, one thirty-minute consultation, a patient and daily 09:00–17:00 availability. Rerunning bootstrap preserves matching credentials and records. It refuses unrelated/remote DB targets. It does not reset your edits.

The smoke script uses only those two synthetic accounts. It checks login, slots, booking, exact replay, duplicate conflict, cross-tenant denial, reschedule, cancellation, and eight simultaneous bookings with one winner. Its created appointments end cancelled; synthetic history/audit entries remain. It prints no passwords/tokens.

To provision a separate local owner manually:

```bash
uv run python -m scheduler.bootstrap --clinic-name "My Test Clinic" --email owner@example.com
```

This prompts privately for a password. It creates a new clinic without scheduling data; configure providers, services and rules through the API. Login email is globally unique in this MVP.

## What to test in Swagger

1. POST `/auth/login` with the first demo owner's email/password. Copy only the access token into **Authorize**. GET `/auth/me` and `/clinic` should show clinic A. Keep the refresh token private.
2. GET `/providers`, `/services`, `/availability-rules` and `/patients`. Create/edit a provider/service and connect them using PUT `/providers/{provider_id}/services/{service_id}`. Staff can manage appointments/patients but scheduling configuration requires owner/admin.
3. GET `/slots` with the demo service ID, optional provider ID, and dates several days ahead. Search range is inclusive and at most 31 days; results are capped at 100. Dates/timestamps are absolute; relative Urdu/English phrases belong to Phase 2.
4. POST `/appointments` with patient_id, provider_id, service_id, an offered timezone-aware starts_at, and `confirmed: true`. Enter a fresh UUID in the required `Idempotency-Key` header. A successful booking returns 201. Numeric/string truthy confirmation values and extra tenant/status fields are rejected.
5. Submit the identical request/key again: it must return the exact original appointment. Change its starts_at while keeping the key: expect 409 `idempotency_conflict`. Submit the same slot with a new key: expect 409 `slot_unavailable`, with up to three alternatives. The database range includes buffers.
6. POST `/appointments/{id}/reschedule` with a different offered starts_at, confirmed true, and a new key. It returns a linked replacement; the old row becomes cancelled. Try moving to an occupied slot: the original must remain booked. POST `/appointments/{id}/cancel` with confirmed true/new key, then verify cancellation and that its time can be booked again.
7. Log in as clinic B. Clinic A patient/provider/appointment IDs must appear not found. Tenant identity is derived from the signed access token and database user record; adding a tenant_id to a mutation body is rejected.
8. POST `/auth/refresh` once: receive a replacement refresh token. Reuse the old refresh token: it must fail and revoke that family, including the replacement. Logout revokes the refresh family. Access tokens last fifteen minutes; logout does not retroactively revoke an already issued access token. Disabling a user denies their subsequent authenticated requests.

Default editable policies: two hours' minimum notice, ninety-day horizon, twenty-four-hour cancellation window and three booked appointments per patient/local day. PATCH `/clinic` to test changes. These are API-enforced for every staff role; there is no silent policy override. A 409 `transaction_conflict` is a rolled-back database deadlock/serialization abort: retry with the same idempotency key. There is no hidden retry.

## Run the automated checks

The test database is a separate tmpfs service on port 55433. Tests refuse cleanup outside the specifically named `voice_agent_test` database; they do not use `.env` or `.env.api` credentials.

```bash
docker compose --env-file /dev/null --profile test up -d --wait postgres-test
uv run ruff check .
uv run ruff format --check .
uv run mypy scheduler
uv run python -m pytest
uv run python tests/run_offline.py
```

The PostgreSQL tests run migrations including a downgrade/upgrade cycle only on the disposable test DB. They cover RLS/foreign keys, role checks, refresh reuse, buffers/DST/exceptions, policies, idempotency, rollback and races, including direct SQL competing inserts. PostgreSQL may reject a direct exclusion race as either an overlap or deadlock victim; both must leave exactly one booked row. See [PostgreSQL's transaction-abort behavior](https://www.postgresql.org/docs/16/explicit-locking.html#LOCKING-DEADLOCKS).

## Stop point

Send the endpoint/step, observed result and expected result for any problem, with credentials and identifying details removed. Phase 1 stays on its feature branch and draft PR for your review. No Phase 1 merge, release promotion, Phase 2 implementation or patient-facing deployment is automatic. Phase 0 voice/GPU and native-language acceptance remain separate open gates.
