"""Server-only bounded LiveKit RoomService calls; invoke outside SQL locks.

API and grants: https://docs.livekit.io/reference/other/roomservice-api/
Both create and delete require roomCreate, as in LiveKit's official Python SDK.
No recording/egress configuration or identifying room metadata is sent.
"""

import re
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx2
import jwt

from scheduler.config import Settings
from scheduler.errors import AppError


class LiveKitRooms:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx2.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.client = client
        self.clock = clock or (lambda: datetime.now(UTC))

    def create(self, room: str) -> None:
        self._request(
            "CreateRoom",
            room,
            {
                "name": room,
                "empty_timeout": 30,
                "departure_timeout": 30,
                "max_participants": 2,
            },
        )

    def delete(self, room: str) -> None:
        self._request("DeleteRoom", room, {"room": room})

    def _request(self, method: str, room: str, body: dict) -> None:
        settings = self.settings
        try:
            endpoint = urlsplit(settings.livekit_api_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
                or not settings.livekit_api_key
                or settings.livekit_api_secret is None
                or not isinstance(room, str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", room)
            ):
                raise ValueError("configuration")
            now = self.clock()
            token = jwt.encode(
                {
                    "iss": settings.livekit_api_key,
                    "iat": now,
                    "nbf": now,
                    "exp": now + timedelta(seconds=60),
                    "video": {"roomCreate": True, "room": room},
                },
                settings.livekit_api_secret.get_secret_value(),
                algorithm="HS256",
            )
            scope = (
                nullcontext(self.client)
                if self.client is not None
                else httpx2.Client(trust_env=False)
            )
            with scope as client:
                response = client.post(
                    settings.livekit_api_url.rstrip("/") + "/twirp/livekit.RoomService/" + method,
                    json=body,
                    headers={"Authorization": "Bearer " + token},
                    timeout=5,
                    follow_redirects=False,
                )
            if len(response.content) > 16384:
                raise ValueError("response")
            payload = response.json()
            if (
                method == "DeleteRoom"
                and response.status_code == 404
                and isinstance(payload, dict)
                and payload.get("code") == "not_found"
            ):
                return
            if response.status_code != 200 or not isinstance(payload, dict):
                raise ValueError("response")
            if method == "CreateRoom" and payload.get("name") != room:
                raise ValueError("room")
        except (httpx2.HTTPError, ValueError, TypeError, jwt.PyJWTError):
            # Neither upstream text, URL, body nor authorization enters the error.
            raise AppError("voice_unavailable", 503) from None
