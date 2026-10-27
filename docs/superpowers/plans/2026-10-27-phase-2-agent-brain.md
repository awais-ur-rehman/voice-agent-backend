# Phase 2 agent brain implementation plan

**Goal:** Complete the owner-approved agent brain and executed text-simulation checkpoint; leave Phase 2 unmerged and stop before voice.
**Spec:** `docs/superpowers/specs/2026-10-27-phase-2-agent-brain-design.md` (approved by owner).
**Architecture:** Existing FastAPI/PostgreSQL staff foundation, separate runtime actor/capability, PostgreSQL-fenced session commands, Valkey live state/holds, code templates/resolution and a constrained Nemotron planner. No fixture oracle enters inference.

## Constraints and review focus

Python 3.12; stable locked/open dependencies; preserve existing volumes and credentials. Every auth-relevant state change commits through the session guard. Factor text must not survive in history/summary/receipts. Confirmations bind current subject/fields/read-back revision and expire on corrections. Compare occupied hold ranges, not only start IDs. Retry after DB commit/cache loss must replay the result without repeating a mutation. Keep deterministic/fake-planner tests separate from actual NIM results. Preserve Phase 0/1 coverage and staff behavior.

## Task 1: Bilingual domain

Files: `brain/normalization.py`, `brain/resolver.py`, `brain/language.py`, `brain/locales/ur_numbers.json`, `tests_brain/test_locale.py`, `tests_brain/test_resolver.py`.
Interfaces: `LocaleError.code`; `number_ur`, `number_en`, `say_time`, `say_date`, `say_currency`, `say_phone`, `say_name`; `resolve(phrase, now, timezone, context, clinic_hours, prayer_break)` returns typed/serializable golden-result fields; `language_transition(current,pending,streak,text,mode)` returns new current/pending/streak.

- [ ] Write and run red tests for all 35 resolver rows, 101 number expectations and 15 normalization rows, plus script/error/counter cases. No runtime fixture loading.
- [ ] Implement deterministic bilingual functions and two-turn language switching. Verify targeted tests, lint and types; commit.

## Task 2: Valkey state/holds and environment

Files: `brain/store.py`, `tests_brain/test_store.py`, Compose/dev-setup/example updates (separate file ownership from root).
Interfaces: `StateStore(client,prefix)`; load/put/repair/delete versioned JSON; acquire/list/release owner-bound occupied-range holds. Compare-and-set and overlap/expiry decisions are atomic. Root PostgreSQL row locks remain authoritative fencing.

- [ ] Red tests for CAS, monotonic repair, expiry, conflicting revisions, overlapping differently-started holds, ownership, duplicate acquisition and cleanup using an isolated Valkey test namespace.
- [ ] Add pinned authenticated Valkey dev/test services without renaming the existing project/volume; append only missing private config keys with explicit setup flag. Verify actual Valkey tests; commit.

## Task 3: Database/runtime security and shared transactions

Files: incremental migration, `scheduler/models.py`, shared appointment services, `brain/state.py`, `brain/security.py`, `brain/sessions.py`, `tests_brain/test_sessions.py`.
Interfaces: immutable runtime agent version; tenant/session-scoped capability separate from staff audience; typed runtime audit actor; SQL guard revision/checkpoint/expiry + command receipt and callback records. Staff appointment routes retain signatures/results. Runtime fields are protected, never assigned from a model patch.

- [ ] Red tests for staff/runtime token separation, cross-tenant/agent/session access, revoked/expired capability, stale command, duplicate/mismatched command, all-new-table RLS and receipt/cache recovery.
- [ ] Implement incremental schema, safe actor attribution, authoritative security transitions and recovery. Verify migrations, Phase 1 behavior and new security tests; commit.

## Task 4: Guarded tools and dialogue engine

Files: `brain/contracts.py`, `brain/tools.py`, `brain/dialogue.py`, `brain/templates.py`, `brain/routes.py`, executed conversation fixtures/tests.
Interfaces: typed versioned state; exact bilingual tool envelope; explicit book/cancel/reschedule state machine and allowed_next; candidate/appointment/slot references bound to the session; revision-bound read-back; state-derived server mutation key.

- [ ] Red tests for lookup masking, phone+factor verification, three failures, subject changes, ownership, new-patient read-back, qualified/negative confirmations, corrections, hold expiry, task parking/resume, summaries and interrupted read-back.
- [ ] Execute real PG/Valkey workflows, callbacks/emergency/end, mutation retries and transactional cache repair. Implement bilingual scripted multi-turn scenario families and variation cases, asserting actual final DB/state effects; keep legacy fixtures unchanged. Verify full deterministic suites; commit.

## Task 5: NIM planner, console and checkpoint

Files: `brain/prompts.py`, `brain/provider.py`, `brain/console.py`, runtime demo bootstrap, live synthetic evaluator, CI/docs/license updates.
Interfaces: layered filtered context; explicit live mode; owner-selected Nemotron thinking disabled; no staff/runtime credentials in provider payload; no model rewriting of code say; provider calls outside DB locks with revision recheck before execution. Session inactivity is thirty minutes, with renewable scoped transport credentials and authoritative SQL expiry checks.

- [ ] Red provider/transport tests for malformed/injected calls, wrong script, bounded requests, timeout/429/circuit behavior, stale plans and credential/factor exclusion. Implement adapter and console with actual-request provenance.
- [ ] Run real hosted English/Urdu lifecycle, correction/digression and family/task-switch paths against synthetic records. Retain every outcome; resolve blocking failures before completion. Do not count fake/scripted output as inference.
- [ ] Run all retained/new tests, lint/types, actual Docker HTTP smoke and CI. Independent whole-branch review, important fixes with regressions, owner test guide/results and focused draft PR into develop. Preserve local service/data and stop before Phase 3/Phase 2 merge.

## Execution

Root coordinates and implements security/workflow/provider integration. Independent bounded workers may own Task 1 and Task 2 files under dispatching-parallel-agents; no overlapping edits or shared test cleanup. Root alone changes pyproject/lock, ORM/migrations and API settings. Use a per-plan ignored ledger to resume committed tasks; review integration and full suites before acceptance.
