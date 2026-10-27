# Phase 0 model comparison status — October 27, 2026

The owner supplied saved keys for MiniMax M3.1 Flash, Muse Glimmer 30B, and DiffusionGemma 26B A4B IT. The active assignment is now spelled `LLM_API_KEY`. The MiniMax key remains active, the other keys remain commented, and local `LLM_MODEL` is set to `minimaxai/minimax-m3.1-flash`. No key or raw report is tracked.

## Checks performed

1. Added documented request profiles and regression coverage before testing. All candidates use the same fixed fixture suite, 300 completion-token budget, and 30-second socket timeout. Sampling and reasoning controls differ as documented below; these are not identical decoding configurations.
2. Sent one tool-calling smoke request to each candidate, using its own locally labeled key. Each stopped without a scorable response. The original report's generic transport/stream error alone did not establish the cause.
3. A MiniMax diagnostic of the same tool request timed out at 30.349 seconds before receiving HTTP response headers or stream events.
4. Checked the active MiniMax key against `GET /v1/models`: HTTP 200, all three exact candidate IDs present. Catalog listing is not proof of per-model inference entitlement or availability.
5. Used that active key for a Nemotron control request: the Urdu fixture passed, first tool output 1351.4 ms. This establishes active-key authentication and the known-working inference route; it does not verify either commented key independently.
6. Sent a basic `Reply with OK.` request without tool definitions to each new candidate, using its own labeled key, with a 32-token cap. Each timed out before HTTP headers/events. These are diagnostic requests, not benchmark cases. No automatic retries or rate-limit saturation runs were performed.

The three initial smoke requests and the three basic diagnostics were launched in parallel within their respective small groups. They cannot be compared with the earlier sequential 40-case latency distribution. Basic diagnostic elapsed times are recorded below solely to establish that no response arrived before the configured timeout.

| Candidate | Basic diagnostic elapsed | Result |
| --- | --- | --- |
| `minimaxai/minimax-m3.1-flash` | 30.374 s | Timeout; no HTTP response headers/events |
| `meta/muse-glimmer-30b` | 30.343 s | Timeout; no HTTP response headers/events |
| `google/diffusiongemma-26b-a4b-it` | 30.361 s | Timeout; no HTTP response headers/events |

These are unresolved endpoint/access/availability failures observed from this machine, not evidence that the models lack tool calling or Urdu capability. No 40-case batches were sent after the failed smoke gates. There are no comparable quality scores or successful latency percentiles for these candidates.

Local evidence is ignored under `results/`: `<candidate>-smoke.json`, `<candidate>-plain-diagnostic.json`, and `nemotron-new-key-control.json`. Diagnostics contain only status/count/timing metadata, not keys or generated text. The original reports are retained unchanged. Transport errors now distinguish timeouts, invalid/incomplete streams, and connection failures without exposing provider bodies or exception details.

## Documented controls and limitations

- **MiniMax:** [NIM API reference](https://docs.api.nvidia.com/nim/reference/minimaxai-minimax-m3-1-flash-infer) recommends temperature 1.0 / top_p 0.95. No thinking-off control was verified for this hosted endpoint, so the profile remains prompt-only and is not labeled thinking-disabled.
- **Muse Glimmer:** [NIM API reference](https://docs.api.nvidia.com/nim/reference/meta-muse-glimmer-30b-infer) documents `reasoning_effort=none` and recommends temperature 0.95 / top_p 1.0. The broader model card gives different sampling guidance; this profile uses endpoint-specific guidance. Actual behavior remains unobserved.
- **DiffusionGemma:** [NIM API reference](https://docs.api.nvidia.com/nim/reference/diffusiongemma-26b-a4b-it-infer) documents `enable_thinking=false`. The profile uses the published example's temperature 1.0 / top_p 0.95. Although the model card describes function calling, the hosted API parameter list does not document `tools`; hosted tool compatibility is unresolved. Even the request without tools timed out.

## Next checkpoint

The owner can try `Reply with OK.` in each NVIDIA model playground and report whether a response appears. If the playground works, compare its generated API example (with the key removed) against the documented endpoint profiles. If it also fails, retry once access/service availability changes or select alternative candidates; do not invent scores for untested models.

Nemotron remains the only candidate with a completed 40-case evaluation: 38 exact matches, with the documented script failure and latency limitations. No production winner is selected. Full Phase 0 and Phase 1 remain gated as previously recorded. Local regression suite: 20 tests pass; offline fixtures remain 20 Urdu, 10 English, and 10 mixed cases.

## Owner decision after playground check

The owner reported that none of the three additional models responded in the NVIDIA playground and instructed us to use Nemotron instead. The extra candidates are deferred; do not keep retrying them without a new access/availability signal or owner request. This report does not independently establish the cause of the playground failures.

The active local development baseline is `nvidia/nemotron-3.5-bolt-e4b`, with `chat_template_kwargs.enable_thinking=false` and parallel tool calls disabled. Credential assignments were preserved. The owner-selected baseline does not erase the earlier 38/40 strict-match result, Devanagari corruption, or latency concerns. The four-model comparison is explicitly deferred, not completed or passed; full voice feasibility remains pending.
