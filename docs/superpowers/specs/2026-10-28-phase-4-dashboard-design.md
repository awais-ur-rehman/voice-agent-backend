# Phase 4: clinic operations dashboard

Status: design and compact operations direction approved by the owner on 2026-10-28. Phase 4 implementation and local integration verification are recorded in the testing/results documents. Fresh whole-branch review, public draft PRs and owner acceptance are tracked separately; this status is not a merge approval.

## Intent and checkpoint

Build the Phase 4 React dashboard from the v0.1 PRD/TRD and v0.2 addendum. A clinic owner should be able to create a clinic, configure its schedule and agent, test a draft, and publish without a developer or CLI. Staff should be able to manage appointments and find patients efficiently.

The owner reports that functionality works but rejects the Phase 3 UI for alignment, padding and visual quality. Their selected direction is a compact operations dashboard. This phase replaces the console's editorial landing layout with a consistent application shell and reusable controls. Functional acceptance does not erase the recorded speech-resource failures.

Stop after Phase 4 code, verification, public draft PRs and precise owner testing instructions. Do not begin Phase 5 or merge Phase 4 automatically.

## Existing system and constraints

- Backend: Python 3.12/FastAPI/PostgreSQL, tenant RLS, staff JWT authentication, rotating refresh tokens, typed scheduling APIs, transactional booking and guarded conversational runtime.
- Frontend: React/Vite/strict TypeScript, TanStack Query, shadcn-style Radix primitives, generated voice contracts, functional LiveKit client. The current App mixes authentication, console and state handling in one large component.
- Agent versions are immutable and sessions pin a version. Publishing currently requires a CLI. Agent configuration lacks greeting, tone and selectable voice IDs. Scheduling CRUD mostly exists; patient merge, browser agent drafts and clinic registration do not.
- Backend Phase 3 PR #4 and frontend Phase 3 PR #1 remain draft/unmerged. Ask for permission to merge them into develop before basing Phase 4 feature branches on develop. Preserve all Phase 3 evidence and migrations.
- Nemotron remains the hosted planner with thinking disabled. Secrets stay server-side. Offline tests never load owner credentials; hosted checks are separate and bounded.
- The 8 GiB CPU machine has not passed ASR or Urdu synthesis acceptance. Voice admission remains disabled until real evidence qualifies the exact runtime. No heavy inference retry or increased resource cap belongs to this phase.

## Approaches and owner decisions

1. **Compact operations dashboard — selected.** Persistent navigation and a stable content grid serve daily scheduling, patient lookup and configuration. The agent test panel sits inside agent configuration. This requires a shared shell and coordinated pages, but matches the owner's requested professional dashboard.
2. **Agent studio first — not selected.** A large conversation panel with surrounding configuration would prioritize agent experimentation but leave scheduling secondary and reproduce the console's hierarchy problem.

The owner chose approach 1 and isolated synthetic draft testing. The typography, dimensions, color palette, dependency choices and internal architecture below are proposed builder decisions, not preferences attributed to the owner.

## Scope boundaries

Included: browser signup/login, clinic setup, provider/service relationships, hours and exceptions, calendar/manual scheduling, patient directory/history/merge, agent CRUD, greetings/tones/languages/instructions, selectable voice metadata, draft/publish/version history/rollback, enable/disable, and a draft test panel.

Knowledge ingestion, files/FAQs/URL indexing and retrieval remain Phase 5. Analytics and persistent call review remain Phase 6. No empty analytics cards, invented call counts, inactive navigation links or fake knowledge-upload screens. The overall PRD's public website widget is not listed in the Phase 4 milestone; this proposal leaves its public visitor admission/abuse controls and distribution for a separately agreed checkpoint. Phase 4 exposes the authenticated dashboard test panel only. Approval of this design explicitly approves this scope boundary.

Speech-dependent voice previews and spoken draft tests will be implemented as capability-aware paths, with acceptance blocked while the required native runtime is unavailable. Phase 4 will make text draft tests usable now. Therefore the full spoken exit criterion remains pending; report a Phase 4 CPU dashboard checkpoint, not complete end-to-end voice acceptance.

The optional outside-hours callback mode in FR-A6 is deferred: the current data model does not distinguish clinic opening hours from provider bookable hours, and the PRD does not define which operations that mode should replace. Preserve current callback behavior; do not add an ambiguous toggle.

## Visual system

Use a neutral operations interface: white surfaces, pale slate canvas, charcoal text and one blue accent. Restrained borders distinguish sections; no decorative hero, serif display heading, gradients or ornamental dashboard metrics. The user's BoardUI reference informs hierarchy and consistent tokens; it does not replace the agreed React/Vite and shadcn foundation.

Concrete starting tokens, tuned after browser inspection:

| Element | Rule |
| --- | --- |
| Spacing | 4, 8, 12, 16, 24, 32 px; repeated layouts use shared tokens |
| Desktop shell | 232 px sidebar, 64 px top bar, 24 px content gutters |
| Small screens | Collapsible navigation, 16 px gutters, no page-wide horizontal overflow |
| Typography | DM Sans; body 14/20, labels 12/16, page title 24/32 semibold |
| Forms | 40 px controls, labels above fields, 8 px label gap, 16–24 px field spacing |
| Tables | 48 px minimum rows, 12/16 px cell padding, aligned action column |
| Touch and focus | 44 px minimum mobile action targets; visible 2 px focus ring |
| Urdu | Noto Nastaliq Urdu, approximately 2.0 line height, automatic text direction, rows expand |
| Motion | Short functional transitions, reduced-motion support; no forced smooth scrolling |

At >=1280 px, calendar and configuration can use two-column regions. Between 768 and 1279 px, reduce secondary columns or move detail into an accessible sheet. Below 768 px, use one column and an appointment list by default. Calendar itself may scroll within its labeled region; the entire document may not overflow.

Shared patterns: AppShell, PageHeader, FormField, FieldError, ErrorSummary, Toolbar, DataTable, StatusBadge, EmptyState, LoadingState, ConfirmDialog and DetailSheet. Build these as repeated needs appear, not as an exhaustive component framework. All forms share label alignment, validation placement, pending behavior and keyboard order.

## Navigation and workflows

### Authentication and first clinic setup

Unauthenticated routes: login and create clinic. A compact auth panel uses the same tokens as the application. Signup collects owner name/email/password and clinic name/timezone. Server atomically creates one tenant and owner, then issues the existing token pair. Duplicate emails use a safe conflict response; password hashing and email normalization reuse the current implementation. No automatic email/invite/reset service is invented.

Registration uses an explicitly enabled deployment setting, restrictive request limits and a narrow server-owned provisioning function; anonymous requests cannot insert users into existing tenants or select roles. Normal API DB credentials remain non-owner without BYPASSRLS. Integration tests prove the provisioning boundary. This local checkpoint is not a claim of production identity verification.

After login, a real-data setup checklist links to clinic profile, providers, services, hours and agent. Completion derives from persisted records. Existing clinics go directly to Schedule. No browser-only completion flags. Owner can complete setup in any order except genuine dependencies, such as assigning a service to a provider.

Clinic settings cover clinic name/timezone, structured English/Urdu address and preparation information, booking notice/horizon/cancellation/daily limit, and staff accounts. Structured profile is a single clinic source of truth for new versions; publishing snapshots the relevant data. Existing sessions retain their pinned snapshot.

### Schedule

Day/week calendar and list share date range and provider filters. Display clinic timezone explicitly, store instants in UTC, derive visible day boundaries using the clinic timezone. Handle DST ambiguity rather than silently using the browser timezone. Changing clinic timezone requires explicit confirmation and explains that existing appointment instants stay unchanged.

Select a slot or New appointment: choose patient, provider, service and an actual API slot; review details, then confirm. Edit/reschedule and cancel use existing transactional APIs and required explicit confirmation/idempotency. No drag-and-drop booking in this checkpoint: moving an event must not bypass the server's slot/confirmation rules. Conflicts show returned alternatives and preserve the user's form values.

Hours pages support provider/service assignment, multiple nonoverlapping working intervals, breaks and date-range exceptions. Friday prayer template splits the chosen Friday interval around editable 12:45–14:30. Ramadan adds explicit custom-hours dates. Templates preview their changes before saving and are idempotent; they never silently overwrite unrelated rules. Eid/holiday dates are entered manually. Show an in-app admin reminder from 14 days before a configured upcoming holiday to review hours; do not calculate moon dates or introduce email scheduling.

### Patients

Search by English/Urdu name and normalized phone; debounce and cancel stale searches. Paginate on the server. Patient details show mixed-script contact fields and appointment history from the API. Shared family phone numbers do not imply the same patient.

Owner/admin can select a source and destination, inspect both records and affected appointment counts, choose each conflicting retained field, and confirm a merge. Staff can create/edit patients and manage bookings but cannot merge. Never auto-merge or discard identity conflicts silently. DOB is displayed only where the operator needs it. URLs use opaque record IDs and do not contain names, phone numbers or transcripts.

### Agents

Agent list shows name, language, enabled state, published version and draft-change state. Create begins with a draft; rename is a draft edit until publish. Delete archives the agent after explicit confirmation; retain versions and historical references.

Agent detail sections: General, Voice, Versions, and Test. General includes English/Urdu greetings, friendly/formal/concise tone, additional instructions, language mode and digit-reading preference. The system AI disclosure and safety rules remain code-owned and cannot be removed by instructions or greeting edits. Validate required language fields according to language mode. Existing callback and scheduling policy remains unchanged by this editor.

Draft saves are explicit, carry a revision and show saved/unsaved state. Navigate-away warns only when actual edits are unsaved. A conflict never overwrites another editor: show reload/review options. Publish validates a complete configuration, reviews the changes, then creates a new immutable version atomically. Version history includes number, timestamp and publisher; rollback creates a fresh version copied from a selected past version and records the source, preserving history. Existing sessions stay pinned. Disabling or archiving blocks new admission; already admitted sessions retain their pinned configuration and may finish within existing deadlines. If an immediate stop is later needed, it requires a separately explicit action.

### Voice catalog and activity

Use server-owned records with language, gender, engine, frozen config, revision and capability/preview status. Show at most three voices per gender per language; bilingual selection requires one English and one Urdu voice of the same gender. Preview only a real pre-rendered asset matching that record's immutable fingerprint. Missing preview explains why the button is disabled; no browser speech synthesis substituted for the selected model.

Catalog metadata may be provisional while hardware evidence is blocked. Publish may store provisional selections with a clear warning; it does not enable voice admission. A spoken session requires all chosen voice/ASR/config fingerprints to satisfy readiness. Voice selection must reach the pinned version, worker synthesis configuration and cache keys. A cosmetic picker wired to a constant voice is unacceptable. Existing versions without new fields retain legacy defaults through an explicit schema migration/reader adapter.

Thinking Orbs is proposed only for genuine agent activity in the test panel. Its listening/working/paused state follows actual transport/planner state; reduced motion pauses it. `thinking-orbs` 0.3.2 metadata reports MIT and React >=18; reverify the package/source before install. React Bits and Lenis are evaluated references, not mandatory dependencies: native calendar/table/form scrolling has no demonstrated need for Lenis, and decorative animation would work against this dashboard.

### Draft testing — owner-selected isolation

Use an explicitly labeled isolated test clinic with synthetic patients and bookings. A test snapshots the selected saved draft and current public clinic scheduling configuration. It copies providers/services/hours/exceptions/profile with new IDs; it never copies real patient records, appointment histories, transcripts or callback phone numbers. The test panel states that changes affect test records only and shows the tested draft revision. Unsaved edits must be saved first. A new test after configuration changes creates a new snapshot; no hidden mid-session mutation.

The server creates a separate synthetic tenant for each test scope and stores a parent-owned mapping to it, creator, original agent/draft revision, expiry and runtime IDs. The mapping is under parent tenant RLS. Dashboard endpoints accept an opaque test ID, check original clinic membership, creator and expiry, then switch the server transaction to the mapped synthetic tenant. Client-provided tenant IDs are never accepted. Synthetic runtime audit actors belong to the synthetic tenant; the parent mapping records the real initiator. Browser receives no arbitrary-tenant authority or runtime bearer capability.

Reuse existing real planner, dialogue state machine, ToolEngine and appointment transactions in that tenant. Staff-authenticated test routes adapt requests to the internal session service; they do not use ad hoc HTTP self-calls or fork the booking implementation. Test snapshot versions have purpose=test and are excluded from published production history. Never move the live agent's active_version_id to test a draft.

Text testing works with the hosted Nemotron and shows conversation and redacted structured tool decisions. Hide tokens, identity verification inputs and internal prompts. Evidence is scoped to the current authorized browser session; no default transcript logging. Real audio uses the same isolated snapshot and existing guarded Phase 3 media path only when native capabilities are ready. Unavailable speech shows a specific blocked state alongside working text tests.

Tests expire after 30 minutes, with active calls bounded by existing 10-minute deadline. Limit active tests per user/clinic; expire/revoke stops new commands, invalidates leases and cleans rooms/capacity. For this checkpoint retain expired synthetic rows, excluded from all production lists and marked with a server-only test marker. A bounded cleanup command revokes sessions and clears ephemeral recovery data; it does not physically delete tenants or bypass immutable version protections. No volume deletion. Long-term retention/purge is separate maintenance work.

The owner rejected the live-clinic alternative for this phase. Do not implement a second test mode speculatively.

## Backend data and contract work

- Incremental migration after 0003. No reset or destructive re-seeding. Add RLS to new tenant-owned tables and composite tenant-qualified FKs. Test with the actual unprivileged application role.
- AgentDraft: one row per agent, typed versioned JSON config, revision, updated_at and updated_by. Extend Agent with archive metadata. AgentVersion gains purpose, publisher/source metadata and monotonically assigned per-agent published number. Existing immutable rows remain intact; safe backfill is migration-owned.
- Agent mutations lock the agent then its draft, validate expected revision, and use existing tenant-scoped idempotency machinery for retried publish/rollback. Archive/disable and new session admission share a lock/check protocol so admission cannot race past a committed disable.
- Typed AgentConfig moves from CLI bootstrap to a shared domain module with backward-compatible v1 reader and explicit v2 fields. Bootstrap imports it. All runtime readers, templates and worker schemas update together. Instructions remain bounded and cannot rewrite tool permissions.
- Clinic profile storage is tenant-owned and structured. New versions snapshot it; schedule policy continues to use authoritative live scheduling data. Version history makes this distinction visible.
- Voice catalog is a small server-owned frozen manifest with validated public projections, not user-editable raw engine JSON. Preview file paths/URLs are allowlisted and contain no signed provider credentials. Reference exact upstream engine/model licenses in dependencies documentation.
- Patient gains revision and merged_into_id. Merge is an audited idempotent transaction. It locks source/destination deterministically with appointment operations, prevents cycles/cross-tenant merges, moves appointment ownership and retains the source tombstone. Define canonical resolution centrally; merged source is hidden from active search and cannot receive new bookings. Preserve historical audit identity and reject conflicts instead of overwriting concurrent edits.
- Merge must preserve booking limits under concurrent operations. Extend the existing patient advisory lock protocol across both IDs and canonical lookup; do not add an incompatible lock order. Invalidate patient-bound in-flight runtime verification when its patient is merged; require re-verification against the canonical record. Do not silently grant source verification to a different identity.
- Patient search adds compatible optional filters/pagination using PostgreSQL simple text search/trigram where appropriate for Urdu and normalized phone. Retain existing API responses for legacy clients; new paginated envelopes use dedicated versioned dashboard endpoints where shape differs.
- Browser registration provisioning, draft tests and role checks require explicit endpoint contracts. Generate a versioned public dashboard OpenAPI/JSON-schema artifact and TS types; do not expose worker credentials or internal schemas. Keep existing voice.v1 wire contract stable unless a genuine versioned change is necessary.
- Preserve existing appointment snapshot durations/buffers, exclusion constraints, idempotency and bilingual code-owned confirmations. UI permissions are advisory; server roles and ownership are authoritative.

## Frontend architecture and dependencies

Keep React/Vite, strict TS and one npm lock. Use a compatible stable router, React Hook Form plus Zod for forms, TanStack Query for server state and shadcn/Radix for accessible dialogs/selects/tabs. Verify versions, peer compatibility and licenses at implementation time. Use FullCalendar's free React/day/time-grid/interaction packages with named-timezone support if its keyboard and responsive integration meet requirements; no premium resource scheduler. Provider filtering avoids paid resource columns. Calendar interaction must have a keyboard-accessible list alternative.

Feature folders: auth, clinic, scheduling, patients, agents and voice. Keep existing voice media/protocol logic independently tested; mount it through a focused test-session adapter rather than rewriting transport with page layout. Shared UI primitives and app shell live outside feature domains. Use route lazy loading for calendar/voice-heavy code where it improves initial load.

Auth provider keeps access and refresh tokens in memory, calls /me for tenant/role, refreshes once on simultaneous expiry, and retries each safe request at most once. Do not place credentials in local/session storage or URL. Full reload requires login in this checkpoint. Logout cancels requests, closes voice, revokes refresh family and clears query data. Cache keys use an internal auth-session identifier plus tenant, not raw tokens. Ignore old responses after account/session switch. Mutations use server-issued/validated IDs and fresh idempotency keys per user intent; retries reuse the same key.

Server field errors map to labeled controls plus a focused summary; API failures preserve input. Query invalidation follows real dependencies (booking changes calendar/history, service changes slots, publish changes agent status). Use optimistic UI only for safe reversible presentation; never claim a confirmed booking or published version before server acknowledgement.

## Authorization matrix

| Capability | Owner | Admin | Staff |
| --- | --- | --- | --- |
| Schedule, patient search/edit/history, manual bookings | yes | yes | yes |
| Clinic/provider/service/hours configuration | yes | yes | no |
| Agent drafts/publish/rollback/archive/test | yes | yes | no |
| Patient merge | yes | yes | no |
| Create ordinary staff users | yes | yes | no |
| Create admin users | yes | no | no |

No new platform-owner backdoor or cross-clinic browser tenant switch. Read-only configuration visibility follows existing safe staff projections; restricted actions never appear as working controls for staff.

## Verification and owner acceptance

Backend: migration on an existing Phase 3 database; fresh database migration; role/RLS and registration boundary; cross-tenant every new resource; optimistic draft races; publish/rollback retries; old sessions pinned; disable/admission race; selected voice fingerprint propagation; unavailable previews; test data isolation and expiry; patient merge races with booking/reschedule/cancel/daily limits and verification; Urdu search; timezone/DST; template preview/idempotency. Preserve Phase 1–3 regression suites. Tests use synthetic fixtures and fake providers; bounded live NIM test is reported separately.

Frontend: lint/type/build, generated-schema drift checks, meaningful form/auth/route/query lifecycle tests, scheduling conflict recovery, draft conflict/publish/rollback, role-gated controls, selected-voice state, isolated text test and explicit unavailable audio states. Preserve critical playback/ACK/interruption tests. Keyboard-check dialogs, forms and calendar-list alternative. Check reduced motion and mixed English/Urdu text.

Browser visual acceptance at 1440, 1024 and 390 px: consistent gutters/column alignment, legible dense tables, no clipped labels, no whole-page overflow, long names and validation errors wrap, Urdu rows are not clipped, empty/loading/error states retain layout. Inspect screenshots, then adjust; a passing build is not UI acceptance.

Owner walkthrough: create a fresh clinic, add provider/service/hours, apply an exception, create/publish agent, run a synthetic text booking and verify it is absent from production calendar, manually book/reschedule/cancel a real synthetic fixture appointment, search Urdu patient/history, merge two synthetic duplicates, save concurrent drafts to see conflict handling, rollback a version, disable new admission, inspect mobile and keyboard layouts. Exact accounts, URLs, commands and known blocks go in the final testing guide without publishing credentials.

Phase 4 delivery report separates completed dashboard behavior, automated results, visual evidence and pending speech/human voice review. Publish backend/frontend draft PRs into develop, leave them unmerged, and stop for owner testing.

## Design review and visual proposal

A fresh builder-check reviewer found one unanswered planning issue: the optional outside-hours callback semantics. The builder resolved it by explicitly deferring that optional feature above. The owner separately selected isolated synthetic tests. No additional product decisions remain open before design approval.

The owner requested a browser mockup. The throwaway preview is outside the product repos at workspace `.superpowers/brainstorm/64138-1793206550/content/operations-dashboard-v1.html`. It shows Schedule and Agents, desktop/tablet/mobile sizes and synthetic English/Urdu content. It performs no API requests. Screenshots are in the same session folder. It is a visual proposal, not completed Phase 4 functionality or a source of domain behavior. Product implementation must use the reviewed React architecture and real endpoints.
