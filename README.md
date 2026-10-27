# Voice Agent Backend

Backend/voice-runtime workspace for an English/Urdu clinic appointment scheduler. The current feature branch contains **CPU-only Phase 0 evaluation tooling**, not a production API, booking engine, or live voice service.

The governing v0.1 product/engineering spec and v0.2 Urdu/NIM addendum live in the parent workspace's `documents/` directory. v0.2 wins conflicts. The owner selected hosted Nemotron with thinking disabled; the other three candidate endpoints and GPU voice latency work are deferred.

Start with [CPU results](docs/phase-0-cpu-results.md) and the [owner test checkpoint](docs/phase-0-owner-checkpoint.md). Do not equate a completed evaluation with a passed voice/product feasibility gate.

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
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m evaluation --dry-run
.venv/bin/python -m evaluation.flows --dry-run
.venv/bin/python -m evaluation.assets
```

CI runs the same offline checks on Python 3.12 after installing the hashed lockfile. No API key or live inference is used in CI.

The preparation package includes:

- Original 40 extraction/clinic-info cases: 20 Urdu, 10 English, 10 mixed.
- Thirty next-action scenarios, each in English and Urdu: supplied synthetic history/state, expected action and arguments; no tool is executed.
- Twelve speech-text samples for native-speaker review: respectful Urdu, language persistence, unknown answers, and exact code-supplied `say` delivery.
- Thirty-five resolver golden cases, all number expectations from 0–100, and 15 structured normalizer cases. These are requirements for the later resolver/normalizer, not implementations.
- A proposed versioned bilingual tool-result JSON Schema and positive/negative examples.
- An unrecorded manifest with 200 Urdu + 50 mixed prompts across ten speaker slots. A prompt is not a recording or a ground-truth transcript.

## Hosted Nemotron evaluation

Use synthetic fixtures only. Obtain a key using [NVIDIA's API Catalog guide](https://docs.api.nvidia.com/nim/docs/api-quickstart).

```bash
# Original single-turn baseline; --limit 1 is a smoke test.
.venv/bin/python -m evaluation --model nvidia/nemotron-3.5-bolt-e4b --limit 1

# Full expanded suite: sixty state-conditioned action probes and twelve prose samples.
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b

# Language samples only, or a selected language for a short manual rerun.
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b --suite language
.venv/bin/python -m evaluation.flows --model nvidia/nemotron-3.5-bolt-e4b --scenario correct-date
```

Nemotron requests explicitly send thinking off, parallel calls off, and `skip_special_tokens=false`, with temperature 1.0/top_p 0.95 from the model-specific guidance. Expanded action probes use `tool_choice=required` across the full tool set; they never force the expected tool name. Language probes have no tools. Original baseline runs used `auto`. Every report records its actual request options; results from different configurations must not be treated as a controlled A/B experiment.

Each request is separated by two seconds by default. No hidden retry/failover or saturation test is performed. `--interval` and `--timeout` control pacing/socket timeout; the 30-second experimental ceiling is not the production three-second failover policy. Runs stop at the first provider failure and retain partial reports. A scoring failure does not stop remaining samples. Exit codes: 0 all requested mechanical checks pass; 1 a scoring/provider failure; 2 invalid configuration.

JSON reports are private files under ignored `results/`. First content/tool deltas are client-observed timing, not tokenizer-level TTFT or end-to-end audio latency. Language checks are heuristics, with **human review pending even on a mechanical pass**. Correct tool selection does not prove an implemented state machine, database transaction, caller verification, or successful booking.

## Review the outputs

```bash
.venv/bin/python -m evaluation.review results/phase0-nemotron-final-72.json --suite language
```

The generated local HTML escapes all model text, renders mixed scripts, and keeps human acceptance separate from automated scores. Open it from the file link in [CPU results](docs/phase-0-cpu-results.md). A fresh live run gets a timestamped report name; pass that path instead. The review command does not overwrite an existing reviewed artifact.

## Planned production boundaries

FastAPI/Python 3.12 owns auth, clinic APIs, policy, verification and persistence. A separate worker process owns Pipecat/LiveKit sessions. PostgreSQL 16/pgvector is durable storage; Valkey holds temporary state. Production scheduling safety belongs in code and database constraints, never prompts. A production LLM adapter follows the spec's OpenAI-compatible SDK path; this small evaluation transport does not replace it.

Backend owns shared schemas under `packages/schemas`; publish versioned artifacts for the separate frontend rather than maintaining handwritten duplicate contracts. Default clinic timezone is `Asia/Karachi`, currency PKR, with Pakistan phone normalization. [Engineering rules](AGENTS.md), [branch workflow](CONTRIBUTING.md), [license ledger](docs/licenses.md), and [comparison status](docs/model-comparison-status.md) apply.
