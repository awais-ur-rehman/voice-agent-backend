# Phase 6 Analytics and Call Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Existing standing owner authorization permits execution without a repeated approval menu.

**Goal:** Deliver truthful tenant-scoped analytics and call review through the local CPU checkpoint.

**Architecture:** Durable canonical session/turn/metric records are captured alongside successful domain commands, then a narrow worker recomputes bounded rollups/summaries/alerts and prunes analytics-only data. Typed dashboard projections drive compact analytics and session-review pages. Native measurements remain authenticated, optional and distinguish actual observations from unavailable data.

**Tech Stack:** Existing Python3.12/FastAPI/SQLAlchemy/Postgres/Valkey and React/Vite/strict TypeScript/TanStack Query/shared primitives; no chart library. Existing pinned OpenTelemetry API may be promoted directly after primary-source verification; no hosted inference for this phase.

**Spec:** `docs/superpowers/specs/2026-11-08-phase-6-analytics-design.md`; governing v0.1 §1.5D/2.12/2.13/4.2 and v0.2 §5/7.5.

## Global Constraints

- Separate named backend/frontend checkouts on `feat/phase-6-analytics`, stacked on Phase5; no merge, force-push, Phase7 or inference. Backend parent97db574; frontend parenta42b04b. Preserve all existing local data/ports/volumes/credentials; dental24sources queued.
- Applied migrations0001–0009 immutable. New analytics tables forced RLS/composite tenant ownership. Only voice_agent_test for DB suites; one suite at a time.
- No raw audio. Transcript capture OFF by default, prospective per-session snapshot; owner/admin settings/text/deletion only. Never store default-off text, raw tool args/readbacks/provider bodies/questions in telemetry or logs. Transcript retention7days allowed1–30; metadata90days/rollups365days. Deletion cannot resurrect on replay.
- Draft-test child events never contaminate original-clinic analytics. Text sessions never counted as voice calls or voice-stage latency.
- Sample durations finite0..600000ms; missing latency/usage/prices remain null with sample/coverage counts. p95 uses raw samples, never averages of percentiles. First-token/byte timing only when actually measured.
- All inference outside SQL locks. Canonical analytics emitted once per committed command; rollback/replay cannot double-count successful bookings. No shared singleton last-request metrics across sessions.
- Keep compact232sidebar/64top/24desktop16mobile gutters,44px actions, shared DM Sans/Noto Urdu, escaped bilingual text, keyboard focus, loading/empty/error/auth reset. No fake trend/progress, new generic metrics grid or browser persistence of sensitive data.
- Worker disabled by default, distinct narrow role/secret and analytics-only grants; no clinical/auth/recovery/source-text access. Local API+lightweight analytics-worker only after backup/migration. Embedding/knowledge/native voice stay disabled.

## Review Focus

1. Concurrent unknown retries, rollback and late worker metrics must not inflate counts or corrupt another call; Task2/3/4 actual command/replay/fence tests.
2. Transcript consent changes, short retention, expired/deleted text and arbitrary bilingual identity literals must not leak via staff projections, traces or summaries; Task2/4 privacy tests and Task5 account-switch UI tests.
3. Initial state task=book is not booking intent; a single call can book and then cancel; zero denominators/partial coverage must remain explicit; Task2/4 controlled domain transition fixtures.
4. Historical timezone/DST and late samples must reconcile raw records with recomputed rollups and exact p50/p95; Task4 timezone/bucket/quantile tests.
5. Disabled speech/embedding, isolated tests, stale rollout/worker, provider errors and unknown billing must remain truthful; Task3/4/5 boundary fixtures and Task6 local text-only smoke.

---

### Task 1: Analytics schema, typed definitions and least-privilege foundation

**Files:** create `analytics/{__init__,models,contracts}.py`, `migrations/versions/0010_analytics.py`, `tests_analytics/{conftest,test_schema,test_rls}.py`; modify `pyproject.toml` lint/test paths and role initialization only if necessary. No runtime capture/rollout yet.

**Interfaces:** canonical models `AnalyticsSettings`, `CallRecord`, `TaskRecord`, `OperationEvent`, `TurnEvent`, `Transcript`, `StageMetric`, `UsageInvocation`, `HourlyRollup`, `DailyRollup`, `AnalyticsAlert` and bounded dirty-work bookkeeping. Use exact binding builder resolutions in design. `TurnObservation` is request-local typed metadata, not a public request DTO. `StageMetric` supports stage/language/origin/actual duration and unique event identity; unknown metrics have no fabricated sample. Call lifecycle includes snapshot purpose/channel/timezone/date/UTC offset/expiry/trace, capture-at-start/consent/deletion state. Settings optimistic revision/capture_enabled/retention_days. Usage records permit unknown usage/rates/cost and safe actual invocation status independent of domain commit. Capture helpers later consume these contracts; API response models may be refined in Task4 with recorded integration notes.

- [x] RED migration/schema tests: forced RLS, foreign tenant/session/agent composites, duplicate stable identities, invalid duration/retention/enums/nonfinite/JSON shape constraints; caller text absent from safe metadata schema; transcript separated.
- [x] RED roles: analytics_worker NOLOGIN/NOSUPERUSER/NOBYPASSRLS/NOINHERIT; no clinical/auth/recovery/source-text access, no transcript text SELECT. Narrow bounded tenant routing and backlog-count/oldest-age functions current-tenant-scoped and worker-only, no public execute. Existing app/knowledge roles and credentials unchanged.
- [x] Implement additive0010 for complete schema, grants, indexes and typed models/contracts. Only discovery grants and analytics-table/required-column access; no default privileges. Separate optional analytics worker password provisioning preserves existing call signatures/secrets. Existing applied migrations byte-identical.
- [x] GREEN dedicated test schema/RLS and static/mypy, migration downgrade/reupgrade only disposable test DB. Commit and report exact columns/enums/helper signatures, tenant-role setup and interfaces for Task2–4; do not wire unimplemented routes.

### Task 2: Durable lifecycle/operation/task capture and transcript consent

**Files:** create `analytics/{capture,lifecycle,tasks,operations,privacy,transcripts}.py` using Task1 schema/contracts, `tests_analytics/{test_capture,test_privacy,test_lifecycle}.py`; extend `analytics/contracts.py` and add `migrations/versions/0011_capture_privacy.py` only if a concrete capture contract requires schema support; modify `brain/{sessions,routes,tools,voice_sessions,voice_routes}.py`, `scheduler/{models,main}.py` only where required, `pyproject.toml` test/lint paths.

**Interfaces:** `begin_session(db, row: AgentSession, now: datetime) -> None`; `record_command(db, row: AgentSession, *, command_id: str, before: dict, after: dict, action: dict | None, result: dict, observation: TurnObservation, now: datetime) -> None`; `finish_session(db, row, *, reason: str, now) -> None`; `TurnObservation` is server-created request-local capture of actual durations/usage and ephemeral input text/literals. No public client can supply it. `AnalyticsSettings`, `CallRecord`, `TurnEvent`, `Transcript`, `StageMetric`, `UsageInvocation` models are tenant-scoped. Record stable session_id/agent_id/version_id/purpose/channel/start/timezone/localdate/deadline/trace_id; avoid clinical FKs/PHI in safe records. Separate transcript rows and irreversible deleted marker.

- [x] RED tests for new session/successful and failed tool capture, concurrent exact replay/rollback, actual book/reschedule/cancel operation counts, info-only initial task=book, handoff/end/expiry hooks, isolated draft child ownership, composite foreign-session insert rejection and worker/no-clinical-access foundation. Use existing command fixture harness rather than duplicate simulator.
- [x] RED privacy tests default-off stores no caller/assistant text; opt-in snapshot persists bounded redacted English/Urdu literals/digits/email/DOB text; future settings changes cannot capture an already-off session; deletion marker wins over replay; no raw tool arguments/checkpoint/provider body in records.
- [x] Implement capture in the command transaction after receipt uniqueness but before commit. Preserve existing lock order; do not add Tenant locks after AgentSession locks. Store executed safe action/result metadata, operation outcome from actual successful confirmation and prior task, not default state or model prose. A command execution observation has actual elapsed command/tool durations; missing stage metrics absent.
- [x] Connect all existing session create/renew/end/voice termination paths so durable deadline/outcome follows actual lifecycle, without changing playback/verification/hold/confirmation behavior. No retroactive fabricated legacy telemetry.
- [x] GREEN focused tests+Ruff/mypy for changed modules and existing adjacent command/draft/voice controls. Commit; report exact schema/interface details for Tasks2/3.

### Task 3: Authenticated native/provider measurements and tracing

**Files:** create `analytics/{telemetry,tracing}.py`, `tests_analytics/{test_metrics,test_usage}.py`; modify `brain/{routes,provider,voice_worker_routes}.py`, voice runtime `{api,pipeline}.py`, native control tests and private metric schema as needed; optional dependency/license files only with evidence.

**Consumes:** Task2 request-local `TurnObservation`, canonical call/event/stage/usage tables. **Produces:** private `POST /voice/worker/{identity}/metrics` or consistent existing worker path; bounded batch max32 observations with canonical command/revision,event_id,allowlisted stage/language/value. Current authority validated using existing worker/runtime/generation/lease guards; no browser-supplied tenant or stage authority.

- [x] RED stale/foreign/ended worker, duplicate metrics/conflicting ID, negative/nonfinite/oversized values/batches, text-vs-voice contamination, missing STT/TTS/first-token observations, concurrent provider-use isolation and late timed-out response tests. No loaded models/real network.
- [x] Implement actual native STT/first published audio/TTS observations using monotonic clocks and current generation fences; call's end-of-speech anchor must be actual VAD end, not utterance start or API request start. Cached/filler audio must be explicitly classified; never claim actual model first byte from completed buffered synthesis. Record interrupted attempts without assigning successful response latency.
- [x] Capture actual NIM reported usage in request-local context, including background grounding context propagation and late-work fences; missing usage remains unknown. Snapshot configured rates only, no assumed free-tier zero. Do not alter strict output validation/budget/model/thinking settings or store response bodies. LLM completion and first-token are distinct stage keys.
- [x] Promote only the existing exact locked OpenTelemetry API after primary-source version/license check; instrument bounded span names/attributes with per-session trace correlation and no PHI. Exporter optional/unconfigured, no new external transmission. Test trace attribute allowlist and disabled export behavior.
- [x] GREEN focused/native control+static/schema checks; one appropriately scoped full backend regression if shared route/provider changes warrant. Commit and report measured/unavailable fields precisely.

### Task 4: Bounded rollup worker, analytics API, alerts and retention

**Files:** create `analytics/{aggregation,queries,routes,alerts,prometheus}.py`, `analytics_worker/{__init__,main,settings}.py`, `tests_analytics/{test_rollups,test_queries,test_alerts,test_retention}.py`, further additive migration only if Task1 schema needs extensions; modify init-role provisioning/Compose/Docker/schema exporter/CI as required.

**Consumes:** canonical Task1–3 records, captured settings and authenticated measurements. **Produces:** staff-authenticated read endpoints `/dashboard/analytics/{overview,quality,latency,intents,knowledge-gaps,alerts}`, `/dashboard/calls`, `/dashboard/calls/{id}`; manager `/dashboard/analytics/settings` GET/PUT and `/dashboard/calls/{id}/transcript` DELETE; private owner/operator metrics endpoint. Typed contracts expose actual numerators/denominators/null coverage/freshness/channel/datezone semantics, bounded pages and enums. Settings manager-only with revision concurrency. Contract exporter staff allowlist must exclude worker/operator-private routes.

**Worker interface:** `run_once(factory, *, clock, limit=100) -> dict` operates bounded discoverable tenant batches, recomputes eligible hourly/daily rollups and fact summaries, expires calls using stored authoritative deadlines, resolves alert state and prunes analytics-only records. Disabled main starts no connection. Separate role discovery returns IDs only; no patient/auth/recovery/KB-content access. Preserve existing worker/app credentials, optional distinct new analytics worker credential.

- [x] RED raw-vs-rollup reconciliation, repeat/late update, bucket concurrency and failed-worker atomicity, multioperation outcomes, initial-info-only intents, exact quantiles, missing usage/rates and zero denominators; date-range<=31/maxpage100, foreign agent/session and staff transcript denial, test exclusion.
- [x] RED DST/historical timezone preservation, setting revision conflict, immediate expiry after retention shortening, delete/replay/worker nonresurrection, metadata90/rollups365 bounded purge and no clinical/source deletions.
- [x] RED alert fire/recovery with >=5 samples for EN/UR e2e thresholds1500/2000ms, toolfailure>10%, actual provider errors/endederror calls and knowledge queue age>15min; per-tenant isolation/no duplicate alert history. Knowledge gaps group only keyed fingerprint/outcomes, count unavailable separately from irrelevant.
- [x] Implement exact documented definitions and current-snapshot queries, persisted replace/recompute rollups, summaries from safe facts only, authenticated bounded Prometheus histograms without high-cardinality/PHI labels. No LLM summaries/hosted calls or notification delivery. Expose worker staleness/coverage instead of silently displaying stale data as current.
- [x] Register typed routes/export contracts, keep runtimeAPI image free of model/parser dependencies and analytics worker defaults disabled. GREEN focused plus full backend/static/contracts and affected image/offline tests. Commit.

### Task 5: Compact analytics and call-review dashboard

**Files:** create `frontend/src/features/analytics/{AnalyticsPage,CallReviewPage,CallDetail,AnalyticsFilters,AnalyticsSettings,charts,presentation}.*` and focused unit fixtures/tests, `frontend/e2e/analytics.spec.ts`; modify API generation/route/navigation/shared styles only where necessary.

**Consumes:** exact generated Task4 schema/operation map. Reuse session-aware TanStack queries, shared PageHeader/QueryState/buttons/dialog/table/form tokens. Lazy routes `/analytics`, `/calls`, `/calls/:identity`; no extra chart dependency or competing UI library.

- [x] RED filter/range/page/auth reset/error/retry, null-rate/latency/cost coverage, real-zero vs missing, purpose/channel distinctions, RTL escaping, staff no-text/manager privacy opt-in/delete and initial/default-off states.
- [x] Implement aligned analytics header/filters, compact metrics strip, accessible daily/peak-hour/intent figures with table/text alternatives, quality/stage sample table, alert state and fingerprint-based knowledge gaps. No fake deltas/voice measurements or raw implementation JSON.
- [x] Implement paginated call/session log and routed detail with code-owned summary, safe tool trace, per-turn measured stage table, opt-in redacted transcript and explicit off/deleted/expired states. Keyboard/focus/back/late-auth responses safe; no transcript storage in browser caches beyond current authenticated in-memory queries, clear on account change.
- [x] Manager capture/retention dialog uses revision guards and explicit prospective consent/remaining-sensitive-data notice, no implied perfect redaction. Transcript deletion must invalidate detail without stale text flashing.
- [x] GREEN full frontend check and maintained serial browser cases at1440/1024/390: aligned spacing/16px mobile/44px controls/no overflow, empty and real-zero/partial data, bilingual timeline, filters/detail/back/focus/privacy and account switch. Visually inspect saved synthetic screenshots; fixtures remain distinct from actual live sessions. Commit.

### Task 6: Local CPU checkpoint, live text demo and publication

**Files:** backend/frontend `docs/phase-6-{testing,results}.md`, README/CONTRIBUTING/CI/license notes, curated synthetic evidence and opt-in maintained local smoke if needed. Controller owns final whole-branch review/publication.

- [x] Private backup of DB and roles, record existing version0009/role flags/counts/mounts, apply additive migrations normally without stamp/reset. Provision only a distinct narrow analytics-worker credential through reviewed path; preserve app/knowledge roles/volumes/secrets. Refresh local API and run lightweight analytics worker, embedding/knowledge/native voice off. Verify source-byte/image/health/CORS and disabled real inference.
- [x] In separate Pearl Dental Demo clinic, create real published-agent Text sessions through normal APIs with deterministic commands and no hosted calls. End info-only/handoff scenarios, refresh worker, compare persisted source events/rollups/API overview/detail and real knowledge-unavailable gap. Demonstrate actual zeros for voice and unavailable speech latency/cost; no fake production events/vectors. Existing dental24sources remain queued. Keep fixtures separate from actual observations.
- [x] Run all appropriate final backend/static/contracts/offline/native controls/image and frontend checks/browser, frozen source heads and exact logs. Tests use voice_agent_test only, no owner dotenv/assets or provider calls in CI. Preserve warnings and prior qualification gaps, don't suppress them.
- [x] Final fresh code review, concrete fixes with regression evidence and bounded re-review. Record definitions/roles/retention/trace-exporter limitations and owner test checklist. Update stackedPhase6 contribution/base workflow allowlists.
- [x] Publish Phase5 dental corpus dependency and Phase6 feature branches as authorized; create/attach public draft Phase6 PRs targeting Phase5 branches, verify authors/bases/heads/files/body/CI. Leave phases4–6 unmerged and stop at Phase6 owner checkpoint. Embedding remains pending unless separately resumed by owner.


## Binding implementation addendum

Read the design's “Builder-check resolutions” in full before task dispatch; these corrections override earlier shorthand in task text. In particular Task2 records canonical operations inside the domain-idempotency execute closure, not by counting successful confirm envelopes. Task2 implements a separate snapshotted pending/accepted/declined transcript-consent gate and bilingual greeting notice; no transcript row before acceptance, no consent utterance retained, current manager disable/retention/tombstones always gate insertion. Task3 native metrics flush before end/revocation; absent actual publication/first-byte/first-token observations remain null. Provider billing invocation ledger is independent of domain rollback and thread-safe. Task4 exact p95 uses percentile_cont on <=100000 retained samples and <=31day range wholly within90day raw coverage; older365day counters remain sealed/not zeroed. Task4 alerts use15minute windows and the exact minimum sample rules from design. Quality flags are repeated same/no-progress prompt twice,>=3 trusted interruptions, observedSTTconfidence<0.5,committedhandoff. Settings/text/deletion manager-only; no-store transcript responses. Update API/native response schemas only under their owning task and report handoffs.


## Publication record

The dental-corpus parent `97db5749b212551fbd09c5587d4708e45b239468` is published. Public draft PRs: [backend #7](https://github.com/awais-ur-rehman/voice-agent-backend/pull/7), [frontend #4](https://github.com/awais-ur-rehman/voice-agent-frontend/pull/4), both targeting `feat/phase-5-knowledge`. Author, dependency bases, files and bodies were verified; neither is merged. Initial hosted voice failures exposed a host-clock-dependent synthetic deadline fixture, corrected with low/high-origin regression evidence in `ec013e0`; production is unchanged. Final exact-head hosted checks are a separate finishing gate and must be green before the owner handoff. Local implementation checklist completion does not imply hosted or owner acceptance. Phase 7 remains unstarted.
