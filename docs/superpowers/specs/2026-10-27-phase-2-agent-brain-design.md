# Phase 2 agent brain design

## Goal and phase boundary

Implement v0.1 Phase 2 and the v0.2 additions: a guarded tool layer, layered prompts, versioned dialogue state in Valkey, patient verification, bilingual deterministic date/time resolution and speech-text normalization, task parking/resume, and an executable text conversation simulator. Stop for owner testing before Phase 3 voice integration and before merging Phase 2.

Reuse the Phase 1 scheduling/domain/database foundation and preserve its staff APIs. Staff tokens are not runtime credentials. Keep the frontend repository separate and unchanged; the React dashboard belongs to Phase 4. No microphone/WebRTC/STT/TTS pipeline, paid messaging, outbound calls, knowledge ingestion/RAG, analytics dashboard or GPU work is introduced. The MVP exclusion of outbound/reminder campaigns takes precedence over the separate product-roadmap use of the phrase “Phase 2.” WhatsApp is an extension plan, not a live integration or an authorization to send messages.

## Approach

Use a server-owned state machine with a constrained Nemotron planner. The spec requires code to own permissions, dates and transitions; a model-led agent with broad staff credentials would contradict that boundary. A fully scripted bot would not exercise the selected NIM model. Keep planner, dialogue/domain, persistence, tool execution and transport in distinct modules, with explicit typed interfaces rather than a generic agent framework.

The real agent reads actual tool results and changes actual state. Do not reuse the Phase 0 fixture-only eligibility builder as production authorization or inject expected fixture answers into requests. Keep the historical unrestricted-model failures and Phase 0 tests intact.

## Runtime configuration, identity and transport

Add minimal tenant-scoped agents and immutable agent versions sufficient for runtime persona, language mode, digit-reading preference and approved instructions. A CLI seeds/publishes synthetic runtime configurations; full draft/publish/rollback management and dashboard UI remain Phase 4. Clinic facts come from Phase 1 structured records. Add optional reviewed `name_ur` data for provider/service/patient read-back; never invent transliteration for a missing pronunciation.

Authenticated staff may create a text-test session for an enabled agent in their own tenant. The server fixes tenant, agent and version, then issues a short-lived session-scoped runtime capability with a separate audience/signing secret. Runtime endpoints accept that capability only; it cannot authorize any staff API. Validate the capability against the live server-side session, its scope, expiry and revocation. Tenant/session/agent identity, verification, confirmed flags and audit actor are never model-assignable arguments.

Provide typed session creation/inspection, text-turn, internal tool-command, resume and end endpoints plus an interactive CLI and scripted simulator. Internal delivery/interruption events require trusted runtime/operator authorization, not an arbitrary caller patch. A session resume requires the original capability or authorized staff; a guessed phone number alone never restores authorization. Transport errors do not expose patient details.

Use a typed runtime actor distinct from the staff Principal. Refactor Phase 1 appointment orchestration into shared transaction services while retaining all HTTP behavior and regressions. Extend audit attribution to distinguish staff users from agents; do not fabricate a staff account or give a model a staff bearer token.

## Dialogue state and transactional consistency

Valkey is the live dialogue store, with a thirty-minute inactivity TTL, versioned JSON and compare-and-set revision checks. Store active task/stage, raw and resolved fields/confirmation revisions, caller versus subject identity, verification status/attempts, offered options, hold metadata, parked tasks, resume hint, language streak, failure/silence/turn counters, the last ten delivered turns and a bounded structured summary. Raw factor values are not retained after verification: redact them from recent turns, summaries, command receipts and recovery payloads as well as factor fields. Summary updates cannot replace authoritative state. End/expiry revokes the session and releases its holds.

Use PostgreSQL session guard records, command receipts and a minimal structured recovery checkpoint alongside Valkey. Security decisions and appointment effects must not trust a stale cache after a crash. Persist every authorization-relevant transition through the session guard, including verification attempts/status, subject selection, hold binding and issued/confirmed read-back revision. A tool command locks the session guard, checks the expected revision and current authorization, and commits any database effect together with its idempotent result/next revision. Valkey is updated after commit. A retry after “DB committed, cache update failed” replays the receipt and repairs the cache; it never repeats the booking. Expired/revoked sessions cannot be resurrected through replay. Conflicting payloads under the same command ID are rejected.

Receipts contain only the structured state needed for recovery and the exact bilingual result, protected by RLS and bounded retention; no raw transcript is stored in PostgreSQL by default. Builder default: purge expired recovery/result payloads after twenty-four hours while retaining non-identifying session/audit metadata. Add a bounded maintenance path and verify the retention boundary. Valkey raw turns expire with the session. Existing patient records remain governed by their separate data lifecycle.

Serialize session commands with revision/fencing checks; a Valkey lock alone is insufficient if its lease expires while an operation runs. Preserve existing patient/provider lock ordering and the buffered database exclusion constraint. Server-generated operation keys bind session, task, confirmation revision and immutable mutation arguments. One transient mutation retry uses exactly the same key; after the next timeout, hand off. Replays do not repeat verification-attempt increments or extend an expired session.

## Verification and workflow guards

Implement the specified book, cancel and reschedule stages explicitly. `allowed_next` derives from live server state. Validate requests against both the public tool contract and current eligibility before executing. Invalid or injected requests fail closed rather than being repaired into an expected answer.

Patient lookup binds the normalized phone/candidate set to the session and returns masked/minimal information before verification. Verify an exact patient using the bound phone and supplied name or DOB; do not fuzzy-match identity or let a model assert success. Ambiguous shared-phone records require disambiguation without exposing names. Three failed attempts trigger handoff and reveal nothing; subject/task switches do not reset the failure budget. Changing subject resets verification and appointment-selection authorization. Caller contact remains distinct from the patient receiving care.

Private appointment lookup and cancel/reschedule require verification for the exact subject. Appointment selection must refer to server-offered records and ownership is rechecked in the database transaction. Critical-field corrections invalidate prior confirmation and affected selections/holds. Only a fresh explicit affirmative caller turn associated with the current code-generated read-back revision can confirm; model arguments cannot set confirmed=true. Negative, qualified or ambiguous replies do not commit. Creating a new patient separately requires name/phone read-back.

Task switching parks collected fields, releases ephemeral holds and invalidates stale read-backs. Restore the parked task without re-asking known fields; recheck identity, policies and availability as necessary. Digressions fetch the fact and retain the pending question. Multi-appointment cases disambiguate before mutation. Interrupted-history tests retain only delivered text and invalidate a truncated critical read-back. Actual audio playback-stop timing remains Phase 3.

## Tools, holds and results

Implement find/verify/create patient, structured clinic facts, available slots, hold, book/find/reschedule/cancel appointments, dialogue field updates, handoff/callback and end-call tools. Knowledge search returns a truthful no_answer until Phase 5. Persist callback requests for staff; no external transfer/message is sent. Emergency routing is a conservative bilingual code flag and fixed configurable script, never diagnosis or casual booking continuation.

Every executed tool returns the versioned bilingual `{ok,data,say,error}` envelope. Preserve existing error catalog semantics; map resolver/normalizer causes to appropriate validation fields without leaking private information. Tool timeouts and bad schemas have deterministic recovery. Critical result text goes directly from code to the caller; the model never rewrites or appends to it.

Offer opaque server-bound slot references. Holds last two minutes and are session-owned. Atomic Valkey operations compare occupied ranges, including buffers, rather than locking only identical start IDs. Expired holds are removed, another session cannot release/use them, and committing requires a current hold for the chosen slot. Agent search filters other sessions' holds. Staff booking can preempt a soft hold; the database remains final authority and the runtime handles the conflict. Test races, overlapping differently-started slots, lease expiry, cache failure and commit/replay recovery.

## Resolver, normalization and language

Implement all thirty-five resolver golden requirements and every v0.2 section 5.3 row against fixed clocks. “Next weekday” means the first occurrence strictly after today; the same weekday means seven days later. Booking-context kal/parson mean tomorrow/day-after; history ambiguity requires clarification. Weeks are Monday–Sunday. Time windows are half-open preferences on appointment starts, filtered by actual opening rules/exceptions. Missing AM/PM is inferred only when one interpretation survives the clinic hours; otherwise ask. Prayer-relative requests require configured break data. Reject Devanagari, invalid/past booking dates and unknown expressions safely.

Implement the full reviewed/proposed 0–100 Urdu lookup and all fifteen normalization goldens independently of the test fixture loader. Produce spoken dates, times, integer PKR amounts, grouped phone digits and approved names; do not send raw digits to Urdu TTS. Preserve the explicit English-digit reading toggle as structured language-aware text. Fractional currency, missing Urdu pronunciation and unsupported numbers return the specified domain errors instead of guessed wording. English normalization is included. No synthesized audio or pronunciation/MOS acceptance is claimed.

Respect fixed en/ur modes and two consecutive dominant-language turns for bilingual mode. Neutral acknowledgements and isolated loanwords do not flip the response language. Phase 2 uses an explicitly identified text-language adapter; it does not claim to implement Whisper language-ID. Keep Urdu output in Urdu script except the configured English-digit segments. Code templates use respectful آپ, one question and short replies. Native wording review remains an owner checkpoint.

## NIM planner and prompt layers

Compose platform rules, language/style, immutable agent persona/preferences, structured clinic facts, trusted current state/allowed_next, bounded summary and recent delivered turns in that order. Mark clinic free text and KB references as untrusted. Exclude capabilities, API keys, DOB and unnecessary identifying fields from model context; process verification factors in code where possible. Synthetic data only for hosted development tests.

Use the owner-selected `nvidia/nemotron-3.5-bolt-e4b` with thinking disabled and a state-constrained JSON action contract. The model may extract literal caller fields or request eligible actions; it cannot compute authoritative dates or directly mutate the database. Keep the provider interface separate from state/transport. Explicit configuration controls timeouts, request budget and circuit behavior; do not invent a validated secondary model. Missing credentials or provider failures lead to a truthful fallback, not a claimed successful conversation. Preserve the existing NIM credential file; inject it server-side only when live mode is explicitly enabled, never into images/logs/prompts.

## Build, testing and acceptance

Add incremental PostgreSQL migrations and an authenticated, digest-pinned Valkey service without renaming/removing the existing development project/volume. Use the [official compatible Python client](https://valkey.io/clients/) with reviewed, locked dependencies. [Atomic operations](https://valkey.io/docs/topics/introduction/) support range-hold updates; database fencing still provides the authorization/commit boundary. Add only missing runtime secrets to private local configuration without replacing existing values. CI uses isolated PostgreSQL and Valkey, fake provider clients and no real credential files. Preserve all forty-eight Phase 1 and sixty-three Phase 0 tests.

The new simulator must execute multi-turn commands against actual PostgreSQL and Valkey and assert final database/state effects, not accept precomputed fixture state as proof. Cover the original thirty scenario families in both languages, plus additional variation cases, rejected malicious tools, verification exhaustion, family subjects, correction, parking/resume, multiple appointments, confirmation invalidation, slot races, retry recovery, reconnect/expiry, summaries, injection, missing knowledge, emergency and language switching. Scripted/fake-planner guard tests and actual NIM-in-the-loop results must be labelled and reported separately; no oracle enters provider requests and no simulated response is counted as inference.

Run real hosted synthetic end-to-end booking, reschedule, cancellation, digression/correction and family/task-switch paths in English and Urdu through the same guarded pipeline. Record actual requests, outcomes and failures; resolve blocking failures before claiming the Phase 2 checkpoint complete. Native language quality is not inferred from mechanical checks. Missing provider access or unfinished scenario coverage must be reported rather than silently waived.

Finish with an independent whole-branch review, full test/lint/type/CI checks, a rebuilt local testable service, interactive/simulator commands, fixtures/demo data, evidence and an exact owner checklist. Merge accepted Phase 1 PR #2 only with owner approval so Phase 2 has a clean develop base. Leave Phase 2 in its own unmerged draft PR and stop before voice integration.
