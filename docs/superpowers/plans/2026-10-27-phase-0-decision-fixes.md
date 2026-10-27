# Phase 0 decision reliability fixes

**Goal:** Address the twenty semantic errors without advancing Phase 1 or changing the original expectations.

**Scope:** Synthetic single-turn supplied-state evaluation only. No production transitions or scheduling tools execute. Keep thinking disabled, the thirteen-action schema, exact code confirmations, and rejection behavior intact.

**Evidence:** The retained 72-case run has sixty valid actions but twenty wrong decisions. Most failures substitute a question for saving raw dates, provider/subject changes, or task switches. Remaining errors confuse server error causes, verification attempt limits, confirmation requirements and availability versus clinic hours.

**Hypothesis:** The current flat prompt omits ordering and operational details for state fields. A concise ordered decision policy with clearly delimited trusted state should improve selection without an oracle, per-scenario branches, automatic retries, or response rewriting.

- [ ] Add request-boundary regression tests: fixture metadata/expected answers cannot affect requests; all thirteen actions remain selectable; state is delimited independently of instructions. Add a separate twelve-scenario bilingual generalization set with changed wording, IDs and thresholds, including contrasting inputs.
- [ ] Rewrite only the action instructions; retain model sampling settings for the first experiment. Validate the payload regressions and run the full original 72 cases plus the 24 generalization actions. Keep all runs, failures and fixture hashes.
- [ ] Investigate any remaining failures rather than weakening expectations. Require a complete rerun after each substantive revision and a second original-action run before claiming repeatability.
- [ ] Review changes independently, run guarded offline tests and CI, update results, owner checklist and existing draft PR. Stop before Phase 1.

**Review focus:** No expected answers or case IDs in requests; fresh fields saved before more questions; true versus false confirmation; verification attempts two versus three; known versus unknown provider/subject IDs; factual hours versus appointment availability; upstream failure cause versus generic human request. These are prompt evaluations, not code-enforced production authorization.
