# Bounded provider routing

`Nemotron.plan(state, text, config, facts)`, `ground(query, language, chunks,
*, deadline)` and `close()` remain the planner interface. `drain(timeout: float)
-> bool` waits at most the supplied 0–60 seconds for outstanding work to finish;
false means work still occupies bounded capacity. `close()` fences admission and
returns without waiting for HTTP, telemetry or client teardown. Call close then
drain for an orderly bounded shutdown; an unresponsive dependency may outlive it
in a daemon worker. Neither operation cancels underlying I/O or frees its slot early.

The existing application creates one facade per process. Its primary and optional
secondary share the total request budget and capacity limit (default four attempts,
validated range 1–20). Each endpoint/key identity shares one token bucket across
models, including equivalent host case/default-port URL spellings. Defaults are
10 requests/minute and burst one, an operator budget, not a measured NVIDIA quota.
Settings do not coordinate quotas across application instances or workers.

Each attempt is limited to three wall seconds; the configurable timeout may shorten
that limit. Each logical operation is bounded by six seconds. Grounding also uses
its earlier absolute monotonic deadline. A new worker rechecks deadline, close,
budget, bucket and circuit after tracing callbacks and before dispatch. Work is
admitted without a queue. A timed-out attempt retains capacity until HTTP, decode,
trace and persistence actually exit. At most one secondary follows primary 429,
5xx, transport failure or timeout. An absent, locally throttled or circuit-open
primary can also yield to a configured secondary without inventing a primary
invocation. Auth, malformed/ineligible action and other policy failures do not
retry. Redirects are not followed. The existing strict decoder, state-specific
schemas, disabled thinking and code-owned tool decisions remain authoritative.

Circuit state is shared by endpoint/key: five retryable failures within a rolling
30 seconds open it for 60 seconds, with one probe at the half-open boundary. Normal
success does not erase failures within that rolling window. A successful or
non-retryable half-open response clears the circuit. Outcomes carry circuit
generations so an older success cannot close a newer open circuit. A caller timeout
is terminal even when the underlying response later succeeds. The monotonic clock
is injectable through the facade for deterministic qualification.

Primary base URL defaults to NVIDIA's `/v1` endpoint; the model defaults to
`nvidia/nemotron-3.5-bolt-e4b`. A secondary requires its own explicit base URL, model
and key; no credential is inherited. URLs allow HTTPS or literal loopback HTTP,
and reject userinfo, query, fragment and whitespace. The `.env.api.example` lists
server-only settings and optional separate USD-per-million input/output rates.
The timeout setting defaults to three and accepts values greater than zero and
at most three seconds. An explicit legacy value such as 30 must be deliberately
changed to 3 (or less) by the operator before rollout; it is rejected rather than
silently accepted. No existing owner environment file is read or edited by
tests. Live secondary quality, target-network latency and actual provider quotas
remain unqualified.

## Usage and availability

At actual dispatch, the worker reserves a UUID and request count in memory. A
normal result and its completion-stage sample are published to the request scope
before signaling the caller, without any SQL wait. The same bounded worker then
persists the start and terminal observations through the independent usage writer.
Healthy persistence retains actual usage across domain rollback and late responses;
late observations retain their original invocation UUID and never reopen a closed
measurement scope or change the returned result. `drain` makes persistence completion
observable for orderly shutdown and deterministic checks, but does not promise that
a failed writer stored anything.

Migration `0017` adds route (`primary` by legacy default, or `secondary`) and nullable
`fallback_from` UUID, and permits the bounded provider labels `nim` and
`openai_compatible`. Secondary attempts after actual primary HTTP point to the
primary UUID. Secondary-only admission has null `fallback_from`. The provenance
link deliberately has no foreign key: an outage can lose the primary observation
while the secondary persists. Start/completion upserts preserve invocation ownership
and immutable provenance. No URL, key, prompt or provider error body is persisted.
Existing RLS remains in force; public dashboard/voice/agent schemas are unchanged.

This is bounded best-effort observation, not a durable billing queue. A stalled
writer occupies one of the same bounded slots and can reduce further provider
admission. An outage may leave no row or only a started row, with unknown usage and
cost. There is no retry queue or extra telemetry thread. Reported tokens and
configured rates are required for a known cost estimate; missing evidence never
becomes zero. No real provider calls, model assets or audio-fallback qualification
are included in the local tests.
