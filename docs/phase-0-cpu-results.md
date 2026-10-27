# Original native-action Phase 0 results — October 27, 2026

**Historical report:** see [protocol and confirmation fixes](phase-0-protocol-fix-results.md) for the newer structured-action and direct-confirmation paths. The figures below are retained unchanged.

CPU evaluation and preparation are complete for the owner-selected Nemotron baseline. **The expanded model flow gate did not pass.** No production API, resolver/normalizer, database transaction, language detector, voice pipeline, or real scheduling tool is implemented by this package. Phase 1 remains stopped for owner review.

## Delivered and checked offline

- 30 authored scenario definitions × English and Urdu = 60 state-conditioned next-action probes, with supplied synthetic history/state and expected action arguments.
- 12 speech-text samples for native review; these are text, not synthesized voices.
- 35 resolver golden cases covering v0.2 section 5.3 plus clinic-local midnight, year/leap boundaries, and corrupted-script rejection requirements.
- 101 canonical Urdu number proposals (0–100) and 15 time/date/fee/phone/name/error normalization cases. These tables are prepared requirements, not implementation test passes.
- A proposed versioned tool-result envelope schema with five positive/negative examples; evaluator tool schemas validate actual returned JSON types.
- An unrecorded plan for 200 Urdu and 50 mixed utterances across ten speaker slots. No recordings, WER/CER, name/number accuracy, or speaker diversity measurements exist yet.
- 33 regression tests pass under Python 3.12.13, both evaluation dry runs pass, and all asset/contract consistency checks pass. Dependencies are hash-locked; six validation packages are listed in the license ledger.

## Hosted final run

Requested `nvidia/nemotron-3.5-bolt-e4b` explicitly. Thinking off, parallel calls off, special-token preservation enabled; temperature 1.0, top_p 0.95, max_tokens 300. Action probes used required-tool mode across all thirteen tools, never a forced expected tool. Language probes had no tools. Two-second gaps, 30-second socket timeout, no automatic retries, synthetic data only.

All **72/72 requests completed**. No reasoning characters or reported reasoning tokens were returned.

| Suite | Mechanical/strict checks passed | First output p50 | p95 |
| --- | --- | --- | --- |
| Next action — English | 8/30 | included below | included below |
| Next action — Urdu | 7/30 | included below | included below |
| Next action — combined | 15/60 | 1111.4 ms | 3015.5 ms |
| Language heuristics/exact delivery | 9/12 | 734.5 ms | 804.7 ms |

These are client-observed first content/tool deltas. Failed action probes can have ordinary malformed tool text as their first output, so their timing is **not usable tool latency**. None of these figures is end-to-end speech latency or evidence of capacity/RPM. Prompt caching may affect this sequential repeated-context run; no cold-start/concurrency claim is made.

### Action failures

41/60 responses emitted tool-like free text rather than one parsed tool call, including strings such as `ask_caller{field:<|"|>time<|"|>}`. These are rejected, never coerced into executable calls or treated as successful bookings. 19/60 responses passed the supplied tool argument schema; 15/60 matched the expected next action and arguments with all checks passing. Additional parsed responses chose a wrong action or arguments.

The initial auto-tool run scored 13/60 actions. Before the final run, the scorer was fixed for JSON type safety, the reschedule fixture gained explicit policy approval, and documented token-preservation/required-tool settings were applied. The original report is retained. A final asset review found a date-correction history line saying Thursday while its supplied state said Wednesday; the history now consistently says Wednesday. A targeted paired rerun completed with one of two strict passes, the same pass count as those two probes in the full run. That rerun is retained separately as `results/phase0-corrected-date-retest.json`; the full-batch figures are not silently rewritten. This is **not a controlled A/B model improvement claim**: configuration/fixture changes and stochastic sampling confound such a comparison. Individual smokes of the documented settings still showed malformed calls. The separately documented `stg/` routing alias returned HTTP 404 and was not used for the full run.

### Language limitations

The three failed samples were `say-date-ur`, `say-fee-ur`, and `say-date-en`. They preserved the supplied date/fee text but appended a follow-up despite an exact-repeat instruction. This confirms why authoritative `say` must bypass the LLM on the eventual normalization/TTS path. That path is not implemented here.

A mechanical pass only checks script, raw digits, length/question count, selected respectful forms, absence of reasoning/tools, and exact `say` equality when required. It does not establish meaning, medical safety, native Urdu, or an actual language-switch state machine. Even mechanically passing examples can sound awkward; the owner must judge them in context.

## Review artifacts

- [All final probes](../results/phase0-nemotron-final-72.html) — local, ignored, synthetic raw context/results.
- [Twelve language samples](../results/phase0-nemotron-final-72-language.html) — start here for native-speaker review.
- [Review notes template](phase-0-review-notes-template.md).
- [Owner commands and acceptance checklist](phase-0-owner-checkpoint.md).
- [Preparation decisions and review notes](phase-0-preparation-notes.md).

Local JSON reports stay ignored and mode 0600. Final raw report SHA-256: `17c1bf116d0c4950c66ce5063834480ad50ae655b853ffb1de50c5c4ee1151a2`. Fixture and prompt hashes plus per-case request options are embedded in it. The hosted model alias does not pin immutable model weights.

The original 40-case simple extraction test remains separate: 38/40 exact matches, including a Devanagari corruption. Its narrow performance cannot substitute for the broader next-action results above.

## Checkpoint decision

The owner-selected development baseline remains Nemotron; no production reliability or license clearance is asserted. Three unavailable comparison models and GPU voice latency work remain explicitly deferred. Native wording approval, real recordings/STT/TTS, target-network voice latency, barge-in, concurrency/RPM, and a validated fallback are still open.

The owner can review the package and explicitly authorize **Phase 1 database/API foundations using synthetic data**, while keeping these model/voice issues as blockers before live agent integration. The original full Phase 0 voice exit gate has not passed. No merge, release, or phase advance is performed automatically.
