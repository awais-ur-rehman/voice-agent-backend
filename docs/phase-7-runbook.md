# Phase 7 local rollout runbook

Use the existing `voice-agent-phase1` project and locked development environments. Owner ports are API `8000`, frontend `5176`, PostgreSQL `55432` and Valkey `56380`. Test PostgreSQL `55433` and test Valkey `56379` are separate disposable services. Keep the development database and private knowledge volumes mounted under their existing identities.

## Before replacing any service

1. Confirm the running API and analytics worker image IDs, Compose files, loopback ports, named mounts, and host and Docker memory headroom using projected fields. Keep the existing private `.local/task7-compose.yaml` CORS override and its explicit localhost origins. Treat dotenv, Compose inputs, browser receipts and recovery bundles as private.
2. Use the [recovery CLI](phase-7-recovery.md) to capture a fresh pre-upgrade bundle A. Verify it separately against the SHA-256 from its trusted creation receipt, then confirm schema `0016` for this upgrade. A failed backup or verification stops the rollout before tag, schema or configuration changes.
3. Match both qualified image IDs and their source maps to the reviewed checkout. Retain the current API and worker IDs under distinct local backout tags before changing their active aliases. Promote only the verified image IDs to `voice-agent-phase1-api:local` and `voice-agent-analytics-worker:local`. Keep the private credential and feature settings. This rollout used the new default three-second provider attempt timeout because the previous setting was implicit.

## Apply and verify

Use these commands only after the backup and image gates pass. The existing private files shown below supply Compose interpolation. Keep them out of terminal traces and reports.

```sh
phase7_compose() {
  docker compose --project-name voice-agent-phase1 \
    --env-file .env.api --env-file .env \
    --env-file .local/embedding.env --env-file .local/knowledge.env \
    --env-file .local/phase6/analytics.env -f compose.yaml "$@"
}

phase7_compose -f .local/task7-compose.yaml -f compose.analytics.yaml \
  run --rm --no-deps --pull never migrate
phase7_compose -f .local/task7-compose.yaml -f compose.analytics.yaml \
  up -d --no-deps --no-build --pull never api
phase7_compose -f compose.analytics.yaml --profile analytics \
  up -d --no-deps --no-build --pull never analytics-worker

curl --fail --silent --show-error http://localhost:8000/health/live
curl --fail --silent --show-error http://localhost:8000/health/ready
```

The migration command applies `0017` and `0018` using normal Alembic history. Keep `init-db`, stamp, downgrade, reset and volume removal outside this procedure. Confirm schema `0018`, role fingerprints and grants, both running image identities, the restricted worker's recent successful evaluation, and the existing API and database/cache bindings. Check exact CORS allow and deny behavior and unauthenticated staff/private metrics responses. Use the disposable checks for quota exhaustion, drain and provider failure. Leave owner drain disabled.

If an image fails after the additive upgrade, retain the failed receipts. Any operational backout uses the retained image IDs and the same `--no-deps --no-build --pull never` commands after confirming those images support the current schema. Keep the owner database at its current revision.

## Capture and drill the upgraded state

After the deployed safety checks pass, create a new private bundle B from schema `0018` and verify it against its separate trusted creation receipt. Drill B using the exact [recovery CLI](phase-7-recovery.md) procedure. Record backup wall time, capture and verification durations, drill wall time, tool restore duration and snapshot age separately. The target owns its own tmpfs and private copy of the blobs. Verify the original source row and role fingerprints, blob metadata and mounts again afterward.

The CLI refuses an unverified source or target. If Docker or PostgreSQL compatibility prevents exact identity, mount or state checks, keep the failure and use a reviewed correction with a fresh drill receipt. Resource admission requires at least 1 GiB available on the host and Docker's reported budget. If the floor is missed, wait for headroom or stop only an identified disposable test service once its tests are idle.

## Exercise the existing browser flow

Open [local Analytics](http://localhost:5176/analytics?start=2026-11-08&end=2026-11-08). Sign in as the existing fictional Pearl Dental Studio — Demo account using its private local credentials. The explicit November 8 cohort shows five Text and zero Voice sessions. Open the information-only call in Calls & sessions and check the protected transcript. Capture is off and the transcript has no retained text. The knowledge gap remains processing pending with 24 queued sources and zero vectors. Leave the cohort as it is. Use the maintained opt-in `frontend/e2e/analytics-local.spec.ts` for the browser check and keep the screenshots and console evidence private.

When the frontend cannot connect, confirm both loopback origins and a successful `OPTIONS` preflight for `http://localhost:5176` before retrying authentication. If port `5176` is free, start the installed Vite server from the frontend checkout with `VITE_API_URL=http://localhost:8000 npm run dev -- --host 127.0.0.1 --port 5176 --strictPort`. An API restart resets process-local metrics. Durable cohort totals remain in PostgreSQL.

See [local results](phase-7-results.md) for observations and open qualification gaps.
