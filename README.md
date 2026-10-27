# Voice Agent Backend

Backend and voice-runtime workspace for an English/Urdu appointment scheduler for Pakistani clinics.

## Status

Phase 0 CPU evaluation tooling is available on `feat/phase-0-feasibility`. There is no production API or voice service yet. No live model, voice, throughput, or latency result has been validated. The owner selected CPU + hosted NIM evaluation; GPU voice latency checks remain pending.

The governing input is **Voice AI Appointment Scheduler — Spec v0.2**, dated October 27, 2026. It is an addendum to v0.1, now also supplied in the parent workspace; v0.2 takes precedence where they conflict.

## Planned boundaries

- FastAPI/Python 3.12 owns clinic APIs, scheduling policies, verification, and persistence.
- A separate worker process owns Pipecat/LiveKit audio sessions and provider adapters.
- PostgreSQL 16 with pgvector stores durable data; Valkey stores temporary state.
- NIM is the development/pilot LLM provider; an OpenAI-compatible adapter must permit vLLM migration.
- Shared JSON Schema contracts live in `packages/schemas` here once implemented. Publish versioned contract artifacts for the separate frontend; do not maintain two handwritten contracts.

English and Urdu launch together, including mixed speech. Default clinic timezone is `Asia/Karachi`, currency is PKR, and phone normalization uses Pakistan numbering rules. The LLM requests operations through tools; code enforces transitions, verification, policy, and booking consistency.

See [Phase 0](docs/phase-0.md), [engineering rules](AGENTS.md), and [contribution workflow](CONTRIBUTING.md).

## Run the CPU evaluation

Requires Python 3.12; no third-party packages or GPU are needed for this experimental runner.

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluation --dry-run
```

Copy `.env.example` to `.env` and set `LLM_API_KEY` locally. Obtain the key through [NVIDIA's API Catalog guide](https://docs.api.nvidia.com/nim/docs/api-quickstart). Select an exact model identifier available to your account from the [NIM LLM reference](https://docs.api.nvidia.com/nim/reference/llm-apis), then smoke-test one synthetic case:

```bash
python3 -m evaluation --model YOUR_MODEL_ID --limit 1
```

For a complete single-model run, omit `--limit`. Repeat for four model candidates. The built-in suite has 40 single-turn cases: 20 Urdu, 10 English, and 10 mixed. It covers raw date-phrase extraction and structured clinic-info tool selection; it is not the multi-turn appointment simulator. Expected arguments are exact matches, so semantically equivalent transformations require human review rather than silently counting as correct.

Reports go to ignored `results/` with private file permissions. They contain synthetic model outputs for review. Exit codes: `0` all requested cases pass; `1` a provider or scoring failure; `2` invalid configuration. Runs stop at the first provider failure, retain the partial report, and do not retry automatically. Default spacing is two seconds between requests; use `--interval` to honor your account allowance. This is not a rate-limit saturation test.

`first_output_ms` is the first meaningful content/tool fragment, while `first_content_ms` is the first content delta and can be absent for tool-only responses. These measure client-observed deltas, not tokenizer-level TTFT. `first_usable_content_ms` is unassessed if inline thinking/analysis tags appear; such responses fail the reasoning-leak check. Raw content/output timing remains available for diagnosis. Percentiles use nearest rank over completed requests and expose sample counts; inspect failure counts alongside latency. The 30-second default socket timeout is an experimental measurement ceiling, not the production three-second fallback threshold.

Urdu-input prose checks detect Arabic-script text and reject Devanagari; mixed-input prose has no assumed output language. These heuristics cannot establish Urdu fluency, respectful register, or absence of Roman Urdu. Tool-only replies have no prose score. Native-speaker review, model-specific reasoning controls, audio tests, GPU performance, and real account rate limits remain pending. Prompt-only reasoning suppression is recorded in every report and is not sufficient for the live voice loop.
