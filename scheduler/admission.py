"""Independent, distributed admission; identities never leave as plaintext keys."""

import hashlib
import hmac

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .errors import AppError

POLICIES = {
    "login": (10, 60),
    "refresh": (30, 60),
    "start_user": (30, 600),
    "start_ip": (120, 600),
}


def bucket_key(settings, scope: str, identity: str) -> str:
    return hmac.new(
        settings.jwt_secret.get_secret_value().encode(),
        b"voice-agent:admission:v1\0" + scope.encode() + b"\0" + identity.encode(),
        hashlib.sha256,
    ).hexdigest()


def _admit(request, scope: str, identity: str) -> None:
    limit, window = POLICIES[scope]
    key = bucket_key(request.app.state.settings, scope, identity)
    try:
        with request.app.state.admission_factory() as db, db.begin():
            row = db.execute(
                text("SELECT * FROM admission_admit(:scope,:key,:limit,:window)"),
                {"scope": scope, "key": key, "limit": limit, "window": window},
            ).one()
    except SQLAlchemyError:
        raise AppError("admission_unavailable", 503, retry_after=1) from None
    if not row.admitted:
        raise AppError("rate_limited", 429, retry_after=row.retry_seconds)


def transport_ip(request) -> str:
    # Never trust Origin, Forwarded or X-Forwarded-For. Uvicorn must also disable
    # proxy headers so the transport peer has not already been overwritten.
    return request.client.host if request.client else "unknown"


def admit_auth(request, scope: str) -> None:
    if scope not in {"login", "refresh"}:
        raise ValueError("Unsupported auth admission scope")
    _admit(request, scope, transport_ip(request))


def require_accepting(request) -> None:
    if request.app.state.settings.drain_mode:
        raise AppError("service_draining", 503, retry_after=3)


def admit_start(request, user) -> None:
    """Call only after authoritative exact-replay checks, with the parent actor."""
    require_accepting(request)
    _admit(request, "start_user", f"{user.tenant_id}:{user.id}")
    _admit(request, "start_ip", transport_ip(request))
