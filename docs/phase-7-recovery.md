# Private local recovery

This operator tool backs up the current local development database and original knowledge files, then verifies or restores that bundle into a new disposable target. It has no HTTP endpoint and never accepts a restore DSN, existing container, or persistent target volume. Actual owner-source backup and Docker restore acceptance belong to the separately reviewed local drill; synthetic tests alone do not establish recovery acceptance.

Run from the backend checkout with its installed Python 3.12 development environment. No dependency installation, model asset, provider, image build or download is needed. The operator uses installed `psycopg` and host development `psutil`; API startup does not import the tool or `psutil`. Docker must already have this exact image:

```text
pgvector/pgvector:0.8.7-pg16-bookworm@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a
```

## Inputs and explicit authorization

Every operation requires `--allow-local-operations`. Without it the CLI returns exit 2 before reading credentials, bundle paths or starting processes. An authorized failure returns exit 1 with a fixed non-sensitive message; success returns exit 0. It never loads `.env` or edits environment/configuration files.

Provision these values in the operator process through the existing private credential handling procedure; do not put secrets in arguments, shell history, reports or screenshots:

- `RECOVERY_SOURCE_DSN`, for backup only: `postgresql` URL for user `postgres`, database `voice_agent`, literal loopback address `127.0.0.1` or `::1`, port **55432**, existing password, no query or fragment. This is a source-only credential. The explicit host address overrides ambient `PGHOSTADDR`.
- `RECOVERY_BLOB_SECRET`, all operations: the retained existing knowledge HMAC secret, at least 32 characters. It must match original receipts. This key authenticates plaintext; it does not encrypt anything.
- `RECOVERY_SOURCE_POSTGRES_ID` and `RECOVERY_SOURCE_API_ID`, examples below: full 64-character IDs of the existing Compose `voice-agent-phase1` services `postgres` and `api`, obtained by the operator. The tool checks their actual returned IDs, running state, Compose labels, the pinned database image, loopback 55432 mapping, and named volume mounts `voice-agent-phase1_postgres-dev-data` at `/var/lib/postgresql/data` and `voice-agent-phase1_knowledge-private-data` at `/var/lib/knowledge` before loading source credentials or executing source capture.
- `RECOVERY_EXPECTED_MANIFEST_SHA256`: the `manifest_sha256` value taken from the separately retained **trusted creation receipt**, supplied to verify/drill. Do not compute this value from the bundle being verified. A modified SQL file plus a recomputed embedded hash is not an authenticated backup.

Docker must use a local Unix socket/context. Remote TCP contexts are refused. The tool never pulls an image. It projects inspect fields without fetching container environment values. The existing API must provide `/app/.venv/bin/python`; the read-only stdlib helper is streamed into that interpreter so authentication sees the original Linux device/inode identities. A host copy of the volume cannot substitute for this view.

Create an operator-owned parent directory with mode `0700`, outside version control; examples use `.local/recovery`. Its parent permissions are not silently changed. Bundle and receipt paths must be new; the creation receipt must be outside the bundle directory. All created files use `0600`, directories `0700`.

```sh
.venv/bin/python -m scripts.recovery backup --allow-local-operations \
  --source-postgres "$RECOVERY_SOURCE_POSTGRES_ID" \
  --source-api "$RECOVERY_SOURCE_API_ID" \
  --bundle .local/recovery/current-bundle \
  --creation-receipt .local/recovery/current-creation.json

.venv/bin/python -m scripts.recovery verify --allow-local-operations \
  --bundle .local/recovery/current-bundle \
  --expected-manifest-sha256 "$RECOVERY_EXPECTED_MANIFEST_SHA256"

.venv/bin/python -m scripts.recovery drill --allow-local-operations \
  --bundle .local/recovery/current-bundle \
  --expected-manifest-sha256 "$RECOVERY_EXPECTED_MANIFEST_SHA256" \
  --scratch-parent .local/recovery \
  --drill-receipt .local/recovery/current-drill.json
```

Use a new set of names for each attempt. No success receipt is written after a failed backup, failed verification, failed restore, or failed target cleanup. An interrupted or failed backup directory is retained for private diagnosis and is not an accepted bundle. Do not publish its contents. A creation receipt records the trusted manifest digest, capture/snapshot timestamps and observed capture duration; the drill receipt records that digest, measured restore duration and verified blob count after target cleanup.

## Contents and consistency

The version 1 directory bundle has exactly five files: `manifest.json`, custom-format `database.dump`, unchanged `roles.sql`, private `state.json`, and plain `blobs.tar`. The manifest bounds and hashes the four content files. Directory archives are not accepted as a CLI input. The internal tar has only bounded regular members with fixed canonical names; symlinks, hardlinks, traversal, duplicates, PAX/GNU extension records, unexpected types, unsafe modes, truncation and trailing content are rejected before extraction. Blob archive names are hashes of the original keys; the authenticated receipts preserve the SQL keys.

Capture acquires existing source upload/blob locks read-only and nonblocking. Inflight uploads/retirements, unknown temporary files, unauthenticated receipts, unsafe links/modes or changing metadata cause refusal. It never creates a source lock or modifies blob bytes, receipts or retirement metadata. Ordinary reads may update filesystem access times; access time is deliberately not a mutation clock.

The database session is read-only with explicit timeouts. Its PostgreSQL system identifier must match the guarded source container. A repeatable-read exported snapshot is shared by table fingerprints and `pg_dump --snapshot --create`. All public tables are included in row counts and deterministic SHA-256 fingerprints. Private metadata also records the migration head, database/schema/table/function ownership and ACLs, policies, constraints, triggers, indexes, extensions, sequence values and active SQL blob references. Role fingerprints include existing password hashes and flags without putting them in the public output. The unchanged private roles SQL necessarily contains role password hashes.

Roles and nontransactional sequence state are compared before/after capture. Authenticated original blob bytes, receipt hashes, device/inode and mutation clocks are compared before/after the database capture; the second pass returns only metadata. Active SQL references must match captured key, tenant, source, revision and content hash and cannot point at retired files. All original files, including retired but still retained originals, are preserved. Deleted source rows may legitimately refer to already-collected files, so only active references require a file.

This is a consistent SQL snapshot plus conservative cross-resource stability checks, **not an atomic database/filesystem transaction**. A source change, inflight lock, configured bound, incompatible ownership or timeout invalidates the attempt. The original receipt is authenticated before any target rebinding. Only copies in a newly created private directory receive new device/inode values and HMAC signatures; key and timestamps remain unchanged. The unchanged live `BlobStore.receipt` verifies the actual restored copies.

The bundle does not contain JWT/runtime/HMAC keys, provider credentials, application configuration, TLS material, model assets, external services or off-site copies. Retain those existing keys separately through their current private procedure. Database passwords are represented by hashes in roles SQL; plaintext login credentials remain external. No source roles/passwords are reset.

## Isolated target and bounded work

The tool creates a random `voice-recovery-<nonce>` container and records its returned ID in a private CID file. It validates the ID, unique label/name, pinned image ID, network, mounts and constraints before restore and again during verification. A failed/lost create reply can be cleaned up only through that new CID file plus matching ID/name/nonce label. A preexisting name is never a cleanup target.

The target has network `none`, no published ports or host mounts, a read-only root filesystem, user `999:999`, all capabilities dropped, `no-new-privileges`, private IPC/PID namespaces, 128 PIDs, **512 MiB memory and swap combined**, and **one CPU**. Its only mounts are tmpfs: 256 MiB PGDATA, 8 MiB socket directory and 16 MiB `/tmp`, all owned by UID/GID 999 with `0700` modes and `noexec,nosuid,nodev`. There is no persistent/anonymous data volume. Readiness checks the final server rather than the temporary initialization server.

A unique `recovery_<nonce>` bootstrap superuser/database allows the unchanged roles SQL to create the original `postgres` role. Roles and dump bytes are streamed into container `psql`/`pg_restore` stdin; the private host bundle is never mounted. Restore uses `--create --exit-on-error --use-set-session-authorization`, preserving original ownership. There is no `--clean`, existing-target restore, source drop/reset, migration stamp or credential rewrite. Even a hash-valid malicious SQL/psql program runs only in the constrained disposable container. This assumes the local Docker daemon/kernel isolation boundary is trusted.

Verification compares the restored private state with the snapshot, checks that `scheduler_app` cannot bypass RLS, and exercises no-tenant and absent-zero-UUID scopes under that restricted role. Bootstrap-only role identity, initdb system-role grantor identities, and initdb's `plpgsql` owner are deliberately excluded from source-owner comparison; application/public ownership, role flags, user memberships, ACLs and extension versions remain compared. A source using the all-zero tenant UUID may fail the conservative absent-tenant probe and needs a reviewed scope adjustment rather than bypassing it.

The limits are deliberately suitable for this small local drill:

| Boundary | Maximum |
| --- | ---: |
| Manifest / private state / roles SQL | 1 MiB each |
| Source database / custom dump | 64 MiB each |
| Source blobs | 128 files; 10 MiB each; 64 MiB total |
| Internal plain tar | 66 MiB; 257 regular members |
| Original receipt | 4096 bytes |
| Public tables / sequences | 128 each |
| Rows | 100,000/table; 200,000 total |
| Roles / memberships scanned | 128 / 256 |
| Per-query returned source metadata | 1 MiB |
| Source connection / statement / lock waits | 5 s / 30 s / 2 s |
| Dump / original-volume helper | 65 s command; helper alarm 60 s |
| Target startup / restore command | 30 s polling / 120 s |
| Other commands / whole runner / cleanup | 60 s / 600 s / 15 s per cleanup command |

The host must have **at least 1 GiB available before bulk verification/capture**, then pass a fresh check before target creation. Docker's reported memory budget minus current reported container use must also leave at least **1 GiB**. The budget is 512 MiB target + at most approximately 256 MiB bounded operator working data/interpreter + 256 MiB reserve; the fresh target check occurs after verified buffers are allocated. Source capture helpers add bounded short-lived memory within the API container during backup (there is no restore target then). These are admission estimates rather than guarantees against concurrent host workload; do not kill unrelated/owner services to force admission. The separate embedding memory gate is unchanged.

Command output goes to anonymous private files with a child file-size limit; stderr is discarded, argv is never a shell, and wall/total deadlines kill the client process group. Source SQL/helper deadlines bound remote exec work; restore failures remove only the newly created target. Cleanup uses reserved bounded commands even when the work deadline expired. If Docker is unavailable during cleanup, acceptance fails and the uniquely labeled disposable target may remain for operator diagnosis; never run broad prune/removal commands. The scratch blob root is removed on both success and failure.

## Validation and remaining qualification

Synthetic tests use temporary originals and fake source/target commands. The separate `RECOVERY_SQL_TEST=1` test reads only the already initialized disposable `voice_agent_test` database under read-only transactions; it accepts the existing synthetic `TEST_DATABASE_URL` at loopback 55433 locally or 5432 in CI, never a workspace dotenv. Coordinate the test DB slot and include this flag in the final serial backend/CI gate. It does not seed, migrate, reset or run Docker.

```sh
.venv/bin/python -m pytest tests_environment/test_recovery.py -q
RECOVERY_SQL_TEST=1 .venv/bin/python -m pytest tests_environment/test_recovery.py::test_recovery_queries_disposable_postgres -q
.venv/bin/ruff check scripts/recovery.py scripts/recovery_bundle.py scripts/recovery_database.py tests_environment/test_recovery.py
.venv/bin/ruff format --check scripts/recovery.py scripts/recovery_bundle.py scripts/recovery_database.py tests_environment/test_recovery.py
.venv/bin/mypy --explicit-package-bases scripts/recovery.py scripts/recovery_bundle.py scripts/recovery_database.py
```

Actual source-volume capture, Docker initialization/inspect compatibility, unchanged roles SQL/custom-dump restoration and observed RPO/RTO must be measured in the reviewed operator drill. Keep failed observations. Report snapshot age and elapsed timings with the exact source/target limits; do not claim an RPO/RTO SLA, encrypted/off-site backup, production failover, TLS or availability from this local evidence.
