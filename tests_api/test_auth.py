from datetime import UTC, datetime, timedelta

import jwt
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
