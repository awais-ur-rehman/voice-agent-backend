# Voice Agent Backend

Backend and voice-runtime workspace for an English/Urdu appointment scheduler for Pakistani clinics.

## Status

Repository setup only. Phase 0 feasibility work is pending; there is no runnable service yet. No model, voice, throughput, or latency result has been validated.

The governing input is **Voice AI Appointment Scheduler — Spec v0.2**, dated October 27, 2026. It is an addendum to v0.1, now also supplied in the parent workspace; v0.2 takes precedence where they conflict.

## Planned boundaries

- FastAPI/Python 3.12 owns clinic APIs, scheduling policies, verification, and persistence.
- A separate worker process owns Pipecat/LiveKit audio sessions and provider adapters.
- PostgreSQL 16 with pgvector stores durable data; Valkey stores temporary state.
- NIM is the development/pilot LLM provider; an OpenAI-compatible adapter must permit vLLM migration.
- Shared JSON Schema contracts live in `packages/schemas` here once implemented. Publish versioned contract artifacts for the separate frontend; do not maintain two handwritten contracts.

English and Urdu launch together, including mixed speech. Default clinic timezone is `Asia/Karachi`, currency is PKR, and phone normalization uses Pakistan numbering rules. The LLM requests operations through tools; code enforces transitions, verification, policy, and booking consistency.

See [Phase 0](docs/phase-0.md), [engineering rules](AGENTS.md), and [contribution workflow](CONTRIBUTING.md).
