from datetime import UTC, datetime, timedelta

import jwt
import pytest
from conftest import PASSWORD, SECRET
from sqlalchemy import text


def login(client, email="owner-a@example.com", password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_login_denial_and_authenticated_me(client):
    assert client.get("/auth/me").status_code == 401
    assert login(client, password="wrong").status_code == 401
    assert (
        login(client, email="missing@example.com").json() == login(client, password="wrong").json()
    )
    result = login(client)
    assert result.status_code == 200
    assert "password" not in result.text
    client.headers["Authorization"] = "Bearer " + result.json()["access_token"]
    assert client.get("/auth/me").json()["role"] == "owner"


def test_jwt_expiry_signature_audience_and_role_recheck(owner, database, seed):
    token = owner.headers["Authorization"].split()[1]
    claims = jwt.decode(
        token, SECRET, algorithms=["HS256"], audience="voice-agent-staff", issuer="voice-agent-api"
    )
    for changed, secret in [
        (claims | {"exp": datetime.now(UTC) - timedelta(seconds=1)}, SECRET),
        (claims | {"aud": "another-api"}, SECRET),
        (claims, "wrong-secret" * 4),
    ]:
        owner.headers["Authorization"] = "Bearer " + jwt.encode(changed, secret, algorithm="HS256")
        assert owner.get("/auth/me").status_code == 401
    owner.headers["Authorization"] = "Bearer " + token
    with database.begin() as conn:
        conn.execute(text("UPDATE users SET active=false WHERE id=:id"), {"id": seed["owner_a"]})
    assert owner.get("/auth/me").status_code == 401


def test_refresh_rotation_reuse_revokes_family_and_commits(client):
    first = login(client).json()
    rotated = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != first["refresh_token"]
    assert (
        client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
        ).status_code
        == 401
    )
    fresh = login(client).json()
    assert (
        client.post("/auth/logout", json={"refresh_token": fresh["refresh_token"]}).status_code
        == 204
    )
    assert (
        client.post("/auth/refresh", json={"refresh_token": fresh["refresh_token"]}).status_code
        == 401
    )


def test_tokens_cannot_select_another_tenant(owner, seed):
    claims = jwt.decode(
        owner.headers["Authorization"].split()[1],
        SECRET,
        algorithms=["HS256"],
        audience="voice-agent-staff",
    )
    claims["tenant"] = str(seed["tenant_b"])
    owner.headers["Authorization"] = "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")
    assert owner.get("/auth/me").status_code == 401


@pytest.mark.parametrize("revoke", ["reuse", "logout"])
def test_family_revocation_cannot_miss_concurrently_rotated_descendant(
    client, database, monkeypatch, revoke
):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import scheduler.auth as auth

    ancestor = login(client).json()["refresh_token"]
    current = client.post("/auth/refresh", json={"refresh_token": ancestor}).json()["refresh_token"]
    issued = threading.Event()
    release = threading.Event()
    original = auth.issue

    def paused_issue(*args, **kwargs):
        result = original(*args, **kwargs)
        issued.set()
        if not release.wait(10):
            raise RuntimeError("Test rotation release timed out")
        return result

    monkeypatch.setattr(auth, "issue", paused_issue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rotation = pool.submit(client.post, "/auth/refresh", json={"refresh_token": current})
        try:
            assert issued.wait(5)
            path = "/auth/refresh" if revoke == "reuse" else "/auth/logout"
            revocation = pool.submit(client.post, path, json={"refresh_token": ancestor})
            deadline = time.monotonic() + 5
            waiting = False
            while time.monotonic() < deadline:
                with database.connect() as conn:
                    waiting = conn.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock')"
                        )
                    ).scalar_one()
                if waiting:
                    break
                threading.Event().wait(0.01)
            assert waiting, "Revocation must overlap the still-uncommitted rotation"
        finally:
            release.set()
        rotated = rotation.result(timeout=5)
        revoked = revocation.result(timeout=5)
    assert rotated.status_code == 200
    assert revoked.status_code == (401 if revoke == "reuse" else 204)
    final = client.post("/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]})
    assert final.status_code == 401
