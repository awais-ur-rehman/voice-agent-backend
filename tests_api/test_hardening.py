"""Synthetic transport and real PostgreSQL security boundary regressions."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from scheduler.config import Settings
from scheduler.main import create_app
from tests_api.conftest import APP_URL, SECRET


def test_private_errors_security_headers_and_validation_paths(client):
    marker = "مصنوعی-phone-token-do-not-echo"
    response = client.post("/auth/login", json={"email": "bad", "password": "x", marker: "secret"})
    assert response.status_code == 422
    assert marker not in response.text
    assert "body.email" in response.json()["error"]["details"]["fields"]
    for response in (
        response,
        client.get("/auth/me"),
        client.get("/not-found"),
        client.post(
            "/auth/register",
            json={
                "email": "x@example.com",
                "password": "Synthetic-password-2026!",
                "clinic_name": "Synthetic",
                "timezone": "UTC",
            },
        ),
    ):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_login_limit_persists_rejected_transactions_and_ignores_forwarded(client):
    for i in range(10):
        response = client.post(
            "/auth/login",
            json={"email": "missing@example.com", "password": "bad"},
            headers={"X-Forwarded-For": f"192.0.2.{i}", "Forwarded": f"for=192.0.2.{i}"},
        )
        assert response.status_code == 401
    response = client.post("/auth/login", json={"email": "missing@example.com", "password": "bad"})
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert response.headers["cache-control"] == "no-store"


def test_refresh_limit_and_cors(client):
    for _ in range(30):
        assert client.post("/auth/refresh", json={"refresh_token": "x" * 40}).status_code == 401
    response = client.post(
        "/auth/refresh",
        json={"refresh_token": "x" * 40},
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 429
    assert "retry-after" in response.headers["access-control-expose-headers"].lower()
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert (
        "access-control-allow-origin"
        not in client.get("/health/live", headers={"Origin": "https://hostile.invalid"}).headers
    )
    assert (
        client.options(
            "/auth/login",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        ).status_code
        == 200
    )


def test_atomic_sql_limit_expiry_and_privileges(database):
    engine = create_engine(APP_URL, hide_parameters=True)
    key = uuid4().hex * 2
    query = text("SELECT * FROM admission_admit('login', :key, 10, 60)")

    def admit(_):
        with engine.begin() as conn:
            return conn.execute(query, {"key": key}).one()

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(admit, range(24)))
        assert sum(row.admitted for row in results) == 10
        assert all(1 <= row.retry_seconds <= 60 for row in results if not row.admitted)
        with database.begin() as conn:
            conn.execute(
                text(
                    "UPDATE admission_buckets SET expires_at=clock_timestamp()-interval '1 second' WHERE bucket_key=:key"
                ),
                {"key": key},
            )
            props = conn.execute(
                text("SELECT prosecdef,proconfig FROM pg_proc WHERE proname='admission_admit'")
            ).one()
            assert props.prosecdef and "search_path=pg_catalog, public" in props.proconfig
            assert not conn.scalar(
                text("SELECT has_table_privilege('scheduler_app','admission_buckets','SELECT')")
            )
            assert not conn.scalar(
                text(
                    "SELECT has_function_privilege('public','admission_admit(text,text,integer,integer)','EXECUTE')"
                )
            )
        assert admit(0).admitted
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "headers,chunks,expected",
    [
        ([(b"content-length", b"-1")], [b"{}"], 400),
        ([(b"content-length", b"2"), (b"content-length", b"2")], [b"{}"], 400),
        ([(b"content-length", b"1")], [b"{}"], 400),
        ([(b"content-length", b"65537")], [b"{}"], 413),
        ([], [b"a" * 40000, b"b" * 30000], 413),
    ],
)
def test_raw_asgi_limits_before_endpoint(headers, chunks, expected):
    app = create_app(Settings(database_url=APP_URL, jwt_secret=SECRET, _env_file=None))
    calls = []

    @app.post("/probe")
    async def probe():
        calls.append(True)
        return {}

    async def run():
        messages = []
        pending = [
            {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
            for i, chunk in enumerate(chunks)
        ]

        async def receive():
            return pending.pop(0) if pending else {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/probe",
                "raw_path": b"/probe",
                "query_string": b"",
                "headers": headers,
                "client": ("192.0.2.1", 123),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        return messages

    messages = asyncio.run(run())
    app.state.engine.dispose()
    assert messages[0]["status"] == expected
    assert not calls


def test_drain_readiness_auth_and_generic_exception(client):
    client.app.state.settings.drain_mode = True
    response = client.get("/health/ready")
    assert response.status_code == 503 and int(response.headers["retry-after"]) >= 1
    assert client.get("/health/live").status_code == 200
    assert client.get("/auth/me").status_code == 401
    from pydantic import SecretStr

    scrape_secret = "synthetic-drain-metrics-distinct-secret-2026"
    client.app.state.settings.analytics_metrics_secret = SecretStr(scrape_secret)
    assert (
        client.get(
            "/private/analytics/metrics", headers={"Authorization": "Bearer " + scrape_secret}
        ).status_code
        == 200
    )

    @client.app.get("/synthetic-error")
    def fail():
        raise RuntimeError("signed-url-token-name-private")

    with TestClient(client.app, raise_server_exceptions=False) as safe:
        response = safe.get("/synthetic-error")
    assert response.status_code == 500
    assert "private" not in response.text
    assert response.json()["error"]["code"] == "internal_error"
    assert response.headers["cache-control"] == "no-store"


def test_admission_does_not_borrow_business_pool(client):
    assert client.app.state.admission_factory.kw["bind"] is not client.app.state.engine
    assert client.app.state.admission_factory.kw["bind"].pool.size() == 2


def test_lifespan_close_drain_dispose_order_without_blocking_loop(seed, monkeypatch):
    import threading

    app = create_app(Settings(database_url=APP_URL, jwt_secret=SECRET, _env_file=None))
    order = []
    release = threading.Event()
    draining = threading.Event()

    class Planner:
        def close(self):
            order.append("close")

        def drain(self, timeout):
            assert timeout == 3
            order.append("drain")
            draining.set()
            assert release.wait(1), "event loop blocked during drain"
            return False

    app.state.brain_planner.close()
    app.state.brain_planner = Planner()
    original = app.state.engine.dispose
    monkeypatch.setattr(app.state.engine, "dispose", lambda: (order.append("dispose"), original()))

    async def check():
        async def tick():
            while not draining.is_set():
                await asyncio.sleep(0.001)
            order.append("tick")
            release.set()

        ticker = asyncio.create_task(tick())
        async with app.router.lifespan_context(app):
            pass
        await ticker

    asyncio.run(check())
    assert order == ["close", "drain", "tick", "dispose"]


@pytest.mark.parametrize(
    "scope,limit,window",
    [("login", 10, 60), ("refresh", 30, 60), ("start_user", 30, 600), ("start_ip", 120, 600)],
)
def test_all_policy_boundaries_and_opaque_rows(client, database, scope, limit, window):
    from scheduler.admission import bucket_key

    key = bucket_key(client.app.state.settings, scope, "synthetic-private-identity")
    assert len(key) == 64 and "identity" not in key
    assert key != bucket_key(client.app.state.settings, "other", "synthetic-private-identity")
    engine = client.app.state.admission_factory.kw["bind"]
    with engine.begin() as conn:
        for i in range(limit + 2):
            row = conn.execute(
                text("SELECT * FROM admission_admit(:scope,:key,:limit,:window)"),
                {"scope": scope, "key": key, "limit": limit, "window": window},
            ).one()
            assert row.admitted == (i < limit)
            assert (row.retry_seconds == 0) if i < limit else (1 <= row.retry_seconds <= window)
    with database.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT hits FROM admission_buckets WHERE scope=:scope AND bucket_key=:key"),
                {"scope": scope, "key": key},
            )
            == limit + 1
        )


def test_limiter_pool_progress_while_business_connection_held(client):
    from types import SimpleNamespace

    from sqlalchemy.orm import sessionmaker

    from scheduler.admission import admit_auth

    small = create_engine(
        APP_URL, pool_size=1, max_overflow=0, pool_timeout=0.1, hide_parameters=True
    )
    previous = client.app.state.session_factory
    client.app.state.session_factory = sessionmaker(small)
    try:
        with small.connect() as held:
            held.execute(text("SELECT 1"))

            def run(i):
                admit_auth(
                    SimpleNamespace(app=client.app, client=SimpleNamespace(host=f"192.0.2.{i}")),
                    "login",
                )

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(run, range(4)))
    finally:
        client.app.state.session_factory = previous
        small.dispose()


def test_admission_unavailable_fails_closed_and_no_identity_leak(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def fail():
        raise OperationalError("private-name-token", {}, Exception("private-name-token"))

    monkeypatch.setattr(client.app.state, "admission_factory", fail)
    response = client.post(
        "/auth/login", json={"email": "private@example.com", "password": "secret"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "admission_unavailable"
    assert response.headers["retry-after"] == "1"
    assert "private" not in response.text


def test_expiry_cleanup_is_bounded_and_rejects_policy_escalation(client, database):
    with database.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admission_buckets SELECT 'login', lpad(to_hex(n),64,'0'), 1, clock_timestamp()-interval '1 minute' FROM generate_series(1,250) n"
            )
        )
    engine = client.app.state.admission_factory.kw["bind"]
    with engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT * FROM admission_admit('login',:key,10,60)"), {"key": uuid4().hex * 2}
            )
            .one()
            .admitted
        )
    with database.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT count(*) FROM admission_buckets WHERE expires_at < clock_timestamp()")
            )
            == 150
        )
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(
            text("SELECT * FROM admission_admit('login',:key,10000,60)"), {"key": uuid4().hex * 2}
        )


def test_boundary_errors_are_cors_readable(client):
    response = client.post(
        "/auth/login", content=b"a" * 65537, headers={"Origin": "http://localhost:5173"}
    )
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


def test_headers_query_and_body_exact_bounds(client):
    @client.app.post("/body-limit")
    async def body_limit():
        return {}

    assert client.post("/body-limit", content=b"x" * 65536).status_code == 200
    assert client.post("/body-limit", content=b"x" * 65537).status_code == 413
    assert client.get("/health/live?" + ("x" * 8192)).status_code == 200
    assert client.get("/health/live?" + ("x" * 8193)).status_code == 414
    assert client.get("/health/live", headers={"X-Big": "x" * 32768}).status_code == 431
    preflight = client.options(
        "/auth/login",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert preflight.headers["cache-control"] == "no-store"
    assert preflight.headers["x-content-type-options"] == "nosniff"


def test_binary_exemption_only_exact_upload_routes(client):
    # A synthetic matching route exercises streaming without filesystem/blob writes.
    client.app.state.settings.knowledge_enabled = True
    identity = str(uuid4())
    path = f"/dashboard/agents/{identity}/knowledge/sources/upload"
    # Existing route requires auth and returns 401 before buffering the binary stream.
    assert client.post(path, content=b"x" * 65537).status_code == 401
    assert client.post(path + "/extra", content=b"x" * 65537).status_code == 413
    assert (
        client.post(
            "/auth/login",
            content=b"x" * 65537,
            headers={"Content-Type": "application/octet-stream"},
        ).status_code
        == 413
    )


@pytest.mark.parametrize("value", [-1, 11, float("inf"), float("nan")])
def test_shutdown_setting_bounds(value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            database_url=APP_URL, jwt_secret=SECRET, shutdown_drain_seconds=value, _env_file=None
        )


def test_shutdown_zero_passes_no_wait_to_provider(client, monkeypatch):
    values = []
    client.app.state.settings.shutdown_drain_seconds = 0
    monkeypatch.setattr(
        client.app.state.brain_planner, "drain", lambda timeout: values.append(timeout) or False
    )

    async def run():
        async with client.app.router.lifespan_context(client.app):
            pass

    asyncio.run(run())
    assert values == [0]


def test_admission_lock_timeout_is_bounded(client, database):
    import time

    from scheduler.admission import bucket_key

    request = {"email": "missing@example.com", "password": "bad"}
    assert client.post("/auth/login", json=request).status_code == 401
    key = bucket_key(client.app.state.settings, "login", "testclient")
    with database.begin() as conn:
        conn.execute(
            text(
                "SELECT 1 FROM admission_buckets WHERE scope='login' AND bucket_key=:key FOR UPDATE"
            ),
            {"key": key},
        )
        started = time.monotonic()
        result = client.post("/auth/login", json=request)
        assert (
            result.status_code == 503 and result.json()["error"]["code"] == "admission_unavailable"
        )
        assert time.monotonic() - started < 1.5


def test_unexpected_errors_and_hostile_fields_never_log_input(client):
    import logging

    marker = "مصنوعی-name-phone-token-signed-url"
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("scheduler.requests")
    handler = Capture()

    @client.app.get("/private-failure")
    def fail():
        raise ValueError(marker)

    logger.addHandler(handler)
    try:
        response = client.get(
            "/private-failure",
            params={"secret": marker},
            headers={"Authorization": "Bearer " + marker.encode().hex()},
        )
        assert response.status_code == 500
        invalid = client.post(
            "/auth/login", json={"email": "bad", "password": "x", marker: "value"}
        )
        assert invalid.status_code == 422
    finally:
        logger.removeHandler(handler)
    assert marker not in response.text + invalid.text + "".join(records)
    assert len(records) == 2
    assert all(
        set(json.loads(record)) == {"request_id", "method", "route", "status", "duration_ms"}
        for record in records
    )


@pytest.mark.parametrize("ownership", ["database", "schema"])
def test_startup_rejects_database_or_schema_ownership(database, ownership):
    # Only the dedicated fixture database; restore original ownership even on failure.
    with database.begin() as conn:
        assert conn.scalar(text("SELECT current_database()")) == "voice_agent_test"
        if ownership == "database":
            original = conn.scalar(
                text(
                    "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()"
                )
            )
            target = "DATABASE voice_agent_test"
        else:
            original = conn.scalar(
                text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public'")
            )
            target = "SCHEMA public"
        quoted = conn.dialect.identifier_preparer.quote(original)
        conn.execute(text(f"ALTER {target} OWNER TO scheduler_app"))
    try:
        app = create_app(Settings(database_url=APP_URL, jwt_secret=SECRET, _env_file=None))
        with pytest.raises(RuntimeError, match="non-owner"):
            with TestClient(app):
                pass
    finally:
        with database.begin() as conn:
            conn.execute(text(f"ALTER {target} OWNER TO {quoted}"))


def test_lifespan_held_provider_callback_times_out_then_disposes(seed, monkeypatch):
    import threading
    import time

    import httpx2

    from analytics.telemetry import MeasurementSink, measurement_scope
    from brain.provider import Nemotron, ProviderError
    from tests_brain.test_provider_router import answer, plan

    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        llm_api_key="synthetic-only",
        shutdown_drain_seconds=0.02,
        _env_file=None,
    )
    app = create_app(settings)
    app.state.brain_planner.close()
    planner = Nemotron(settings, transport=httpx2.MockTransport(lambda request: answer()))
    app.state.brain_planner = planner
    entered, release = threading.Event(), threading.Event()
    disposed = []
    original = app.state.engine.dispose
    monkeypatch.setattr(app.state.engine, "dispose", lambda: (disposed.append(True), original()))

    def persist(value):
        entered.set()
        assert release.wait(2)

    try:
        with measurement_scope(MeasurementSink(writer=persist)):
            assert plan(planner).name == "ask_caller"
        assert entered.wait(1)
        started = time.monotonic()

        async def run():
            async with app.router.lifespan_context(app):
                pass

        asyncio.run(run())
        assert time.monotonic() - started < 0.5
        assert disposed == [True]
        assert not planner.drain(0)
        with pytest.raises(ProviderError):
            plan(planner)
    finally:
        release.set()
        assert planner.drain(1)


def test_registration_retains_five_per_hour_with_hmac_and_retry(database, seed):
    import hashlib

    from scheduler.admission import bucket_key

    settings = Settings(
        database_url=APP_URL, jwt_secret=SECRET, registration_enabled=True, _env_file=None
    )
    peer = "synthetic-registration-" + uuid4().hex
    app = create_app(settings)
    with TestClient(app, client=(peer, 50000)) as api:
        for i in range(6):
            response = api.post(
                "/auth/register",
                json={
                    "email": f"{uuid4().hex}@example.com",
                    "password": "Synthetic-password-2026!",
                    "clinic_name": "Synthetic Clinic",
                    "timezone": "Asia/Karachi",
                },
            )
            assert response.status_code == (201 if i < 5 else 429), response.text
        assert response.headers["retry-after"] == "3600"
    with database.connect() as conn:
        key = bucket_key(settings, "registration", peer)
        assert (
            conn.scalar(
                text("SELECT attempts FROM registration_attempts WHERE bucket=:key"), {"key": key}
            )
            == 6
        )
        assert (
            conn.scalar(
                text("SELECT count(*) FROM registration_attempts WHERE bucket IN (:raw,:legacy)"),
                {"raw": peer, "legacy": hashlib.sha256(peer.encode()).hexdigest()},
            )
            == 0
        )


def test_configured_dashboard_origin_reads_retry_after(seed):
    app = create_app(
        Settings(
            database_url=APP_URL,
            jwt_secret=SECRET,
            cors_origins=["http://localhost:5176"],
            drain_mode=True,
            _env_file=None,
        )
    )
    with TestClient(app) as api:
        response = api.get("/health/ready", headers={"Origin": "http://localhost:5176"})
        assert response.status_code == 503
        assert response.headers["retry-after"] == "3"
        assert response.headers["access-control-allow-origin"] == "http://localhost:5176"
        assert "retry-after" in response.headers["access-control-expose-headers"].lower()
        assert (
            "access-control-allow-origin"
            not in api.get("/health/ready", headers={"Origin": "https://hostile.invalid"}).headers
        )


@pytest.mark.parametrize("size,expected", [(32768, 200), (32769, 431)])
def test_raw_aggregate_header_boundary(size, expected):
    from scheduler.http_security import RequestLimits

    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await RequestLimits(endpoint)(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"x", b"x" * (size - 1))],
                "query_string": b"",
            },
            receive,
            send,
        )
        return messages

    assert asyncio.run(run())[0]["status"] == expected


def test_expired_reset_and_concurrent_denials_keep_one_window(client, database):
    from datetime import timedelta

    engine = client.app.state.admission_factory.kw["bind"]
    key = uuid4().hex * 2
    query = text("SELECT * FROM admission_admit('login',:key,10,60)")
    with engine.begin() as conn:
        conn.execute(query, {"key": key})
    with database.begin() as conn:
        conn.execute(
            text(
                "UPDATE admission_buckets SET hits=11,expires_at=clock_timestamp()-interval '1 second' WHERE bucket_key=:key"
            ),
            {"key": key},
        )
    with engine.begin() as conn:
        before = conn.scalar(text("SELECT clock_timestamp()"))
        assert conn.execute(query, {"key": key}).one().admitted
        after = conn.scalar(text("SELECT clock_timestamp()"))
    with database.connect() as conn:
        hits, expiry = conn.execute(
            text("SELECT hits,expires_at FROM admission_buckets WHERE bucket_key=:key"),
            {"key": key},
        ).one()
        assert hits == 1
        assert before + timedelta(seconds=60) <= expiry <= after + timedelta(seconds=60)
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        ):
            assert not conn.scalar(
                text("SELECT has_table_privilege('scheduler_app','admission_buckets',:privilege)"),
                {"privilege": privilege},
            )

    def admit(_):
        with engine.begin() as conn:
            return conn.execute(query, {"key": key}).one().admitted

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(admit, range(15))) == 9
    with database.connect() as conn:
        assert conn.execute(
            text("SELECT hits,expires_at FROM admission_buckets WHERE bucket_key=:key"),
            {"key": key},
        ).one() == (11, expiry)
