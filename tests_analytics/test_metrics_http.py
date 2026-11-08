"""Actual worker/runtime capability and body-read authority boundaries."""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.models import StageMetric, TurnEvent
from brain.voice_sessions import create_voice
from scheduler.config import Settings
from scheduler.db import Principal
from scheduler.main import create_app
from scheduler.models import VoiceSession
from tests_analytics.test_metrics import sample
from tests_api.conftest import APP_URL, NOW, SECRET
from tests_brain.conftest import seed  # noqa: F401


@pytest.fixture
def native_worker(database, seed):  # noqa: F811
    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="runtime-native-metrics-distinct-secret-2026",
        voice_worker_secret="worker-native-metrics-distinct-secret-2026",
        voice_bootstrap_secret="bootstrap-native-metrics-distinct-secret-2026",
        livekit_api_key="synthetic",
        livekit_api_secret="livekit-native-metrics-distinct-secret-2026",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        voice_enabled=True,
        _env_file=None,
    )
    owner = Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner")
    with Session(database) as db:
        voice = create_voice(db, owner, seed["agent_a"], "metric-fixture", NOW, settings)
        voice.status = "active"
        identity = voice.session_id
        voice_id = voice.id
        db.commit()
    now = [NOW]
    app = create_app(settings, clock=lambda: now[0])
    with TestClient(app) as client:
        grant = client.post(
            "/voice/worker/claim",
            headers={
                "Authorization": "Bearer " + settings.voice_bootstrap_secret.get_secret_value()
            },
            json={
                "tenant_id": str(owner.tenant_id),
                "voice_id": str(voice_id),
                "worker_id": str(uuid4()),
            },
        )
        assert grant.status_code == 200, grant.text
        grant = grant.json()
        headers = {
            "Authorization": "Bearer " + grant["token"],
            "X-Runtime-Capability": grant["runtime_token"],
        }
        yield (
            client,
            f"/voice/worker/sessions/{identity}/metrics",
            headers,
            {"generation": grant["generation"], "revision": 0, "observations": [sample()]},
            now,
            voice_id,
            app,
        )


def test_metrics_auth_bounds_retries_and_private_validation(native_worker, database):
    client, url, headers, payload, _, _, _ = native_worker
    assert client.post(url, content=b"bad private text").status_code == 401
    response = client.post(url, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert client.post(url, headers=headers, json=payload).status_code == 200
    bad = payload | {
        "observations": [sample(event_id=payload["observations"][0]["event_id"], duration_ms=99)]
    }
    assert client.post(url, headers=headers, json=bad).status_code == 409
    assert (
        client.post(
            url, headers=headers, json=payload | {"generation": payload["generation"] + 1}
        ).status_code
        == 401
    )
    assert client.post(url, headers=headers, content=b"x" * 32769).status_code == 413
    response = client.post(
        url,
        headers=headers,
        json=payload | {"observations": [sample(metadata={"text": "private identifying value"})]},
    )
    assert response.status_code == 422 and "private identifying value" not in response.text
    foreign = url.replace(url.split("/")[-2], str(uuid4()))
    assert client.post(foreign, headers=headers, json=payload).status_code == 401
    with Session(database) as db:
        assert len(list(db.scalars(select(StageMetric)))) == 1
        assert not list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "turn.user")))


def test_metrics_recheck_expired_authority_after_body_read(native_worker, database):
    import json

    client, url, headers, payload, now, _, _ = native_worker

    def body():
        raw = json.dumps(payload).encode()
        yield raw[:2]
        now[0] = NOW + timedelta(seconds=16)
        yield raw[2:]

    assert client.post(url, headers=headers, content=body()).status_code == 403
    with Session(database) as db:
        assert not list(db.scalars(select(StageMetric)))


def test_metrics_current_worker_ended_and_revision_fences(native_worker, database):
    client, url, headers, payload, _, voice_id, _ = native_worker
    assert client.post(url, headers=headers, json=payload | {"revision": 1}).status_code == 409
    with Session(database) as db, db.begin():
        db.get(VoiceSession, voice_id).generation += 1
    assert client.post(url, headers=headers, json=payload).status_code == 403
    with Session(database) as db:
        assert not list(db.scalars(select(StageMetric)))


def test_provider_actual_invocation_survives_later_turn_conflict(native_worker, database):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx2

    from analytics.models import UsageInvocation
    from brain.provider import Nemotron

    client, url, headers, payload, _, _, app = native_worker
    entered, release = threading.Event(), threading.Event()

    def serve(request):
        entered.set()
        assert release.wait(3)
        return httpx2.Response(
            200,
            json={
                "usage": {"prompt_tokens": 13, "completion_tokens": 4},
                "choices": [{"message": {"content": '{"name":"ask_caller","arguments":{}}'}}],
            },
        )

    from pydantic import SecretStr

    app.state.settings.llm_api_key = SecretStr("synthetic")
    previous = app.state.brain_planner
    provider = Nemotron(app.state.settings, transport=httpx2.MockTransport(serve))
    app.state.brain_planner = provider
    base = url.removesuffix("/metrics")
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(
                client.post,
                base + "/turn",
                headers=headers,
                json={
                    "command_id": str(uuid4()),
                    "expected_revision": 0,
                    "text": "Please help with a clinic question.",
                    "utterance_started_at": NOW.isoformat(),
                    "language": "en",
                    "confidence": 0.9,
                    "uncertain": False,
                },
            )
            assert entered.wait(2)
            assert (
                client.post(
                    base + "/interrupt", headers=headers, json={"command_id": str(uuid4())}
                ).status_code
                == 200
            )
            release.set()
            assert pending.result(timeout=3).status_code == 409
        with Session(database) as db:
            rows = list(db.scalars(select(UsageInvocation)))
            assert len(rows) == 1 and rows[0].status == "success" and rows[0].input_tokens == 13
            assert not list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "turn.user")))
    finally:
        release.set()
        provider.close()
        app.state.brain_planner = previous


def test_ended_metrics_cannot_use_terminal_cleanup_authority(native_worker, database):
    from analytics.models import CallRecord
    from scheduler.models import AgentSession

    client, url, headers, payload, _, voice_id, _ = native_worker
    with Session(database) as db, db.begin():
        voice = db.get(VoiceSession, voice_id)
        parent = db.get(AgentSession, voice.session_id)
        parent.status = "ended"
        parent.revoked_at = NOW
        call = db.get(CallRecord, parent.id)
        call.status = "ended"
        call.ended_at = NOW
        call.outcome = "info_only"
        call.end_reason = "caller_end"
    assert client.post(url, headers=headers, json=payload).status_code == 401
    with Session(database) as db:
        assert not list(db.scalars(select(StageMetric)))


def test_response_diagnostic_links_only_actual_same_session_receipt(native_worker, database):
    from analytics.models import CallRecord
    from brain.contracts import Action

    client, url, headers, payload, _, _, app = native_worker
    app.state.brain_planner.plan = lambda *_: Action(name="ask_caller", arguments={})
    command_id = str(uuid4())
    turn = client.post(
        url.removesuffix("/metrics") + "/turn",
        headers=headers,
        json={
            "command_id": command_id,
            "expected_revision": 0,
            "text": "Please help with the clinic.",
            "utterance_started_at": NOW.isoformat(),
            "language": "en",
            "confidence": 0.9,
            "uncertain": False,
        },
    )
    assert turn.status_code == 200, turn.text
    observation = sample(
        stage="audio_queue",
        output_kind="response",
        command_id=command_id,
        metadata={"cache_hit": False, "output_state": "queued"},
    )
    body = payload | {"revision": turn.json()["revision"], "observations": [observation]}
    assert client.post(url, headers=headers, json=body).status_code == 200
    wrong = body | {
        "observations": [observation | {"event_id": str(uuid4()), "command_id": str(uuid4())}]
    }
    assert client.post(url, headers=headers, json=wrong).status_code == 409
    with Session(database) as db:
        metric = db.scalar(select(StageMetric).where(StageMetric.stage == "audio_queue"))
        assistant = db.scalar(select(TurnEvent).where(TurnEvent.kind == "turn.assistant"))
        assert metric.turn_event_id == assistant.event_id
        call = db.scalar(select(CallRecord))
        assert call.channel == "voice"
    assert (
        client.post(
            url.removesuffix("/metrics") + "/interrupt",
            headers=headers,
            json={"command_id": str(uuid4())},
        ).status_code
        == 200
    )
    assert client.post(url, headers=headers, json=body).status_code == 409
    with Session(database) as db:
        interruption = db.scalar(
            select(TurnEvent).where(TurnEvent.safe_metadata["delivery"].astext == "interrupted")
        )
        assert interruption.safe_metadata["trusted"] is True
        assert (
            len(list(db.scalars(select(StageMetric).where(StageMetric.stage == "audio_queue"))))
            == 1
        )
        assert not list(
            db.scalars(
                select(StageMetric).where(
                    StageMetric.stage.in_(["tts_first_byte", "end_of_speech_to_audio"])
                )
            )
        )
