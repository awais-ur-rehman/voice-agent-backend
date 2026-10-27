import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash

from .config import Settings

passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash("not-a-real-user-password")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def refresh_secret(tenant_id: UUID) -> str:
    return str(tenant_id) + "." + secrets.token_urlsafe(48)


def access_token(user_id: UUID, tenant_id: UUID, settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "tenant": str(tenant_id),
            "iat": now,
            "exp": now + timedelta(minutes=settings.access_minutes),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
