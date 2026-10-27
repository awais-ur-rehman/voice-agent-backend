# Phase 0: bilingual voice feasibility

Status: preparation only; no benchmark has run. Owner confirmed on October 27, 2026 that no GPU is available and a NIM API key still needs to be generated.

## Governing specifications

The owner supplied both v0.1 (PRD, TRD, flows and implementation plan) and v0.2 (Urdu + NIM + open-source addendum) in the parent workspace's `documents` directory. v0.2 takes precedence. Separate frontend/backend repositories replace v0.1's suggested monorepo at the owner's request.

## Scope and boundaries

Phase 0 validates the streaming voice stack; it does not implement clinic auth, scheduling, dashboards, or production patient flows. Backend owns evaluation and voice processes. Frontend will provide a minimal browser microphone test console, transcript, interruption behavior, and stage timing display once the phase design is agreed.

No GPU is needed to call hosted NIM. That does not provide the spec's self-hosted Whisper/Parler compute. CPU experiments may establish functionality but cannot validate a GPU deployment's latency or concurrency. No cloud GPU purchase is authorized.

## Proposed order

1. CPU-compatible preparation: synthetic evaluation fixtures, schemas, a hosted-LLM evaluation runner, monotonic stage measurements, explicit missing-resource diagnostics, and a reproducible environment.
2. With the owner's locally configured key: verify catalog access and compare four accessible model candidates on 40 scripted turns each (20 Urdu, 10 English, 10 mixed). Score valid tool JSON, correct tools/arguments, Urdu-script quality, and TTFT. Distinguish automated checks from native-speaker judgments. Measure observed rate limiting conservatively and record errors/retries; do not claim an account-wide contractual RPM cap from one run.
3. With suitable compute: Pipecat + self-hosted LiveKit + Silero + faster-whisper large-v3-turbo + Kokoro + Indic Parler-TTS. Compare Whisper turbo/large-v3 with/without vocabulary prompts. Record device, exact artifact revisions, region, and warm/cold conditions.
4. Evaluate six Urdu voice candidates, first-byte latency, sentence consistency, and native-speaker MOS. Catalog target is three voices per gender per language, subject to owner confirmation and verified model capabilities/licenses.
5. Measure English/Urdu end-to-end p50/p95 from a Pakistani network and interruption stop time. Owner runs the browser acceptance flow and reviews findings before any Phase 1 work.

## Exit evidence (all pending)

- Four-model bake-off results, accessible model identifiers, selected primary/secondary, and observed rate behavior.
- At least 200 recorded Urdu utterances across 10 speakers, plus 50 mixed utterances; document recording consent and keep identifiable audio private. Include the approximately one-hour clinic-style corpus required by v0.2 section 5.6.
- WER/CER plus name/number accuracy, including vocabulary-prompt comparisons.
- Six Urdu voice samples, human MOS, and TTS first-byte results. If Urdu TTFB exceeds 600 ms on target GPU, evaluate a licensed faster alternative before Phase 3.
- English target p50 <800 ms, p95 <1.5 s; Urdu target p50 <1.2 s, p95 <2 s. Barge-in stop target <200 ms. Targets are not measured results.
- Exact model/voice licenses and revisions recorded in `docs/licenses.md`; verify streaming capabilities rather than assuming full-sequence generation measures first audio.
- First 30 bilingual simulator scenarios and resolver/normalizer tables prepared before production code.
- Owner decisions captured: Urdu register, non-Urdu/English behavior, compute, pilot data/provider terms, catalog size, web-only scope.

## Spec issues to resolve during design

- v0.1 assumes 20 calls/node; v0.2 warns hosted limits may allow only a handful. Treat concurrency as measured capacity, not a guaranteed MVP number.
- Booking flow holds a selected slot, while v0.1's soft-hold paragraph also describes holding offered slots. Define exact semantics before the slot engine is built.
- Cancellation policy failure cannot be overridden merely by caller confirmation; policy and staff authority must define the permitted path.
- Restricting Whisper language choices and streaming Parler audio are requirements to prove through the chosen adapters, not presumed stock API features.
- Verify the existence/license of an Urdu Piper fallback and each model's terms; do not assume all suggested candidates have interchangeable licenses or hardware footprints.
- v0.2 refers to WhatsApp in Phase 2 while v0.1 uses later-phase terminology; keep paid messaging out of the current phase and resolve scope explicitly.

## Owner checkpoint

Preparation is not Phase 0 completion. Do not proceed to Phase 1 until the evidence above is collected or the owner explicitly revises scope and accepts documented gaps.
