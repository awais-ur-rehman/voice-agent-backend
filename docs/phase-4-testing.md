# Phase 4 local testing

Use the existing `voice-agent-phase1` Compose project. Preserve `.env`, `.env.api` and the PostgreSQL volume. Do not run `down -v`, reset the development database or enable native inference for this checkpoint.

## API and migration

In `backend`, retain the existing private `.env.api` settings and explicitly set `SCHEDULER_REGISTRATION_ENABLED=true` for loopback QA; its Compose default is **false**. Keep `SCHEDULER_VOICE_ENABLED=false`. Then:

```sh
UV_CACHE_DIR=.local/uv-cache uv sync --locked --all-groups
docker compose --env-file .env.api --env-file .env up --build -d api
```

The second env file supplies the already-authorized hosted NIM key at runtime only. Omit it for credential-free operation. Never print dotenv files, resolved Compose configuration, tokens or conversations. Startup initializes the existing restricted role and runs additive Alembic migrations before the API. API 8000, development PostgreSQL 55432 and Valkey 56380 remain loopback-only; tests use separate 55433/56379 services.

## Reproducible automated checks

```sh
docker compose --env-file .env.api --profile test up -d --wait postgres-test valkey-test
UV_CACHE_DIR=.local/uv-cache uv run --locked ruff check .
UV_CACHE_DIR=.local/uv-cache uv run --locked ruff format --check .
UV_CACHE_DIR=.local/uv-cache uv run --locked mypy scheduler brain
UV_CACHE_DIR=.local/uv-cache uv run --locked python -m scripts.export_voice_contracts --check
UV_CACHE_DIR=.local/uv-cache uv run --locked python -m scripts.export_agent_contracts --check
UV_CACHE_DIR=.local/uv-cache uv run --locked python -m scripts.export_dashboard_contracts --check
UV_CACHE_DIR=.local/uv-cache uv run --locked python -m pytest
.venv/bin/python tests/run_offline.py
.venv/bin/python -m evaluation --dry-run
.venv/bin/python -m evaluation.flows --dry-run --fixture-set all --action-policy state_constrained
.venv/bin/python -m evaluation.flows --suite confirmations
.venv/bin/python -m evaluation.assets
voice/.venv/bin/python -m pytest voice/tests
```

Run only one database pytest process at a time. The native command uses the already-prepared control environment and does not load/download models. Follow the separate Phase 3 environment guide if that environment is absent. Runtime packaging has an independent CI check:

```sh
docker build --target runtime -t clinic-runtime:test .
docker run --rm --network none clinic-runtime:test uv run --no-sync python -c 'from brain.voice_catalog import catalog; from brain.agent_config import AgentConfig; assert len(catalog()) == 12; assert AgentConfig().schema_version == 2'
```

## Owner walkthrough

Open frontend `http://localhost:5173`, create a **synthetic** clinic, and save credentials privately. Configure clinic address/preparation, provider/service assignments and hours. Preview Friday/Ramadan templates before applying. Search an Urdu patient name; explicitly review retained fields before merging synthetic duplicates. Book an offered slot, review before confirmation, then inspect history, reschedule and cancel. Check staff cannot edit drafts/settings or merge patients.

Create an agent, save its bilingual draft, select provisional voice profiles and publish. Versions show the current tenant-scoped publisher display name when available; historical/missing names have a truthful fallback and the immutable publisher UUID remains in the API record. Restore creates another immutable version. Disable prevents new admissions; existing sessions retain their pinned version.

Start an isolated test with an enabled agent. Use the displayed synthetic patient phone/DOB, request a provider/service/date, choose an offered time, request readback and explicitly confirm. Verify the parent live calendar is unchanged and end the test. Hosted output is nondeterministic: report actual executed decisions and failures, not an assumed success. Task7 bounded this to seven API turns plus one separate one-request provider smoke, with Nemotron thinking disabled. Deterministic tests use fake providers and remain separate evidence.

Speech remains unavailable. Do not raise memory caps, load native models, enable voice or infer ASR/Urdu acceptance from text success.

## Production preview origin

Normal frontend origin remains `http://localhost:5173`, API `http://localhost:8000`; Vite has no API proxy. For Task7 production QA, `npm run build` and `npx vite preview --host 127.0.0.1 --port 5176 --strictPort` served `http://localhost:5176`. An ignored local Compose override added **only** `http://localhost:5176` alongside 5173 to `SCHEDULER_CORS_ORIGINS`; no wildcard was used. Reapply an explicit local override when reproducing that preview; otherwise use the default 5173 origin. No browser/provider secrets belong in Vite configuration.

Inspect1440/1024/390 layouts, actual day/week calendar transitions, booking/details dialogs, Urdu labels, lazy editor navigation and console errors. Mobile has a server-backed appointment list. Refresh intentionally signs out because credentials remain in memory. See [results](phase-4-results.md) for measured limits and retained warnings.
