# Phase 0 progress — October 27, 2026

- Repository setup and protected main/develop branches: complete.
- Owner selected CPU + hosted NIM; no GPU spending authorized.
- Experimental hosted text/tool runner: implemented with 40 synthetic cases; no third-party runtime dependencies.
- Verification: 20 unittest cases pass under Python 3.12; dry run validates 20 Urdu, 10 English, and 10 mixed cases; diff whitespace check passes.
- Independent review: three correctness findings addressed with failing-then-passing regression tests: malformed content/tool indices, inline reasoning leakage, and mixed-input response-language assumptions.
- Live Nemotron evaluation with thinking explicitly disabled: 40/40 requests completed, 40/40 valid schemas and expected tools, 38/40 exact arguments. First tool output p50 1110.2 ms, p95 1527.7 ms. No returned reasoning or reasoning tokens. See [results](nemotron-phase-0-results.md). No model winner or end-to-end voice latency claimed.
- Human Urdu quality, multi-turn simulator scenarios, resolver/normalizer tables, audio datasets, voice catalog, and full streaming audio/GPU feasibility: pending.
- Phase 1: not started. Owner testing checkpoint remains open.

Implementation decision: use standard-library HTTP for this isolated experimental runner. Production LLM adapter remains the spec's OpenAI SDK path; this spike does not replace that architecture.

Additional candidate checks: MiniMax, Muse Glimmer, and DiffusionGemma smoke requests produced no scorable result; basic requests also timed out at 30 seconds. The active key passed catalog access and a Nemotron control. Full comparison batches remain pending. See [comparison status](model-comparison-status.md).

Owner decision: all three additional candidates were also unresponsive in the owner's playground check. Use Nemotron as the development baseline with thinking disabled; further comparison attempts are deferred. Full voice feasibility remains pending and Phase 1 has not started.

Selection verification: a fresh request using the saved active `.env` passed the Urdu smoke case in 1194.1 ms to first tool output, with explicit thinking-off configuration and no returned reasoning text. All 20 local regression tests pass. This single request is not a new full benchmark.
