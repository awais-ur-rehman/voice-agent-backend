# Phase 2 verification checkpoint

Measured October 28, 2026 on `feat/phase-2-agent-brain`. Phase 1 PR #2 was merged into `develop` with owner approval. Phase 2 remains a draft for owner testing; Phase 3 has not started.

## Executed checks

| Check | Observed result |
| --- | --- |
| Full application/environment/brain suite | 422 passed, four explicitly hosted tests skipped; 53.77 s |
| Retained Phase 0 offline checks | 63 passed |
| Hosted NIM acceptance suite | Four passed together; 52.46 s |
| Ruff / format / mypy | Passed; 65 formatted files, 32 typed source modules |
| PostgreSQL migration round trip | 0002 → 0001 → 0002 passed; head has 17 forced-RLS tenant tables |
| Rebuilt Docker HTTP smoke | Passed: runtime/staff separation, tenant denial, verification, holds, booking/replay, reschedule, cancellation and ended-session rejection; zero provider calls |
| Independent review | Important findings fixed with regressions; no unresolved P1 reported |
| GitHub CI | Pending current draft PR checks |

The scripted suite executes the thirty original scenario families in both languages, plus four variations, against real PostgreSQL and Valkey. It validates every tool envelope and asserts scheduling/state effects. Additional tests cover golden resolution/normalization, authorization, exact verification, ambiguous shared phones, confirmation invalidation, expired holds, buffered overlap/races, receipt recovery, retention, capability rotation, interrupts, silence and transient mutation retries. It preserves the Phase 0 fixtures and keeps production imports independent of their expected answers.

## Actual hosted acceptance

Model: `nvidia/nemotron-3.5-bolt-e4b`, hosted alias. Thinking explicitly disabled. Temperature zero; strict JSON schemas restrict actions and opaque offers to the live server state. The hosted alias does not identify immutable model weights.

| Synthetic path | Outcome | Actual model requests |
| --- | --- | --- |
| English booking → rescheduling → cancellation, fee digression/date correction | Passed | 16 |
| Urdu booking → rescheduling → cancellation, fee digression/date correction | Passed | 16 |
| English family subject change → task parking/resume → child booking | Passed | 6 |
| Urdu family subject change → task parking/resume → child booking | Passed | 6 |
| Total | Four executed paths passed | 44 |

Phone/factor checks, explicit confirmation and clear family-subject authority resets execute directly in code. They are not counted as model requests. The model receives actual tool-derived state/public catalog facts; no expected fixture action or precomputed patient authorization enters inference. Critical output comes from bilingual code templates, including names, phone digits, original/replacement dates and times.

These are bounded acceptance paths through the constrained pipeline, not an unrestricted model accuracy claim. Earlier hosted runs exposed malformed action keys, omitted/unsupported literal fields, inferred subject resets, clinic-hours/slot confusion, resume errors and a missed family change. They failed their assertions. They drove schema, stage, language and authority-reset fixes before the final combined pass. Later runs have unique private evidence files in ignored `results/`; the first diagnostic runs reused a filename, so their command outcomes remain in the task history rather than a complete separate raw transcript. No failed run is counted as a pass.

## Owner testing

Follow [the owner guide](phase-2-testing.md). The local API is running at `http://127.0.0.1:8000/docs`; local synthetic agents are provisioned. The private demo file remains `.local/phase1-demo-credentials.json` with mode 0600. Existing database volumes/accounts and the NIM credential file were preserved.

Review Urdu wording, pronunciation fields, grouped phone digits, exact dates/times, corrections, qualified/negative confirmations, family subjects and task resume. A UI/dashboard is Phase 4. Audio playback, STT/TTS, GPU voice latency and native pronunciation/MOS acceptance are not established by text tests. No external call or message is sent by callback/handoff tools. Hosted runtime requests have a bounded per-process budget; the development default is 100, with a circuit after repeated provider failures.
