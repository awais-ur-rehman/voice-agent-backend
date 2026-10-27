# Phase 0 state-constrained action experiment

## Evidence and decision

Three experiments improved the original action score from 40/60 to 53/60, 52/60 and 56/60. The latest run fixes seventeen of the original twenty failures but still mistranslates two English dates, asks for an appointment instead of saving an Urdu date, and introduces an early-confirmation question while resuming. All twenty-eight new variation/contrast actions pass. One of nine model language replies still asks two questions. None of these failures is hidden or rescored.

Further prompt-only tuning has diminishing returns. v0.2 section 7.4 calls for trusted `allowed_next` context and code-enforced transitions. The next experiment should test that boundary, while preserving the unfiltered thirteen-action diagnostic and its scores.

## Proposed implementation boundary

Add an **evaluation-only** action eligibility/schema builder. It consumes only supplied state and current input, never IDs, rationale, category, expected answers or fixture history labels.

- Permit a question only where its field is pending: `time` when awaiting time with a known date; `confirmation` at the confirmation stage with unconfirmed critical fields; verification in the appropriate unverified workflow; appointment selection when multiple verified appointments are already available.
- Restrict patient/appointment/slot/provider arguments to supplied eligible identifiers; restrict mutation candidates using supplied confirmation, ownership, stage and policy flags.
- Apply emergency, verification-attempt and exhausted-retry/no-answer gates from trusted state. Keep ordinary fact questions, handoff requests and field/task corrections available where appropriate.
- Ground `date_phrase` to literal contiguous spans of the current input, so the constrained decoder cannot translate English into Urdu or copy a date from examples. This selects no expected date: all bounded word spans are candidates, and the model must still select the relevant phrase.
- Pass the derived action contract as `allowed_next` context and the response schema. Validate against both the global tool schema and the state/input-derived schema before exposing an action. Reject instead of correcting an output after inference.

Use an explicit diagnostic mode/label for this experiment. Record the actual constrained schema and generation configuration in each row. Report unfiltered model accuracy and state-constrained pipeline accuracy separately; a constrained pass does not establish that the unfiltered model is repaired.

## Tests and acceptance

Test absent/present fields, verification attempts two/three, confirmed/unconfirmed mutations, known/unknown IDs, readonly fact digressions, raw span preservation, and metadata/oracle invariance. Keep original fixture/scorer files unchanged. Run all original sixty actions, twenty-eight variations/contrasts, nine model language samples and three exact code confirmations. Repeat the original actions on an unchanged candidate before accepting a reliability result.

This prototype executes no tool, mutates no dialogue state, and starts no API/database/voice phase. Production state validation, tenant authorization, task-stack updates, verification, concurrency, idempotency and speech acceptance remain future implementation gates.
