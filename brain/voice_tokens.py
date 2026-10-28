"""Distinct short-lived RTC and internal worker capabilities."""

import secrets
from datetime import datetime, timedelta
from uuid import UUID

import jwt

from scheduler.errors import AppError
from scheduler.security import token_hash

from .voice_sessions import WorkerAuthority

WORKER_AUDIENCE = "voice-agent-media-worker"


def _worker_secret(settings) -> str:
    secret = settings.voice_worker_secret
    if secret is None:
        raise AppError("voice_unavailable", 503)
    value = secret.get_secret_value()
    others = [settings.jwt_secret, settings.runtime_secret, settings.livekit_api_secret]
    if any(
        item is not None and secrets.compare_digest(value, item.get_secret_value())
        for item in others
    ):
        raise AppError("voice_unavailable", 503)
    return value


def media_token(settings, room: str, participant: str, now: datetime) -> str:
    if not settings.livekit_api_key or not settings.livekit_api_secret:
        raise AppError("voice_unavailable", 503)
    return jwt.encode(
        {
            "iss": settings.livekit_api_key,
            "sub": participant,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=120),
            "video": {
                "room": room,
                "roomJoin": True,
                "canPublish": True,
                "canSubscribe": True,
                "canPublishData": True,
                "canPublishSources": ["microphone"],
                "canUpdateOwnMetadata": False,
            },
        },
        settings.livekit_api_secret.get_secret_value(),
        algorithm="HS256",
    )


def worker_token(settings, authority: WorkerAuthority, tenant: UUID, now: datetime) -> str:
    return jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "aud": WORKER_AUDIENCE,
            "sub": str(authority.worker_id),
            "voice": str(authority.voice_id),
            "tenant": str(tenant),
            "generation": authority.generation,
            "jti": secrets.token_urlsafe(32),
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        _worker_secret(settings),
        algorithm="HS256",
    )


def decode_worker(
    settings, token: str, now: datetime, *, cleanup: bool = False
) -> tuple[WorkerAuthority, UUID]:
    try:
        claims = jwt.decode(
            token,
            _worker_secret(settings),
            algorithms=["HS256"],
            audience=WORKER_AUDIENCE,
            issuer=settings.jwt_issuer,
            options={
                "verify_exp": False,
                "verify_iat": False,
                "require": [
                    "iss",
                    "aud",
                    "sub",
                    "voice",
                    "tenant",
                    "generation",
                    "jti",
                    "iat",
                    "exp",
                ],
            },
        )
        grace = 30 if cleanup else 0
        if not claims["iat"] <= now.timestamp() < claims["exp"] + grace:
            raise ValueError("expired")
        generation = claims["generation"]
        if type(generation) is not int or generation < 1:
            raise ValueError("generation")
        return WorkerAuthority(
            UUID(claims["voice"]), UUID(claims["sub"]), generation, token_hash(claims["jti"])
        ), UUID(claims["tenant"])
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        raise AppError("unauthorized", 401) from None
