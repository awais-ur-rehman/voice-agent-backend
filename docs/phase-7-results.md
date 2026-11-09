# Phase 7 local rollout and recovery results

The local owner stack is running schema `0018` with the Phase 7 API and analytics worker. The isolated recovery drill passed on the current-schema backup. The binding 20-client synthetic booking p95 is **159.576139 ms** against a strict **under 150 ms** target. That result leaves full Phase 7 qualification open. [Local rollout evidence](evidence/phase-7-local-rollout.json) · [Synthetic load evidence](evidence/phase-7-synthetic-load.json)

## Local rollout

The development PostgreSQL and Valkey containers and named data mounts kept their existing identities. Backup A was separately verified before the two original image IDs were pinned under local backout tags. The normal Alembic command applied `0017` and `0018` in 1.753204 seconds. The API and analytics worker were recreated individually with `--no-deps`. After the recovery compatibility fixes passed the drill, the final two images were promoted and those services were refreshed again without schema changes.

All 31 projected configuration checks passed, including the existing explicit CORS origins, registration setting, primary credential, disabled secondary routing, disabled voice, and enabled knowledge and analytics settings. The provider timeout previously used an implicit default. The new image applies its three-second default with the existing files intact. Drain is false. The enabled worker reported fresh evaluation and retained its read-only root, restricted role, resource limits, dropped capabilities and private tmpfs.

Final loopback probes returned liveness and readiness `200`, unauthenticated staff `401`, unauthenticated private metrics `403`, allowed and unlisted CORS preflight `200` and `400`, and oversized body and query `413` and `414`. The responses carried the required security headers and a request ID. Provider, embedding, knowledge ingestion and native voice activity remained outside the live exercise.

| Running image | Image ID |
| --- | --- |
| API | `sha256:5d238677b1e2e58d9363e2fbcc6990aa8be31b75e5b54994061c9e7a16bcbc5c` |
| Analytics worker | `sha256:ad2fee939b779f60ea7160acc545e802585bf636281450b046f92fe6d2575503` |

The 141-path check image context matched the reviewed Task 5 source except for `scripts/recovery.py`. Both images passed all 11 build, import, refusal, constraint and parity checks. The final context SHA-256 is `3ce71083f2387760f9627340f54e8e01edb8bc5578b0caeabc26bee8cb790479`. The backend recovery suite passed 116 tests with its explicit dedicated-SQL opt-in skipped in the standalone run. That SQL probe passed in the reviewed Task 5 serial suite. Scoped Ruff, formatting and operator mypy passed. The broader unchanged product and frontend gates retain their Task 5 evidence. [Security review](phase-7-security-review.md)

## Backup and restore measurements

| Backup | Schema | Backup wall | Internal capture | Trusted verify wall | Snapshot age at verify |
| --- | --- | --- | --- | --- | --- |
| A, before upgrade | `0016` | 1.179828 s | 1.043 s | 0.001528 s | 0.640502 s |
| B, after upgrade | `0018` | 1.077157 s | 0.926 s | 0.001215 s | 0.568971 s |

The successful drill used the same verified Backup B after the compatibility diagnostics. Its full CLI wall time was **21.124841 s** and the tool's internal restore duration was **17.726 s**. The snapshot was **482.765526 s** old at drill start and **503.890368 s** old at completion. This age includes the intervening diagnostic work. Before target creation, host headroom was approximately 1.58 GiB and Docker headroom was approximately 2.93 GiB. Both disposable test services stayed running.

The isolated pinned PostgreSQL target ran with network `none`, no published ports or durable data volume, 512 MiB memory and swap combined, one CPU, and the three verified tmpfs paths. Verification matched migration state, ownership, role flags and password hashes, ACLs, sequences, catalog and fingerprints across 42 public tables. Negative RLS probes passed for 38 app-readable forced-RLS tables in each of the no-tenant and absent-tenant scopes. The live receipt verifier authenticated all six restored blob copies. The tool removed its container and scratch root before writing the acceptance receipt. Positive restored API read coverage remains pending.

The first backup guard refused Docker's omitted `HostConfig.Tmpfs` field before credential lookup. The first drill then refused Docker's empty top-level mount list and cleaned its target before start. The next drill and two diagnostics restored the dump, detected extension owner and ACL representation differences, and cleaned their targets. The final tool verifies actual kernel mounts, restores the pinned image's socket directory to `0700` inside the isolated target, connects for dump restoration as the original `postgres` role, and recognizes only PostgreSQL's identical built-in owner-only table ACL. The trusted bundle stayed intact through those retries. [Recovery procedure](phase-7-recovery.md)

## Source and browser observations

Before Backup B and again after the drill, browser check and final service refresh, the protected clinical and knowledge row fingerprints, source role fingerprints, and all six original blob and receipt identities remained stable. Expected differences were the migration row and admission schema, the analytics worker heartbeat and open backlog alert clocks and age, and one normal login admission bucket and one refresh token from the browser case. The dental sources remained 24 queued with zero vectors and zero generations.

The existing frontend port had no listener at preflight. The installed Vite server was started on the same loopback port `5176` with strict binding and the API origin `http://localhost:8000`. The maintained real Chrome case passed in 2.9 seconds with a 6.6-second Playwright invocation. It checked the existing November 8 cohort of five Text and zero Voice, an information-only call, capture off and no transcript text. It recorded zero browser console warnings and errors, unexpected post-login writes, or local and session storage keys. The final API refresh changed only recovery tool bytes relative to that real-browser run. No new demo sessions were created to refill process-local metrics.

The recovery evidence is one local CPU observation. Production TLS and at-rest protection, off-site recovery and failover, service RPO/RTO and availability, the public widget, live provider and secondary quality, GPU/native concurrency and latency, Urdu voice and fallback, embedding and RAG quality, and pilot operation remain open. Keep the Phase 7 checkpoint unmerged for owner review. [Owner checks](phase-7-testing.md)
