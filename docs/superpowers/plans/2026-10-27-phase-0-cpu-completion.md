# Remaining CPU-only Phase 0 implementation plan

**Goal:** Complete the owner-authorized CPU evaluation package using Nemotron, with executable offline checks, observed hosted results, and a concrete human checkpoint before Phase 1.

**Architecture:** Keep evaluation-only fixtures and adapters under `evaluation/`. Probe next actions from synthetic history and supplied state without executing booking tools. Prepare golden resolver/normalizer tables for Phase 2; do not implement the production scheduling engine in Phase 0. Generate a local review report from hosted results.

**Spec:** Owner's v0.1/v0.2 in `../documents`, `docs/phase-0.md`, and the existing CPU design. The owner selected Nemotron and deferred failed model comparisons and GPU voice latency tests.

## Global constraints

Python 3.12; preserve ignored `.env`; synthetic data only; thinking explicitly disabled; no production booking mutations; no fabricated latency/voice/MOS/RPM results; stop at owner acceptance before Phase 1.

## Review focus

- Supplied history is evaluation context, not proof that a production state machine exists.
- A correct tool with the wrong ownership/confirmation identifiers must fail, including added arguments.
- Thinking/script failures and unsafe prose must not disappear behind a valid tool call.
- Human Urdu naturalness and clinical/emergency wording require human review; heuristic checks are not certification.
- Partial hosted failures and the earlier baseline remain visible; do not tune fixtures to make the chosen model pass.

## Task 1: evaluation assets

- [x] Write 30 scenario definitions, each with English and Urdu transcripts, supplied dialogue state, expected next action, and rationale; include digression, correction, task switching, verification, ownership, ambiguous dates, interruptions/reconnects, errors, KB injection, and emergencies.
- [x] Write resolver golden inputs/outputs covering every v0.2 section 5.3 row with fixed clinic time and explicit ambiguity/read-back decisions. Add normalizer expectations for 0–100, time/date/PKR/phone/name cases.
- [x] Prepare synthetic recording scripts and a native-speaker review rubric; record unresolved product choices and recommended defaults as proposals, not owner approvals.

## Task 2: executable next-action and language evaluation

- [x] Write failing tests for fixture validation, exact next-action scoring, unsafe extra arguments, script detection in tool args, and report escaping.
- [x] Implement `evaluation/flows.py`, `evaluation/flow_scoring.py`, and `evaluation/review.py`. CLI supports offline validation and hosted runs with private reports; it reuses the existing streaming transport/configuration. Add a small speech-text review set using code-supplied `say` templates and questions, not invented booking facts.
- [x] Run the whole offline suite and validate all fixtures. Run the paired 30-scenario suite (60 next-action probes) and prose review cases against hosted Nemotron, with spacing and no hidden retries.
- [x] Inspect every failed probe; retain raw reports, classify model errors separately from harness defects, and generate escaped local HTML for native review. Fix harness defects with regressions, not fixture changes that hide model failures.

## Task 3: owner checkpoint

- [x] Obtain a fresh review of the evaluation package and fix actionable harness issues with regressions.
- [x] Update CI, README, phase status, license caveats, observed results, and the owner's exact commands/review checklist. Distinguish completed CPU preparation from deferred voice gates and unbuilt Phase 1/2 functionality.
- [x] Push to the existing Phase 0 feature branch/update its draft PR; verify local tests and CI. Do not merge or start Phase 1.

## Execution record

- Added hash-locked jsonschema tooling for real JSON Schema validation; experimental HTTP transport still uses the standard library.
- Kept two evaluation-only adapters explicit and never executed booking tools.
- Independent review completed; JSON numeric/boolean false pass fixed with a reproducing regression, and the reschedule policy fixture made explicit.
- Retained all hosted failures and original raw reports. Documented token-preservation, required-tool and routing diagnostics did not establish a fix.
- Final 72-request run completed: strict actions 15/60, language mechanical checks 9/12. No returned reasoning. The flow gate failed; human review and original voice evidence remain pending.
- Corrected a contradictory date-history fixture and reran only its two language probes (one strict pass); expected actions unchanged and full-batch statistics retained.
- Whole offline suite: 33 tests pass; dry runs and asset/schema validation pass.
- Owner's exact test commands and acceptance decisions are in docs/phase-0-owner-checkpoint.md. Phase 1 remains stopped.

CI verification: both offline evaluation checks passed for implementation commit 034ba22. The PR remains draft into develop; no merge or Phase 1 work was performed.
