# Phase 4 Task 2 implementation report

Status: DONE_WITH_CONCERNS (speech acceptance remains deliberately blocked; one native cancellation failure is recorded below).
Implementation base: fa56619. The controller's documentation-only 75934f2 arrived during implementation and was preserved. No future task implementation, frontend changes, pushes, merges, model downloads, inference, increased resource caps or private env reads were performed.

## Delivered behavior

- Manager-only agent create/draft/save/publish/history/rollback/enable/archive; safe staff summaries/catalog; PostgreSQL forced tenant RLS. Migration 0005 backfills draft rows and historical version numbers, adds publication/audit/archive metadata and tenant-qualified FKs. Existing immutable-version trigger remains enabled; migration-owned backfill temporarily disables/re-enables it within the migration transaction. App UPDATE/DELETE privileges on published versions remain revoked.
- Agent row then draft row locking, revision conflicts, transactional idempotent publication/rollback, immutable old snapshots, monotonic per-agent publication numbers and publisher/time/source metadata. Draft names do not change published identity until publish. CLI publishing uses the shared config and assigns publication numbers.
- Versioned config v2 (strict bounded fields), explicit legacy reader, clinic snapshot bounds matching Task 1 (2000 address / 4000 preparation), same-gender bilingual selections, language-required greetings and protected code-owned AI disclosure. Tone affects the general invitation in runtime and enters the untrusted persona layer; critical bilingual templates remain unchanged.
- New admission shares the agent-row lock with disable/archive. Already admitted sessions retain pinned versions and normal security/deadline/revocation guards; disable/archive no longer implicitly invalidates those sessions.
- Twelve frozen metadata candidates, three per gender/language, exact pinned Kokoro IDs and fixed Urdu descriptions/seed. All remain provisional with no previews. The browser gets typed safe catalog records without arbitrary provider credentials/config URLs.
- Server-selected snapshots reach native English tensor selection / Urdu description and generation settings, cache keys and exact-profile readiness fingerprints. Native package reads a pure shared manifest and wire-projected snapshots, importing no brain/scheduler/API dependencies. Missing selected tensors fail before inference starts. Legacy config/cache/gate defaults remain unchanged, including the historical English-female / Urdu-male mismatch.
- V2 bilingual admission requires both chosen profile fingerprints plus language readiness, never just global language flags. Exact-profile evidence includes ASR/model/tokenizer pins and dependency-lock hashes. Existing legacy loopback files cannot qualify new profiles. No exact-profile evidence was generated here.
- Rolling back a v1 version copies its original v1 JSON without synthesizing voice choices. Subsequent v2 publication requires an explicit save with schema 2 and both IDs; no silent upgrade/substitution.
- Public frozen schema/export command: packages/schemas/agents.v1.json and scripts/export_agent_contracts.py; versioned config, CRUD/draft/publication contracts and typed VoiceOut projection. Public operator documentation: docs/phase-4-agents.md and docs/licenses.md.

## TDD RED evidence

Commands used the controller-authorized API environment: `UV_CACHE_DIR=.local/uv-cache uv run --locked pytest ...`; native commands used `voice/.venv/bin/python -m pytest ...`. Database commands were escalated only for isolated loopback PostgreSQL on 55433; no two database test processes ran simultaneously.

1. Initial four-feature RED after fixing fixture setup: `tests_brain/test_agent_management.py -q` → 4 failed. Exact names:
   - test_shared_versioned_config_exists (missing module assertion)
   - test_profile_bounds_legacy_and_bilingual_gender (missing shared config)
   - test_frozen_catalog_honest_and_safe (missing catalog)
   - test_draft_publish_replay_rename_rollback_archive (404 route)
   Initial GREEN: 4 passed.
2. Runtime RED: test_disable_archive_existing_session_pinned failed because decode_runtime rejected disabled agents; test_disclosure_custom_greeting_tone_and_snapshot failed because tone was absent from persona. After valid synthetic fixture settings were supplied, these were genuine feature failures. GREEN: 7 passed.
3. Native RED: test_selected_profile_reaches_native_child_and_cache rejected the selected frozen profile at adapter boundary. GREEN after native propagation: 9 native-control tests passed.
4. Readiness RED: test_v2_readiness_requires_both_exact_profile_fingerprints failed accepting a qualified exact-profile mock because the readiness reader did not support config snapshots. GREEN: 9 agent tests passed. The separate negative legacy-flags test confirmed unavailable profile admission.
5. Upgrade RED: test_legacy_draft_requires_explicit_v2_voice_choices returned 200 instead of 422 for unupgraded v1 publish. GREEN: 12 agent tests passed.
6. CLI RED: test_cli_publication_has_monotonic_version_numbers found number=None. GREEN: 14 agent tests passed.
7. Missing-assets RED: test_missing_selected_tensor_never_starts_inference reached the fake supervisor despite an absent selected tensor. GREEN: full native suite 34 passed.
8. Public-schema RED: test_public_voice_response_has_frozen_typed_schema found no OpenAPI array items/VoiceOut response. GREEN: 17 agent tests passed.

Additional substantive tests cover actual unprivileged-role RLS and immutable UPDATE denial, concurrent saves (one 200/one 409), same-key concurrent publish replay (two identical 200s/one version), immutable old versions, rollback source/new number, draft rename separation, no truncation at full clinic bounds, legacy rollback identity, archive new admission, existing-session pinning, and PostgreSQL lock-timeout proof that admission waits behind a concurrently locked disable. Tone tests explicitly preserve critical confirmations/emergency/identity output.

## Verification and failures encountered

- Earlier full offline run: 605 passed, 4 hosted checks skipped, 1 failure: tests_api/test_database.py::test_migration_round_trip_and_forced_rls expected 18 RLS tables; migration 0005 correctly adds the 19th. Updated the regression expectation. This test exercises downgrade-to-base / fresh-head migration as well as forced RLS.
- Later full offline run: 613 passed, 4 skipped in 70.02 seconds, before the last typed catalog contract test was added.
- Final full offline run: `UV_CACHE_DIR=.local/uv-cache uv run --locked pytest -q` with isolated loopback escalation → 614 passed, 4 skipped in 69.32 seconds. Skips are the existing explicit-hosted tests: tests_brain/test_live_nim.py:24 (2) and :163 (2).
- Final targeted agent run: 17 passed in 3.54 seconds.
- Native complete suite: 34 passed, 1 existing audioop deprecation warning, in 1.26 seconds. No models loaded. One preceding `voice/.venv/bin/python -m pytest voice/tests -q` run with `sandbox_permissions=require_escalated` had 33 passed/1 failed: voice/tests/test_process.py::test_cancel_running_child_discards_its_late_result hit PermissionError from os.killpg in unchanged supervisor cancellation code during concurrent cancellation. The Popen returncode was already -9 when killpg raised PermissionError. Immediate full repeat of the same command with `sandbox_permissions=require_escalated` passed (34 passed, 1 warning). No supervisor/process-code change was made; the cause was not determined, and this does not establish resolution or speech acceptance.
- Earlier native complete run: 32 passed/1 failed because voice/tests/test_cache.py::test_public_cache_key_changes_with_the_frozen_voice_configuration depended on the legacy FROZEN_VOICES module projection. Restored the legacy projection and kept legacy cache-key contents unchanged; subsequent complete runs passed (33 before the missing-tensor test, 34 afterward).
- Static: ruff check passed; mypy passed (49 files); schema export `--check` passed; git diff --check passed. Initial mypy rejected a Literal[2] override of Literal[1]; replaced this with a shared field base and separate v1/v2 subclasses.
- Test/setup corrections explicitly recorded: initial sandbox PostgreSQL connection was denied and rerun with approved loopback escalation. Initial test owner/client imports were missing; corrected before observing feature RED. A test Settings setup initially lacked a runtime secret, then reused the JWT secret; corrected to distinct synthetic values before observing the intended disable failure. One attempted helper edit used unavailable `python`; reran with python3. Ruff initially reported compressed statement/style issues, fixed by formatting. Ruff's unused-import fix removed pytest fixture imports, causing fixture-not-found errors in five tests (test_draft_publish_replay_rename_rollback_archive, test_authorization_cross_tenant_and_immutable_versions, test_concurrent_editors_and_publish_retry, test_profile_publish_snapshot_and_legacy_rollback, test_legacy_draft_requires_explicit_v2_voice_choices); restored explicit self-alias imports and reran green. A final I001 import-order issue was fixed. No test failures were silently excluded.

## Files

New: brain/agent_config.py; brain/voice_catalog.py; scheduler/dashboard/agents.py; migrations/versions/0005_agent_drafts.py; tests_brain/test_agent_management.py; scripts/export_agent_contracts.py; packages/schemas/agents.v1.json; voice/catalog.json; voice/src/voice_runtime/profiles.py; docs/phase-4-agents.md.
Updated: brain/bootstrap.py, prompts.py, security.py, sessions.py, templates.py, tools.py, voice_routes.py, voice_worker_routes.py; scheduler/main.py, models.py; tests_api/test_database.py; voice/src/voice_runtime/cache.py, pipeline.py, speech/adapter.py, speech/child.py, worker.py; voice/tests/test_native_control.py; docs/licenses.md; this report.

## Self-review concerns / limits

- All voice previews/profile metadata are provisional. No speech acceptance is claimed; ASR and Urdu acceptance remain blocked. Most new English tensors are not installed. No new asset-download allowlist or external model adoption was attempted.
- Urdu gender/style labels describe frozen prompts and require native human review; they do not establish measured speaker properties. English IDs were verified in the pinned Hugging Face tensor directory; existing model-license entries remain authoritative. No commercial approval is asserted.
- Exact-profile readiness currently additionally requires the legacy language gate; this is deliberately conservative and cannot admit an unqualified profile. Any later adoption/evidence change must qualify the whole chosen runtime rather than inferring readiness from metadata.
- The unchanged native process-group cancellation failure above should remain visible in later verification. Both the failing run and the clean repeat were escalated; this was not a default-sandbox run. The clean repeated full native run is evidence of this task's passing control/config regressions, not proof the cancellation issue is resolved.
- No frontend behavior/browser acceptance or isolated draft-test implementation belongs to this task. The typed contracts are prepared for those subsequent tasks.

## Review round 1 fixes (base 8df0382)

Both Important findings in task-2-review.md were verified against the actual callers and fixed. Original Task 2 test evidence above was console-only: no historical raw logs were saved, and none have been reconstructed. This round saves the new raw output files listed below, including setup/invocation errors.

### Legacy CLI semantics

`brain.bootstrap.main` now parses the config file with `read_config`, and `publish_agent` accepts either typed version. A file without schema_version therefore publishes v1 and retains absent voice_profiles/voice IDs and the legacy readiness branch. The synthetic demo seed explicitly constructs LegacyAgentConfig. Dashboard-created agents still start at v2; no historical immutable versions were changed. New tests invoke the actual CLI main with old JSON and the real isolated database, then inspect the published row and readiness reader. A separate test invokes actual synthetic seeding and checks every created agent version remains v1. Settings/credentials in tests are explicit synthetic values; no private env files were read.

### Manifest-owned tensor boundary

`voice_runtime.assets.require_manifest_file` now requires an explicitly approved artifact path, a unique member of the valid pinned manifest, matching size through snapshot validation and matching selected-file SHA256. Both native synthesis and readiness qualification invoke this boundary; merely placing am_adam.pt alongside valid baseline af_heart assets cannot qualify or synthesize it. Even adding the candidate to a local manifest does not adopt an unapproved path. No allowlist was expanded and no assets were downloaded/adopted. V2 English gate fingerprints additionally carry the verified selected tensor path/SHA256 so a changed tensor cannot reuse old exact-profile evidence. Legacy fingerprint shape stays unchanged.

The no-inference regressions build an actual synthetic snapshot tree and a valid pinned baseline manifest, then place an unlisted candidate file beside it. RED proved synthesis reached the fake supervisor and readiness advertised that candidate. GREEN proves both paths reject it. Further checks cover approved af_heart success, manifest membership despite on-disk presence, same-size digest tampering, unapproved-but-listed candidate rejection, and gate fingerprint changes with validated tensor content. The existing wire propagation unit test explicitly mocks the approved-file boundary; asset-ownership tests use the real boundary.

### Captured commands and results

All log paths below are relative to `.superpowers/sdd/2026-10-28-phase-4-dashboard/`. API/database commands used `sandbox_permissions=require_escalated` for only the isolated PostgreSQL loopback. No two DB test processes ran together. Focused assets/control tests used the default sandbox (no subprocess inference). Final whole-native tests used require_escalated for existing process-control tests.

- `task-2-fix-cli-red.log`: `UV_CACHE_DIR=.local/uv-cache uv run --locked pytest tests_brain/test_agent_management.py -q` → 2 failed, 17 passed. Exact RED names: test_cli_legacy_json_preserves_voice_and_readiness_defaults; test_seed_default_publication_explicitly_remains_legacy. Both published schema 2 instead of expected schema 1.
- `task-2-fix-cli-green.log`: same command → 19 passed in 4.42 seconds.
- `task-2-fix-assets-red-setup.log`: initial `voice/.venv/bin/python -m pytest voice/tests/test_assets.py voice/tests/test_native_control.py -q` → 2 failed, 15 passed, existing audioop warning. The synthesis fixture initially lacked inference_python, causing AttributeError after bypassing the intended asset boundary; the readiness test already showed the real erroneous qualification. Preserved this setup-error output and completed the fake settings before the decisive RED run.
- `task-2-fix-assets-red.log`: same focused native command → 2 failed, 15 passed, existing audioop warning. Exact intended RED names: test_unlisted_candidate_tensor_never_starts_synthesis (fake supervisor assertion that unlisted candidate reached inference); test_unlisted_candidate_tensor_never_qualifies_readiness (candidate fingerprint returned instead of empty list).
- `task-2-fix-assets-green.log`: same command after the boundary fix → 17 passed, existing audioop warning, in 0.66 seconds.
- `task-2-fix-assets-final.log`: same command including additional positive/membership/digest/fingerprint coverage → 19 passed, existing audioop warning, in 0.66 seconds.
- `task-2-fix-native-final.log`: `voice/.venv/bin/python -m pytest voice/tests -q` → 38 passed, one unchanged audioop deprecation warning, in 1.17 seconds. No model inference was run. The earlier escalated killpg cancellation failure remains an unresolved observation; no process.py cancellation code was changed, and this pass is not a cancellation fix.
- `task-2-fix-static-invocation-error.log`: ruff and mypy passed, then a command typo `python -m scripts/export_agent_contracts` produced ModuleNotFoundError. The slash was corrected to the module's dotted name; no production change was needed.
- `task-2-fix-static-final.log`: ruff check passed, mypy passed (49 files), `UV_CACHE_DIR=.local/uv-cache uv run --locked python -m scripts.export_agent_contracts --check` passed, git diff --check passed. The final two successful commands produce no stdout; all commands ran under shell set -e.
- `task-2-fix-pytest-final.log`: `UV_CACHE_DIR=.local/uv-cache uv run --locked pytest -q` → 616 passed, 4 existing explicit-hosted skips, in 71.24 seconds.

Changed in this round: brain/bootstrap.py; tests_brain/test_agent_management.py; voice/src/voice_runtime/assets.py, speech/adapter.py, worker.py; voice/tests/test_assets.py, test_native_control.py; docs/phase-4-agents.md; this appended report and raw logs. No API schema, migration, catalog profile, dependency, model pin, allowlist, resource limit, inference engine or frontend changes were made.

Known acceptance limits are unchanged: all profile previews remain missing/provisional; ASR/Urdu acceptance stays blocked. Neither the audioop warning nor the earlier cancellation PermissionError is described as fixed.
