# Phase 3 safe-code checkpoint — October 28, 2026

The guarded API, native media/inference code and separate browser console are implemented. **The full speech-to-speech exit is not passed.** The owner selected CPU development, then explicitly chose to finish safe code while Urdu acceptance remained blocked. Both language capabilities currently stay false because the specified English ASR also exceeded the tested safe memory budget. Do not advance to Phase 4 or advertise working spoken scheduling from these results.

## Verified evidence

| Check | Evidence | What it does not prove |
| --- | --- | --- |
| Retained Phase 0 offline suite | 63 passed | Native Urdu corpus, model capacity or speech latency |
| API/domain/voice guards | Full serial PostgreSQL/Valkey suite; final count below | A real spoken booking |
| Native control/process suite | 32 passed, including real bounded OS child cancellation/RSS/deadline checks | Successful heavy-model inference |
| Frontend | 26 tests, schema drift, lint, strict types and production build passed | Microphone/speaker acceptance |
| Chromium fixture flow | Desktop/mobile, keyboard, reduced motion, Urdu labels, unavailable pipeline | Real backend speech or audible playback |
| Actual LiveKit RoomService | Create/delete passed with scoped server grants | RTC audio |
| Actual Pipecat/LiveKit RTC PCM | Two successive post-restart passes: 28,600 caller input samples, 45,360 decoded output samples, nonzero output | ASR, TTS, NIM or appointments |
| Real English Kokoro | 55,800 nonzero samples at 24 kHz, 2.325 s audio | Native pronunciation/MOS or speech-to-speech |

Final local backend verification: **575 passed, 4 explicitly hosted-only tests skipped**. Draft PRs target `develop`; their checks show the live CI status. No hosted NIM requests were needed for these Phase 3 code/transport checks; the retained Phase 2 provider evidence remains separate.

## Measured resource limits

Apple M2 arm64, 8 GiB physical RAM. Kokoro `af_heart`, seed 42, CPU two threads: cold imports/load 73.018 s; first genuine PCM 74.112 s; generation 1.097 s; child lifecycle 82.816 s; sampled peak RSS 870,301,696 bytes. These are one cold sample, not invented p50/p95 figures or native model streaming.

The exact specified turbo ASR artifact on CPU int8 was attempted with a 1024 MiB child cap, 256 MiB remaining-system floor and 180 s deadline. It was killed at sampled RSS 1,091,911,680 bytes; no transcript was accepted. This demonstrates the tested budget was insufficient, not an empirical minimum hardware requirement.

Urdu Parler was never loaded. Available memory measured 1,899,823,104 bytes after the earlier reversible service stop, with 8,789,753,856 bytes used swap. Its 937,803,241 FP32 parameters alone require about 3.49 GiB; a conservative 4.5 GiB headroom preflight refused inference. All model files/revisions/hashes were downloaded privately; download success is not inference acceptance. No smaller model, browser speech substitute, fabricated Urdu output or GPU profile was substituted.

After the owner selected blocked acceptance, an optional additional recognition retry was cancelled when automatic approval review rejected stopping development services for it. No services were stopped for that retry and no larger-budget retry occurred.

## RTC startup investigation

Initial native SDK and Pipecat connections repeatedly timed out in ICE/peer-connection establishment, including a bounded 60.016 s direct-SDK attempt reporting `wait_pc_connection timed out`. HTTP room operations worked, JWT timing/grants were valid, and proxy environment variables were absent. After restarting only the new local media service, the same PCM smoke passed twice, including once after warning-only logging was restored. The underlying cause was not established; this is an unresolved startup observation rather than a claimed root-cause fix. The smoke creates/deletes only its transient synthetic room and never loads speech models.

## Independent review and fixes

A fresh backend review identified four important findings, each reproduced before its fix: producer/consumer queue field mismatch; old delivery ledger blocking the next readback; cleanup after the brain revoked its runtime token; and disconnected grace expiry leaving capacity occupied. Regression tests now cover staff-to-worker queue roundtrip and acknowledgment recovery, SQL readback-ledger replacement, end-only worker cleanup after brain revocation, terminal response completion, and autonomous tenant-scoped cleanup after the 30-second grace. Cleanup never restores planning/scheduling authority to an ended parent.

A fresh frontend review identified three important findings, each reproduced before its fix: worker `listening` discarded a still-pending played segment; a zero-volume baseline could later acknowledge silent audio; and expired authentication retained the previous account's session. The fix preserves valid evidence until playback/invalidation, invalidates audibility transitions, and clears/fences all account-bound state on authentication loss. All 26 frontend tests pass, including an additional worker-disconnect regression.

## Builder decision and remaining work

The native environment is split into locked media/control and inference environments because released Parler dependencies require protobuf <5 while Pipecat/LiveKit require >=5. The cost is two setup commands; the API environment is not downgraded. Completed TTS segments are labelled honestly rather than described as native streaming.

Deferred minor: the lazily loaded LiveKit browser chunk is about 532 kB raw/138 kB gzip; sign-in does not load it. Python 3.12 also reports the pinned upstream Pipecat `audioop` deprecation warning. Neither is represented as a runtime failure.

Still required: safe successful English/Urdu real speech loopbacks; browser microphone/speaker testing; real spoken booking/reschedule/cancel, interruption/correction/family/reconnect with database-effect assertions; native Urdu MOS/corpus; GPU latency and production capacity; production license/redistribution review. No p50/p95, native quality, hearing proof or complete Phase 3 acceptance is claimed. Preserve the [earlier unreproduced Phase 2 retry investigation](phase-2-results.md); this work does not invent a root cause for it.


Additional control regressions verify that barge-in does not wait for a planner lock, SQL interruption wins over an in-flight planner result, delayed replies cannot reduce the worker revision, VAD uses a conservative pre-roll start bound, and stale worker capabilities are revoked on disconnect/recovery. Cached public greetings and delayed fillers are separate from task output/history; noncritical backchannels resume only code-owned text at whole-segment boundaries. Readiness evidence is bound to current model revisions, frozen voice configuration and both environment locks. These deterministic checks do not substitute for blocked real speech acceptance.
