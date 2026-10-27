# Phase 0 progress — October 27, 2026

## CPU-only package delivered

- Separate public backend/frontend repositories, protected main/develop branches, current Phase 0 feature branch and draft PR.
- Owner-selected Nemotron development baseline with thinking disabled; no secondary model validated. Three additional model candidates are deferred after API and owner playground failures.
- Original hosted extraction baseline: 38/40 strict matches. Its Devanagari corruption and latency limits are retained.
- Thirty bilingual supplied-state scenarios: 60 next-action probes. Twelve additional speech-text samples with local escaped HTML for native review.
- Final hosted expanded run: 72/72 requests completed; 15/60 strict next-action checks and 9/12 language mechanical checks passed. No reasoning was returned. **The model flow gate did not pass.**
- 35 resolver goldens, 101 Urdu number expectations and 15 structured normalizer cases. No resolver/normalizer implementation is claimed.
- Proposed versioned bilingual tool-result schema plus positive/negative examples, validated with hash-locked JSON Schema tooling.
- Unrecorded manifest: 200 Urdu + 50 mixed script prompts across ten speaker slots; no actual audio, speaker coverage or STT metrics claimed.
- Independent harness review completed. Fixed JSON numeric/boolean false equality with a failing-then-passing regression, clarified mechanical versus semantic language acceptance, and supplied explicit rescheduling policy approval in the fixture.
- 33 local regression tests pass; both offline dry runs and asset consistency checks pass. CI runs the same offline checks without an API key.

## Owner checkpoint remains open

Read [CPU results](phase-0-cpu-results.md), [test checklist](phase-0-owner-checkpoint.md), and the local twelve-sample language HTML. Submit native wording corrections, review the proposed resolver/number expectations, and accept or edit MVP defaults. No human score or clinical/speech quality approval has been invented.

Phase 1 has **not** started. The owner may explicitly authorize database/API foundations with synthetic data while retaining the model/voice findings as blockers before live agent integration. No automatic merge or phase advance is performed.

## Deferred or unresolved

The original full Phase 0 voice gate has not passed: real recordings, Whisper WER/CER/name/number accuracy, Urdu TTS six-voice/MOS/latency evidence, a verified licensed fallback, Pipecat/LiveKit browser audio, barge-in, target-network end-to-end p50/p95, concurrency/RPM, provider failover and production licensing/deployment decisions remain open. GPU voice latency is deferred by owner instruction, not silently waived.
