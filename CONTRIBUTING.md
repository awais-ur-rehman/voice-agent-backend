# Contribution workflow

- `main` is the release branch. `develop` integrates the current phase.
- Branch from `develop`: `feat/phase-0-<topic>`, `feat/phase-1-<topic>`, `feat/phase-4-<topic>`, `fix/<topic>`, or `docs/<topic>`.
- Keep commits focused, with plain descriptions (`feat:`, `fix:`, `docs:`, `chore:`).
- Open pull requests into `develop`. Describe the behavior, validation, limitations, and owner testing steps.
- Complete the current phase and owner checkpoint before starting the next phase. Promote `develop` to `main` through a release pull request after owner acceptance; tag releases with semantic versions once runnable releases exist.
- Urgent release fixes branch from `main` as `hotfix/<topic>` and merge back into both long-lived branches.
- Never force-push or delete `main` or `develop`. Require pull requests and resolve review conversations. Add required CI checks when executable checks exist; do not configure nonexistent checks.
- Commit dependency locks, migrations, and public schemas with the changes they support.
- Never commit `.env`, API keys, patient information, raw recordings, or unredacted evaluation output. Use synthetic public fixtures.

Phase 0 checks: install the hash-locked requirements in a Python 3.12 virtual environment, then run `.venv/bin/python tests/run_offline.py`, both evaluation dry runs, and `.venv/bin/python -m evaluation.assets` as documented in README. CI runs the same offline checks on Python 3.12. Once production code is introduced, expand CI with the relevant lint/type/build and behavior checks before integration.

Offline regression tests must not load the workspace `.env` or inherit real credentials. Mock environment loading for application-CLI tests, use isolated fake environment values, and use temporary files for loader tests. Live evaluation commands are explicit owner-authorized work and are excluded from unit tests/CI.

Phase 4: run Ruff lint/format, mypy, all public schema export checks and `uv run --locked python -m pytest` against the dedicated disposable test database. Run only one database suite at a time. CI also builds the runtime Docker image and imports its shared voice catalog without network access. Follow [Phase 4 testing](docs/phase-4-testing.md) for offline/native control and live evidence boundaries.
