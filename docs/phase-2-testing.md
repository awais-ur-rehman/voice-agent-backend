# Phase 2 text agent owner checkpoint

Phase 2 adds the guarded text runtime. Voice, microphones, WebRTC, STT/TTS, external messaging, outbound calls and the staff dashboard remain outside this checkpoint. Leave the Phase 2 branch unmerged until the owner accepts its evidence and native wording.

## Prepare the local services

Use Python 3.12 and the checked-in lock from the backend directory:

```sh
uv sync --frozen
uv run python scripts/dev_setup.py --enable-brain
docker compose --env-file .env.api --profile test up -d --wait postgres-test valkey-test
docker compose --env-file .env.api up --build -d api
uv run python -m brain.bootstrap --demo
```

Setup appends only missing brain secrets to private `.env.api`. It preserves existing API/database passwords and JWT secrets. The Compose project remains `voice-agent-phase1`; its PostgreSQL volume stays intact. Development Valkey listens on localhost port 56380; the isolated test service uses port 56379. Both require authentication. Live state is intentionally ephemeral: restart recovery uses the live SQL guard and structured recovery checkpoint. No raw transcript recovery is promised.

The demo command preserves the existing `.local/phase1-demo-credentials.json` accounts and passwords, adds a deterministic synthetic agent per clinic and stores each `agent_id` alongside its clinic. Its stdout contains tenant, agent and version identifiers, never passwords. The credentials file must remain a private regular file with mode `0600`. Read it locally when you need the synthetic owner email/password; do not paste it into reports or commit it. Demo Urdu pronunciations are explicit synthetic data and require native owner review.

## Run the deterministic checks

```sh
uv run python tests/run_offline.py
uv run pytest
uv run ruff check .
uv run mypy
```

Database tests use the isolated test database and synthetic data. Coordinate simultaneous test runs because the existing Phase 1 fixtures reset that database. Valkey tests clean up only their random key namespace. No live provider calls belong in CI or offline tests.

A passing scripted test proves its executed assertions only. It does not prove hosted model inference, native Urdu quality, pronunciation, or physical audio interruption behavior. Record actual test counts and failures from the current run; this guide intentionally supplies no claimed pass count.

## Start an interactive text session

Retrieve an agent identifier from demo stdout or the private demo file. Pass identifiers and email as arguments; enter the staff password at the hidden prompt:

```sh
uv run python -m brain.console --email demo-owner-a@example.com --agent-id AGENT_UUID --live
```

The default service setup above loads `.env.api` only. To explicitly enable the existing NIM credential file for hosted synthetic testing, run:

```sh
docker compose --env-file .env.api --env-file .env up --build -d api
```

Do not recreate or overwrite `.env`. Compose maps `LLM_API_KEY` to `SCHEDULER_LLM_API_KEY` only in the API runtime environment; it does not inject the key into images, build arguments or the database services. Never print environment values or resolved Compose configuration. Deterministic tests continue without this credential file.

`--live` explicitly permits hosted model requests. The server must have a valid server-side planner configuration and provider access. Missing credentials or a provider outage must yield a truthful fallback; never count fallback text as inference. The console starts a session using staff authentication, then uses the scoped runtime capability. Tokens stay in memory. `/renew` rotates a still-live capability. `/quit` closes the console; use the end-call workflow to end/revoke a session and release holds before quitting.

The console prints the code-generated response in the state's selected language, followed on completion by successful HTTP command count and server-reported provider request count. These are separate measures. It never displays capabilities or raw state/history. Current sessions expire after thirty minutes of inactivity; renewal cannot revive an expired session. The console does not persist credentials or reconnect tokens to disk.

## Execute a synthetic script

Save a list of synthetic caller turns to an untracked JSON file, for example:

```json
[
  {"text": "I would like to book an appointment."},
  {"text": "Tomorrow, please."}
]
```

Run it through the same hosted planner and guarded HTTP pipeline:

```sh
uv run python -m brain.console --email demo-owner-a@example.com --agent-id AGENT_UUID --live --script .local/synthetic-turns.json
```

For deterministic operator-directed execution, omit `--live` and supply a typed `action` in each turn:

```json
[
  {"text": "What clinic information is available?", "action": {"name": "get_clinic_info", "arguments": {}}}
]
```

A booking flow that needs a caller callback/contact number must invoke `set_caller_contact` with its `phone` argument separately from `find_patient`. Caller contact and the patient receiving care are different fields; do not assume a patient lookup supplies the caller's contact, particularly for family bookings. Subject changes require fresh subject-specific authorization.

Use relative requests such as “tomorrow” in an interactive run. The API resolves them against its actual clock and clinic timezone. Fixed test clocks/dates in integration fixtures are synthetic test inputs and must not be copied into a current booking request. Follow the returned offered options and questions; a fixed two-turn script does not guarantee a completed appointment.

Use current tool contract names and arguments from `brain/contracts.py`. A rejected command is a failed step, not permission to inject confirmed/verified fields. Runtime caller identity, verification, tenant ownership and confirmation revisions remain server-owned. Scripts do not imply final database assertions: use the executable integration/simulation tests for that evidence.

The HTTP flow is `POST /auth/login`, `POST /agent-sessions` with `agent_id`, then `POST /runtime/sessions/{id}/turn` for live text or `/commands` for typed operator commands. Each command carries a unique `command_id` and current `expected_revision`. Session inspection is `GET /runtime/sessions/{id}`; token rotation is `POST /runtime/sessions/{id}/renew`. Do not put bearer tokens in command-line arguments or shell history.

## Trusted events and staff resume

The operator/runtime event endpoint is `POST /runtime/sessions/{id}/events`, authenticated with the original scoped capability. Each request carries `command_id`, current `expected_revision`, and `kind`: `silence`, `delivery_interrupted`, or `upstream_timeout`. `delivered_text` is optional and must contain only actually delivered text for an interruption. A caller/model action cannot manufacture these events. Use the test harness or an authorized operator to exercise them; they are not ordinary text messages or console slash commands.

An authorized staff bearer can call `POST /agent-sessions/{id}/resume` with no request body to rotate the capability for a still-live session in its own tenant. The runtime bearer uses `/runtime/sessions/{id}/renew` instead. Ended, revoked or expired sessions cannot be revived. A guessed phone number never authorizes resume. The current console creates a fresh session on startup and does not persist tokens; staff resume is an API acceptance check.

## Publish an agent version

Write a reviewed JSON configuration with only the bounded fields below. Publishing creates a new immutable version and selects it for future sessions; existing sessions retain their original version:

```json
{
  "name": "Synthetic Receptionist",
  "language_mode": "bilingual",
  "instructions": "Ask one question at a time.",
  "read_digits_in_english": false
}
```

```sh
uv run python -m brain.bootstrap --publish --tenant-id TENANT_UUID --agent-id AGENT_UUID --config .local/agent-config.json
```

The CLI uses the dedicated API configuration and application database role. The supplied agent must belong to the tenant. Instructions are bounded to 2000 characters, name to 100 characters, language to `en`, `ur`, or `bilingual`; unknown/security fields are rejected. This is local administrative provisioning, not a public management API.

## Purge expired recovery payloads

Run bounded maintenance separately for each tenant:

```sh
uv run python -m brain.bootstrap --purge-recovery --tenant-id TENANT_UUID --limit 500
```

The application role and tenant RLS scope the operation. The command clears expired receipt payloads and checkpoints after their retention boundary; it retains session/audit metadata. It prints only cleared row counts. It does not delete patient records or flush Valkey.

## Owner acceptance evidence

Hosted NIM lifecycle execution and owner acceptance are pending. Record completed results only after checking the actual requests and resulting database effects. Current deterministic totals are preliminary and must be refreshed after the final changes.

Before accepting the checkpoint, record the tested revision, current full-suite/lint/type results, rebuilt service results, and the actual hosted request totals/outcomes. Exercise English and Urdu booking, rescheduling, cancellation, correction/digression and family/task switching against synthetic records, checking the resulting database appointments. Separately verify shared-phone ambiguity, exact-subject verification, three failed attempts, stale confirmation, interrupted read-back, overlapping slot holds, expiry/reconnect, cache-loss replay, duplicate command IDs and tenant/session authorization failures.

Keep hosted outcomes and failures separate from deterministic/fake-planner results. A completed phase requires the remaining simulator scenario families and real hosted paths to be executed and reviewed; an available CLI alone is preparation. Native Urdu wording and pronunciation need owner review. Actual playback-stop timing and audio quality wait for Phase 3. Do not merge this phase or begin voice integration without owner direction.

## Additional checkpoint commands and configuration

The rebuilt local API can be checked without any hosted requests:

```sh
uv run python -m scripts.smoke_phase2
```

This uses the existing synthetic demo clinics, verifies the committed booking through replay/reschedule/cancel, and ends its runtime session. It leaves synthetic audit/history records and cancels its own created appointments.

The hosted acceptance suite uses only the disposable test database and synthetic records. Explicitly opt in when you intend to use the saved NIM key:

```sh
RUN_NIM_PHASE2=1 uv run pytest tests_brain/test_live_nim.py -q
```

To publish a new immutable runtime version, save a bounded JSON configuration privately and run `uv run python -m brain.bootstrap --publish --tenant-id TENANT_UUID --agent-id AGENT_UUID --config .local/agent-config.json`. Existing sessions keep their original version. Optional configuration includes `prayer_break: ["13:00", "14:30"]` and `clinic_profile` with `address_en`, `address_ur`, `preparation_en` and `preparation_ur`. Missing bilingual information returns `no_answer`. Reviewed `name_ur` fields can be supplied through the staff provider/service/patient APIs; renaming clears an old pronunciation unless a replacement is supplied.

See [the measured results](phase-2-results.md) for exact current verification evidence and its limits.

For a new patient, provide the phone, then the name. If an English name has no approved Urdu spelling, the runtime asks for that explicit Urdu spelling; it never invents a transliteration. The separate name/phone readback must be confirmed before creating the patient. These identity turns stay in code and are excluded from planner requests. Correcting a phone requires fresh verification.
