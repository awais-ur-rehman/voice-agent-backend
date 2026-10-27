# Phase 0: bilingual voice feasibility

The owner authorized a CPU-only evaluation/preparation checkpoint using hosted Nemotron. That package is delivered; **the expanded model flow checks did not pass**, and human review remains open. See [observed results](phase-0-cpu-results.md), [current status](phase-0-progress.md), and [owner test checklist](phase-0-owner-checkpoint.md).

## Governing scope

The owner's v0.1 PRD/TRD/flows/phase plan and v0.2 Urdu/NIM addendum remain in `../documents`; v0.2 wins conflicts. The owner requested separate frontend/backend repositories instead of the suggested monorepo. No production scheduling service or dashboard is implemented in Phase 0.

The owner has no GPU, chose CPU + hosted NIM preparation, selected Nemotron after three additional candidates were unavailable, and deferred further model comparisons/GPU voice latency work. Hosted model selection is a development baseline, not production approval. No GPU purchase is authorized.

## CPU checkpoint deliverables

- Original 40-case hosted extraction baseline and documented failures.
- Thirty paired English/Urdu next-action scenarios, 60 hosted probes, and 12 speech-text review samples. These use supplied state/history and never execute tools; they are not the future full state-machine/database simulator.
- Resolver golden requirements covering every v0.2 section 5.3 row, all Urdu number values 0–100 and structured normalizer cases, with proposed decisions awaiting native/owner review.
- Proposed shared tool-result JSON Schema and executable consistency checks.
- Reproducible Python 3.12 tooling, hashed dependencies, offline CI, secret-safe private reports, a native review artifact and exact owner testing instructions.
- Unrecorded audio collection template for the later 200 Urdu/50 mixed corpus, not fabricated WER/CER or MOS.

## Original full Phase 0 evidence still open

- Comparable four-model results/validated primary+secondary and account rate capacity. Extra comparisons are currently owner-deferred, not passed.
- At least 200 Urdu utterances from ten actual consenting speakers, 50 mixed utterances, and approximately one hour of clinic-style audio; then stock Whisper turbo/large-v3 comparisons with/without vocabulary prompts and name/number accuracy.
- Six Urdu voice candidates, human MOS, consistency and first audio byte on target compute. If Urdu TTS first byte exceeds 600 ms on target GPU, evaluate an appropriately licensed faster alternative before Phase 3.
- Pipecat + self-hosted LiveKit + Silero + Whisper + Kokoro + Parler with real browser streaming/interruption/reconnect measurements.
- English p50 <800 ms / p95 <1.5 s; Urdu p50 <1.2 s / p95 <2 s; barge-in stop <200 ms. These are targets, not observed complete-pipeline results.
- Exact model/voice revisions, commercial-friendly terms, fallback voice existence/license, deployment/data-processor choices, and load/failover evidence.

## Decisions to review before foundations

The checklist records proposed conversational Urdu, Urdu/English-only MVP, web-first delivery, six voices per language, synthetic-only hosted development, and the resolver's strictly-future next-weekday rule. They are proposals until the owner accepts or changes them. Locale and policy decisions must remain explicit; the model cannot invent them.

Unresolved implementation details remain tracked for their owning phases: selected-slot versus offered-slot hold semantics; explicit staff authority for cancellation policy exceptions; actual Whisper language restriction and streaming Parler capabilities; and paid messaging phase terminology. The scheduling database remains the authority; prompts never replace guards or exclusion constraints.

## Stop point

Phase 1 has not started. Review the delivered CPU package and model failures first. The owner may explicitly authorize Phase 1 **database/API foundations with synthetic data**, retaining unresolved LLM/voice findings as gates before agent integration. If the original full voice feasibility criterion is required before foundations, the evidence above remains missing. Do not merge, release, or advance automatically.
