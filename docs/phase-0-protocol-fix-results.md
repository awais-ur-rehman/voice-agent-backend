# Action protocol and exact confirmation fixes — October 27, 2026

The application-boundary formatting and confirmation-delivery issues are fixed in the evaluation path. The provider's native function parser is **not** claimed to be repaired. The default now uses a constrained JSON action channel; native tool calling remains an explicit diagnostic mode. No scheduling tool executes, and Phase 1 has not started.

## Changes

1. Actions use NVIDIA's documented [JSON Schema response mode](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3_5-bolt-e4b-infer), with all thirteen available action schemas. No expected fixture action/arguments are supplied to the request. There is no forced expected tool name.
2. The adapter accepts one complete JSON document only, validates the actual name/arguments against the allowed schema, and then creates an internal action representation. Function-like prose, prefixes/fences, unknown names, extra fields, duplicate keys, incorrect JSON types, non-finite numbers, mixed native calls, reasoning, and Devanagari in decoded strings are rejected. It never guesses how to repair malformed text.
3. Authoritative confirmations come directly from the selected language of a validated bilingual tool-result envelope. They bypass the model and are returned unchanged. Even building a model request for an authoritative confirmation is refused. Invalid confirmation fixtures are rejected before any inference.
4. Reports identify model responses versus code templates. Code confirmations have no model latency/usage and are not counted as successful model inference. A confirmation-only CLI works without reading credentials or using the network.

This is a typed **request** boundary, not production authorization. A schema-valid action can still be wrong for the task, patient, or state. The production state-machine, verification, ownership, idempotency and database guards still need implementation in their planned phases.

## Rerun evidence

The saved full rerun completed all 72 cases: **69 hosted model responses plus 3 local code deliveries**. Thinking remained disabled. No returned reasoning was observed.

| Check | Result |
| --- | --- |
| Structured action protocol and argument schema | 60/60 valid |
| Malformed action responses rejected in this rerun | 0/60 |
| Exact next action and arguments | 40/60 correct |
| Model-generated language mechanical checks | 9/9 passed; human review pending |
| Direct code confirmation exactness | 3/3 passed; zero model requests |
| Offline regression suite | 44 tests passed |

All 60 saved action outputs were revalidated after independent review added an escaped-Unicode regression; their decoded action representations were unchanged. The three code confirmations match their authoritative envelopes exactly, without a suffix, prefix, or paraphrase.

First control-output p50/p95: 809.4/1501.2 ms. Complete validated-action readiness p50/p95: 1200.4/2003.7 ms. These are client-side action timings, not audio or tokenizer TTFT. Only the nine model-generated language samples contribute language latency; code delivery has no LLM timing. The default output representation and request prompts differ from the old native run, so these are observed pipeline results rather than a controlled model-quality A/B claim.

Raw report: `results/phase0-protocol-fixed-72.json` (ignored, synthetic only, private file permissions). SHA-256: `40a417428489ea5146cf3b6c37c49c40c19b81ba967d445914bb5da790380b07`. Original native-call and model-repeat failures remain retained unchanged.

## Remaining decision errors

The twenty wrong next-action cases are:

- `date-tomorrow:en`
- `date-tomorrow:ur`
- `date-kal:ur`
- `date-parson:en`
- `date-parson:ur`
- `correct-provider:ur`
- `park-for-cancel:en`
- `park-for-cancel:ur`
- `family-subject:en`
- `family-subject:ur`
- `unverified-cancel:en`
- `third-verification-failure:en`
- `third-verification-failure:ur`
- `confirmation-required:ur`
- `slot-taken:ur`
- `second-timeout:ur`
- `unknown-knowledge:en`
- `unknown-knowledge:ur`
- `human-request:ur`
- `interrupted-history:ur`

They include failing to persist a date before asking another question, wrong task-switch/family-subject actions, incorrect third-verification-failure handling, and incorrect handoff reasons. They are well-formed control requests, **not fixed decision logic**. The full suite correctly exits 1 for these semantic failures; no score or expected fixture was weakened to make it pass.

Native review also remains necessary for the nine generated replies: script/length checks cannot establish meaning or natural Urdu, and the greeting still refers awkwardly to a synthetic clinic. That is not accepted as polished production wording.

## Reproduce offline first

```bash
.venv/bin/python tests/run_offline.py
.venv/bin/python -m evaluation.flows --suite confirmations
.venv/bin/python -m evaluation.assets
```

`tests/run_offline.py` removes inherited LLM credential variables, refuses the workspace credential-loader file read, blocks socket connections, and redacts NIM-key patterns in unittest diagnostics. Tests use fake credentials and mocked environment loading where appropriate. The fixture-only confirmation command does not need an API key. Native action diagnostics remain available with `--action-protocol native`; normal action evaluations default to `json_schema`.

With a separately configured valid key, a future explicit live rerun uses:

```bash
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b
```

No automatic live request is performed by CI.

## Owner review and stop point

- [Updated twelve-sample language/delivery report](../results/phase0-protocol-fixed-72-language.html): code confirmations are visibly labelled **code template — no model request**.
- [All action/language results](../results/phase0-protocol-fixed-72.html).
- [Owner checkpoint](phase-0-owner-checkpoint.md).

The two requested defects are addressed at the evaluation/application boundary, but **the complete action-correctness gate remains failed at 40/60**. Phase 1 remains stopped. Human approval, real speech/GPU feasibility, and all production scheduling guards remain separate unresolved work.
