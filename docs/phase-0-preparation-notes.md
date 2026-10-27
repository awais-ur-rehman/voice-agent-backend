# Phase 0 preparation decisions and review notes

## Scope decisions

- The owner selected Nemotron as the development baseline after three additional candidates failed both API and owner playground checks. The four-model comparison remains deferred, not passed.
- This package probes a next action from supplied state/history. It does not implement or execute a state machine, booking transaction, authorization guard, or a complete multi-turn conversation simulator. The 30 bilingual scenarios are written now so the later simulator can be developed against expectations.
- `ask_caller` and `speak_template` are explicitly evaluation-only adapters. They represent a question or a fixed code-template selection without asking the model to invent a production API.
- Added `jsonschema` for real Draft 2020-12 validation rather than a partial handwritten schema checker. Six tooling packages are hash-locked and licensed in the ledger. This expands the original transport-only standard-library setup; no model weights have been installed.
- Resolver choices and canonical number spellings are proposed test oracles awaiting owner/native review, not implemented algorithms or owner-approved language decisions.
- Use an explicit model ID on live commands. The first two new smoke commands read a stale MiniMax environment selection and timed out; their diagnostic files are retained but are not part of the Nemotron benchmark.

## Verification and independent review

The independent reviewer found that Python dictionary comparison could accept JSON number `1` as boolean `true`. A regression reproduced that false pass. Returned action arguments now undergo the actual tool JSON Schema and type-strict canonical JSON comparison. All local tests pass after the fix.

The review also identified two interpretation issues: language checks are mechanical heuristics with semantic judgment still pending, and the rescheduling commit fixture needed an explicit policy approval. Both are now explicit in labels/docs and the corrected fixture. The original raw report is retained; the final suite records the corrected fixture hash.

A broader 13-tool probe exposed malformed tool syntax emitted as ordinary text. NVIDIA's documented `skip_special_tokens=false` setting was tested separately; it did not resolve the smoke failure. Required-tool mode was also tested without forcing the expected tool name; that smoke still returned malformed text. The final suite uses these documented request controls and retains failures rather than coercing free text into executable calls. These are diagnostics, not proof of the provider's internal root cause.

The model appended prose after authoritative `say` strings in the initial run. The production design already requires code-generated confirmations. The integration must pass those `say` strings directly to normalization/TTS, not rely on a model repeating them exactly. That integration is not implemented by this evaluator.

## Native wording references

The 0–100 list is an authored proposed canonical lookup, not a copied or automatically validated language authority. Native-speaker review remains required for every entry, alternate spellings, number reading, and time/date/fee expressions. Introductory references for the reviewer include [Michigan State's Basic Urdu number lesson](https://openbooks.lib.msu.edu/urdu/chapter/2-3/) and [Five College Center's Pakistan number materials](https://www.langmedia.fivecolleges.edu/resources/pakistan/basic-communications/numbers). These references do not independently verify every value in this package or grant a voice-model license.

No emergency numbers or clinical response script is certified here. The emergency scenario only selects the proposed fixed-template adapter; clinic-approved localized content and runtime keyword detection remain later work.

A final routing diagnostic used the `stg/` alias mentioned in NVIDIA's API reference for the same model. It returned HTTP 404 on the first request; no remaining batch requests were sent. The working public model alias remains the baseline. No further diagnostic retries were attempted.

Final fixture consistency correction: the date-correction scenario's prior caller history now says Wednesday to match its supplied Wednesday state. Its expected Thursday correction is unchanged. Added `--scenario` for reproducible targeted reruns; the two corrected-language probes were rerun separately and produced one strict pass. The original full run and aggregate statistics remain untouched, and its language review samples are unaffected.
