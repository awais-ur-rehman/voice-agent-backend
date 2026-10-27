# CPU-only Phase 0 owner checkpoint

This checkpoint covers synthetic hosted text evaluation and preparation only. There is no runnable clinic backend, booking calendar, microphone console, or speech pipeline yet. A model probe selecting a tool does not execute that tool or prove booking safety. Phase 1 remains stopped until the owner reviews this package and explicitly directs us to continue.

## 1. Run the offline checks

From the backend repository, using Python 3.12:

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python --require-hashes requirements.lock
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m evaluation --dry-run
.venv/bin/python -m evaluation.flows --dry-run
.venv/bin/python -m evaluation.assets
```

Alternatively, `python3.12 -m venv .venv` followed by `.venv/bin/python -m pip install --require-hashes -r requirements.lock` works without uv. The prepared local `.venv` already exists on the owner's machine. Do not run `venv` again over an active environment; start with the test commands when using this checkout.

Expected: all regression tests pass; baseline dry run reports 40 fixtures; expanded dry run reports 60 next-action probes and 12 language probes. Asset validation reports 101 number values, 35 resolver cases, 15 structured normalizer cases, 200 Urdu + 50 mixed recording prompts across 10 speaker slots, and zero recorded audio. Passing these checks validates the harness/assets, not a production resolver, normalizer, STT, or scheduling engine.

## 2. Review actual Nemotron replies

Open the final local language review report linked in `docs/phase-0-cpu-results.md`. Review all 12 samples, particularly:

- `greeting-ur`: natural AI disclosure and a respectful greeting.
- `missing-time-ur`: asks for appointment time rather than a vague or unnatural question; known fields are not repeated.
- `verification-ur`: asks one verification question, discloses nothing, and does not claim cancellation.
- The three language-switch examples: one English loanword/one switched turn does not flip the current language; two consecutive switched turns follow the supplied current-language state. These are supplied-state probes, not an implemented language detector/streak manager.
- `no-answer-ur`: natural Urdu, admits unavailable information, and offers a callback without inventing a guarantee.
- `roman-urdu-instruction`: remains in Urdu script.
- The three `say-*` examples: the model must not alter a date/fee or append a follow-up when asked for exact delivery. Regardless of model results, the production path must bypass the LLM for authoritative `say` strings, as the spec requires.

For each sample, send its ID plus: meaning correct yes/no, naturalness 1–5, politeness 1–5, one-question rule yes/no, and your suggested correction. Mechanical script/length checks cannot prove semantic safety or naturalness. A sentence falsely claiming a cancellation could pass these heuristics; human review must reject it.

## 3. Review the scenario and golden requirements

- Read `evaluation/fixtures/flows.json`: 30 scenarios have both English and Urdu variants, supplied state/history, expected next action, and rationale. `ask_caller` and `speak_template` are evaluation-only adapters, not final public API contracts. The other tool schemas are proposed evaluation contracts; the Phase 2 simulator will test executed state transitions and database effects later.
- Check every word in `evaluation/fixtures/normalizer_golden.json`, especially irregular numbers, doctor-name pronunciation, phone grouping, and fee/date wording. These are proposed canonical expectations, not a finished normalizer or synthesized voices. Spellings such as چھ/چھے have variants; record the clinic's preferred forms before freezing production snapshots.
- Review `evaluation/fixtures/resolver_golden.json`. Proposed rule: “next Tuesday” means the first Tuesday strictly after today (Monday → tomorrow; Tuesday → seven days later), with absolute-date read-back. Booking-context کل/پرسوں mean tomorrow/day after tomorrow; history-context ambiguity asks for clarification. Windows are half-open; actual clinic hours, Friday breaks, and exceptions filter them. Tell us if these choices should change.
- `packages/schemas/tool-result.v1.schema.json` is a proposed shared envelope. Offline examples validate success/error consistency and required bilingual `say`; they do not establish tenant authorization or PHI minimization for a live API.

## 4. Confirm or edit the MVP defaults

These remain proposals unless the owner explicitly accepts them:

| Decision | Proposed baseline |
| --- | --- |
| Urdu register | Conversational, respectful آپ, common English loanwords in Urdu script |
| Other languages | Ask callers to continue in Urdu or English; no Punjabi/Pashto/Sindhi promise in MVP |
| Channel | Browser/WebRTC first; SIP/phone numbers and paid messaging deferred |
| Voice catalog | Three female + three male voices per language; actual Urdu voices remain unverified |
| Hosted data | Synthetic fixtures only for now; real patient use needs a separate deployment/data-processor decision |
| Compute | CPU development now; no GPU rental authorized |
| LLM | Owner-selected hosted Nemotron with thinking disabled; failed comparisons deferred; no validated secondary model |

## 5. Explicit risk checkpoint before Phase 1

Read `docs/phase-0-cpu-results.md`, including failed actions and any malformed tool output. Do not call CPU Phase 0 “passed” just because the evaluation ran. The owner can authorize Phase 1 **database/API foundations with synthetic data**, keeping model/tool reliability as an unresolved gate before Phase 2/3 integration. If the owner requires the original full voice feasibility gate before any Phase 1 work, it remains blocked on the voice/GPU evidence below. No automatic merge or phase advance is performed.

The hosted model's OpenMDW-1.1 terms and future self-hosted artifact approval remain recorded in `docs/licenses.md`; a hosted synthetic evaluation does not clear a production license/deployment path.

## Deferred voice evidence — not required to run these CPU checks

- 200 Urdu + 50 mixed recordings, at least 10 actual consenting speakers, and approximately one hour of clinic-style audio. `evaluation/fixtures/recording_manifest.template.json` is an unrecorded public plan. Copy it into ignored `recordings/` before entering any private paths, speaker metadata, or consent information.
- Record synthetic patient details only. Let speakers use their own accents; self-described accent/context metadata can help cover Karachi/Lahore/Punjabi/Pashtun variation without inventing diversity claims. Include mobile microphones and both quiet/background-noise conditions.
- The script prompt is not automatically a ground-truth transcript: a reviewer must transcribe what was actually spoken, including corrections and omissions. Do not pad silence to manufacture an hour; collect additional natural utterances/dialogue when needed.
- STT turbo/large-v3 with/without vocabulary prompting: WER, CER, and name/number accuracy.
- Six Urdu voice candidates: native-speaker MOS, sentence consistency, first audio byte, exact licenses/revisions, and a verified fallback.
- LiveKit/Pipecat browser audio, real interruption stop time, reconnect behavior, and English/Urdu end-to-end p50/p95 on target hardware/network. Model selection and concurrency/RPM/failover capacity also remain unresolved measurements.

None of these audio metrics is marked complete by this package. The owner has deferred GPU voice latency checks, not waived speech quality or production correctness.
