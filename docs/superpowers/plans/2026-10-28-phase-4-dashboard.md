# Phase 4 Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved compact clinic operations dashboard and isolated synthetic draft testing, then stop for owner testing.

**Architecture:** Extend the existing tenant-scoped scheduling API with registration/profile, patient merge, versioned agent drafts and isolated test scopes. Build feature-oriented React routes around generated public contracts, accessible shared controls and the approved visual tokens. Preserve transactional scheduling, immutable runtime versions and the Phase 3 media transport.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL/RLS, Valkey; React/Vite/strict TypeScript, TanStack Query, shadcn/Radix, compatible router/forms/calendar packages verified before installation.

**Spec:** `docs/superpowers/specs/2026-10-28-phase-4-dashboard-design.md` (owner approved 2026-10-28).

## Global Constraints

- Work in the separate backend/frontend repositories under the owner's requested workspace. Use `feat/phase-4-dashboard`, base `develop`; Phase 3 merges are explicitly authorized. Leave Phase 4 PRs unmerged.
- Keep API, voice workers, domain logic, and provider adapters separate. Enforce tenant ownership, verification, scheduling policy, idempotency, and concurrency in code/database constraints, never solely through prompts.
- Secrets stay server-side. Never load owner dotenv in offline tests or log credentials/patient transcripts. Public fixtures are synthetic.
- No heavy voice inference or increased resource caps. Voice admission stays evidence-gated. Knowledge ingestion, analytics, public widget and optional outside-hours callback mode are deferred by approved design.
- Agent drafts save with revision conflicts; publish/rollback creates immutable versions and sessions stay pinned. Patient merge is explicit, manager-only, audited, canonical and concurrency-safe.
- Draft tests use a separately tenant-isolated synthetic clinic; never copy real patients/appointments or expose runtime capabilities to browser.
- Desktop: 232 px sidebar, 64 px top bar, 24 px gutters. Mobile: 16 px gutters, 44 px actions, no whole-page overflow. DM Sans and Noto Nastaliq Urdu, `dir=auto`, adequate line height. No decorative metrics/hero.
- Reuse one coherent primitive foundation and generated backend contracts. Verify stable versions, peer compatibility and licenses before installation; one npm lockfile.

## Review Focus

- A second editor or repeated publish cannot silently overwrite a newer draft or publish twice (Task 2).
- A booking concurrently targeting a patient being merged must preserve limits and require fresh runtime identity verification (Task 1).
- An old request after account switch cannot restore another clinic's data or send mutations under a new login (Task 4).
- Clinic-local dates, browser timezone differences, DST folds and Urdu text must not produce shifted appointments or clipped controls (Task 5).
- Test-session expiry, forged IDs, missed cleanup and copied profile changes must never cross the live/synthetic tenant boundary (Task 3).

## File map

- Backend `scheduler/dashboard/`: typed public contracts, profile/registration, patient merge/search and agent draft routes. Keep modules separated by domain; mount router in `scheduler/main.py`.
- Backend `brain/agent_config.py`, `brain/voice_catalog.py`: shared versioned config and frozen public voice metadata. Existing bootstrap/runtime/native readers use these interfaces rather than CLI-only models.
- Backend `brain/draft_tests.py` and `brain/draft_test_routes.py`: synthetic scope lifecycle and staff-to-runtime gateway. Existing ToolEngine remains the implementation of bookings.
- Backend migrations `0004_dashboard_core.py`, `0005_agent_drafts.py`, `0006_draft_tests.py`: additive tenant-qualified constraints and RLS; retain existing data and protections.
- Backend `scripts/export_dashboard_contracts.py`, `packages/schemas/dashboard.v1.json`: stable public dashboard schema artifact.
- Frontend `src/app/`: auth session/provider, router and shell; `src/features/{auth,clinic,scheduling,patients,agents}/` own their workflows. Preserve `features/voice/{media,protocol}.ts`.
- Frontend `src/components/ui/` and shared form/table/page patterns, `src/api/dashboard.generated.ts` and typed API adapter; tokens and responsive CSS live in focused style files.

### Task 1: Browser clinic setup and canonical patient management

**Files:** Create `scheduler/dashboard/{__init__,contracts,profile,patients}.py`, migration `0004_dashboard_core.py`, `tests_api/test_dashboard_core.py`; modify models/config/main/auth/appointments and runtime patient lookup/verification only as required.

**Interfaces:**
- Existing `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/me` stay compatible. Add `POST /auth/register` with clinic name, timezone, owner email/password, optional display name; return existing Tokens.
- Add `GET/PATCH /dashboard/clinic` with clinic profile/policy and revision; `GET /dashboard/setup` with real persisted checklist.
- Add `GET /dashboard/patients?q=&limit=&offset=` -> `{items,total,limit,offset}`, `GET /dashboard/patients/{id}` -> patient plus revision/merge state; `POST /dashboard/patients/{source_id}/merge` body `{destination_id,source_revision,destination_revision,fields:{name,name_ur,phone,dob}}`, idempotency header, -> retained canonical patient.
- `canonical_patient(db, tenant_id, identity, *, lock=False)` centralizes tombstone resolution. Book/move/cancel locking must coordinate with merge and preserve existing API shapes.

- [ ] Write tests proving disabled-by-default registration, atomic owner/new-tenant provisioning, denied role/tenant injection, normalized duplicate email, tenant-safe profile revision conflict and setup checklist.
- [ ] Write tests proving Urdu/name/phone search, staff merge forbidden, cross-tenant IDs denied, two-source/destination chains/cycles rejected, retries stable, conflicting retained fields explicit, appointment history retained, merged source cannot accept booking, concurrent merge/booking preserves daily limits and stale runtime verification is rejected.
- [ ] Run the new tests and observe missing endpoints/behavior fail: `.venv-api/bin/python -m pytest tests_api/test_dashboard_core.py -q` (use actual existing API environment path if different; record it).
- [ ] Implement narrow deployment-enabled registration with request limits and a secure server-owned provisioning function. Add clinic profile/revisions, patient tombstones/search/merge and one consistent advisory/row-lock order. No BYPASSRLS credential in API.
- [ ] Run new tests plus existing scheduling/auth/runtime regression targets; lint/type-check touched modules. Commit focused migration/domain changes after passing.

### Task 2: Agent drafts, publishing and honest voice selection

**Files:** Create `brain/agent_config.py`, `brain/voice_catalog.py`, `scheduler/dashboard/agents.py`, migration `0005_agent_drafts.py`, `tests_brain/test_agent_management.py`; update bootstrap/prompt/greeting/runtime/native config readers and public docs/licenses as needed.

**Interfaces:**
- `GET/POST /dashboard/agents`; `GET/PATCH/DELETE /dashboard/agents/{id}` (DELETE archives, PATCH enable with revision).
- `GET/PUT /dashboard/agents/{id}/draft` -> `{revision,config,updated_at}`; PUT body `{revision,config}`; stale revision -> 409.
- `POST /dashboard/agents/{id}/publish` body `{revision}`, idempotency; `GET /dashboard/agents/{id}/versions`; `POST /dashboard/agents/{id}/rollback` body `{version_id,revision}`, idempotency. Version records expose immutable number, publisher/time and rollback source.
- `GET /dashboard/voices` -> frozen catalog records with id/lang/gender/engine/config fingerprint/preview availability/readiness. Browser never gets arbitrary provider config or credential URLs.
- `AgentConfig` versioned typed model supports legacy default adapter and v2 greeting_en/ur, tone, instructions, language_mode, read_digits_in_english, voice_en_id/voice_ur_id and profile snapshot. Exact JSON fields are frozen in exported schema before frontend work.

- [ ] Write failing tests for manager authorization/RLS, revisions/conflicting editors, immutable old versions, stable publish replay, rollback creates new version, draft rename versus published identity, archived/disabled new admission, old sessions remain pinned, protected disclosure and bounded config, matching-gender bilingual validation, unavailable voice previews/admission and selected profile propagation to native/cache fingerprints.
- [ ] Run `.venv-api/bin/python -m pytest tests_brain/test_agent_management.py -q`; observe red before implementing.
- [ ] Add typed config/draft domain with atomic locks and audit metadata. Keep immutable DB trigger; backfill only via migration. Source catalog metadata from pinned existing model assets, max three profiles/gender/language, exact licenses and provisional status. Wire greeting/tone/profile through actual session/runtime readers and voice fields through native synthesis/cache. Do not claim untested voices ready.
- [ ] Run new tests, current brain/voice suites and native control/config tests (no inference). Commit after passing.

### Task 8: Dashboard scheduling projections and atomic templates

**Execution order:** after Task 2 and before Task 3. Read-only integration inspection found that legacy APIs cannot read provider/service associations, calendar rows contain IDs without names, and multi-request templates would permit partial updates. This task closes those interfaces before exporting the frontend contract; it implements already-approved scheduling scope, not a new phase.

**Files:** Create `scheduler/dashboard/scheduling.py`, `tests_api/test_dashboard_scheduling.py`; extend dashboard router/contracts. No migration unless an actual constraint requires it; existing provider row locks supply serialization.

**Interfaces:**
- `GET /dashboard/provider-services?limit=&offset=` -> `{items:[{provider_id,service_id}],total,limit,offset}` for persisted assignment editing.
- `GET /dashboard/appointments?date_from=&date_to=&provider_id=&limit=&offset=` -> paginated AppointmentOut plus patient/provider/service English/Urdu display names. Require a bounded clinic-local date range up to 42 inclusive days; use UTC comparisons as existing appointments API, no DOB/phone projection or N+1 browser requests.
- `GET /dashboard/providers/{id}/availability` -> `{revision,rules,exceptions}`, revision is a deterministic digest of the authoritative provider snapshot.
- `POST /dashboard/providers/{id}/availability/preview` and `/apply` take the same typed template body `{revision,template,break_start?,break_end?,start_date?,end_date?,start_time?,end_time?}`. Preview returns `{revision,changes,after}` without writes; apply requires Idempotency-Key and returns the new snapshot. `friday_prayer` defaults12:45–14:30 and splits only overlapping Friday rules. `ramadan` creates explicit custom_hours over the given range; reject conflicting unrelated exceptions rather than overwriting holidays. Exact repeated template is a no-op, stable retries replay; changed snapshot ->409.

- [ ] Write failing real DB tests for tenant/role projections, pagination/count/date-range limits/timezone boundaries, provider/service relation persistence and synthetic Urdu display names.
- [ ] Write failing template tests: preview no writes, Friday changes only selected provider/Friday interval, Ramadan conflicts preserve existing holidays, repeated template/noop/idempotency, stale revisions reject, atomic rollback on validation failure and competing edits under provider lock.
- [ ] Run `UV_CACHE_DIR=.local/uv-cache uv run --locked pytest tests_api/test_dashboard_scheduling.py -q` and observe red before implementation.
- [ ] Implement typed projections and one provider-locked template service shared by preview/apply, with code-owned deterministic transformations. Retain existing CRUD API compatibility and tenant RLS.
- [ ] Run focused and full API/brain suites, Ruff/mypy; commit after passing. Task 3 must export these paths; Tasks 4–5 must consume them instead of assembling unsafe partial template writes or fetching patient names per event.

### Task 3: Isolated synthetic draft testing and public contracts

**Files:** Create `brain/{draft_tests,draft_test_routes}.py`, migration `0006_draft_tests.py`, `tests_brain/test_draft_tests.py`, `scripts/export_dashboard_contracts.py`, `packages/schemas/dashboard.v1.json`; modify session service adapters only where required.

**Interfaces:**
- `POST /dashboard/agents/{agent_id}/tests` body `{draft_revision}` and idempotency -> `{id,draft_revision,expires_at,status,transport_capabilities,conversation}`.
- `GET /dashboard/tests/{id}`; `POST /dashboard/tests/{id}/turns` body `{command_id,revision,text}` -> `{id,revision,status,messages,tool_trace}`; `POST /dashboard/tests/{id}/end` idempotent. Staff JWT only; no runtime bearer token in response.
- Real-audio adapter `POST /dashboard/tests/{id}/voice` -> existing public VoiceSnapshot only when selected profile is ready. Parent staff ownership is validated before switching to the mapped synthetic tenant; worker credentials stay separate.
- Synthetic tests expire after 30 minutes; existing media calls cap at 10 minutes. At most one active test per creator/agent and bounded per-clinic admission; explicit replacement ends previous scope. Cleanup revokes and clears ephemeral data, retains marked synthetic DB rows.
- Public schema contains typed endpoint input/output components and paths sufficient for the frontend to generate types; exclude internal worker/runtime credentials. Existing voice.v1 stays stable unless a versioned field addition is essential.

- [ ] Write failing integration tests: clone only public schedule/profile and selected saved draft; no copied patient/appointment/callback data; correct remapped IDs; full synthetic booking uses real ToolEngine yet leaves original tenant unchanged; forged/cross-user/cross-tenant/expired mapping forbidden; retries do not create duplicate scopes; parent disable/archive stops new tests; pinned snapshot unaffected by later drafts; cleanup releases rooms/leases; voice unavailable is explicit.
- [ ] Run `.venv-api/bin/python -m pytest tests_brain/test_draft_tests.py -q` and observe red.
- [ ] Implement parent RLS map, server-created synthetic tenant/actor, explicit purpose=test snapshots and trusted scoped gateway to existing planner/session services. Never accept client tenant IDs or bypass verification in production. Redact identity inputs from structured trace; no transcript logs.
- [ ] Export schema deterministically; test regeneration drift and no internal secrets/contracts. Run new/full API/brain suites and export check. Commit public contract plus feature.

### Task 4: React application shell, auth and clinic configuration

**Files:** Frontend `src/app/{App,router,auth,AppShell}`, `src/features/auth/`, `src/features/clinic/`, reusable UI/form/table patterns, `src/api/{dashboard.generated,dashboard,session}`, styles, scripts/contracts, package lock, dependency/license docs and related tests.

**Interfaces:** Consume Tasks 1–3 schema; generate types from committed artifact and preserve voice schema generation. Auth session exposes `{me,sessionId,request,logout}`; request supports typed GET/POST/PUT/PATCH/DELETE, signal and idempotency key. Query keys use sessionId/tenant, never tokens. Feature routes consume shared page/form/error patterns.

- [ ] Write failing tests for login/signup, /me role, simultaneous refresh singleflight, old-session response isolation, logout cancellation/cache/media cleanup, field errors preserving edits, setup checklist from API, staff read-only/hidden manager actions and actual persisted clinic/provider/service/relationship/hour/exception/user forms.
- [ ] Run `npm test` targeted files; observe red for new behavior.
- [ ] Verify stable dependency versions/licenses/peers from primary sources before npm install. Add compatible router, React Hook Form/Zod, Radix primitives and generated schema tooling only as needed. Record package rationale including Thinking Orbs/React Bits/Lenis review.
- [ ] Build auth/session and 232/64/24 shell tokens, mobile16 gutters, shared accessible dialogs/selects/forms/tables, clinic settings and real-data onboarding. Friday/Ramadan templates preview changes and are retry-safe; holiday reminders derive manual dates. Preserve no-patient-browser-storage rule.
- [ ] Run targeted tests, lint/type/build/schema checks. Commit foundation/configuration without fake links or invented metrics.

### Task 5: Calendar, booking forms and patient workflows

**Files:** Frontend `src/features/scheduling/{SchedulePage,AppointmentForm,AvailabilityPage}`, `src/features/patients/{PatientsPage,PatientDetail,MergeDialog}`, feature API hooks and tests; calendar dependency/license entries.

**Interfaces:** Consume Task 4 authenticated request and shared components; existing `/appointments`, `/slots`, `/patients` mutations plus paginated dashboard patient search/merge. Query state uses clinic-local date and opaque IDs; no names/phones in URLs. Backend UTC is authoritative.

- [ ] Write failing tests: fetched calendar/list, real-slot booking review/confirmation/idempotency, reschedule/cancel conflict alternatives, patient pagination/Urdu search/history and explicit merge conflict fields with role gate. Test browser timezone differing from clinic, DST fold/gap handling and long mixed-script content.
- [ ] Run targeted frontend tests and observe red.
- [ ] Verify/install compatible free FullCalendar React/day/time-grid/interaction/named-timezone packages; no paid resource plugin. Implement provider-filtered day/week/list calendar and mobile appointment list, accessible keyboard alternative, forms that preserve values on conflict, patient detail/history and explicit merge. No drag-and-drop mutation bypass.
- [ ] Run targeted tests and full frontend checks. Commit scheduling/patients deliverable.

### Task 6: Agent editor, version history and isolated test panel

**Files:** Frontend `src/features/agents/` list/editor/draft/voices/versions/test components and hooks; focused adapter to existing `features/voice/media.ts`; tests and truthful capability status.

**Interfaces:** Consume Task 2 agent/voice APIs and Task 3 test gateway, Task 4 session/request/components. Wire generated immutable voice selections into draft saves. Test text responses contain only the parent-owned opaque scope and redacted structured trace; real media uses existing VoiceSnapshot when admitted.

- [ ] Write failing tests for create/edit/unsaved navigation/conflicting draft, save/publish review/replay, rollback/new version, archive/disable state, matching gender voice selection, missing previews, isolated text-test start/turn/end/expiry and speech-unavailable explanation. Preserve media playback ACK/interruption tests and test account switch disposes any test/media session.
- [ ] Run targeted tests and observe red.
- [ ] Implement the approved compact split editor/test panel, stack/sheet at small widths. Use real capability states and actual Thinking Orb activity (MIT package reverified) with reduced motion; no simulated readiness. Guarded audio retains existing bound worker/protocol semantics. Version history has real authors/timestamps; raw engine config stays server-side.
- [ ] Run full frontend checks and existing voice tests. Commit complete editor and test workflow.

### Task 7: Integration, visual evidence and owner checkpoint

**Files:** Backend/frontend README/CONTRIBUTING/CI changes needed by new checks, `docs/phase-4-testing.md`, `docs/phase-4-results.md`, synthetic visual evidence; final public PR descriptions.

**Interfaces:** The complete feature branches and actual running API/frontend, existing Compose project and preserved volumes; fake-provider deterministic tests versus separately bounded hosted Nemotron smoke.

- [ ] Run additive migrations on the existing local dev database, preserving credentials/data/volumes. Enable local registration explicitly without printing private env values. Rebuild only necessary services and retain voice-disabled config.
- [ ] Run backend lint/type/API+brain/offline/native control checks and frontend schema/lint/type/test/build/meaningful browser tests. Inspect results; fix actual failures without weakening tests.
- [ ] Visually inspect 1440/1024/390 layouts through CUA and save screenshots. Exercise a fresh synthetic clinic setup, configuration, publish, text-test booking isolated from live schedule, manual booking/reschedule/cancel, Urdu search and merge, version rollback and role checks. Bounded hosted NIM smoke is separate from deterministic suites and never prints secrets/transcripts.
- [ ] Fresh whole-branch review of contracts/security/concurrency and UI/flows. Fix important findings with regressions; record limitations truthfully. Verify docs give exact reproducible owner steps and block descriptions.
- [ ] Push each feature branch, create public draft PR into develop and attach each PR to the chat. Verify exact heads/base/author/files and CI, leave PRs unmerged. Report measured results and owner checklist, then stop. Do not begin Phase 5.

## Execution record

Owner approved implementation through checkpoint and the Phase 3 merges. Use the subagent-driven workflow for focused sequential tasks with review gates. Keep the existing named backend/frontend checkouts on feature branches: the owner's environment, local secrets and running services resolve these paths, and no additional worktree was requested. No new worktree is created.

Self-review: all approved design sections map to Tasks 1–7; deferred scope is explicit. Task 4 waits for exported contracts from Task 3; Tasks 5–6 consume Task 4 shared patterns. Task 7 verifies the actual integrated product and pending speech boundary. Planning does not count as completed functionality.
