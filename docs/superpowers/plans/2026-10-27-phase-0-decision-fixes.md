# Phase 0 decision reliability fixes

**Goal:** Address the twenty semantic errors without advancing Phase 1 or changing the original expectations.

**Scope:** Synthetic single-turn supplied-state evaluation only. No production transitions or scheduling tools execute. Keep thinking disabled, the thirteen-action schema, exact code confirmations, and rejection behavior intact.

**Evidence:** The retained 72-case run has sixty valid actions but twenty wrong decisions. Most failures substitute a question for saving raw dates, provider/subject changes, or task switches. Remaining errors confuse server error causes, verification attempt limits, confirmation requirements and availability versus clinic hours.

**Hypothesis:** The current flat prompt omits ordering and operational details for state fields. A concise ordered decision policy with clearly delimited trusted state should improve selection without an oracle, per-scenario branches, automatic retries, or response rewriting.

- [x] Add request-boundary regression tests: fixture metadata/expected answers cannot affect requests; all thirteen actions remain selectable; state is delimited independently of instructions. Add a separate fourteen-scenario bilingual generalization/contrast set with changed wording, IDs and thresholds.
- [x] Rewrite only the action instructions; retain model sampling settings for the first experiment. Validate the payload regressions and run the full original 72 cases plus the first 24 generalization actions. Keep all runs, failures and fixture hashes.
- [x] Investigate any remaining failures rather than weakening expectations. Test the owner-approved state-constrained action prototype, then repeat the original actions on an unchanged passing candidate.
- [x] Review changes independently, run guarded offline tests and CI, update results, owner checklist and existing draft PR. Stop before Phase 1.

**Review focus:** No expected answers or case IDs in requests; fresh fields saved before more questions; true versus false confirmation; verification attempts two versus three; known versus unknown provider/subject IDs; factual hours versus appointment availability; upstream failure cause versus generic human request. These are prompt evaluations, not code-enforced production authorization.

## Experiments retained

- Policy v1 at temperature 1: original actions 53/60, author-created variations 23/24, generated language 7/9, exact code confirmations 3/3. No protocol rejections. Full 96-case run correctly exits 1.
- Same policy, action temperature changed to 0 only: original actions 52/60, variations 23/24. No protocol rejections. Full 84-action run correctly exits 1. Lower temperature alone did not resolve the errors.
- Independent review identified an incidental date in a clinic-fact question being mistaken for an appointment change. Added four English/Urdu contrasts and scoped date/provider updates accordingly. Clarified already-retrieved appointment selection, resume stage, and availability versus hours. Four examples use different dates/IDs and are shared across every request, never selected using the fixture oracle.
- Policy v2 at action temperature 0: original actions 56/60, variations/contrasts 28/28, generated language 8/9, exact code confirmations 3/3. It fixed seventeen original failures but introduced an early-confirmation resume error. The full 100-case run correctly exits 1. Retain this unrestricted model result.
- Both original scenario expectations and scoring remain unchanged. All 49 guarded offline tests passed, including a fresh checkout, and CI passed on c2cf14c.
- After three incomplete experiments, discussed the architecture with the owner. The owner explicitly approved [the state-constrained Phase 0 experiment](../specs/2026-10-27-phase-0-state-constrained-actions.md). It constrains pending questions, literal date spans and eligible state-bound arguments before inference; it never rewrites a wrong output into an expected answer.
- The explicit `state_constrained` policy is separate from default `unrestricted` diagnostics. Each row records actual allowed names/count and schema; reports distinguish singleton actions dictated by state. Sixteen of eighty-eight action probes have a single eligible name; seventy-two retain three or four choices. Local validation checks both global and state/input contracts.
- Independent review found no oracle leakage or eligibility bypass within valid supplied synthetic states. Corrected a count label and added mismatched-subject lookup/cancel/reschedule regressions. Added preflight rejection of invalid late fixtures before credential loading/inference. All 63 guarded offline tests pass. The full state-constrained run passed 100/100 checks: 60 original actions, 28 new actions, nine language mechanical checks and three code confirmations. An unchanged repeat passed all sixty original actions, with identical prompt/eligibility hashes and generation configurations. Both runs exit 0; no protocol rejections or returned reasoning tokens were observed. Full-run action readiness p50/p95: 1240.3/1993.2 ms; repeat: 1187.3/2135.5 ms. Native acceptance and voice evidence remain open; Phase 1 has not started.
