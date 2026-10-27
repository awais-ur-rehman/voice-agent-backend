# Phase 0 CPU evaluation design

The owner selected CPU-compatible tooling plus hosted NIM on October 27, 2026. This is an experimental feasibility slice, not production scheduling code. GPU voice latency gates remain pending. The owner will obtain a NIM key.

## Approaches

The selected approach starts hosted text/tool evaluation without local model weights. Renting a GPU would enable the full voice benchmark sooner but introduces a cost the owner has not authorized. CPU-only speech experiments cannot substantiate the GPU voice latency targets.

## Deliverable

A Python 3.12 command-line runner uses NVIDIA's documented streaming chat-completions HTTP endpoint. Python's standard library is sufficient for this small spike and introduces no runtime package licenses or lock resolution. It is not the future production LLM adapter, which the spec assigns to the OpenAI SDK.

Keep fixtures, transport, scoring, and CLI separate. Forty synthetic turns contain 20 Urdu, 10 English, and 10 mixed inputs. Each requests either `update_dialogue_state` with a raw date phrase or `get_clinic_info` with a structured field. They test single-turn tool selection/extraction, not appointment execution, patient verification, or multi-turn simulator completeness.

Merge streamed tool fragments by index. Record first meaningful content/tool-delta latency separately from first content-token latency; metadata-only deltas do not count. Reject incomplete streams and malformed payloads. Score exact tool and argument matches with schema validity tracked separately. Urdu-script checks are heuristics; native-speaker quality remains unscored.

Run one explicit model at a time, with a user-controlled request interval and no hidden retry/failover that would contaminate timing. Expose timeout and 429 failures. Never claim observed throughput is the account's RPM allowance. No automated saturation test.

The `.env` file is ignored and loaded without executing shell code. Existing process environment takes precedence. Credentials are never printed. Disallow credential-bearing URLs and insecure remote HTTP. Permit HTTP only for loopback test/vLLM endpoints. Disable redirects to avoid forwarding credentials to another origin.

Write local JSON reports with UTC time, model, endpoint, fixture identity, Python/platform metadata, per-case status and summary p50/p95 using nearest-rank percentiles. Save raw synthetic responses for human review, label the report synthetic-only, and ignore `results/`. No patient recordings or real patient inputs are accepted by the built-in suite.

## Verification and checkpoint

Tests cover fragmented calls, misleading metadata, incomplete streams, schema/argument failures, script checks, missing credentials, secret-safe errors, endpoint restrictions, and report percentiles. An offline dry run validates fixture composition without credentials or HTTP. CI runs tests on Python 3.12. Live model access, reasoning-mode controls, human Urdu quality, rate behavior, audio quality, and end-to-end timing remain unverified until exercised.

The frontend remains documentation-only in this slice: the browser voice console depends on a working audio transport, not a text evaluation runner. Stop before Phase 1.
