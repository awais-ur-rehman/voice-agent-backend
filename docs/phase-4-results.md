# Phase 4 integration results

Measured 2026-10-29 local time on the existing development stack. Synthetic data only. Phase 4 design was approved 2026-10-28; this is local implementation evidence, not owner acceptance or a merge claim.

| Check | Result | Raw evidence |
| --- | --- | --- |
| Runtime image | Catalog and v2 config import succeeds without network | `evidence/task-7/image-{build,green}.log` |
| Ruff / mypy | Pass;52 typed source files | `backend-static.log` |
| API/brain/environment | 664 passed, 4 explicit hosted skips | `backend-full.log` |
| Final agent management | 21 passed, including cross-tenant projection defense | `agent-final.log` |
| Public schema exports | All 3 check commands exit0 | `contracts.log` |
| Offline suite | 63 tests; dry-run 40 cases / 100 probes; confirmation 3/3; no model requests | `offline.log` |
| Native control only | 38 passed; upstream audioop deprecation retained | `native-control.log` |
| Real deployed API | Setup, Friday/Ramadan preview/apply, manual book/move/cancel, Urdu search/merge, rollback/disable, staff denials pass | `live-api.log` |
| Deployed authority | Restricted nonowner role, fixed definer owner/path, enabled trigger; actual runtime and owner updates rejected | `deployment-security.log`, `authority.log` |
| Real hosted isolated booking | Seven API turns all succeeded; separate SQL confirms child booking and parent calendar unchanged | `live-hosted.log`, `authority.log` |

Unqualified log names above are under `docs/evidence/task-7/`. API turns are not provider request counts: deterministic guard steps may execute without inference. A separate bounded direct provider smoke records the actual request count and validated action in `hosted-provider-one-request.log`. Nemotron uses `nvidia/nemotron-3.5-bolt-e4b`, `enable_thinking=false`; no transcript or key is logged.

The packaging RED (`image-red.log`) was reproduced before Task7: `/app/voice/catalog.json` absent from the actual runtime image. Runtime now copies only the shared JSON; native environments/models remain excluded. CI verifies the real image import. Whitespace create/v2 names now fail validation and valid new names are trimmed; legacy v1 parsing stays unchanged. Publisher names are projected through both user ID and tenant ID, with null history and inconsistent cross-tenant metadata covered. Immutable publisher IDs remain intact.

`domain-red-corrected.log` demonstrates two behavioral failures before fixes; `domain-green.log` passes them. Earlier `domain-red.log` includes sandbox loopback denial, and `domain-red-authorized.log` includes a rejected impossible cross-tenant fixture insert. Those are retained as unsuccessful test setup attempts, not claimed behavioral RED. `authority-first-attempt.log` expected the trigger SQLSTATE from the restricted runtime, which correctly denied UPDATE earlier at permissions; the final check separately verifies both permission and owner-trigger enforcement.

Frontend evidence lives in the sibling frontend repository:96 tests, production build,3 maintained fixture browser tests, real CUA screenshots at 1440/1024/390, actual UI booking and publisher history. The final friendly speech-copy change has a focused 11-test pass plus lint/build. Real UI allocation/end works against the deployed API; API automation covers the broader setup/merge/roles flow. See its `docs/phase-4-results.md` for exact visual boundaries.

Known limits: native ASR/English/Urdu speech, physical microphone/speaker quality, GPU and performance acceptance remain pending. All voices are provisional. The shared entry >500KB warning remains; no threshold was raised. Earlier Vite ResizeObserver and lazy-navigation auth observations were not reproduced during the scoped production CUA flow; their original causes remain unknown. Native cancellation passes in this authorized run, but the earlier sandbox process-group denial is not relabeled a product fix. Whole-branch review, public draft PRs and owner acceptance are controller-owned next steps; no Phase 5 work is included.

Final-review follow-ups: track the Python 3.12 upstream `audioop` deprecation separately from the earlier process-group `PermissionError`. The Task7 and final-fix authorized 38-test native control runs passed, but neither warning's cause is resolved by that result. Reproduce the process-control environment safely and check upstream audioop migration separately; do not enable voice, raise resource caps or infer native runtime acceptance. No new inference run is justified by the dashboard fixes.


Final fix verification (2026-10-29): publication and rollback now lock Tenant before Agent/draft, matching isolated allocation and preserving profile snapshot/capacity serialization. A deterministic two-connection barrier captured PostgreSQL 40P01 before the fix for both operations; both now serialize without a deadlock (revision conflict is allowed). `docs/evidence/final-fix/` retains RED/GREEN and full outputs: 666 passed / 4 explicit hosted skips, Ruff/mypy/schema checks passed, 63 offline tests plus dry-run probes and code-only confirmations passed with zero model requests. The unchanged native control suite passed 38 tests with the same audioop warning; the earlier process-group error remains a separate unresolved issue. Actual API image rebuilt/redeployed using both existing env files and the private exact-origin override, preserving project/volumes. Running code has tenant-first locking and voice disabled; readiness passed. No NIM or native inference rerun occurred.
