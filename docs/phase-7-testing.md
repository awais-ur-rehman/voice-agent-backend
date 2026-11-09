# Phase 7 owner checkpoint

The local CPU checkpoint covers the staff API, the analytics worker, the existing dental dashboard, and a private isolated recovery procedure. Use the [rollout runbook](phase-7-runbook.md) for local operations and [observed results](phase-7-results.md) for the rollout and remaining qualification gaps.

## Owner checks

1. Check the existing API liveness and readiness at `/health/live` and `/health/ready` on port 8000. Check that the analytics worker has evaluated recently. Keep the existing private CORS origins for ports 5173 and 5176 and leave drain disabled.
2. Open [the existing November 8 dashboard](http://localhost:5176/analytics?start=2026-11-08&end=2026-11-08) and sign in with the fictional dental clinic account from the private local credentials file. Verify five Text and zero Voice admissions. Open the existing information-only session and use **Check protected transcript** to confirm capture was off and there is no retained text. The dashboard's unknown cost and speech timing reflect the lack of provider and speech observations for this cohort.
3. For an automated check of that same real API flow, use the opt-in command in the frontend's `docs/phase-7-testing.md`. It confirms the served API origin before authentication, and asserts no unexpected post-login mutations, console warnings or errors, or local or session storage keys. Keep artifacts private.
4. Check the private Backup A and Backup B creation and verification receipts and the isolated drill receipt. A is the `0016` pre-upgrade snapshot and B is the `0018` post-upgrade snapshot. Use the [recovery guide](phase-7-recovery.md) to reproduce the checks with new private paths and the retained original HMAC secret.

## Disposable verification

Run database checks serially using only the dedicated PostgreSQL and Valkey test services. The [security review](phase-7-security-review.md) lists the guarded load command and coverage for application quotas, drain, redaction, conflicts, provider failure and cache recovery. The frontend fixture tests use intercepted responses. Recovery tests use synthetic files or the dedicated test database after the explicit SQL opt-in.

```sh
.venv/bin/python -m pytest tests_environment/test_recovery.py -q
.venv/bin/ruff check scripts/recovery.py scripts/recovery_bundle.py scripts/recovery_database.py tests_environment/test_recovery.py
.venv/bin/mypy --explicit-package-bases scripts/recovery.py scripts/recovery_bundle.py scripts/recovery_database.py
```

The 20-client control-plane workload qualifies correctness separately from latency. The recorded booking p95 exceeds its strict 150 ms target. The local tests exercise CPU operation and do not establish public widget, production TLS/storage, off-site recovery, live provider/voice, Urdu, pilot, or availability targets.
