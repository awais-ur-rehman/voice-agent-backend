"""Agent draft and frozen profile acceptance contracts, synthetic fixtures only."""

import importlib.util

import pytest
from pydantic import ValidationError

from tests_api.conftest import client as client
from tests_api.conftest import owner as owner


def test_shared_versioned_config_exists():
    assert importlib.util.find_spec("brain.agent_config") is not None


def test_profile_bounds_legacy_and_bilingual_gender():
    from brain.agent_config import AgentConfig, read_config

    config = AgentConfig(clinic_profile={"address_en": "a" * 2000, "preparation_ur": "ب" * 4000})
    assert len(config.clinic_profile.preparation_ur) == 4000
    assert read_config({"language_mode": "bilingual"}).schema_version == 1
    with pytest.raises(ValidationError):
        AgentConfig(clinic_profile={"address_en": "a" * 2001})
    with pytest.raises(ValidationError):
        AgentConfig(voice_en_id="kokoro-af-heart-v1", voice_ur_id="parler-ur-male-v1")


def test_frozen_catalog_honest_and_safe():
    from brain.voice_catalog import catalog

    records = catalog()
    assert records
    for record in records:
        assert record["readiness"] == "provisional"
        assert record["preview_available"] is False
        assert record["preview_url"] is None
        assert len(record["config_fingerprint"]) == 64
        assert "config" not in record


def test_draft_publish_replay_rename_rollback_archive(owner, seed):
    created = owner.post("/dashboard/agents", json={"name": "First"})
    assert created.status_code == 201, created.text
    agent = created.json()
    path = "/dashboard/agents/" + agent["id"]
    draft = owner.get(path + "/draft").json()
    config = draft["config"] | {"name": "Published"}
    saved = owner.put(path + "/draft", json={"revision": draft["revision"], "config": config})
    assert saved.status_code == 200, saved.text
    assert (
        owner.put(
            path + "/draft", json={"revision": draft["revision"], "config": config}
        ).status_code
        == 409
    )
    body = {"revision": saved.json()["revision"]}
    headers = {"Idempotency-Key": "publish-one"}
    published = owner.post(path + "/publish", json=body, headers=headers)
    assert published.status_code == 200, published.text
    assert owner.post(path + "/publish", json=body, headers=headers).json() == published.json()
    current = owner.get(path + "/draft").json()
    renamed = owner.put(
        path + "/draft",
        json={"revision": current["revision"], "config": config | {"name": "Unpublished"}},
    )
    assert renamed.status_code == 200
    assert owner.get(path).json()["name"] == "Published"
    rollback = owner.post(
        path + "/rollback",
        json={"revision": renamed.json()["revision"], "version_id": published.json()["id"]},
        headers={"Idempotency-Key": "rollback-one"},
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["number"] == 2
    assert rollback.json()["rollback_source_id"] == published.json()["id"]
    versions = owner.get(path + "/versions").json()
    assert len(versions) == 2 and versions[1]["config"]["name"] == "Published"
    assert owner.delete(path).status_code == 200
    assert (
        owner.post(
            path + "/publish", json=body, headers={"Idempotency-Key": "publish-archived"}
        ).status_code
        == 404
    )


def test_disable_archive_existing_session_pinned(database, seed):
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from brain.security import decode_runtime
    from brain.sessions import create_session, renew_session
    from scheduler.config import Settings
    from scheduler.db import Principal
    from scheduler.models import Agent, AgentSession
    from tests_api.conftest import APP_URL, SECRET

    now = datetime(2026, 11, 1, tzinfo=UTC)
    settings = Settings(
        database_url=APP_URL, jwt_secret=SECRET, runtime_secret=SECRET + "-runtime", _env_file=None
    )
    staff = Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner")
    with Session(database) as db, db.begin():
        created = create_session(db, staff, seed["agent_a"], now, settings)
        actor = decode_runtime(db, created["token"], settings, now=now)
        agent = db.get(Agent, seed["agent_a"])
        agent.enabled = False
        agent.archived_at = now
        db.flush()
        assert decode_runtime(db, created["token"], settings, now=now) == actor
        assert renew_session(db, actor, now, settings)["session_id"] == created["session_id"]
        assert db.get(AgentSession, actor.id).version_id == seed["version_a"]
        with pytest.raises(Exception) as error:
            create_session(db, staff, seed["agent_a"], now, settings)
        assert error.value.status == 404


def test_disclosure_custom_greeting_tone_and_snapshot():
    from brain.agent_config import AgentConfig, greeting
    from brain.prompts import messages

    config = AgentConfig(greeting_en="Welcome to Test Clinic.", tone="formal").model_dump()
    assert greeting(config, "en") == "I am an AI scheduling assistant. Welcome to Test Clinic."
    assert "اے آئی" in greeting(config, "ur")
    text = " ".join(m["content"] for m in messages({"stage": "start"}, "hello", config, {}))
    assert "formal" in text
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(
            config | {"voice_profiles": {"en": {"config": {"name": "arbitrary"}}}}
        )


def test_authorization_cross_tenant_and_immutable_versions(owner, database, seed):
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import DBAPIError

    from tests_api.conftest import APP_URL, PASSWORD

    foreign = str(seed["agent_b"])
    assert owner.get("/dashboard/agents/" + foreign).status_code == 404
    assert owner.get("/dashboard/agents/" + foreign + "/draft").status_code == 404
    login = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()
    owner.headers["Authorization"] = "Bearer " + login["access_token"]
    assert owner.post("/dashboard/agents", json={"name": "No"}).status_code == 403
    assert owner.get("/dashboard/agents/" + str(seed["agent_a"]) + "/draft").status_code == 403
    engine = create_engine(APP_URL)
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text("SELECT set_config('app.tenant_id',:tenant,true)"),
                {"tenant": str(seed["tenant_a"])},
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM agent_drafts WHERE tenant_id=:tenant"),
                    {"tenant": seed["tenant_b"]},
                ).scalar()
                == 0
            )
            with pytest.raises(DBAPIError):
                conn.execute(
                    text("UPDATE agent_versions SET config='{}' WHERE id=:id"),
                    {"id": seed["version_a"]},
                )
    engine.dispose()


def test_v2_readiness_never_accepts_legacy_language_flags():
    import json
    from types import SimpleNamespace

    from brain.agent_config import AgentConfig
    from brain.voice_routes import _ready
    from scheduler.errors import AppError

    client = SimpleNamespace(
        ttl=lambda key: 10, get=lambda key: json.dumps({"en": True, "ur": True})
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(voice_enabled=True),
                brain_store=SimpleNamespace(client=client),
            )
        )
    )
    with pytest.raises(AppError) as error:
        _ready(request, AgentConfig().model_dump())
    assert error.value.code == "voice_unavailable"


def test_v2_readiness_requires_both_exact_profile_fingerprints():
    import json
    from types import SimpleNamespace

    from brain.agent_config import AgentConfig
    from brain.voice_routes import _ready
    from scheduler.errors import AppError

    config = AgentConfig().model_dump()
    fingerprints = [row["config_fingerprint"] for row in config["voice_profiles"].values()]
    ready = {"en": True, "ur": True, "profiles": fingerprints}
    client = SimpleNamespace(ttl=lambda key: 10, get=lambda key: json.dumps(ready))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(voice_enabled=True),
                brain_store=SimpleNamespace(client=client),
            )
        )
    )
    assert _ready(request, config) is request.app.state.brain_store
    ready["profiles"] = fingerprints[:1]
    with pytest.raises(AppError):
        _ready(request, config)
    ready.update(profiles=fingerprints, ur=False)
    with pytest.raises(AppError):
        _ready(request, config)


def test_concurrent_editors_and_publish_retry(owner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = (
        "/dashboard/agents/"
        + owner.post("/dashboard/agents", json={"name": "Concurrent"}).json()["id"]
    )
    draft = owner.get(path + "/draft").json()
    barrier = Barrier(2)

    def save(name):
        barrier.wait(timeout=3)
        return owner.put(
            path + "/draft",
            json={"revision": draft["revision"], "config": draft["config"] | {"name": name}},
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(save, ["One", "Two"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    current = owner.get(path + "/draft").json()
    barrier = Barrier(2)

    def publish(_):
        barrier.wait(timeout=3)
        return owner.post(
            path + "/publish",
            json={"revision": current["revision"]},
            headers={"Idempotency-Key": "concurrent-publish"},
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(publish, [0, 1]))
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert len(owner.get(path + "/versions").json()) == 1


def test_profile_publish_snapshot_and_legacy_rollback(owner, database, seed):
    from sqlalchemy.orm import Session

    from scheduler.models import Tenant

    with Session(database) as db, db.begin():
        tenant = db.get(Tenant, seed["tenant_a"])
        tenant.address_en = "a" * 2000
        tenant.preparation_ur = "ب" * 4000
    path = "/dashboard/agents/" + str(seed["agent_a"])
    draft = owner.get(path + "/draft").json()
    # Legacy migration/read adapter does not silently invent v2 voice choices.
    assert "schema_version" not in draft["config"]
    from brain.agent_config import AgentConfig

    config = AgentConfig(name="New").model_dump()
    saved = owner.put(
        path + "/draft", json={"revision": draft["revision"], "config": config}
    ).json()
    published = owner.post(
        path + "/publish",
        json={"revision": saved["revision"]},
        headers={"Idempotency-Key": "full-profile-publish"},
    )
    assert published.status_code == 200, published.text
    snapshot = published.json()["config"]["clinic_profile"]
    assert len(snapshot["address_en"]) == 2000 and len(snapshot["preparation_ur"]) == 4000
    current = owner.get(path + "/draft").json()
    old = owner.post(
        path + "/rollback",
        json={"revision": current["revision"], "version_id": str(seed["version_a"])},
        headers={"Idempotency-Key": "legacy-rollback"},
    )
    assert old.status_code == 200, old.text
    assert old.json()["config"] == {"language_mode": "bilingual"}
    assert "voice_profiles" not in old.json()["config"]


def test_legacy_draft_requires_explicit_v2_voice_choices(owner, seed):
    path = "/dashboard/agents/" + str(seed["agent_a"])
    draft = owner.get(path + "/draft").json()
    assert (
        owner.post(
            path + "/publish",
            json={"revision": draft["revision"]},
            headers={"Idempotency-Key": "legacy-must-upgrade"},
        ).status_code
        == 422
    )
    assert (
        owner.put(
            path + "/draft", json={"revision": draft["revision"], "config": draft["config"]}
        ).status_code
        == 422
    )


def test_cli_publication_has_monotonic_version_numbers(database, seed):
    from sqlalchemy.orm import Session

    from brain.agent_config import AgentConfig
    from brain.bootstrap import publish_agent
    from scheduler.models import AgentVersion

    with Session(database) as db, db.begin():
        identity = publish_agent(db, seed["tenant_a"], seed["agent_a"], AgentConfig())
        row = db.get(AgentVersion, identity)
        assert row.number is not None and row.number > 0


def test_admission_waits_for_disable_row_lock(database, seed):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime

    from sqlalchemy import select, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from brain.sessions import create_session
    from scheduler.config import Settings
    from scheduler.db import Principal
    from scheduler.models import Agent
    from tests_api.conftest import APP_URL, SECRET

    settings = Settings(
        database_url=APP_URL, jwt_secret=SECRET, runtime_secret=SECRET + "-runtime", _env_file=None
    )
    staff = Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner")

    def admission():
        with Session(database) as db, db.begin():
            db.execute(text("SET LOCAL lock_timeout='100ms'"))
            try:
                create_session(
                    db, staff, seed["agent_a"], datetime(2026, 11, 1, tzinfo=UTC), settings
                )
            except OperationalError as error:
                return error.orig.sqlstate
        return "admitted"

    with Session(database) as db, db.begin():
        row = db.scalar(select(Agent).where(Agent.id == seed["agent_a"]).with_for_update())
        row.enabled = False
        db.flush()
        with ThreadPoolExecutor(max_workers=1) as worker:
            assert worker.submit(admission).result(timeout=3) == "55P03"


def test_code_tone_changes_general_invitation_only():
    from brain.templates import result, toned_question

    assert toned_question("ask", "formal")["say"]["en"] == "How may I assist with your appointment?"
    assert toned_question("ask", "concise")["say"]["en"] == "What appointment do you need?"
    for key in ("confirmed", "cancelled", "emergency", "factor"):
        assert toned_question(key, "concise") == result(key)


def test_exported_agent_contract_has_no_drift():
    from pathlib import Path

    from scripts.export_agent_contracts import render

    assert (
        Path(__file__).resolve().parents[1] / "packages/schemas/agents.v1.json"
    ).read_text() == render()


def test_public_voice_response_has_frozen_typed_schema(owner):
    response = owner.get("/dashboard/voices")
    assert response.status_code == 200 and len(response.json()) == 12
    schema = owner.get("/openapi.json").json()
    items = schema["paths"]["/dashboard/voices"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["items"]
    assert items["$ref"].endswith("/VoiceOut")


def test_cli_legacy_json_preserves_voice_and_readiness_defaults(
    database, seed, tmp_path, monkeypatch
):
    import sys
    from types import SimpleNamespace

    from pydantic import SecretStr
    from sqlalchemy.orm import Session

    from brain import bootstrap
    from brain.voice_routes import _language_mode
    from scheduler.models import Agent, AgentVersion
    from tests_api.conftest import APP_URL

    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"language_mode":"bilingual"}')
    monkeypatch.setattr(
        bootstrap, "load_settings", lambda: SimpleNamespace(database_url=SecretStr(APP_URL))
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brain.bootstrap",
            "--publish",
            "--tenant-id",
            str(seed["tenant_a"]),
            "--agent-id",
            str(seed["agent_a"]),
            "--config",
            str(legacy),
        ],
    )
    bootstrap.main()
    with Session(database) as db:
        agent = db.get(Agent, seed["agent_a"])
        version = db.get(AgentVersion, agent.active_version_id)
        assert version.config["schema_version"] == 1
        assert "voice_profiles" not in version.config
        assert "voice_en_id" not in version.config and "voice_ur_id" not in version.config
        assert _language_mode(db, SimpleNamespace(version_id=version.id)) == "bilingual"


def test_seed_default_publication_explicitly_remains_legacy(database, tmp_path):
    from sqlalchemy.orm import Session

    from brain.bootstrap import seed_brain_demo
    from scheduler.models import AgentVersion
    from tests_api.conftest import ADMIN_URL

    summary = seed_brain_demo(ADMIN_URL, tmp_path / "synthetic-credentials.json")
    with Session(database) as db:
        for row in summary:
            from uuid import UUID

            config = db.get(AgentVersion, UUID(row["version_id"])).config
            assert config["schema_version"] == 1
            assert "voice_profiles" not in config


def test_new_agent_names_normalized_without_changing_legacy():
    from brain.agent_config import AgentConfig, LegacyAgentConfig
    from scheduler.dashboard.agents import AgentCreate

    for model in (AgentCreate, AgentConfig):
        with pytest.raises(ValidationError):
            model(name=" \t\n ")
        assert model(name=" Reception ").name == "Reception"
    assert LegacyAgentConfig(name="   ").name == "   "


def test_publisher_display_is_tenant_scoped_with_historical_fallback(owner, database, seed):
    from sqlalchemy.orm import Session

    from scheduler.models import User

    with Session(database) as db, db.begin():
        db.get(User, seed["owner_a"]).display_name = "QA Clinic Owner"
        db.get(User, seed["owner_b"]).display_name = "Private Other Clinic"
        # Defense in depth even for an inconsistent historical row: projection cannot
        # resolve another clinic's staff through a privileged maintenance session.
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from scheduler.dashboard.agents import version_out

        historical = SimpleNamespace(
            id=seed["version_a"],
            number=1,
            config={},
            publisher_id=seed["owner_b"],
            tenant_id=seed["tenant_a"],
            created_at=datetime.now(UTC),
            rollback_source_id=None,
        )
        assert version_out(db, historical).publisher_name is None
    path = "/dashboard/agents/" + str(seed["agent_a"])
    versions = owner.get(path + "/versions").json()
    assert owner.get("/dashboard/agents/" + str(seed["agent_b"]) + "/versions").status_code == 404
    assert versions[-1]["publisher_name"] is None
    created = owner.post("/dashboard/agents", json={"name": "QA Reception"}).json()
    path = "/dashboard/agents/" + created["id"]
    published = owner.post(
        path + "/publish", json={"revision": 1}, headers={"Idempotency-Key": "publisher-projection"}
    )
    assert published.status_code == 200
    assert published.json()["publisher_name"] == "QA Clinic Owner"
    assert owner.get(path + "/versions").json()[0]["publisher_name"] == "QA Clinic Owner"
    with Session(database) as db, db.begin():
        db.get(User, seed["owner_a"]).display_name = None
    assert owner.get(path + "/versions").json()[0]["publisher_name"] is None
