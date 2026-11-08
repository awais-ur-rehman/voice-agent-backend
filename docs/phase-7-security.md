# Phase 7 HTTP and admission boundary

The API applies these protections to the existing staff, runtime and private routes. No new public endpoint or remote drain control is added. Existing tenant, staff/runtime, consent, worker, idempotency and retention checks remain authoritative.

## Distributed admission

Migration **0018** adds `admission_buckets` and the narrow `admission_admit(text,text,integer,integer)` function. The function returns `admitted` and integer `retry_seconds`. It accepts only these policies:

| Scope | Limit | Window |
| --- | ---: | ---: |
| `login` | 10 per transport IP | 60 seconds |
| `refresh` | 30 per transport IP | 60 seconds |
| `start_user` | 30 per authenticated tenant/user | 600 seconds |
| `start_ip` | 120 per transport IP | 600 seconds |

Fixed windows begin at the first attempt, use PostgreSQL time, and count failed credential attempts. Counters saturate at limit+1. A rejected request does not extend the window. Each call removes at most 100 expired rows with `FOR UPDATE SKIP LOCKED`; expiry is lazy, so an idle database can retain expired opaque rows until another admission. Upserts serialize each bucket atomically across API processes. No optional Valkey dependency, in-memory fallback, patient rows or caller-controlled SQL identifiers are involved.

The table contains only fixed scope names, 64-character HMAC-SHA256 keys, bounded counters and expiry timestamps. Keys use the JWT secret with the purpose prefix `voice-agent:admission:v1` and scope separation; IPs, email, token, request keys and user IDs are not stored in plaintext. The function uses `SECURITY DEFINER` and fixed `search_path=pg_catalog, public`, schema-qualifies its table, rejects unsupported policy parameters, revokes PUBLIC execution, grants only execution to `scheduler_app`, and does not grant that role table access. Migrations 0001–0017 remain unchanged.

`admit_auth(request, scope)` runs before credential lookup/verification. `admit_start(request, user)` runs after authentication; text starts call it before session creation, voice starts use `create_voice(..., admit_new=callback)` after the locked exact-replay/conflict check, and draft starts call it inside `provision` after the locked replay check. Draft voice callbacks capture the authenticated **parent** user, never the synthetic child user. Exact voice/draft replay is uncharged even while draining. User and IP checks run in order as independent attempts; a user attempt can remain charged if the IP bucket or later business validation rejects. This is not an idempotency receipt or successful-session counter.

Admission commits in a separate short transaction, including denied attempts, so rollback of a login or clinical transaction cannot reset quota. The app has a separate admission pool (2 connections, no overflow, 1-second checkout timeout) to avoid business-pool starvation while the caller holds an authoritative replay lock. Admission connections use a 2-second connect timeout, 1-second SQL statement timeout, and 500ms lock timeout. Overload/database errors fail closed; there is no cross-pool business-row access. `app.state.admission_factory` is the deterministic internal test-injection seam.

Registration keeps the existing feature flag (disabled by default), independent transaction and **5 attempts/hour/IP** policy in migration 0004. New bucket writes now use the separately scoped HMAC `registration` key and rejection returns conservative `Retry-After: 3600`. Old rows are retained. Deploying this change rotates the legacy SHA256 bucket identity once, so an active old window can effectively reset; JWT secret rotation similarly rotates HMAC bucket identities. No production quota continuity guarantee or migration of legacy counters is claimed.

## HTTP response and body contract

Every application HTTP response, including preflight, unmatched paths and errors, receives `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and a server-generated `X-Request-ID`. Explicit configured CORS origins are retained; `Retry-After` and `X-Request-ID` are exposed. CORS is not authentication. The outer perimeter applies the same explicit-origin exposure to errors rejected before routing.

| Error code | HTTP status | Retry-After |
| --- | ---: | --- |
| `rate_limited` (login/refresh/start) | 429 | remaining window, integer 1–3600 |
| `admission_unavailable` | 503 | 1 |
| `service_draining` | 503 | 3 |
| `request_too_large` | 413 | absent |
| `invalid_content_length` | 400 | absent |
| `query_too_large` | 414 | absent |
| `headers_too_large` | 431 | absent |
| `internal_error` | 500 | absent |

`AppError(..., retry_after=N)` accepts only an integer from 1 through 3600; the centralized handler emits the header. Existing auth, validation, ownership and conflict codes remain unchanged. Frontends should read the header for waits rather than infer duration from message text.

Ordinary request bodies are capped at **65,536 bytes before parsing or endpoint work**, including streamed bodies and declared-length lies. Duplicate, negative, nondecimal or conflicting Content-Length/Transfer-Encoding framing is rejected. The cap applies regardless of Content-Type. Only the exact UUID-based knowledge upload/replacement routes retain their existing streaming **10MiB** cap and upload deadline; the perimeter does not buffer those files. Query strings are capped at **8,192 raw ASGI bytes**; aggregate header names+values at **32,768 raw ASGI bytes**. The HTTP server may reject framing or headers earlier; these limits do not promise server acceptance at every boundary.

Validation errors expose schema/route-declared structural field paths and bounded numeric indices. Unknown extra object keys are truncated to a safe parent path, and values are never returned. Request logs contain only generated request ID, a fixed method or `OTHER`, matched route template or `unmatched`, status and duration. Unexpected exceptions return a generic envelope; exception text, type, traceback, bodies, query values, credentials and signed URLs are not logged by this boundary. This does not claim arbitrary transcript text is de-identified.

Both API Docker entrypoints explicitly use `--no-proxy-headers` and `--no-access-log`. Raw `Forwarded`, `X-Forwarded-For` and `Origin` do not determine identity. A future trusted ingress deployment must deliberately configure and validate trusted proxy addresses at the ASGI server and its network boundary; enabling arbitrary proxy trust defeats transport-IP quotas. Local HTTP does not claim TLS, HSTS or encryption at rest.

## Drain and shutdown

`SCHEDULER_DRAIN_MODE=false` by default. When true, readiness and new text/voice/draft/voice-attachment admission return 503 with `Retry-After: 3`. Exact replay, existing inspect/resume/end, auth, heartbeat, metrics and cleanup remain available subject to their existing authorization/readiness constraints. CPU voice readiness and capacity remain unchanged. This is restart-time configuration, not a remotely writable flag or a claim of multi-node routing readiness.

`SCHEDULER_SHUTDOWN_DRAIN_SECONDS=3` is finite and bounded from 0 through 10. Lifespan preserves existing cleanup, closes the provider to permanently fence new attempts, then awaits its bounded drain in a worker thread before disposing admission and business engines. Zero explicitly means no wait for provider completion. Incomplete or failed drain emits only a fixed generic notice. Successful drain means provider callbacks completed; it does **not** guarantee durable usage persistence during database failure. Timeout can leave late provider work/callbacks completing after engine disposal; no durable-observation promise is made.

Startup requires the application role to be non-superuser, non-bypass-RLS and a non-owner of public tables, the current database and the public schema (including implicit `pg_database_owner`). Catalog checks are read-only; deployments still require properly provisioned restricted credentials.

## Verification limits

Synthetic HTTP/raw-ASGI and real restricted PostgreSQL/Valkey tests cover atomic quotas, expiry, grants, rejected login persistence, replay/drain semantics, parent identity, cross-tenant/runtime separation, CORS, streamed limits, existing binary bounds, generic diagnostics and asynchronous shutdown. Test settings explicitly avoid owner dotenv files. Provider/media calls are synthetic; no native voice capacity, external provider SLA, TLS deployment, durable outage recovery or actual rolling rollout is certified here. Final broad image/deployment qualification belongs to the Phase 7 final gate.
