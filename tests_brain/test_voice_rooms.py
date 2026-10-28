"""RoomService requests use short server grants and bounded, sanitized failures."""

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx2
import jwt
import pytest
from pydantic import SecretStr

from scheduler.errors import AppError

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
SECRET = "synthetic-room-secret-00000000000000"
ROOM = "voice_0123456789abcdef0123456789abcdef"


def settings(**changes):
    return SimpleNamespace(
        **(
            {
                "livekit_api_url": "http://127.0.0.1:7880",
                "livekit_api_key": "synthetic-key",
                "livekit_api_secret": SecretStr(SECRET),
            }
            | changes
        )
    )


def rooms(handler, **changes):
    from brain.voice_rooms import LiveKitRooms

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return LiveKitRooms(settings(**changes), client=client, clock=lambda: NOW)


@pytest.mark.parametrize(
    ("operation", "method", "body"),
    [
        (
            "create",
            "CreateRoom",
            {
                "name": ROOM,
                "empty_timeout": 30,
                "departure_timeout": 30,
                "max_participants": 2,
            },
        ),
        ("delete", "DeleteRoom", {"room": ROOM}),
    ],
)
def test_room_request_has_exact_opaque_body_and_minimal_server_grant(operation, method, body):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.method == "POST"
        assert str(request.url) == f"http://127.0.0.1:7880/twirp/livekit.RoomService/{method}"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == body
        assert request.extensions["timeout"] == {"connect": 5, "read": 5, "write": 5, "pool": 5}
        authorization = request.headers["authorization"]
        assert authorization.startswith("Bearer ")
        claims = jwt.decode(
            authorization[7:],
            SECRET,
            algorithms=["HS256"],
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
        assert claims["iss"] == "synthetic-key"
        assert claims["exp"] - claims["iat"] == 60
        assert claims["nbf"] == claims["iat"] == int(NOW.timestamp())
        assert claims["video"] == {"roomCreate": True, "room": ROOM}
        assert set(claims) == {"iss", "iat", "nbf", "exp", "video"}
        return httpx2.Response(200, json={"name": ROOM} if operation == "create" else {})

    assert getattr(rooms(handle), operation)(ROOM) is None
    assert len(seen) == 1


def test_delete_missing_room_is_idempotent_only_for_structured_twirp_not_found():
    service = rooms(
        lambda _request: httpx2.Response(
            404,
            json={
                "code": "not_found",
                "msg": "upstream details must not escape",
            },
        )
    )
    assert service.delete(ROOM) is None


@pytest.mark.parametrize(
    ("operation", "status", "payload"),
    [
        ("create", 404, {"code": "not_found"}),
        ("delete", 404, {"code": "permission_denied"}),
        ("delete", 404, "not_found"),
        ("create", 401, {"code": "unauthenticated"}),
        ("create", 403, {"code": "permission_denied"}),
        ("create", 429, {}),
        ("create", 500, {}),
        ("create", 302, {}),
    ],
)
def test_http_errors_are_sanitized_and_redirects_are_not_followed(
    operation,
    status,
    payload,
    caplog,
):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx2.Response(
            status,
            json=payload,
            headers={
                "Location": "https://untrusted.invalid/?token=sensitive-upstream-body",
            },
        )

    caplog.set_level(logging.DEBUG)
    with pytest.raises(AppError) as caught:
        getattr(rooms(handle), operation)(ROOM)
    assert caught.value.code == "voice_unavailable"
    assert caught.value.status == 503
    assert caught.value.body() == {
        "error": {
            "code": "voice_unavailable",
            "message": "voice unavailable",
        }
    }
    assert caught.value.__suppress_context__
    assert len(seen) == 1
    assert SECRET not in caplog.text
    assert "sensitive-upstream-body" not in caplog.text


@pytest.mark.parametrize("exception", [httpx2.ReadTimeout, httpx2.ConnectError])
def test_network_failure_discards_original_sensitive_exception(exception, caplog):
    def fail(request):
        raise exception("sensitive-upstream-body token=private", request=request)

    with pytest.raises(AppError) as caught:
        rooms(fail).create(ROOM)
    assert caught.value.code == "voice_unavailable"
    assert caught.value.__suppress_context__
    assert "sensitive" not in str(caught.value.body()) + caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"livekit_api_key": None},
        {"livekit_api_secret": None},
        {"livekit_api_url": "ws://127.0.0.1:7880"},
        {"livekit_api_url": "http://user:secret@localhost:7880"},
        {"livekit_api_url": "http://localhost:7880/?token=private"},
    ],
)
def test_invalid_server_configuration_fails_before_any_request(changes):
    def unexpected(_request):
        pytest.fail("invalid server configuration issued a request")

    with pytest.raises(AppError) as caught:
        rooms(unexpected, **changes).create(ROOM)
    assert caught.value.code == "voice_unavailable"


@pytest.mark.parametrize(
    "room", ["", "patient full name", "../room", "room?token=private", "x" * 129]
)
def test_room_identifiers_are_bounded_and_cannot_contain_patient_prose(room):
    def unexpected(_request):
        pytest.fail("invalid room identifier issued a request")

    with pytest.raises(AppError):
        rooms(unexpected).create(room)


@pytest.mark.parametrize("payload", [{}, {"name": "wrong-room"}, ["not-a-room"]])
def test_create_requires_room_identity_in_success_response(payload):
    with pytest.raises(AppError):
        rooms(lambda _request: httpx2.Response(200, json=payload)).create(ROOM)


def test_invalid_json_success_does_not_claim_provisioning():
    with pytest.raises(AppError):
        rooms(lambda _request: httpx2.Response(200, text="sensitive upstream body")).create(ROOM)
