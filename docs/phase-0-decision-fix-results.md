# Phase 0 decision fixes — October 27, 2026

The owner-approved **state-constrained evaluation path passes 100/100 mechanical checks**: sixty original actions, twenty-eight author-created variation/contrast actions, nine model-generated language samples and three exact code confirmations. All twenty original decision failures pass, with no regression among the forty previously correct actions. An unchanged second run also passes all sixty original actions. Phase 1 has not started.

This fixes the tested pipeline using code-derived eligibility; it does **not** establish unrestricted model reliability. The latest unrestricted model still scored 56/60 original actions. All earlier failed reports are retained. No original expected answer or scorer was changed.

## What changed

- Ordered the action instructions around error causes, field capture before follow-up questions, caller versus subject, and the current workflow stage. Clarified appointment availability versus clinic opening hours and incidental dates/provider names in fact questions. Shared examples have different dates/IDs and are identical for every request.
- Added an explicit `--action-policy state_constrained` experiment. `evaluation/eligibility.py` derives eligible action schemas solely from supplied synthetic state and current input. It consumes no case IDs, expected answers, rationale or category. The default `unrestricted` diagnostic keeps all thirteen actions selectable.
- Pending-question gates prevent asking for final confirmation while collecting time, or selecting an appointment before a verified list exists. Mutation/lookup candidates use eligible server identifiers, confirmation, verification, stage and policy flags. Trusted emergency, exhausted-verification, timeout and no-answer states dictate the appropriate next action.
- Raw date arguments must be literal contiguous word spans of the current caller text. This prevents translation or copying example dates. All bounded spans are candidates; the code supplies no expected date. This experiment accepts one to forty input words and synthetic version-one state only.
- The provider receives the derived response schema and `allowed_next` names. Local validation checks the complete response against both global tool definitions and the state/input-derived contract. It rejects invalid responses without repairing them into expected answers. No tool executes and no dialogue state changes.
- Reports preserve actual schemas/configuration, policy labels, eligible-name counts and source/fixture hashes. Sixteen of the eighty-eight actions have one eligible name dictated by trusted state; the other seventy-two retain three or four names for model selection. Those sixteen are not evidence of unrestricted model reasoning.

Authoritative confirmations still bypass the model and return the exact bilingual `say` string. No suffix, prefix or paraphrase was introduced.

## Measurements

All runs use `nvidia/nemotron-3.5-bolt-e4b`, thinking disabled. The [NVIDIA endpoint reference](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3_5-bolt-e4b-infer) documents JSON Schema output and temperature zero. Nemotron action requests now use temperature zero; language requests retain temperature one/top-p 0.95. No hidden retries, best-of selection, response correction or discarded failure reports are used.

| Experiment | Original actions | New actions | Model language | Code confirmations |
| --- | --- | --- | --- | --- |
| Original structured-output baseline | 40/60 | Not run | 9/9 | 3/3 |
| Ordered policy v1, temperature 1 | 53/60 | 23/24 | 7/9 | 3/3 |
| Same policy, action temperature 0 | 52/60 | 23/24 | Not run | Not run |
| Clarified policy v2, unrestricted | 56/60 | 28/28 | 8/9 | 3/3 |
| Same policy, state-constrained pipeline | **60/60** | **28/28** | **9/9** | **3/3** |
| Unchanged state-constrained repeat | **60/60** | Not included | Not included | Not included |

The first full constrained run made 97 model requests and three code deliveries, with zero protocol rejections and zero returned reasoning tokens. It exits 0. First action-control output p50/p95 was 799.4/960.0 ms; complete validated-action readiness was 1240.3/1993.2 ms. The unchanged sixty-action repeat made sixty model requests, with ten singleton choices dictated by state, zero protocol rejections and exit 0. It used identical prompt/eligibility hashes and generation configurations. Repeat action readiness p50/p95 was 1187.3/2135.5 ms. These are client-observed action timings, not tokenizer TTFT, speech latency or end-to-end audio feasibility.

Generalization cases are author-created coverage with different wording, IDs, task types and counter boundaries, plus bilingual opening-hours/fees contrasts. They are not hidden independent validation. Earlier unrestricted runs also demonstrate generated-language variability; a final mechanical pass does not waive native review or establish that the one-question rule is reliable in production.

## Evidence and reproduction

Raw reports remain private, ignored synthetic artifacts:

- `results/phase0-decision-policy-v1-96.json`
- `results/phase0-decision-policy-v1-temp0-84.json`
- `results/phase0-decision-policy-v2-100.json`
- `results/phase0-state-constrained-100.json` — SHA-256 `f3fd04e38b354f2dc00ec9a8aef642e84eb57f813a13bcc4c23bc0968f50fbc6`
- `results/phase0-state-constrained-repeat-60.json` — SHA-256 `ccbf05a7aa9703c7e680719bc69a1378305a3ee704b4f211595e814fe447b510`

Offline checks, with no key or network:

```bash
.venv/bin/python tests/run_offline.py
.venv/bin/python -m evaluation.flows --dry-run --fixture-set all --action-policy state_constrained
.venv/bin/python -m evaluation.flows --suite confirmations
.venv/bin/python -m evaluation.assets
```

All 63 regression tests pass, including a fresh checkout. They cover literal date grounding, pending fields, known versus unknown identifiers, verification attempts two/three, confirmed/unconfirmed mutations, policy denial, mismatched verified subject, error priorities, no state mutation, oracle invariance, native-protocol bypass rejection, and invalid late fixtures rejected before credentials or inference. Independent review found no blocking eligibility issue within valid supplied synthetic states. CI checks both unrestricted and constrained dry runs without credentials.

An explicit future hosted reproduction, using a valid local key, is:

```bash
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b --fixture-set all --action-policy state_constrained
```

For the unrestricted model diagnostic, use `--action-policy unrestricted`. Do not compare a state-constrained score with an unrestricted score as if they measured the same capability.

## Owner testing and phase boundary

1. Review the [twelve language/delivery samples](../results/phase0-state-constrained-100-language.html): meaning, natural Urdu, politeness, one question, and exact date/fee confirmations. Code confirmations are labelled separately.
2. Review the [action results](../results/phase0-state-constrained-100.html), especially dates, patient/subject changes, parked tasks, verification limits, timeout retry keys and appointment selection. The HTML labels constrained policy/counts; schemas and supplied context remain inspectable.
3. Follow the remaining [owner checkpoint](phase-0-owner-checkpoint.md), including resolver/number wording and MVP defaults. Send corrections using the sample IDs.

These remain supplied-state, single-next-action probes. The prototype neither executes a conversation/state machine nor proves tenant authorization, database effects, slot races, idempotency, patient verification, task-stack transitions or clinically safe speech. Production code must validate actual authoritative state and enforce every permission/transaction; this fixture-only eligibility builder is not a production authorization library. Voice/STT/TTS/GPU evidence and native acceptance remain deferred/open. No merge or Phase 1 advance is automatic.
