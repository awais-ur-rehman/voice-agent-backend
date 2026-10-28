from uuid import uuid4

import jwt
import pytest

from scheduler.config import Settings
from scheduler.errors import AppError
from tests_api.conftest import NOW, SECRET


def test_browser_grant_is_one_room_microphone_only():
    from brain.voice_tokens import media_token

    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        livekit_api_key="synthetic-key",
        livekit_api_secret=SECRET,
        _env_file=None,
    )
    token = media_token(settings, "opaque-room", "opaque-caller", NOW)
    claims = jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    assert claims["iss"] == "synthetic-key"
    assert claims["sub"] == "opaque-caller"
    assert claims["video"] == dict(
        room="opaque-room",
        roomJoin=True,
        canPublish=True,
        canSubscribe=True,
        canPublishData=True,
        canPublishSources=["microphone"],
        canUpdateOwnMetadata=False,
    )
    assert claims["exp"] - claims["iat"] == 120
    assert "runtime_token" not in claims


def test_worker_claims_cannot_be_decoded_as_staff_or_runtime():
    from brain.voice_sessions import WorkerAuthority
    from brain.voice_tokens import decode_worker, worker_token

    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        voice_worker_secret="synthetic-worker-signing-key-distinct-2026",
        _env_file=None,
    )
    authority = WorkerAuthority(uuid4(), uuid4(), 3, "unused")
    token = worker_token(settings, authority, uuid4(), NOW)
    decoded, tenant = decode_worker(settings, token, NOW)
    assert decoded.worker_id == authority.worker_id
    assert decoded.generation == 3
    with pytest.raises(AppError):
        decode_worker(
            settings, jwt.encode({"aud": "voice-agent-staff"}, SECRET, algorithm="HS256"), NOW
        )
