"""Knowledge API boundaries through the real restricted application role."""

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests_api.conftest import PASSWORD


def setup_api(owner, tmp_path):
    owner.app.state.settings.knowledge_enabled = True
    owner.app.state.settings.knowledge_blob_root = tmp_path
    agent = owner.post("/dashboard/agents", json={"name": "Knowledge test"}).json()["id"]
    return f"/dashboard/agents/{agent}/knowledge"


def write(owner, path, body=None, key=None, method="post", **kwargs):
    return owner.request(
        method, path, json=body, headers={"Idempotency-Key": key or str(uuid4())}, **kwargs
    )


def test_default_disabled_capability(owner):
    agent = owner.post("/dashboard/agents", json={"name": "Knowledge test"}).json()["id"]
    result = owner.get(f"/dashboard/agents/{agent}/knowledge/capabilities")
    assert result.status_code == 200
    assert result.json()["enabled"] is False
    assert result.json()["processing_available"] is False


def test_source_lifecycle_receipts_and_revisions(owner, tmp_path, database):
    base = setup_api(owner, tmp_path)
    body = {"kind": "text", "name": "  Hours ", "text": "Open weekdays", "format": "txt"}
    created = write(owner, base + "/sources", body, "create-source-key")
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["name"] == "Hours" and row["revision"] == 1 and not row["available"]
    assert row["status"] == "queued"
    assert write(owner, base + "/sources", body, "create-source-key").json() == row
    assert (
        write(
            owner, base + "/sources", body | {"text": "different"}, "create-source-key"
        ).status_code
        == 409
    )
    url = base + "/sources/" + row["id"]
    update = body | {"revision": 1, "text": "New hours"}
    changed = write(owner, url, update, "update-source-key", method="put")
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    assert write(owner, url, update, "update-source-key", method="put").json() == changed.json()
    assert write(owner, url, update, method="put").status_code == 409
    assert (
        write(
            owner,
            url,
            {"revision": 2, "kind": "url", "name": "URL", "url": "https://example.com"},
            method="put",
        ).status_code
        == 409
    )
    deleted = write(owner, url + "?revision=2", key="delete-source-key", method="delete")
    assert deleted.status_code == 200, deleted.text
    assert (
        write(owner, url + "?revision=2", key="delete-source-key", method="delete").json()
        == deleted.json()
    )
    assert owner.get(url).status_code == 404
    assert owner.get(base + "/sources").json() == []
    with database.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM knowledge_sources")).scalar_one() == 1
        assert (
            conn.execute(
                text("SELECT count(*) FROM knowledge_jobs WHERE status='cancelled'")
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "text", "name": " ", "text": "yes", "format": "txt"},
        {"kind": "text", "name": "x", "text": " ", "format": "txt"},
        {"kind": "text", "name": "x", "text": "yes", "format": "pdf"},
        {"kind": "faq", "name": "x", "question": " ", "answer": "yes"},
        {"kind": "faq", "name": "x", "question": "yes", "answer": " "},
        {"kind": "url", "name": "x", "url": " "},
        {"kind": "url", "name": "x", "url": "http://example.com"},
        {"kind": "file", "name": "x", "format": "txt"},
    ],
)
def test_invalid_sources(owner, tmp_path, body):
    base = setup_api(owner, tmp_path)
    assert write(owner, base + "/sources", body).status_code == 422


def test_manager_tenant_and_agent_boundaries(owner, tmp_path):
    base = setup_api(owner, tmp_path)
    row = write(
        owner,
        base + "/sources",
        {"kind": "faq", "name": "FAQ", "question": "When?", "answer": "Today"},
    ).json()
    other = setup_api(owner, tmp_path)
    assert owner.get(other + "/sources/" + row["id"]).status_code == 404
    login = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()
    owner.headers["Authorization"] = "Bearer " + login["access_token"]
    assert (
        write(
            owner, base + "/sources", {"kind": "url", "name": "x", "url": "https://example.com"}
        ).status_code
        == 403
    )
    login = owner.post(
        "/auth/login", json={"email": "owner-b@example.com", "password": PASSWORD}
    ).json()
    owner.headers["Authorization"] = "Bearer " + login["access_token"]
    assert owner.get(base + "/sources").status_code == 404


def test_upload_digest_and_retry(owner, tmp_path, database):
    base = setup_api(owner, tmp_path)
    data = b"Synthetic clinic hours"
    headers = {
        "Idempotency-Key": "upload-source-key",
        "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        "Content-Type": "application/octet-stream",
    }
    url = base + "/sources/upload?name=Hours&format=txt"
    result = owner.post(url, content=data, headers=headers)
    assert result.status_code == 201, result.text
    assert owner.post(url, content=data, headers=headers).json() == result.json()
    bad = headers | {"Idempotency-Key": "bad-digest-key", "X-Content-SHA256": "0" * 64}
    assert owner.post(url, content=data, headers=bad).status_code == 422
    assert not list(tmp_path.glob("*.tmp"))
    with database.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM knowledge_sources")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM knowledge_jobs")).scalar_one() == 1


def test_raw_auth_before_stream_and_positive_control(owner, tmp_path):
    import asyncio

    import httpx2

    base = setup_api(owner, tmp_path)

    async def run():
        read = []

        async def body():
            read.append(True)
            yield b"Valid synthetic bytes"

        headers = {
            "Idempotency-Key": "stream-check-key",
            "X-Content-SHA256": hashlib.sha256(b"Valid synthetic bytes").hexdigest(),
        }
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=owner.app), base_url="http://test"
        ) as api:
            response = await api.post(
                base + "/sources/upload?name=X&format=txt", headers=headers, content=body()
            )
            assert response.status_code == 401
            assert read == [], "unauthenticated bytes were consumed"
            response = await api.post(
                base + "/sources/upload?name=X&format=txt",
                headers=headers | {"Authorization": owner.headers["Authorization"]},
                content=body(),
            )
            assert response.status_code == 201, response.text
            assert read == [True]

    asyncio.run(run())


def test_upload_rechecks_revoked_manager_before_commit(owner, tmp_path, database, seed):
    import asyncio

    import httpx2

    base = setup_api(owner, tmp_path)

    async def run():
        async def body():
            with database.begin() as db:
                db.execute(
                    text("UPDATE users SET role='staff' WHERE id=:id"), {"id": seed["owner_a"]}
                )
            yield b"Valid bytes"

        headers = {
            "Idempotency-Key": "revoked-stream-key",
            "X-Content-SHA256": hashlib.sha256(b"Valid bytes").hexdigest(),
            "Authorization": owner.headers["Authorization"],
        }
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=owner.app), base_url="http://test"
        ) as api:
            result = await api.post(
                base + "/sources/upload?name=X&format=txt", headers=headers, content=body()
            )
            assert result.status_code == 403, result.text
        with database.connect() as db:
            assert db.execute(text("SELECT count(*) FROM knowledge_sources")).scalar_one() == 0

    asyncio.run(run())


def test_chunked_limits_deadline_cleanup_and_no_sql_lock(owner, tmp_path, monkeypatch, seed):
    import asyncio

    import httpx2

    from knowledge import storage
    from scheduler.db import tenant_context

    base = setup_api(owner, tmp_path)
    agent = base.split("/")[3]

    async def run():
        async def chunks():
            with owner.app.state.session_factory() as db, db.begin():
                tenant_context(db, seed["tenant_a"])
                db.execute(
                    text("SELECT id FROM agents WHERE id=:id FOR UPDATE NOWAIT"), {"id": agent}
                )
            yield b"abc"
            yield b"def"

        async def slow():
            yield b"a"
            await asyncio.sleep(0.2)
            yield b"b"

        headers = {
            "Idempotency-Key": "bounded-stream-key",
            "X-Content-SHA256": hashlib.sha256(b"abcdef").hexdigest(),
            "Authorization": owner.headers["Authorization"],
        }
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=owner.app), base_url="http://test"
        ) as api:
            monkeypatch.setattr(storage, "MAX_BYTES", 5)
            response = await api.post(
                base + "/sources/upload?name=X&format=txt", headers=headers, content=chunks()
            )
            assert response.status_code == 413
            assert not list(tmp_path.glob("*.tmp"))
            monkeypatch.setattr(storage, "UPLOAD_SECONDS", 0.03)
            response = await api.post(
                base + "/sources/upload?name=X&format=txt", headers=headers, content=slow()
            )
            assert response.status_code == 408
            assert not list(tmp_path.glob("*.tmp"))
            assert not list(tmp_path.glob("*.blob"))

    asyncio.run(run())


def test_real_role_rls_composite_ownership_and_worker_privileges(owner, tmp_path, database, seed):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session

    from knowledge.queue import discover_jobs
    from scheduler.db import tenant_context

    base = setup_api(owner, tmp_path)
    created = write(
        owner, base + "/sources", {"kind": "text", "name": "X", "text": "Synthetic", "format": "md"}
    ).json()
    other = write(
        owner,
        base + "/sources",
        {"kind": "text", "name": "Other", "text": "Other", "format": "txt"},
    ).json()
    source_id, agent_id = created["id"], created["agent_id"]
    generation_id = uuid4()
    with database.begin() as db:
        db.execute(
            text(
                "INSERT INTO knowledge_generations(id,tenant_id,agent_id,source_id,source_revision,profile,chunk_count) VALUES(:id,:tenant,:agent,:source,1,:profile,1)"
            ),
            {
                "id": generation_id,
                "tenant": seed["tenant_a"],
                "agent": agent_id,
                "source": other["id"],
                "profile": "a" * 64,
            },
        )
    factory = owner.app.state.session_factory
    with factory() as db, db.begin():
        assert db.scalar(text("SELECT current_user")) == "scheduler_app"
        assert tuple(
            db.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one()
        ) == (False, False)
        tenant_context(db, seed["tenant_b"])
        assert db.execute(text("SELECT id FROM knowledge_sources")).all() == []
    for sql, args in [
        ("SELECT * FROM knowledge_discover_jobs()", {}),
        ("SELECT knowledge_activate_generation(NULL,NULL,1,NULL,NULL,NULL)", {}),
        (
            "UPDATE knowledge_sources SET active_generation_id=:generation WHERE id=:source",
            {"generation": generation_id, "source": source_id},
        ),
        (
            "UPDATE knowledge_sources SET tenant_id=:tenant WHERE id=:source",
            {"tenant": seed["tenant_b"], "source": source_id},
        ),
        (
            "UPDATE knowledge_sources SET agent_id=:agent WHERE id=:source",
            {"agent": uuid4(), "source": source_id},
        ),
    ]:
        with pytest.raises(DBAPIError), factory() as db, db.begin():
            tenant_context(db, seed["tenant_a"])
            db.execute(text(sql), args)
    with database.connect() as connection, connection.begin():
        connection.execute(text("SET LOCAL ROLE knowledge_worker"))
        with Session(bind=connection) as db:
            assert db.scalar(text("SELECT current_user")) == "knowledge_worker"
            assert tuple(
                db.execute(
                    text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                ).one()
            ) == (False, False)
            routing = discover_jobs(db)
            assert len(routing) == 2
            assert set(routing[0]) == {"tenant_id", "agent_id", "source_id", "job_id"}
            assert db.execute(text("SELECT id FROM knowledge_sources")).all() == []
            tenant_context(db, seed["tenant_a"])
            assert len(db.execute(text("SELECT id FROM knowledge_sources")).all()) == 2
            for table in [
                "patients",
                "appointments",
                "users",
                "agent_versions",
                "agent_sessions",
                "idempotency_records",
            ]:
                with pytest.raises(DBAPIError), db.begin_nested():
                    db.execute(text(f"SELECT * FROM {table}"))
            with pytest.raises(DBAPIError), db.begin_nested():
                db.execute(text("UPDATE knowledge_generations SET chunk_count=2"))


def test_worker_lease_replacement_delete_and_recovery_fence(owner, tmp_path, database, seed):
    from sqlalchemy.orm import Session

    from knowledge.queue import claim_job, discover_jobs, lease_current
    from scheduler.db import tenant_context

    base = setup_api(owner, tmp_path)
    row = write(
        owner, base + "/sources", {"kind": "url", "name": "X", "url": "https://example.com"}
    ).json()

    def worker(action):
        with database.connect() as connection, connection.begin():
            connection.execute(text("SET LOCAL ROLE knowledge_worker"))
            with Session(bind=connection) as db:
                tenant_context(db, seed["tenant_a"])
                result = action(db)
                db.flush()
                return result

    route = worker(lambda db: discover_jobs(db)[0])
    lease = worker(lambda db: claim_job(db, route["source_id"], route["job_id"]))
    assert lease is not None
    assert worker(lambda db: claim_job(db, route["source_id"], route["job_id"])) is None
    assert worker(
        lambda db: lease_current(db, route["source_id"], route["job_id"], 1, lease["lease_token"])
    )
    with database.begin() as db:
        db.execute(text("UPDATE knowledge_jobs SET lease_until=now()-interval '1 second'"))
    replacement = worker(lambda db: claim_job(db, route["source_id"], route["job_id"]))
    assert replacement["lease_token"] != lease["lease_token"]
    assert not worker(
        lambda db: lease_current(db, route["source_id"], route["job_id"], 1, lease["lease_token"])
    )
    url = base + "/sources/" + row["id"]
    changed = write(owner, url + "/reindex", {"revision": 1}, "reindex-source-key")
    assert changed.status_code == 200, changed.text
    assert (
        write(owner, url + "/reindex", {"revision": 1}, "reindex-source-key").json()
        == changed.json()
    )
    assert not worker(
        lambda db: lease_current(
            db, route["source_id"], route["job_id"], 1, replacement["lease_token"]
        )
    )
    route2 = worker(lambda db: discover_jobs(db)[0])
    lease2 = worker(lambda db: claim_job(db, route2["source_id"], route2["job_id"]))
    assert write(owner, url + "?revision=2", method="delete").status_code == 200
    assert not worker(
        lambda db: lease_current(
            db, route2["source_id"], route2["job_id"], 2, lease2["lease_token"]
        )
    )
    assert worker(discover_jobs) == []


def test_source_capacity_rejects_65th(owner, tmp_path, database, seed):
    base = setup_api(owner, tmp_path)
    agent = base.split("/")[3]
    with database.begin() as db:
        db.execute(
            text(
                "INSERT INTO knowledge_sources(tenant_id,agent_id,kind,name) SELECT :tenant,:agent,'text','Synthetic' FROM generate_series(1,64)"
            ),
            {"tenant": seed["tenant_a"], "agent": agent},
        )
    assert (
        write(
            owner,
            base + "/sources",
            {"kind": "text", "name": "Extra", "text": "Extra", "format": "txt"},
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "text", "name": "x" * 201, "text": "x", "format": "txt"},
        {"kind": "text", "name": "X", "text": "x" * 1_000_001, "format": "txt"},
        {"kind": "faq", "name": "X", "question": "x" * 1001, "answer": "x"},
        {"kind": "faq", "name": "X", "question": "x", "answer": "x" * 100_001},
        {"kind": "url", "name": "X", "url": "https://example.com/" + "x" * 2048},
        {"kind": "url", "name": "X", "url": "https://user:secret@example.com"},
    ],
)
def test_source_field_upper_bounds(owner, tmp_path, body):
    base = setup_api(owner, tmp_path)
    assert write(owner, base + "/sources", body).status_code == 422


def test_file_revision_recheck_during_stream_and_retry(owner, tmp_path, database):
    import asyncio

    import httpx2

    base = setup_api(owner, tmp_path)
    data = b"First bytes"

    def headers(key, data):
        return {
            "Idempotency-Key": key,
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
            "Authorization": owner.headers["Authorization"],
        }

    first = owner.post(
        base + "/sources/upload?name=X&format=txt",
        content=data,
        headers=headers("file-first-key", data),
    ).json()
    url = base + "/sources/" + first["id"]

    async def run():
        async def body():
            yield b"New "
            assert write(owner, url + "/reindex", {"revision": 1}).status_code == 200
            yield b"bytes"

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=owner.app), base_url="http://test"
        ) as api:
            response = await api.put(
                url + "/content?revision=1&name=X&format=txt",
                content=body(),
                headers=headers("file-stale-key", b"New bytes"),
            )
            assert response.status_code == 409, response.text

    asyncio.run(run())
    changed = owner.put(
        url + "/content?revision=2&name=X&format=md",
        content=b"New bytes",
        headers=headers("file-replace-key", b"New bytes"),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 3
    replay = owner.put(
        url + "/content?revision=2&name=X&format=md",
        content=b"New bytes",
        headers=headers("file-replace-key", b"New bytes"),
    )
    assert replay.json() == changed.json()
    with database.connect() as db:
        assert db.execute(text("SELECT count(*) FROM knowledge_sources")).scalar_one() == 1
        assert db.execute(text("SELECT count(*) FROM knowledge_jobs")).scalar_one() == 3
        key = db.execute(text("SELECT blob_key FROM knowledge_sources")).scalar_one()
        assert first["id"].replace("-", "") in key


def test_storage_quota_reserve_symlinks_and_disconnect(owner, tmp_path, monkeypatch):
    import asyncio

    import httpx2

    from knowledge import storage

    base = setup_api(owner, tmp_path)
    data = b"abc"
    headers = {
        "Idempotency-Key": "capacity-upload-key",
        "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
    }
    url = base + "/sources/upload?name=X&format=txt"
    monkeypatch.setattr(storage, "QUOTA_BYTES", 2)
    assert owner.post(url, content=data, headers=headers).status_code == 507
    monkeypatch.setattr(storage, "QUOTA_BYTES", 1024**3)
    monkeypatch.setattr(storage, "DISK_RESERVE", 1024**6)
    assert owner.post(url, content=data, headers=headers).status_code == 507
    monkeypatch.setattr(storage, "DISK_RESERVE", 0)
    (tmp_path / "unexpected-link").symlink_to("/tmp")
    assert owner.post(url, content=data, headers=headers).status_code == 503
    (tmp_path / "unexpected-link").unlink()

    async def run():
        async def body():
            yield b"a"
            raise RuntimeError("synthetic disconnection")

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=owner.app), base_url="http://test"
        ) as api:
            with pytest.raises((RuntimeError, ExceptionGroup)):
                await api.post(
                    url,
                    headers=headers | {"Authorization": owner.headers["Authorization"]},
                    content=body(),
                )

    asyncio.run(run())
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.blob"))


def test_api_role_cannot_change_source_kind(owner, tmp_path, seed):
    from sqlalchemy.exc import DBAPIError

    from scheduler.db import tenant_context

    base = setup_api(owner, tmp_path)
    row = write(
        owner, base + "/sources", {"kind": "text", "name": "X", "text": "x", "format": "txt"}
    ).json()
    with pytest.raises(DBAPIError), owner.app.state.session_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        db.execute(text("UPDATE knowledge_sources SET kind='url' WHERE id=:id"), {"id": row["id"]})


def test_preview_is_unavailable_until_actual_generation_and_contract_has_binary(owner, tmp_path):
    from pydantic import ValidationError

    from knowledge.contracts import RetrievalQuery

    base = setup_api(owner, tmp_path)
    row = write(
        owner, base + "/sources", {"kind": "text", "name": "X", "text": "x", "format": "txt"}
    ).json()
    preview = owner.get(base + "/sources/" + row["id"] + "/preview").json()
    assert preview == {
        "source_id": row["id"],
        "available": False,
        "reason": "processing_unavailable",
        "excerpts": [],
    }
    for query in ("", "  ", "x" * 1001):
        with pytest.raises(ValidationError):
            RetrievalQuery(query=query)
    assert RetrievalQuery(query=" سوال ").query == "سوال"
    schema = owner.get("/openapi.json").json()
    for path, method in [
        ("/dashboard/agents/{agent_id}/knowledge/sources/upload", "post"),
        ("/dashboard/agents/{agent_id}/knowledge/sources/{source_id}/content", "put"),
    ]:
        assert (
            schema["paths"][path][method]["requestBody"]["content"]["application/octet-stream"][
                "schema"
            ]["format"]
            == "binary"
        )
    assert "blob_key" not in schema["components"]["schemas"]["SourceOut"]["properties"]


def test_unknown_retry_concurrently_creates_one_source_and_job(owner, tmp_path, database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import event

    @event.listens_for(owner.app.state.engine, "begin")
    def bounded_test_lock(connection):
        connection.exec_driver_sql("SET LOCAL lock_timeout='2s'")

    base = setup_api(owner, tmp_path)
    barrier = Barrier(2)
    data = b"Synthetic unknown outcome retry"
    headers = {
        "Idempotency-Key": "same-concurrent-upload",
        "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
    }

    def upload(_):
        barrier.wait(timeout=5)
        return owner.post(base + "/sources/upload?name=X&format=txt", content=data, headers=headers)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(upload, range(2)))
    assert [r.status_code for r in results] == [201, 201]
    assert results[0].json() == results[1].json()
    with database.connect() as db:
        assert db.execute(text("SELECT count(*) FROM knowledge_sources")).scalar_one() == 1
        assert db.execute(text("SELECT count(*) FROM knowledge_jobs")).scalar_one() == 1


def test_exhausted_expired_lease_becomes_failed(owner, tmp_path, database, seed):
    from sqlalchemy.orm import Session

    from knowledge.queue import claim_job, discover_jobs
    from scheduler.db import tenant_context

    base = setup_api(owner, tmp_path)
    write(owner, base + "/sources", {"kind": "text", "name": "X", "text": "x", "format": "txt"})

    def worker(action):
        with database.connect() as connection, connection.begin():
            connection.execute(text("SET LOCAL ROLE knowledge_worker"))
            with Session(bind=connection) as db:
                tenant_context(db, seed["tenant_a"])
                result = action(db)
                db.flush()
                return result

    route = worker(lambda db: discover_jobs(db)[0])
    for _ in range(3):
        assert worker(lambda db: claim_job(db, route["source_id"], route["job_id"])) is not None
        with database.begin() as db:
            db.execute(text("UPDATE knowledge_jobs SET lease_until=now()-interval '1 second'"))
    assert len(worker(discover_jobs)) == 1, (
        "final crashed lease must remain discoverable for terminal recovery"
    )
    assert worker(lambda db: claim_job(db, route["source_id"], route["job_id"])) is None
    with database.connect() as db:
        assert db.execute(text("SELECT status,attempts FROM knowledge_jobs")).one() == ("failed", 3)
        assert db.execute(text("SELECT status,error_code FROM knowledge_sources")).one() == (
            "failed",
            "lease_exhausted",
        )


def test_reindex_retains_previous_generation_until_delete(owner, tmp_path, database, seed):
    base = setup_api(owner, tmp_path)
    row = write(
        owner,
        base + "/sources",
        {"kind": "text", "name": "X", "text": "Synthetic reference", "format": "txt"},
    ).json()
    identity = uuid4()
    with database.begin() as db:
        db.execute(
            text(
                "INSERT INTO knowledge_generations(id,tenant_id,agent_id,source_id,source_revision,profile,chunk_count) VALUES(:id,:tenant,:agent,:source,1,:profile,1)"
            ),
            {
                "id": identity,
                "tenant": seed["tenant_a"],
                "agent": row["agent_id"],
                "source": row["id"],
                "profile": "a" * 64,
            },
        )
        db.execute(
            text(
                "UPDATE knowledge_sources SET status='ready',active_generation_id=:generation WHERE id=:source"
            ),
            {"generation": identity, "source": row["id"]},
        )
    url = base + "/sources/" + row["id"]
    reindexed = write(owner, url + "/reindex", {"revision": 1}).json()
    assert reindexed["status"] == "queued" and reindexed["available"] is True
    assert reindexed["active_generation_id"] == str(identity) and reindexed["chunk_count"] == 1
    deleted = write(owner, url + "?revision=2", method="delete").json()
    assert deleted["available"] is False and deleted["active_generation_id"] is None


def test_query_event_redacted_question_is_nullable_and_bounded(owner, tmp_path, seed):
    from sqlalchemy.exc import DBAPIError

    from scheduler.db import tenant_context

    base = setup_api(owner, tmp_path)
    fields = {"tenant": seed["tenant_a"], "agent": base.split("/")[3], "hash": "a" * 64}
    sql = text(
        "INSERT INTO knowledge_query_events(tenant_id,agent_id,command_id,query_hash,language,purpose,decision,duration_ms,redacted_question) VALUES(:tenant,:agent,:command,:hash,'en','preview','unavailable',1,:question)"
    )
    with owner.app.state.session_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        for question in (None, "Synthetic safe question", "x" * 1000):
            db.execute(sql, fields | {"command": uuid4(), "question": question})
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(sql, fields | {"command": uuid4(), "question": "x" * 1001})
        assert db.scalar(text("SELECT count(*) FROM knowledge_query_events")) == 3


def test_worker_activation_complete_fenced_and_permanently_sealed(owner, tmp_path, database, seed):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session

    from knowledge.models import Chunk, Generation
    from knowledge.profile import PROFILE
    from knowledge.queue import activate_generation, claim_job, discover_jobs
    from scheduler.db import tenant_context

    base = setup_api(owner, tmp_path)
    source = write(
        owner,
        base + "/sources",
        {"kind": "text", "name": "X", "text": "Synthetic", "format": "txt"},
    ).json()
    source_id = source["id"]

    def worker(action):
        with database.connect() as connection, connection.begin():
            connection.execute(text("SET LOCAL ROLE knowledge_worker"))
            with Session(bind=connection) as db:
                tenant_context(db, seed["tenant_a"])
                result = action(db)
                db.flush()
                return result

    def chunk(db, generation, ordinal):
        db.add(
            Chunk(
                tenant_id=seed["tenant_a"],
                agent_id=source["agent_id"],
                source_id=source_id,
                generation_id=generation,
                ordinal=ordinal,
                heading="Synthetic",
                language="en",
                text="Synthetic safe evidence",
                token_count=4,
                embedding=[1.0] + [0.0] * 1023,
            )
        )
        db.flush()

    def stage(db, revision, count=2, profile=PROFILE.fingerprint):
        generation = Generation(
            tenant_id=seed["tenant_a"],
            agent_id=source["agent_id"],
            source_id=source_id,
            source_revision=revision,
            profile=profile,
            chunk_count=count,
        )
        db.add(generation)
        db.flush()
        chunk(db, generation.id, 0)
        return generation.id

    def activate(db, route, lease, generation, **kwargs):
        return activate_generation(
            db,
            route["source_id"],
            route["job_id"],
            lease["source_revision"],
            lease["lease_token"],
            generation,
            **kwargs,
        )

    route = worker(lambda db: discover_jobs(db)[0])
    lease = worker(lambda db: claim_job(db, route["source_id"], route["job_id"]))
    generation = worker(lambda db: stage(db, 1))
    assert worker(lambda db: activate(db, route, lease, generation)) is False  # incomplete
    worker(lambda db: chunk(db, generation, 1))
    assert (
        worker(lambda db: activate(db, route, lease | {"lease_token": uuid4()}, generation))
        is False
    )
    assert worker(lambda db: activate(db, route, lease, generation, profile="b" * 64)) is False
    assert worker(lambda db: activate(db, route, lease, generation)) is True
    url = base + "/sources/" + source_id
    assert owner.get(url).json()["available"] is True
    with pytest.raises(DBAPIError):
        worker(lambda db: chunk(db, generation, 2))
    # A replacement seals its predecessor permanently, not just while it is active.
    assert write(owner, url + "/reindex", {"revision": 1}).status_code == 200
    route2 = worker(lambda db: discover_jobs(db)[0])
    lease2 = worker(lambda db: claim_job(db, route2["source_id"], route2["job_id"]))
    assert worker(lambda db: activate(db, route2, lease2, generation)) is False  # wrong revision
    next_generation = worker(lambda db: stage(db, 2, 1))
    assert worker(lambda db: activate(db, route2, lease2, next_generation)) is True
    with pytest.raises(DBAPIError):
        worker(lambda db: chunk(db, generation, 3))
    # Staged work cannot activate after deletion even with the once-valid lease.
    assert write(owner, url + "/reindex", {"revision": 2}).status_code == 200
    route3 = worker(lambda db: discover_jobs(db)[0])
    lease3 = worker(lambda db: claim_job(db, route3["source_id"], route3["job_id"]))
    third = worker(lambda db: stage(db, 3, 1))
    assert write(owner, url + "?revision=3", method="delete").status_code == 200
    assert worker(lambda db: activate(db, route3, lease3, third)) is False


def test_activation_checks_database_expiry_after_generation_lock(owner, tmp_path, database, seed):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy.orm import Session

    from knowledge.models import Chunk, Generation
    from knowledge.profile import PROFILE
    from knowledge.queue import activate_generation, claim_job, discover_jobs
    from scheduler.db import tenant_context

    base = setup_api(owner, tmp_path)
    source = write(
        owner,
        base + "/sources",
        {"kind": "text", "name": "X", "text": "Synthetic", "format": "txt"},
    ).json()

    def worker(action):
        with database.connect() as connection, connection.begin():
            connection.execute(text("SET LOCAL ROLE knowledge_worker"))
            with Session(bind=connection) as db:
                tenant_context(db, seed["tenant_a"])
                result = action(db)
                db.flush()
                return result

    route = worker(lambda db: discover_jobs(db)[0])
    lease = worker(lambda db: claim_job(db, route["source_id"], route["job_id"]))

    def stage(db):
        row = Generation(
            tenant_id=seed["tenant_a"],
            agent_id=source["agent_id"],
            source_id=source["id"],
            source_revision=1,
            profile=PROFILE.fingerprint,
            chunk_count=1,
        )
        db.add(row)
        db.flush()
        db.add(
            Chunk(
                tenant_id=seed["tenant_a"],
                agent_id=source["agent_id"],
                source_id=source["id"],
                generation_id=row.id,
                ordinal=0,
                language="en",
                text="Synthetic",
                token_count=1,
                embedding=[1.0] + [0.0] * 1023,
            )
        )
        db.flush()
        return row.id

    generation = worker(stage)
    entered = Event()
    pid = []

    def activate(db):
        pid.append(db.scalar(text("SELECT pg_backend_pid()")))
        entered.set()
        return activate_generation(
            db, route["source_id"], route["job_id"], 1, lease["lease_token"], generation
        )

    with database.begin() as db:
        db.execute(
            text(
                "UPDATE knowledge_jobs SET lease_until=clock_timestamp()+interval '400 milliseconds'"
            )
        )
    with ThreadPoolExecutor(1) as pool:
        with database.begin() as hold:
            hold.execute(
                text("SELECT id FROM knowledge_generations WHERE id=:id FOR UPDATE"),
                {"id": generation},
            )
            pending = pool.submit(worker, activate)
            assert entered.wait(2)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                wait = hold.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": pid[0]},
                )
                if wait == "Lock":
                    break
                time.sleep(0.01)
            assert wait == "Lock", "activation did not reach the held generation lock"
            time.sleep(0.45)
        assert pending.result(timeout=3) is False
    with database.connect() as db:
        assert (
            db.scalar(
                text("SELECT sealed_at FROM knowledge_generations WHERE id=:id"), {"id": generation}
            )
            is None
        )
        assert (
            db.scalar(
                text("SELECT active_generation_id FROM knowledge_sources WHERE id=:id"),
                {"id": source["id"]},
            )
            is None
        )


def test_unavailable_blob_root_returns_safe_error(owner, tmp_path):
    import asyncio

    import httpx2

    base = setup_api(owner, tmp_path)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("Synthetic existing file")
    owner.app.state.settings.knowledge_blob_root = blocked

    async def run():
        headers = {
            "Idempotency-Key": "unavailable-root-key",
            "X-Content-SHA256": hashlib.sha256(b"abc").hexdigest(),
            "Authorization": owner.headers["Authorization"],
        }
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=owner.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as api:
            result = await api.post(
                base + "/sources/upload?name=X&format=txt", headers=headers, content=b"abc"
            )
            assert result.status_code == 503, result.text
            assert result.json()["error"]["code"] == "storage_unavailable"
            assert str(blocked) not in result.text
        assert blocked.read_text() == "Synthetic existing file"

    asyncio.run(run())
