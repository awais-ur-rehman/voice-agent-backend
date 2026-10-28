# Phase 3 owner testing checkpoint

This is the safe-code/transport checkpoint. **Voice start should currently report unavailable:** English ASR exceeded its tested memory budget, Urdu failed the safe headroom preflight, and both readiness capabilities remain false. GPU latency and all real speech-booking acceptance remain pending. Do not replace the specified models or enable readiness by editing evidence files.

## Start the prepared workspace

From `backend/`, preserve both saved credential files and all existing volumes:

```sh
uv sync --locked --all-groups
python3 scripts/dev_setup.py --enable-voice
uv run python -m scripts.setup_livekit
docker compose --env-file .env.api --env-file .env --profile voice up --build -d api livekit
uv run python -m scripts.smoke_phase3
```

Setup appends missing private media keys to `.env.api`, keeps existing database/staff/runtime credentials and leaves `SCHEDULER_VOICE_ENABLED=false`. The NIM/HF file remains `.env`; only the API gets the NIM key, and only explicit asset download reads HF_TOKEN. Never print resolved Compose config or either env file. LiveKit is loopback-only at 7880/7881 TCP and 7882 UDP. Existing PostgreSQL data is retained under the original `voice-agent-phase1` project.

From the separate `frontend/` repository:

```sh
npm ci
npm run dev
```

Open **http://localhost:5173** (the API CORS origin). API docs remain at http://127.0.0.1:8000/docs. Use the existing synthetic demo owner account `demo-owner-a@example.com`; its generated password is in private `backend/.local/phase1-demo-credentials.json`. Do not paste passwords or API keys into chat. No real patient data is needed.

## Test now

1. Sign in; verify only your tenant's published agent is offered. Sign out and try the second synthetic clinic; its catalog must be isolated.
2. Select an agent and start. With the current false readiness, expect an explicit unavailable pipeline error, no microphone/connected/speaking claim, and no new appointment.
3. Test invalid credentials, keyboard navigation, narrow/mobile layout, reduced-motion settings and mixed English/Urdu labels. Captions and credentials must disappear on sign-out/refresh rather than enter browser storage.
4. Staff credentials must not authorize `/voice/worker/ready`, `/claim`, or runtime mutation. See automated tests for media-only grants, fully acknowledged current readbacks and early-yes rejection.
5. Run the real transport-only smoke below. It sends synthetic PCM, proves decoded RTC audio and cleans its own room; it does not perform speech recognition or book an appointment.

```sh
UV_CACHE_DIR=.local/uv-cache uv sync --project voice --locked --all-groups
UV_CACHE_DIR=.local/uv-cache uv run --project voice --locked python -m voice_runtime.rtc_smoke
```

Initial ICE startup timed out until the new local LiveKit service was restarted; two post-restart PCM passes were recorded. If the smoke fails, preserve its sanitized result under `.local/voice/`, check the localhost media ports, and report the outcome. Do not count HTTP room creation as RTC success. The root cause of the initial startup failure remains unconfirmed.

## Automated checks

```sh
uv run pytest
uv run python tests/run_offline.py
uv run ruff check .
uv run ruff format --check .
uv run mypy scheduler brain
uv run python -m scripts.export_voice_contracts --check
UV_CACHE_DIR=.local/uv-cache uv run --project voice --locked pytest voice/tests
```

PostgreSQL/Valkey tests use only the dedicated test stores; run destructive database suites serially. Native process tests need ordinary macOS child-process inspection permission and fail closed in a sandbox that blocks it. The isolated test stores use `docker compose --profile test up -d postgres-test valkey-test`; do not delete any development volume.

Frontend: `npm run check` and `npm run test:browser`. Install Playwright Chromium with `npx playwright install chromium`, or use the documented `PLAYWRIGHT_CHROMIUM_EXECUTABLE` override for an already installed shell. Browser fixtures prove interface behavior only; [frontend evidence](https://github.com/awais-ur-rehman/voice-agent-frontend/blob/feat/phase-3-voice-console/docs/phase-3-testing.md) records screenshots and exact limitations.

## Resume real speech acceptance on suitable hardware

Read [dependency/resource evidence](phase-3-dependencies.md) before another model load. The separately locked inference environment uses `uv sync --project voice/inference --locked`; pinned models and hashes live under ignored `.local/`. Explicit CLI download/gate commands are in that document. The worker reads bootstrap authorization only from `.env.api`, never receives the API JWT signing key and cannot mark a language ready merely because weights exist.

On a host with sufficient measured RAM, rerun real speech loopbacks under recorded safe budgets and review the generated evidence. Only after genuine successful language gates should `SCHEDULER_VOICE_ENABLED` be enabled and the native worker started. Then test microphone/autoplay denial, actual audible English/Urdu output, full readback followed by a fresh yes, early yes/interruption, expired two-minute hold, family change, correction/digression, reschedule/cancel, reconnect within/after 30 seconds and cleanup. Assert actual database outcomes and exact command receipt replay. Keep synthetic recordings private; native quality/MOS and GPU SLOs remain distinct gates.

Stop here. Phase 4 is not started; Phase 3 branches/PRs remain unmerged for owner testing.
