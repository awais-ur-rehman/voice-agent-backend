# Phase 5 local checkpoint

Use synthetic clinic data only. Metadata/source editing can run while embedding and ingestion remain disabled. Never create fixture vectors or ready generations in development to make the interface appear available.

## Local services

Retain Compose project `voice-agent-phase1`, `compose.yaml`, the existing private local override, PostgreSQL 16/Bookworm and `voice-agent-phase1_postgres-dev-data`. PostgreSQL is localhost:55432, API localhost:8000, Valkey localhost:56380; disposable tests use 55433/56379. Back up the database and roles privately before migration; inspect backup exit codes and retain the prior volume. Apply `alembic upgrade head` through the normal migrate service. The reviewed head is `0009`; never stamp, downgrade or rewrite applied migrations.

The local checkpoint uses `.env.api`, `.local/embedding.env` and a mode-0600 `.local/knowledge.env` as Compose inputs. The latter supplies a newly generated distinct `KNOWLEDGE_BLOB_SECRET` and `SCHEDULER_KNOWLEDGE_ENABLED=true`. The API maps it to `SCHEDULER_KNOWLEDGE_BLOB_SECRET`; a future worker maps it to `KNOWLEDGE_WORKER_BLOB_SECRET`. It never reaches the model service/browser. Explicit Compose mappings override `env_file`: preserve the existing API's `LLM_API_KEY` interpolation privately rather than replacing or exposing its credential. Do not copy model-provider dotenv files into worker/embedding services. Missing blob key intentionally blocks raw writes and cleanup.

Keep `SCHEDULER_VOICE_ENABLED=false`, `EMBEDDING_ENABLED=false`, and `KNOWLEDGE_WORKER_ENABLED=false`. Only the API needs refreshing here. A local operator with the private files can use:

```sh
docker compose --env-file .env.api --env-file .local/embedding.env --env-file .local/knowledge.env -p voice-agent-phase1 -f compose.yaml -f .local/task7-compose.yaml build api
docker compose --env-file .env.api --env-file .local/embedding.env --env-file .local/knowledge.env -p voice-agent-phase1 -f compose.yaml -f .local/task7-compose.yaml run --rm --no-deps migrate
docker compose --env-file .env.api --env-file .local/embedding.env --env-file .local/knowledge.env -p voice-agent-phase1 -f compose.yaml -f .local/task7-compose.yaml up -d --no-deps api
```

The local override allows the dedicated `http://localhost:5176` frontend origin. Do not publish private override/env files. For a new checkout, configure its CORS origin explicitly rather than assuming those ignored files exist.

## Checks and owner flow

Install locked optional test groups, then run Ruff check/format, `mypy scheduler brain knowledge knowledge_worker`, all three `scripts.export_*_contracts --check`, and `.venv/bin/python -m pytest` against only `voice_agent_test`. One database suite at a time. Run Phase 0 offline/evaluation dry runs and `voice/.venv/bin/python -m pytest voice/tests`; these native tests are controls, not voice inference. API/worker/embedding image smoke checks use no network, no owner assets, and disabled startup. CI must never load owner dotenv/model assets or run live provider calls.

In the synthetic clinic, open Agents → Knowledge. Add/edit text, FAQ, public URL, and a small file; reopen each editor to verify protected detail prefill. Replace file content, reindex, preview, and delete a disposable source. With model execution disabled, sources remain queued, processing is unavailable, search is disabled, and no citations/answers are invented. Start the isolated test and ask `Is bicycle parking available?`; the queued corpus produces a safe no-answer trace. End the test. Parent appointments and knowledge audits must remain unchanged; test audit effects belong to the child. Structured clinic fields continue to use code-owned routing.

The [ingestion](phase-5-ingestion.md), [sources](phase-5-sources.md), [retrieval](phase-5-retrieval.md), and [embedding](phase-5-embedding.md) documents specify format/token/resource bounds, URL isolation, forced RLS, revision/lease fencing, untrusted references and private blob retention. Documents are reference data, never scheduling authority. Literal citation, language/script and operation-claim validation remain mandatory. Wire grammar permits empty answer/citations so the provider can decline, but server validation never accepts that as a ready answer.

Hosted fixed-context probes are separately authorized and counted. They use the existing Nemotron model/budget, thinking disabled and 512 output tokens. They do not exercise embedding, retrieval recall or a ready development corpus. Real CPU qualification must remain deferred until owner authorization; see the explicit opt-in command in the embedding guide. Phase 6 and merges remain outside this checkpoint.
