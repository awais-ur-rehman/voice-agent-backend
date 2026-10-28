# Phase 3 CPU Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline implementation; bounded independent work may use dispatching-parallel-agents. Steps use checkbox syntax for tracking.

**Goal:** Deliver the approved real English/Urdu CPU browser-voice testing checkpoint, retain GPU latency/native-corpus gates as pending, and stop before Phase 4.
**Architecture:** Self-hosted LiveKit carries caller/worker media; Pipecat controls generation-fenced speech and interruption. A separately locked native Python speech worker talks to the existing SQL-fenced brain through authenticated voice endpoints. A minimal separate React frontend gets media-only grants.
**Tech stack:** Python 3.12, existing FastAPI/PostgreSQL/Valkey, Pipecat/LiveKit/Silero, faster-whisper CPU int8, Kokoro, gated Indic Parler; React/Vite/TypeScript/TanStack Query/shadcn primitives.
**Spec:** `docs/superpowers/specs/2026-10-28-phase-3-cpu-voice-design.md` (owner approved October 28, 2026).

## Global constraints

- Apple M2 arm64, 8 GiB RAM; one CPU call and one heavy inference job at a time initially. Measure before integrating heavy models; no silent model downgrade, fabricated Urdu inference or unapproved paid compute.
- Python 3.12; existing API environment stays independent from speech dependencies. Stable compatible pinned packages, separate locks and exact artifact/image revisions/licenses.
- Browser never receives runtime, NIM, HF, worker or LiveKit signing credentials. No raw audio/identifying transcripts persisted by default. Public fixtures synthetic.
- Every authorization-relevant voice transition uses SQL guard/worker fencing. Code readback must finish before a fresh affirmative utterance can authorize mutation.
- Thirty-minute session inactivity; two-minute holds are not extended for slow CPU synthesis. Ten-minute call maximum; thirty-second reconnect grace; silence reprompts six/twelve seconds then end.
- Preserve existing data/credentials/Compose volume identifiers and all 429 Phase 2 + 63 Phase 0 checks. Never relabel the earlier unreproduced retry assertion as a confirmed fix.
- Phase 3 feature branches/draft PRs in separate repos; no merge or Phase 4 work at this checkpoint.

## Review focus

- Early affirmative starts before readback ends but ASR finishes after: reject confirmation (Task 2/3).
- An interrupted/stale worker completes synthesis or HTTP planning after reconnect: discard output and prohibit state advancement (Task 2/3).
- CPU synthesis consumes the hold or process memory: release/invalidate safely and report actual failure, never book/relabel a mock as voice (Task 1/3).
- Browser fabricates a delivery acknowledgment or another caller identity: bound segments/grants/participant and no state patch authority (Task 2/4).
- ASR script/digit/name mistakes or two shared-phone candidates: deterministic conservative parsing/exact verification/readback, never fuzzy repair (Task 1/2/3).

## Task 1: Real speech capability and memory gate

Files: create `voice/pyproject.toml`, `voice/uv.lock`, `voice/src/voice_runtime/{config,assets,cli}.py`, `voice/src/voice_runtime/speech/{contracts,process,whisper,kokoro,parler}.py`, `voice/tests/test_speech_contracts.py`, `voice/tests/test_process.py`; update `docs/licenses.md`, add `docs/phase-3-dependencies.md`.
Interfaces: immutable `PCMChunk(samples: bytes,sample_rate:int,channels:int,text_span:tuple[int,int]|None)`; `ASRResult(text:str,language:Literal['en','ur'],confidence:float|None,uncertain:bool)`; speech adapters expose cancellable `transcribe(pcm,language_mode,public_vocab)` and `synthesize(text,language,voice_config,generation)`; each reports actual artifact/device/streaming capability. Subprocess supervisor owns deadlines, memory observation and cancellation rather than running blocking generation on the media loop.

- [ ] Write failing contract/process tests: reject malformed/unbounded PCM, stale generations, invalid-script results, unsupported language, and memory/deadline failure; a killed job cannot emit later output. Expected test returns a failure rather than a fake successful result.
- [ ] Run `uv run --project voice pytest voice/tests/test_speech_contracts.py voice/tests/test_process.py` red before adapter implementation (use light test doubles only for process failure paths).
- [ ] Review primary source versions/peer compatibility/native wheels/licenses; create isolated locked environment, pinned artifact manifest and explicit asset-download CLI using saved HF_TOKEN without printing it or signed URLs. Start specified turbo CPU int8; record exact settings. No package/weight enters API image or Git.
- [ ] Implement the interfaces and real Kokoro/Parler/Whisper adapters, with bounded inference and clear native-streaming versus completed-segment behavior. Keep heavy jobs serialized; observe peak RSS and unload/process-reclaim if residency is unsafe.
- [ ] Execute real English/Urdu synthesis and ASR on synthetic clips; record cold/warm load, first PCM/completion/peak RSS, hashes/device and outcomes in ignored private results. These are synthetic loopback checks, not native WER/MOS. If safe Urdu inference fails, stop claiming functional acceptance and report the measured resource blocker.
- [ ] Verify targeted tests/lint/types and actual real-audio artifact contents; commit focused Task 1 files. Root reviews this gate before connecting all heavy models to a live call.

## Task 2: Voice grants, session fencing and core delivery guards

Files: create `brain/voice_contracts.py`, `brain/voice_sessions.py`, `brain/voice_routes.py`, `brain/voice_delivery.py`, incremental migration, tests `tests_brain/test_voice_sessions.py`, `test_voice_delivery.py`, `test_voice_input.py`; modify `scheduler/models.py`, `scheduler/config.py`, `scheduler/main.py`, `brain/{state,sessions,tools}.py` only at shared boundaries. Export `packages/schemas/voice.v1.json` with `scripts/export_voice_contracts.py`.
Interfaces: typed voice create/inspect/end/resume responses; browser `MediaGrant(url,room,participant,token,expires_at,session_id)`; internal `WorkerGrant` never in browser response. Version-one control messages bind session/generation/segment/participant/sequence. New tenant-scoped VoiceSession links AgentSession + immutable version/opaque room, status and SQL worker lease. Delivery metadata stays under authoritative session checkpoint/guard; no transcript table is needed for this checkpoint.

- [ ] Write and run red real-SQL tests: cross-tenant denial; browser media JWT cannot call staff/runtime/worker APIs; least-privilege room/mic grants; failed room provisioning cannot leave usable session; duplicate creation is one session; worker lease loss/renewal/expiry/reconnect; a guessed phone cannot resume.
- [ ] Write and run red core tests: issued voice readback cannot confirm; fully acknowledged current readback plus later affirmative may; early-start/late-ASR yes, truncated/stale generation, missing/fabricated sample offsets and slow-synthesis hold expiry never mutate. Raw text endpoint/capability cannot bypass a voice-mode core guard. Existing text-mode confirmations continue passing.
- [ ] Add exact reviewed `name_ur` factor matching plus conservative digit-sequence input parsing; write bilingual/shared-phone/ambiguous-number red tests. Never add private patient candidates to ASR vocabulary or move factors into NIM.
- [ ] Implement separate transport/worker authorization, incremental RLS schema, lease/revision fencing and core confirmation context. External room operations occur outside SQL locks with compensation, receipts and capacity checks. Keep user/model fields unable to set transport/delivery/verification.
- [ ] Export schema/generated contract material and test drift. Verify migration down/up, existing Phase 1/2 behavior and all new security tests serially on dedicated stores; commit.

## Task 3: Pipecat/LiveKit runtime and actual playback lifecycle

Files: create `voice/src/voice_runtime/{api,session,pipeline,delivery,cache,metrics}.py`, `voice/tests/test_session.py`, `test_delivery.py`, `test_livekit.py`, `voice/tests/test_real_voice.py`; add pinned LiveKit Compose config/profile, opt-in setup/config examples and `scripts/smoke_phase3.py`.
Interfaces: Task 1 speech contracts, Task 2 version-one HTTP/control schemas. Controller owns `generation`, `utterance_started_at`, delivery segments and independent VAD/control processing. API client retains same command ID/payload for ambiguous transport retries and reconciles receipts before a correction. Worker lease/generation is required for every voice state transition.

- [ ] Red deterministic tests: immediate stop/queue flush, delayed planner/TTS completion after interruption, partial segment acknowledgment, backchannel versus early critical yes, maximum call time, six/twelve-second silence, reconnect grace/expiry and cleanup. Use clocks/barriers rather than arbitrary sleeps.
- [ ] Implement real Pipecat LiveKit transport, caller-track filtering, Silero VAD/endpointing, bounded PCM conversion and subprocess speech adapters. Restrict ASR language to en/ur and pass honest trusted metadata into two-turn response language selection.
- [ ] Implement protected issued/delivered/invalidated readback flow, conservative fully-played segment ledger and bounded output queue. Cancel stale inference/publication; do not invent heard-word alignment. Retry a noncritical backchannel at a safe boundary and fully reissue critical readback.
- [ ] Add exact static greeting/filler cache with artifact/config hashes and no dynamic patient audio persistence. Use only code-selected say for actual output. Surface memory/model/timeout/output failures and callback behavior honestly.
- [ ] Exercise a real local LiveKit room/RTC subscriber with the actual worker and synthetic PCM. Verify actual decoded audio, cancellation/disconnect/expired hold/receipt reconciliation and cleanup. Separate this real integration from fake adapters and from browser/speaker manual acceptance.
- [ ] Verify runtime tests/lint/types and safe setup preserving existing data; commit. No fake audio stage counts as Task 1 or browser inference evidence.

## Task 4: Minimal separate browser voice harness

Repo: `../frontend`; branch from its `develop` into `feat/phase-3-voice-console` (no dashboard). Files: package/lock/build config, `src/app`, `src/features/auth`, `src/features/voice`, `src/components/ui`, `src/api/voice.generated.ts`, protocol snapshot, feature interaction tests and browser smoke.
Interfaces: backend's exported version-one contract; existing staff login; tenant agent choices; media grant only in the browser. LiveKit client handles mic/remote audio and bounded control messages. Tokens/captions remain in memory.

- [ ] Read frontend-design and vercel-react-best-practices; inspect the named design references and actual licenses before adoption. Pick one accessible primitive system; motion/orb only for real audio activity with reduced-motion support. Review stable package compatibility before scaffolding.
- [ ] Write red meaningful interaction tests: mic/autoplay denied, busy/model failure, stale room, reconnect/end cleanup and forged/stale acknowledgment behavior. Assert real visible states, not mock presence or invented metrics.
- [ ] Build React/Vite/strict TS + TanStack Query/shadcn minimal test page with reusable forms/controls/statuses, staff sign-in and agent selection, actual media states, mic/end/reconnect, ephemeral captions and measured timings. Broader dashboard/agent editor/voice picker remain Phase 4.
- [ ] Generate types from pinned backend schema; CI checks snapshot/type drift and frozen installs. Verify responsive desktop/mobile, keyboard/focus/reduced motion, and mixed/Urdu captions with actual screenshots/browser interaction.
- [ ] Run actual local browser-to-worker audio booking tests where hardware/automation permits. If a physical speaker/mic action requires the owner, state precisely what automated audio did prove and leave that manual acceptance explicit; no typed shortcut may masquerade as spoken booking. Commit frontend separately.

## Task 5: Whole-phase evidence and owner checkpoint

Files: `docs/phase-3-results.md`, `docs/phase-3-testing.md`, dependency/license ledger, backend/frontend CI and READMEs.

- [ ] Run all retained/new backend/runtime/frontend checks and actual model/room/browser tests. Execute synthetic English/Urdu booking/reschedule/cancel plus correction/digression/family/reconnect, asserting final real database outcomes and no incomplete confirmation mutation. Record failures, cold/warm conditions, genuine model request counts, CPU time/memory and p50/p95; keep raw audio/transcripts private/untracked.
- [ ] Fresh independent whole-branch review against approved design, fix important findings with regressions, rerun appropriate checks and actual services. Preserve the earlier Phase 2 retry investigation rather than inventing a root-cause fix.
- [ ] Rebuild runnable services, publish separate public draft PRs into develop, attach both to chat, verify CI and branch contents. No Phase 3 merge. Deliver exact CPU setup/owner browser testing steps, native-quality/manual-playback items and explicitly pending GPU SLO/corpus gates; stop before Phase 4.

## Execution choice and self-review

The owner has explicitly approved completing this design through the testing checkpoint; routine execution choices do not require another approval menu. Root implements the shared API/guard/runtime integration and coordinates bounded independent speech and frontend work when interfaces are ready, with fresh review. Task 1 resource proof comes before integrating all heavy models. PostgreSQL destructive tests run serially; no worker runs a competing seed reset. All design sections map to Tasks 1–5; the five review-focus conditions have owning tests above. Follow the existing feature checkout, preserve ignored credentials/data and do not create a new user chat.
