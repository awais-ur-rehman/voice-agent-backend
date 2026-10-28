# Phase 4 agent management checkpoint

Manager actions (owner/admin) are under `/dashboard/agents`. Staff can list safe
agent summaries and `/dashboard/voices`; draft/version contents and mutations
require a manager. The server enforces tenant scope with PostgreSQL forced RLS.

Create with `{ "name": "Clinic receptionist" }`. The saved draft contains the
complete `schema_version: 2` configuration and a `revision`. PUT `/agents/{id}/draft`
with `{revision,config}`; a conflicting save returns 409 without overwriting.
Names change in the draft until publication. PATCH `/agents/{id}` uses
`{revision,enabled}`. DELETE archives and retains historical references.

Publish uses POST `/agents/{id}/publish`, `{revision}`, and a fresh
`Idempotency-Key` (8–100 letters/digits/underscores/hyphens). Reuse the same key
and body for a transport retry. Rollback uses POST `/agents/{id}/rollback`,
`{revision,version_id}`, and the same idempotency protocol. Each successful
publication/rollback creates a new immutable version with a monotonically
assigned publication number, publisher/time and optional rollback source. A new
user intent needs a new key. GET `/agents/{id}/versions` returns newest first.
The clinic's structured profile is snapshotted on publish, without truncation;
rollback copies the selected historical snapshot. Live scheduling policy stays
authoritative for scheduling transactions.

`packages/schemas/agents.v1.json` freezes agent config v2 plus dashboard agent
request/response schemas. Regenerate/check with:

```
uv run --locked python -m scripts.export_agent_contracts
uv run --locked python -m scripts.export_agent_contracts --check
```

Legacy config has no `schema_version` (or version 1). Its reader preserves the
old frozen voice defaults and old readiness policy. A rollback of a legacy
version preserves its original config; it does not quietly select v2 voices.
To publish v2 afterward, explicitly save schema 2 and both voice IDs. New v2
bilingual configurations require matching configured gender. The catalog has
three candidates per gender per language. It is metadata only: all profiles
are provisional and all preview controls must be disabled, using the returned
reason. No browser-generated substitute preview is provided.

New voice sessions require the exact selected profile fingerprints advertised
by the trusted native worker, in addition to the language gates. Native workers
qualify profile evidence only from
`.local/voice/profile-<config_fingerprint>-loopback.json` with `status=passed`
and a fingerprint exactly matching `gate_fingerprint(language, profile)`.
That fingerprint includes ASR/model/description-tokenizer revisions, frozen
profile config and dependency-lock digests. Asset manifests and selected tensor
presence are checked separately. Existing legacy loopback files cannot qualify
a v2 profile. No such v2 evidence was generated in this checkpoint; current ASR
and Urdu acceptance remain blocked. Most English candidate tensors are not in
the installed allowlist/manifest and require a separately authorized asset
adoption before they can be evaluated.

The native package consumes only the shared JSON manifest and wire-projected
snapshots. It does not import brain, scheduler or API dependencies. Selected
English tensors/Urdu descriptions reach the inference child; public cache keys
include selected profile configuration/fingerprint, retaining legacy keys for
legacy calls. Arbitrary provider JSON is rejected. Missing selected tensors
fail before spawning inference. Dynamic greetings never enter the public clip
cache; the code-owned AI disclosure prefixes editable greetings. Tone changes
the general invitation while critical confirmations, emergency and identity
questions retain their bilingual code templates.

Disabling/archiving and new admission lock the same agent row. Committed
changes block new sessions. Already admitted sessions retain their original
version and may finish subject to normal session/deadline/revocation guards.
There is no immediate-stop action in this checkpoint.
