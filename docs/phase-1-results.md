# Phase 1 verification — October 27, 2026

The approved staff API foundation is implemented and left running locally for owner testing. Phase 0 PR #1 was merged into develop with explicit owner authorization. Phase 1 remains on `feat/phase-1-foundations`, draft [PR #2](https://github.com/awais-ur-rehman/voice-agent-backend/pull/2), unmerged. Phase 2 has not started.

## Delivered

- FastAPI/Python 3.12, validated Pydantic contracts, locked dependencies, structured logs that exclude bodies/query strings/credentials, OpenAPI at `/docs`.
- Frozen Alembic migration for twelve tenant-owned tables with FORCE RLS, composite tenant foreign keys, a narrow privileged login lookup and a separate non-owner/NOBYPASSRLS application role. Startup rejects superuser, bypass or table-owner connections.
- Argon2 passwords, short-lived signed access tokens, database-backed roles and rotating hashed refresh tokens. Reuse/logout serialize by refresh family before token-row locks so concurrent descendants cannot escape revocation.
- Clinic policies, providers/services/associations, weekly hours, breaks, closed/custom date exceptions, normalized patient records and bounded availability searches.
- UTC storage and clinic-local rules, including elapsed durations/buffers, DST gaps and separate fold intervals. Raw caller-relative-date interpretation remains Phase 2.
- Confirmed booking, atomic linked rescheduling and cancellation, exact successful idempotent replay, request-hash mismatch protection, safe conflict alternatives and mutation audit records.
- PostgreSQL exclusion constraint over buffered half-open reservations, consistent patient/provider lock ordering and patient daily-limit serialization across providers.
- Digest-pinned Docker development environment and separate ephemeral PostgreSQL test service. Private generated API configuration and demo credentials are ignored/excluded from images; the Phase 0 NIM file was preserved.

## Verification evidence

| Check | Result |
| --- | --- |
| PostgreSQL/API/domain/environment suite | 48 passed |
| Retained Phase 0 offline suite | 63 passed; no provider requests |
| Ruff lint and format | Passed |
| mypy | Passed, fourteen application modules |
| Fresh migration + downgrade/upgrade | Passed on the disposable `voice_agent_test` DB |
| Missing/wrong tenant, composite FK and unsafe role | Rejected |
| Same idempotency key, six concurrent API requests | One appointment/one audit event, six identical successful responses |
| Same slot, eight concurrent API requests | One success, seven conflicts |
| Daily limit across two providers | One success when limit is one |
| Competing direct SQL inserts without API locks | One booked row; overlap/deadlock victim rejected |
| Failed reschedule after cancellation flush | Original restored; no replacement/audit/idempotency residue |
| Independent whole-branch review | Two important findings fixed with reproducing regressions |
| GitHub API and evaluation CI | Passed on implementation commit `7bb1552` |
| Final Docker rebuild and real HTTP smoke | Passed full lifecycle, two-clinic denial, exact replay, eight-request race |

The reviewer reproduced a refresh-revocation snapshot race and incorrect gap/fold working-window conversion. Both failures were observed before correction; barrier-controlled PostgreSQL reuse/logout races and gap-boundary/partial-fold tests now pass. Additional tests reject truthy numeric/string confirmations, verify redacted request logging, reject unsafe ownership, and handle extreme calendar inputs without server errors. The post-cancellation failure test strengthens evidence for rollback already implemented.

PostgreSQL can abort a direct competing exclusion insert as SQLSTATE 23P01 or 40P01. The direct test accepts only those rejection outcomes and independently asserts exactly one booked row; a separate buffered-overlap test requires the exclusion violation itself. API deadlock/serialization aborts return a safe 409 `transaction_conflict` and roll back; clients may retry the same key. No hidden retry or discarded failure result is used. See [PostgreSQL's documented abort behavior](https://www.postgresql.org/docs/16/explicit-locking.html#LOCKING-DEADLOCKS).

The real HTTP smoke uses only the two generated synthetic clinics. It exercises login, slot search, booking, replay, collision, cross-tenant denial, linked reschedule, cancellation and eight simultaneous booking attempts. Smoke-created bookings end cancelled and their synthetic history/audit entries remain available.

## Owner handoff

Open http://127.0.0.1:8000/docs and follow [the testing guide](phase-1-owner-checkpoint.md). Private demo credentials are at `.local/phase1-demo-credentials.json`; they are outside the Phase 0 report server's root and must stay local. Default policies are editable. Development data is persistent; restarting does not require removing volumes.

This is a staff API checkpoint, not a patient-facing deployment. No agent/session verification boundary, soft holds, relative-date resolver, dialogue state, voice pipeline, dashboard, GPU quality or production capacity approval is claimed. The separate frontend repo is unchanged. No Phase 1 merge, release promotion or Phase 2 continuation will happen before the owner tests and directs the next step.
