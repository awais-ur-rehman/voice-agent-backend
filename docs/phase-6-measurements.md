# Phase 6 measurement contract

Measurements are actual observations with missing coverage preserved. They do not establish microphone, GPU, network or physical audio acceptance.

## Provider invocation ledger

`brain.routes.run_command` owns a request-local `MeasurementSink` outside the SQL command transaction. Each actual NIM HTTP attempt has a new server UUID. Started and completed usage writes use short independent tenant-scoped transactions, so malformed replies, provider failures, late grounding responses and subsequent domain conflicts do not erase actual attempts. Budget/circuit/deadline refusals before HTTP create no invocation. The existing global counter still bounds the provider's request budget; it is never differenced to attribute billing.

Grounding threads receive a copied context with frozen server session ownership. A completed request closes its observation sink: late work may complete only its original invocation ledger record, never append a successful latency to another turn or mutate an appointment. Successful/malformed fully buffered HTTP responses can supply `llm_completion`; the HTTP clock excludes output decoding and ledger writes. Failure/timeout attempts have no successful completion sample. The nonstreaming adapter supplies no `llm_first_token` observation.

Only actual nonnegative integer `usage.prompt_tokens` and `usage.completion_tokens` are retained; missing, boolean, negative, nonfinite or excessive counts remain null. No provider bodies, prompts, exception messages, headers or raw questions enter telemetry. Unavailable writes are best effort and may leave a started/unknown invocation, rather than fail a scheduling operation.

Optional `SCHEDULER_LLM_INPUT_RATE_USD_PER_MILLION` and `SCHEDULER_LLM_OUTPUT_RATE_USD_PER_MILLION` are finite nonnegative USD rates per million reported tokens. Both default to null. Each invocation snapshots the configured rates before HTTP; cost is a configured USD estimate, not a provider invoice. It requires both reported counts and both configured rates. No free-tier, cached-token discount or implicit zero is assumed. Values beyond ledger bounds remain unavailable. A different currency requires a new explicit contract, rather than mixing units in this ledger.

## Native observations

Private `POST /voice/worker/sessions/{identity}/metrics` requires the current worker token plus runtime capability. The server checks identity, lease, generation, mirror lifetime and revocation both before consuming and after reading the body. Both authorization transactions have 5 second SQL and 1 second lock limits. The body is bounded to 32768 bytes before JSON parsing and contains 1–32 observations. No browser tenant/session/turn/stage authority is accepted.

Payload: `{generation, revision, observations:[{event_id, command_id?, stage, language, duration_ms, observed_at, output_kind, metadata}]}`. Unknown fields are forbidden. IDs are UUIDs; revision must equal the current server revision. A response's command ID must resolve to an actual same-session receipt at that revision; the server derives its assistant event linkage. Input observations have no command ID, accepted caller turn, transcript or planner effect. Event IDs/payloads survive one ambiguous retry; exact duplicates write no new samples, while conflicts fail the whole batch. Older revisions, including interrupted work, remain missing.

`observed_at` must be timezone aware, within the captured call lifetime, no older than ten minutes and at most five seconds ahead. Durations must be finite 0–600000 milliseconds. Trusted metadata is a narrow typed allowlist: actual STT confidence/uncertainty, cache hit and generated/queued/interrupted state. Low-confidence/rejected ASR is observed before any caller-turn API request; it may set the safe quality flag but never create an accepted caller turn.

| Stage | Exact observed boundary |
| --- | --- |
| `stt_completion` | Owned ASR invocation through returned result, including thread/child overhead. |
| `tts_buffered_generation` | Actual uncached synthesis invocation through completed PCM buffer; generated/interrupted classification, never model first byte. |
| `audio_queue` | Actual paced enqueue loop for one completed segment, including its existing pacing waits; never publication/drain/hearing. |
| `end_of_speech_to_audio_queue` | Native monotonic last VAD-voiced frame arrival boundary to the first completed frame enqueue for that exact response context. |

`output_kind` distinguishes input, response, greeting, filler, resumed and other control output. Queue/buffer metadata retains cache/filler/greeting classification. Canonical `tts_first_byte`, `end_of_speech_to_audio` and `llm_first_token` remain absent. The pinned Pipecat queue API does not prove precise original-turn/frame association at actual LiveKit publication; no dependency patch or mutable-latest-turn inference was added. Existing trusted interruption commands record interrupted output separately without successful response latency.

The native queue keeps at most 32 pending observations and one sender task. Enqueue never waits on the network; a short best-effort flush runs before owned terminal/disconnect operations. Stale epochs/revisions, overflow, failures and late revocation can drop observations. Terminal cleanup authority does not admit late telemetry; goodbye output generated after core end can remain unavailable.

## Traces and privacy

Only `runtime.command`, `provider.invoke` and `native.metrics` spans exist, correlated with the existing server-owned call trace UUID. Attributes contain one fixed component category. Automatic exception recording/status descriptions are disabled; errors set a fixed status without exception text. The API uses its default no-op behavior: no SDK/exporter, automatic HTTP/SQL instrumentation, external telemetry request or native/browser trace authority was introduced. Optional downstream export needs a separate reviewed configuration and is not qualified here.
