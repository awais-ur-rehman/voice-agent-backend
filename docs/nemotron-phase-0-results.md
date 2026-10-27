# Nemotron Phase 0 hosted evaluation — October 27, 2026

## Scope and configuration

Owner requested `nvidia/nemotron-3.5-bolt-e4b` with thinking disabled. Ran on the local macOS development machine using Python 3.12.13 and NVIDIA's hosted HTTPS chat-completions endpoint. Network geography was not independently verified. No local GPU or downloaded model weights were used.

- Explicit `chat_template_kwargs: {enable_thinking: false}` and `parallel_tool_calls: false`.
- Model-card sampling: temperature 1.0, top_p 0.95; max_tokens 300. This differs from the spec's generic temperature 0.3 baseline and is recorded for reproducibility.
- Sequential requests with a two-second gap, 30-second socket timeout, no automatic retries/fallback.
- One Urdu smoke request passed first, then the unchanged 40-case suite (20 Urdu, 10 English, 10 mixed).
- Fixture SHA-256: `4e4f00546e43382b42ea07de796c779581eb3265f04eb7001b934caec9df9dc6`.
- Local raw report: `results/nemotron-thinking-off-40.json` (ignored; file permissions 0600).
- Raw-report SHA-256: `118a43be51718b82ddbae7b0db2d5f39aa11b65a86d2fb81dc260b430952c7ca`.
- Provider/model configuration was captured in the raw report. The hosted alias is not an immutable weight revision.

## Results

All 40 HTTP/stream requests completed. All 40 outputs had valid tool argument schemas and selected the expected tool. Strict exact-argument scoring passed 38/40 (95%). This is not a booking success rate: cases only test raw date extraction and clinic-info tool selection.

| Input language | Strict passes | First tool output p50 (ms) | p95 (ms) |
| --- | --- | --- | --- |
| ur | 19/20 | 1091.9 | 2637.9 |
| en | 10/10 | 1114.4 | 1527.7 |
| mixed | 9/10 | 1114.6 | 1187.6 |
| Overall | 38/40 | 1110.2 | 1527.7 |

Total streamed response time: p50 1111.6 ms, p95 1528.1 ms. Slowest first tool output was 4580.4 ms. Percentiles use nearest rank; the Urdu p95 is larger than the overall p95 because group sample counts and ranks differ.

All responses were tool-only, so prose first-token timing and spoken Urdu quality are unassessed. These are client-observed first tool deltas, not tokenizer-level TTFT or end-to-end audio latency. The observed LLM stage already consumes much of the spec's complete voice budget; no low-latency voice feasibility claim follows from this run.

The request explicitly disabled thinking. Separately returned reasoning characters, inline thinking tags, and reported reasoning-token counts were all zero across 40 requests. This establishes observed output/usage behavior, not inspection of the provider's internals.

40/40 requests reported cached prompt tokens. This sequential repeated-prompt run includes cache effects; it is not a cold-start or concurrency benchmark. No provider errors or rate-limit responses occurred at this pacing; the account RPM cap remains unknown.

## The two exact-match failures

1. `ur-09`: input `جمعرات کو آنا ہے۔`; expected date phrase `جمعرات`, returned `جمعرات کو`. The weekday is unchanged, but the model included the postposition. Preserve the failed exact score; resolver acceptance of equivalent date phrases belongs in its own tests.
2. `mixed-03`: input `Appointment اگلے منگل کو چاہیے۔`; expected `اگلے منگل`, returned `अगले منگل को`. This is a real script-preservation failure: Devanagari appeared inside tool arguments. It requires a script guard and cross-model comparison, not a silent fixture change.

The default prose script heuristic does not assess tool-only responses. Exact argument matching caught the corrupted phrase here; a future runtime must validate script where appropriate before date resolution. Do not broadly reject Devanagari from all patient text without specifying the language policy.

## Reproduce and checkpoint

With a locally configured ignored `.env`:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluation --dry-run
python3 -m evaluation --model nvidia/nemotron-3.5-bolt-e4b
```

The live runner correctly exited 1 for two scoring failures; it did not hide them as transport successes. Local regression suite: 18 tests pass. A separate reasoning-returned scoring regression was added while the long-running process was active; offline re-scoring of every saved response with that check produced exactly the same 38/40 result. The raw report was retained unchanged.

This is one model's initial hosted text/tool evaluation. Three more model comparisons, human Urdu speech/prose review, the recorded STT datasets, six Urdu TTS voice evaluations, full browser audio/barge-in measurements, and GPU gates remain pending. The owner explicitly deferred GPU voice latency checks. Do not begin Phase 1 or promote this candidate to the production default from this report alone.
