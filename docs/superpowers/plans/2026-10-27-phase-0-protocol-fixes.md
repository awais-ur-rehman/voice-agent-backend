# Phase 0 action protocol and confirmation delivery fix

**Goal:** Eliminate malformed action serialization at the application boundary and model-added confirmation prose, then rerun offline and hosted probes without starting Phase 1.

**Architecture:** Keep native tool calling as an explicit diagnostic mode. Add a constrained JSON action mode that uses all available action schemas, validates the whole response and tool arguments, and converts only valid JSON control data into an internal action. Never parse function-like free prose or execute an evaluation tool. Deliver authoritative bilingual `say` from a validated tool-result envelope directly in code, with no LLM request or rewriting.

## Boundaries and review focus

- No expected fixture action/arguments may enter the provider request or output schema.
- Unknown tools, additional arguments, duplicate keys, mixed tool/prose output, reasoning, malformed JSON and invalid Unicode-script outputs fail closed.
- Protocol validity and correct action selection are separate results. This does not implement the production scheduling/auth state machine.
- Code confirmations are explicitly labelled as code delivery, not counted as successful LLM inference; retain original model-repeat failures.
- No automatic retries, discarded failure reports, key exposure, or fixture weakening.

## Tasks

- [x] Reproduce failures using saved responses; write regressions for a constrained action contract and exact code-delivered confirmation, including rejection cases and zero network access on confirmation delivery.
- [x] Implement action protocol adapter and delivery function. Integrate explicit `json_schema`/`native` modes with truthful report provenance and timing/sample counts.
- [x] Smoke-test the provider's structured output capability with thinking disabled. If accepted, rerun all 60 action scenarios and all 12 language/delivery samples; keep native and earlier reports unchanged.
- [ ] Independently review the adapter and fix actionable findings with regressions. Run offline checks and CI, update the results/checkpoint report and draft PR. Do not merge or advance Phase 1.

## Execution evidence

- JSON Schema constrained output accepted by the same hosted Nemotron; no native free-text salvage implemented.
- Full rerun: 60/60 structured actions valid, 40/60 semantic decisions correct; 9/9 model-language mechanical checks and 3/3 direct confirmations pass. Code and model sources are separate.
- Independent review found Unicode escapes bypassing a raw-text script check; a regression reproduced it and decoded-string validation fixed it. All saved outputs were revalidated with no changed action representations.
- Added preflight validation of authoritative confirmation fixtures and zero-network code-only execution.
- Unit/CI runner isolates fake credentials, blocks workspace credential-loader reads and socket connections, with diagnostic key-pattern redaction. All 44 tests pass under these guards.
- Phase 1 remains stopped; semantic action failures and human/voice acceptance remain open.
