# Phase 7 synthetic control-plane qualification

## Reproduce the local profile

Use the prepared Python 3.12 backend environment and the dedicated synthetic fixture services. Coordinate one PostgreSQL test workload at a time. For a fresh test stack, these commands select only the disposable services and provision their test schema through the maintained test fixture.

```sh
docker compose --env-file /dev/null --profile test up -d --wait postgres-test valkey-test
env -i PATH=/usr/bin:/bin .venv/bin/python -m pytest -q tests_api/test_hardening.py::test_atomic_sql_limit_expiry_and_privileges
```

Run the fixed profile from the backend checkout. Keep the raw receipts in an ignored private directory.

```sh
umask 077
mkdir -p .local/phase7-load
env -i PATH=/usr/bin:/bin LC_ALL=C TZ=UTC .venv/bin/python -m scripts.hardening_load \
  --allow-disposable-fixtures \
  > .local/phase7-load/measurements.json 2> .local/phase7-load/requests.log
```

The CLI requires explicit opt-in and the exact published synthetic PostgreSQL and Valkey fixture URLs on loopback ports 55433 and 56379. Both PostgreSQL logins check the connected database identity. The admin connection checks for schema 0018 before fixture writes. The CLI uses two reserved synthetic tenant IDs, two derived admission keys and an isolated Valkey state prefix. The cleanup transaction checks each reserved tenant name. Before application imports, the CLI replaces the inherited environment with a short allowlist. Provider and media adapters are isolated, Python socket access is fenced to the test cache, and PostgreSQL uses a fixed literal loopback DSN.

The default 20-client, 30-round profile qualifies only if both target p95s are strictly below 150 ms, correctness checks pass, and the observed admitted peak is exactly 20 in each of the five measured groups. Those groups are search, original information commands, information replay, independent bookings and booking contention. Warmup and winner replay have separate reporting. A smaller `--clients` or `--rounds` run provides a functional probe with a null qualification result. Exit 2 indicates a refused invocation. Exit 1 indicates a setup or execution error or an unmet measurement or correctness check. Completed measurement failures retain their JSON report.

## Observations

Measured with the in-process HTTP ASGI transport on an eight-logical-CPU arm64 Darwin host with Python 3.12.13, PostgreSQL 16.15 and Valkey 8.1.11. The [aggregate evidence](evidence/phase-7-synthetic-load.json) records the exact source digest, runtime versions, status counts, sample counts and private receipt hashes. Warmup and winner replay are separate groups. Quantiles use linear interpolation at `(n - 1) × q` and report null when there are no qualifying samples.

| Workload | HTTP outcomes | Peak attempted / admitted | p50 ms | p95 ms |
| --- | --- | ---: | ---: | ---: |
| Staff slot search | 600 × 200 | 20 / 20 | 67.70 | 105.79 |
| Runtime information command | 600 × 200 | 20 / 20 | 220.25 | 277.87 |
| Exact information command replay | 600 × 200 | 20 / 20 | 106.47 | 142.85 |
| Independent confirmed booking | 600 × 201 | 20 / 20 | 116.41 | 159.58 |
| Single-slot booking contention | 1 × 201 and 19 × 409 | 20 / 20 | 96.40 | 141.02 |

An attempted interval begins when the client submits a request. App in-flight begins at ASGI entry. Admitted overlap counts the ASGI intervals of requests that returned an HTTP success or the expected business-layer slot conflict. The measurement covers overlapping accepted HTTP control-plane requests from ASGI entry through response completion. The booking fixture gives each client its own provider and patient, with three half-hour slots per day for ten days. The separate race targets one slot and requires one winner, exact winner replay, and one corresponding SQL appointment and idempotency receipt. Information replays match the original command payload, result, revision and SQL recovery projection.

The recorded qualification predates the CLI's measured-overlap check. Its five measured groups each reached an admitted peak of 20. Slot search measured 105.79 ms p95 and independent successful booking measured 159.58 ms p95. Correctness passed, and the booking latency left qualification unmet under both decisions. Earlier booking p95 observations were 147.61 ms, 144.17 ms and 156.39 ms on their respective source digests. The 156.39 ms result followed the HTTP compatibility and cancellation fixes. A separate diagnostic found 23 SQL statements per successful booking, with a combined median of 8.19 ms for two lookups of rows the transaction already held. Booking now reuses the locked provider and canonical patient and retains the original merged-patient check. All four qualification receipts are retained. Dataset, wave size, timing boundaries and threshold stayed fixed. The information and contention figures have separate reporting and no 150 ms acceptance threshold.

## Security and fault matrix

| Boundary | Maintained evidence | Observed behavior |
| --- | --- | --- |
| Provider retries, breaker and delayed completion | `tests_brain/test_provider_router.py`, `tests_brain/test_hardening_chaos.py` | Synthetic 429 and 503 responses permit one secondary attempt. A delayed secondary timeout stays authoritative and retains provenance without adding an operation or transcript. Existing tests cover breaker open, half-open and blocked work. |
| SQL recovery and cache loss | `tests_brain/test_routes.py`, `tests_brain/test_hardening_chaos.py` | Deleting the synthetic Valkey state forces SQL receipt replay and repairs the cache at the same revision. A replay leaves one receipt and issues zero provider attempts. |
| Partial booking writes and database failures | `tests_brain/test_hardening_chaos.py` | PostgreSQL serialization failure and an injected driver outage after appointment flush produce safe 409 and 503 envelopes. The transaction rolls back the appointment, audit and receipt. The same key can commit once after recovery. |
| Drain and active requests | `tests_api/test_hardening.py`, `tests_brain/test_hardening_chaos.py`, `tests_brain/test_voice_worker_routes_sql.py` | Liveness remains available. Readiness and new session admission return 503 while an existing turn reaches its bounded result. Existing authorized voice heartbeat and session recovery remain available. |
| Cancellation, reconnect and authority | `tests_brain/test_voice_sessions.py`, `tests_brain/test_voice_worker_routes_sql.py`, `voice/tests/test_process.py`, `voice/tests/test_measurements.py` | SQL lease generation fences stale workers. Native synthetic child and event controls discard canceled or delayed generation output. |
| Request limits, authentication and log privacy | `tests_api/test_hardening.py`, `tests_brain/test_hardening_chaos.py`, `tests_brain/test_voice_tokens.py`, `tests_knowledge/test_sources.py` | Body, header, query and admission limits bound request work. The two inline knowledge write routes have a 6 MiB JSON cap that preserves their existing text and FAQ limits. Staff, runtime and worker scopes remain separate. Hostile fields and raw exception text stay out of public responses and structured request logs. |
| Knowledge input and grounding | `tests_knowledge/test_grounding.py`, `tests_knowledge/test_runtime.py` | Literal citations cannot authorize a mutation or an injected instruction. Tests use deterministic inputs and adapters. |
| Recovery tooling | `tests_environment/test_recovery.py`, [operator procedure](phase-7-recovery.md) | The serial gate opts into the read-only disposable SQL probe with `RECOVERY_SQL_TEST=1`. Actual backup, restore and rollout have their own operator gate. |

The hosted-model tests have four opt-in skips in the backend suite. Native checks exercise synthetic control paths. Live voice capacity, English and Urdu audio quality, live provider fallback, GPU latency, deployed transport, production SLOs, real retrieval, public widget access, TLS, storage encryption and offsite recovery require separate qualification.
