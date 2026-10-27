# Voice Agent Backend

Phase 1 contains a runnable staff scheduling API: authentication, PostgreSQL tenant isolation, scheduling configuration, slot generation, and transactional booking/rescheduling/cancellation. The separate `evaluation/` package retains the synthetic CPU-only Phase 0 evidence. The React dashboard is a later phase.

## Phase 1 quick start

Requires Docker Desktop, uv and Python 3.12. From this backend directory:

```bash
uv sync --locked --all-groups
python3 scripts/dev_setup.py
docker compose --env-file .env.api up --build -d api
uv run python -m scheduler.bootstrap --demo
uv run python scripts/smoke_phase1.py
```

Open [API docs](http://127.0.0.1:8000/docs). Synthetic demo accounts are generated into the private ignored `.local/phase1-demo-credentials.json`; passwords/tokens are never printed. Existing Phase 0 `.env` is preserved. See the [Phase 1 owner testing guide](docs/phase-1-owner-checkpoint.md) for login, booking/replay/races, tenant isolation, policies and the stop point. Staff APIs are not LLM tool endpoints. Phase 1 remains unmerged for owner acceptance.

## Retained Phase 0 evaluation

The governing v0.1 product/engineering spec and v0.2 Urdu/NIM addendum live in the parent workspace's `documents/` directory. The [decision-fix results](docs/phase-0-decision-fix-results.md) preserve unrestricted failures separately from the passing state-constrained experiment. Voice/GPU and native acceptance remain open. The commands below apply to evaluation tooling, not the Phase 1 API.

## Setup

Requires Python 3.12. No local GPU is needed for these text/fixture checks.

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python --require-hashes requirements.lock
```

Or create a Python 3.12 virtual environment and run `.venv/bin/python -m pip install --require-hashes -r requirements.lock`. Skip environment creation if the prepared `.venv` already exists. The direct evaluation transport uses the standard library; fixture/contract validation uses hash-locked `jsonschema` and its dependencies.

Copy `.env.example` to `.env` only on first setup and configure `LLM_API_KEY` locally. Never overwrite an existing secret file. The runner expects one active key; commented alternatives are ignored. `LLM_MODEL` should be `nvidia/nemotron-3.5-bolt-e4b`; the commands below also pass the model explicitly to avoid stale environment selections.

## Offline verification

```bash
.venv/bin/python tests/run_offline.py
.venv/bin/python -m evaluation --dry-run
.venv/bin/python -m evaluation.flows --dry-run
.venv/bin/python -m evaluation.flows --dry-run --fixture-set all --action-policy state_constrained
.venv/bin/python -m evaluation.assets
.venv/bin/python -m evaluation.flows --suite confirmations
```

CI runs the same offline checks on Python 3.12 after installing the hashed lockfile. The test runner blocks workspace credential-loader reads and socket connections and clears inherited LLM credentials; fixture tests use fake credentials. No API key or live inference is used in CI. Confirmation-only delivery is also checked directly without credentials.

The preparation package includes:

- Original 40 extraction/clinic-info cases: 20 Urdu, 10 English, 10 mixed.
- Thirty next-action scenarios, each in English and Urdu: supplied synthetic history/state, expected action and arguments; no tool is executed.
- Fourteen additional bilingual variation/contrast scenarios for state eligibility and field/intent distinctions. These are author-created coverage, not hidden validation.
- Twelve language/delivery samples: nine model-generated speech-text replies for native review and three authoritative confirmations delivered directly from code, with source provenance recorded.
- Thirty-five resolver golden cases, all number expectations from 0–100, and 15 structured normalizer cases. These are requirements for the later resolver/normalizer, not implementations.
- A proposed versioned bilingual tool-result JSON Schema and positive/negative examples.
- An unrecorded manifest with 200 Urdu + 50 mixed prompts across ten speaker slots. A prompt is not a recording or a ground-truth transcript.

## Hosted Nemotron evaluation

Use synthetic fixtures only. Obtain a key using [NVIDIA's API Catalog guide](https://docs.api.nvidia.com/nim/docs/api-quickstart).

```bash
# Original single-turn baseline; --limit 1 is a smoke test.
.venv/bin/python -m evaluation --model nvidia/nemotron-3.5-bolt-e4b --limit 1

# Recommended Phase 0 experiment: 88 state-constrained actions + 12 language/delivery samples.
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b --fixture-set all --action-policy state_constrained

# Unrestricted diagnostic: the original sixty actions + twelve language/delivery samples.
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b --action-policy unrestricted

# Language samples only, or a selected language for a short manual rerun.
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b --suite language
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b --scenario correct-date
```

Nemotron requests explicitly send thinking off, parallel calls off, and `skip_special_tokens=false`, with top_p 0.95. Action probes use temperature 0; language samples retain temperature 1.0. Expanded action probes default to `response_format=json_schema`: the model chooses among all thirteen action schemas, and the complete JSON response must pass strict local validation before an internal action request is exposed. No expected answer is supplied, no free-form function text is repaired, and no scheduling tool executes. `--action-protocol native` retains the previous native-call path as a diagnostic. Original extraction baselines used native `auto`; original expanded native runs used `required`. Reports identify the actual mode; results are not a controlled A/B model-quality experiment.

The explicit `state_constrained` policy derives `allowed_next` names and argument constraints from supplied synthetic state and literal current-input spans. It rejects ineligible responses without rewriting them. Singleton actions dictated by state are counted separately. The default `unrestricted` diagnostic retains all thirteen action names; its results measure a different capability. Neither mode executes a tool or implements production authorization. See [decision-fix results](docs/phase-0-decision-fix-results.md).

Each request is separated by two seconds by default. No hidden retry/failover or saturation test is performed. `--interval` and `--timeout` control pacing/socket timeout; the 30-second experimental ceiling is not the production three-second failover policy. Runs stop at the first provider failure and retain partial reports. A scoring failure does not stop remaining samples. Exit codes: 0 all requested mechanical checks pass; 1 a scoring/provider failure; 2 invalid configuration.

JSON reports are private files under ignored `results/`. First content/control deltas are client-observed timing, not tokenizer-level TTFT or end-to-end audio latency. `action_ready_ms` records when a whole validated structured action is available. Code confirmations have no model latency or usage; their source is `code_template`. The three confirmation samples validate the exact server-authored bilingual `say` string and bypass inference completely, including refusal to build a model request for that content. Language checks are heuristics, with **human review pending even on a mechanical pass**. Correct tool selection does not prove an implemented state machine, database transaction, caller verification, or successful booking.

## Review the outputs

```bash
.venv/bin/python -m evaluation.review results/phase0-state-constrained-100.json --suite language
```

The generated local HTML escapes all model text, renders mixed scripts, and keeps human acceptance separate from automated scores. Open it from the file link in [latest results](docs/phase-0-decision-fix-results.md). A fresh live run gets a timestamped report name; pass that path instead. The review command does not overwrite an existing reviewed artifact.

## Planned production boundaries

FastAPI/Python 3.12 owns auth, clinic APIs, policy, verification and persistence. A separate worker process owns Pipecat/LiveKit sessions. PostgreSQL 16/pgvector is durable storage; Valkey holds temporary state. Production scheduling safety belongs in code and database constraints, never prompts. A production LLM adapter follows the spec's OpenAI-compatible SDK path; this small evaluation transport does not replace it.

Backend owns shared schemas under `packages/schemas`; publish versioned artifacts for the separate frontend rather than maintaining handwritten duplicate contracts. Default clinic timezone is `Asia/Karachi`, currency PKR, with Pakistan phone normalization. [Engineering rules](AGENTS.md), [branch workflow](CONTRIBUTING.md), [license ledger](docs/licenses.md), and [comparison status](docs/model-comparison-status.md) apply.
