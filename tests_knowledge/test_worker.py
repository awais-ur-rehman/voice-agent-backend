"""Lifecycle races use actual worker credentials against voice_agent_test only."""

import hashlib
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from knowledge.embedding import EmbeddingUnavailable
from knowledge.models import Job, Source
from knowledge.queue import claim_job, discover_jobs
from knowledge.storage import BlobStore
from scheduler.db import tenant_context
from tests_api.conftest import ADMIN_URL
from tests_knowledge.test_chunking import tokenizer
from tests_knowledge.test_sources import setup_api, write


@pytest.fixture
def worker_factory(database):
    from scripts.init_db import init_database

    assert make_url(ADMIN_URL).database == "voice_agent_test"
    init_database(
        ADMIN_URL,
        os.environ.get("TEST_APP_PASSWORD", "phase1_app_test_only"),
        "phase5_worker_test_only",
    )
    url = make_url(ADMIN_URL).set(username="knowledge_worker", password="phase5_worker_test_only")
    engine = create_engine(url, hide_parameters=True)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def source(owner, tmp_path):
    base = setup_api(owner, tmp_path)
    row = write(
        owner,
        base + "/sources",
        {"kind": "text", "name": "Hours", "text": "Open weekdays", "format": "txt"},
    ).json()
    return base, row


def claim(factory, tenant, source_id=None):
    with factory() as db, db.begin():
        route = next(
            r
            for r in discover_jobs(db)
            if source_id is None or r["source_id"] == UUID(str(source_id))
        )
        tenant_context(db, tenant)
        assert db.scalar(text("SELECT current_user")) == "knowledge_worker"
        return claim_job(db, route["source_id"], route["job_id"])


class Embeddings:
    def __init__(self, barrier=None, fail=False, ready=True):
        self.barrier, self.fail, self.ready = barrier, fail, ready

    def available(self):
        return self.ready

    def embed(self, texts, *, deadline):
        assert len(texts) == 1 and deadline > time.monotonic()
        if self.barrier:
            self.barrier()
        if self.fail:
            raise EmbeddingUnavailable("rpc_unavailable")
        return [[1.0] + [0.0] * 1023]


def state(factory, tenant, source_id):
    with factory() as db, db.begin():
        tenant_context(db, tenant)
        s = db.get(Source, UUID(str(source_id)))
        return s.status, s.active_generation_id, s.error_code


def process(factory, store, embeddings, lease):
    from knowledge_worker.runner import process_job

    return process_job(
        factory, store, embeddings, lease, clock=time.monotonic, tokenizer=tokenizer()
    )


def test_worker_stages_complete_generation_with_restricted_role(
    owner, tmp_path, seed, worker_factory
):
    base, row = source(owner, tmp_path)
    lease = claim(worker_factory, seed["tenant_a"])
    assert process(worker_factory, BlobStore(tmp_path), Embeddings(), lease) == "ready"
    result = owner.get(base + "/sources/" + row["id"]).json()
    assert result["available"] and result["status"] == "ready" and result["chunk_count"] == 1
    with worker_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        assert db.scalar(text("SELECT sealed_at IS NOT NULL FROM knowledge_generations")) is True
        assert db.scalar(text("SELECT text FROM knowledge_chunks")) == "Open weekdays"
        assert db.scalar(text("SELECT status FROM knowledge_jobs")) == "ready"


def test_unavailable_does_not_claim_load_tokenizer_or_exhaust_jobs(
    owner, tmp_path, seed, worker_factory
):
    from knowledge_worker.runner import run_once

    _, row = source(owner, tmp_path)
    for _ in range(4):
        assert (
            run_once(
                worker_factory,
                BlobStore(tmp_path),
                Embeddings(ready=False),
                tokenizer_loader=lambda: pytest.fail("unavailable tokenizer load"),
            )
            == 0
        )
    with worker_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        job = db.scalar(select(Job))
        assert (job.status, job.attempts, job.lease_token) == ("queued", 0, None)
    assert state(worker_factory, seed["tenant_a"], row["id"]) == ("queued", None, None)


def test_old_ready_generation_survives_reindex_failure(owner, tmp_path, seed, worker_factory):
    base, row = source(owner, tmp_path)
    assert (
        process(
            worker_factory,
            BlobStore(tmp_path),
            Embeddings(),
            claim(worker_factory, seed["tenant_a"]),
        )
        == "ready"
    )
    old = state(worker_factory, seed["tenant_a"], row["id"])[1]
    assert (
        write(owner, base + "/sources/" + row["id"] + "/reindex", {"revision": 1}).status_code
        == 200
    )
    lease = claim(worker_factory, seed["tenant_a"])
    assert process(worker_factory, BlobStore(tmp_path), Embeddings(fail=True), lease) == "queued"
    assert state(worker_factory, seed["tenant_a"], row["id"]) == (
        "queued",
        old,
        "embedding_unavailable",
    )
    with worker_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        assert db.scalar(text("SELECT count(*) FROM knowledge_generations")) == 1


@pytest.mark.parametrize("action", ["delete", "reindex", "takeover"])
def test_late_worker_cannot_stage_or_activate_after_barrier(
    owner, tmp_path, seed, worker_factory, database, action
):
    base, row = source(owner, tmp_path)
    lease = claim(worker_factory, seed["tenant_a"])
    entered, release = threading.Event(), threading.Event()

    def barrier():
        entered.set()
        assert release.wait(10)

    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            process(worker_factory, BlobStore(tmp_path), Embeddings(barrier), lease)
        )
    )
    worker.start()
    try:
        assert entered.wait(10)
        # A separate connection obtains Source lock during inference: no SQL locks leak.
        with database.begin() as db:
            db.execute(
                text("SELECT id FROM knowledge_sources WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": row["id"]},
            )
        if action == "delete":
            assert (
                write(
                    owner, base + "/sources/" + row["id"] + "?revision=1", method="delete"
                ).status_code
                == 200
            )
        elif action == "reindex":
            assert (
                write(
                    owner, base + "/sources/" + row["id"] + "/reindex", {"revision": 1}
                ).status_code
                == 200
            )
        else:
            with database.begin() as db:
                db.execute(
                    text(
                        "UPDATE knowledge_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
                    ),
                    {"id": lease["job_id"]},
                )
            replacement = claim(worker_factory, seed["tenant_a"])
            assert replacement["lease_token"] != lease["lease_token"]
            assert (
                process(worker_factory, BlobStore(tmp_path), Embeddings(), replacement) == "ready"
            )
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and results == ["stale"]
    with database.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM knowledge_generations")) == (
            1 if action == "takeover" else 0
        )
        assert db.scalar(text("SELECT count(*) FROM knowledge_chunks")) == (
            1 if action == "takeover" else 0
        )


def test_two_workers_only_one_claim_and_one_current_generation(
    owner, tmp_path, seed, worker_factory
):
    _, row = source(owner, tmp_path)
    with worker_factory() as db, db.begin():
        route = discover_jobs(db)[0]
    start = threading.Barrier(2)
    results = []

    def work():
        start.wait()
        with worker_factory() as db, db.begin():
            tenant_context(db, seed["tenant_a"])
            lease = claim_job(db, route["source_id"], route["job_id"])
        results.append(
            process(worker_factory, BlobStore(tmp_path), Embeddings(), lease)
            if lease
            else "unclaimed"
        )

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == ["ready", "unclaimed"]
    assert state(worker_factory, seed["tenant_a"], row["id"])[0] == "ready"


def test_fenced_renew_failure_and_expired_third_attempt(
    owner, tmp_path, seed, worker_factory, database
):
    from knowledge_worker.runner import fail_job, renew_lease

    _, row = source(owner, tmp_path)
    first = claim(worker_factory, seed["tenant_a"])
    assert renew_lease(worker_factory, first)
    with database.begin() as db:
        db.execute(
            text(
                "UPDATE knowledge_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
            ),
            {"id": first["job_id"]},
        )
    second = claim(worker_factory, seed["tenant_a"])
    assert not renew_lease(worker_factory, first)
    assert fail_job(worker_factory, first, "embedding_unavailable", retryable=True) == "stale"
    with database.begin() as db:
        db.execute(
            text(
                "UPDATE knowledge_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
            ),
            {"id": first["job_id"]},
        )
    third = claim(worker_factory, seed["tenant_a"])
    assert third["lease_token"] != second["lease_token"]
    with database.begin() as db:
        db.execute(
            text(
                "UPDATE knowledge_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
            ),
            {"id": first["job_id"]},
        )
    assert claim(worker_factory, seed["tenant_a"]) is None
    assert state(worker_factory, seed["tenant_a"], row["id"]) == ("failed", None, "lease_exhausted")


def test_blob_read_checks_ownership_checksum_symlink_and_size(tmp_path):
    from knowledge.parsing import IngestionError

    tenant, source_id = uuid4(), uuid4()
    store = BlobStore(tmp_path)
    key = f"{tenant.hex}-{source_id.hex}-1-{uuid4().hex}.blob"
    data = b"Synthetic private bytes"
    (tmp_path / key).write_bytes(data)
    checksum = hashlib.sha256(data).hexdigest()
    assert store.read(key, tenant, source_id, 1, checksum) == data
    for args in [
        (key, uuid4(), source_id, 1, checksum),
        ("../escape", tenant, source_id, 1, checksum),
        (key, tenant, source_id, 2, checksum),
        (key, tenant, source_id, 1, "0" * 64),
    ]:
        with pytest.raises(IngestionError):
            store.read(*args)
    (tmp_path / key).unlink()
    (tmp_path / key).symlink_to("/etc/hosts")
    with pytest.raises(IngestionError):
        store.read(key, tenant, source_id, 1, checksum)


def test_cleanup_only_unreferenced_owned_blobs_after_grace(
    owner, tmp_path, seed, worker_factory, database
):
    from knowledge_worker.cleanup import cleanup_blobs

    base, row = source(owner, tmp_path)
    tenant = seed["tenant_a"]
    sid = UUID(row["id"])

    def blob(t, s, revision=1, age=25 * 3600):
        key = f"{t.hex}-{s.hex}-{revision}-{uuid4().hex}.blob"
        path = tmp_path / key
        path.write_bytes(b"bytes")
        os.utime(path, (time.time() - age,) * 2)
        if t == tenant and s == sid:
            store = BlobStore(
                tmp_path,
                secret="isolated-blob-ownership-secret-for-tests-only",
                clock=lambda: time.time() - age,
            )
            fd = os.open(path, os.O_RDONLY)
            try:
                store._write_receipt(key, fd, hashlib.sha256(b"bytes").hexdigest())
            finally:
                os.close(fd)
        return key

    current = blob(tenant, sid)
    orphan = blob(tenant, sid)
    recent = blob(tenant, sid, age=30)
    foreign = blob(seed["tenant_b"], sid)
    unknown = blob(tenant, uuid4())
    link = blob(tenant, sid)
    (tmp_path / link).unlink()
    (tmp_path / link).symlink_to(tmp_path / current)
    with database.begin() as db:
        db.execute(
            text("UPDATE knowledge_sources SET blob_key=:key WHERE id=:id"),
            {"key": current, "id": sid},
        )
    store = BlobStore(tmp_path, secret="isolated-blob-ownership-secret-for-tests-only")
    now = datetime.now(UTC)
    assert cleanup_blobs(worker_factory, store, now=now) == 0
    assert all((tmp_path / k).exists() for k in [current, orphan, recent, foreign, unknown, link])
    assert (
        write(owner, base + "/sources/" + row["id"] + "?revision=1", method="delete").status_code
        == 200
    )
    assert cleanup_blobs(worker_factory, store, now=now) == 0
    # First observation after deletion starts retirement; an old creation time is insufficient.
    assert cleanup_blobs(worker_factory, store, now=now + timedelta(hours=25)) == 0
    assert cleanup_blobs(worker_factory, store, now=now + timedelta(hours=50)) == 3
    assert not any((tmp_path / k).exists() for k in [current, orphan, recent])
    assert (tmp_path / foreign).exists() and (tmp_path / unknown).exists()
    assert (tmp_path / link).is_symlink()


def test_file_reindex_reuses_verified_original_revision_blob(owner, tmp_path, seed, worker_factory):
    base = setup_api(owner, tmp_path)
    data = b"Synthetic original file"
    row = owner.post(
        base + "/sources/upload?name=File&format=txt",
        content=data,
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
    ).json()
    assert (
        process(
            worker_factory,
            BlobStore(tmp_path),
            Embeddings(),
            claim(worker_factory, seed["tenant_a"]),
        )
        == "ready"
    )
    assert (
        write(owner, base + "/sources/" + row["id"] + "/reindex", {"revision": 1}).status_code
        == 200
    )
    assert (
        process(
            worker_factory,
            BlobStore(tmp_path),
            Embeddings(),
            claim(worker_factory, seed["tenant_a"]),
        )
        == "ready"
    )
    assert owner.get(base + "/sources/" + row["id"]).json()["revision"] == 2


def test_invalid_parser_document_never_stages_partial_generation(
    owner, tmp_path, seed, worker_factory
):
    base = setup_api(owner, tmp_path)
    data = b"not a pdf"
    row = owner.post(
        base + "/sources/upload?name=File&format=pdf",
        content=data,
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
    ).json()
    assert (
        process(
            worker_factory,
            BlobStore(tmp_path),
            Embeddings(),
            claim(worker_factory, seed["tenant_a"]),
        )
        == "failed"
    )
    assert state(worker_factory, seed["tenant_a"], row["id"]) == ("failed", None, "invalid_format")
    with worker_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        assert db.scalar(text("SELECT count(*) FROM knowledge_generations")) == 0


def test_final_activation_rejection_rolls_back_staging(
    owner, tmp_path, seed, worker_factory, monkeypatch
):
    import knowledge.activation as activation

    _, row = source(owner, tmp_path)
    # Boundary rejects after inserts; no unsealed generation or chunks may survive.
    monkeypatch.setattr(activation, "activate_generation", lambda *a, **k: False)
    assert (
        process(
            worker_factory,
            BlobStore(tmp_path),
            Embeddings(),
            claim(worker_factory, seed["tenant_a"]),
        )
        == "stale"
    )
    with worker_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        assert db.scalar(text("SELECT count(*) FROM knowledge_generations")) == 0
        assert db.scalar(text("SELECT count(*) FROM knowledge_chunks")) == 0
    assert state(worker_factory, seed["tenant_a"], row["id"])[1] is None


def test_upload_without_cleanup_secret_fails_closed_but_text_still_works(owner, tmp_path):
    base = setup_api(owner, tmp_path)
    owner.app.state.settings.knowledge_blob_secret = None
    data = b"private test bytes"
    response = owner.post(
        base + "/sources/upload?name=File&format=txt",
        content=data,
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert response.status_code == 503
    assert not list(tmp_path.glob("*.blob"))
    assert (
        write(
            owner,
            base + "/sources",
            {"kind": "text", "name": "Hours", "text": "Open", "format": "txt"},
        ).status_code
        == 201
    )


def test_authenticated_orphan_cleanup_rejects_tamper_and_held_lock(tmp_path, worker_factory):
    import asyncio
    import json

    from knowledge_worker.cleanup import cleanup_blobs

    secret = "isolated-blob-ownership-secret-for-tests-only"
    store = BlobStore(tmp_path, secret=secret, clock=lambda: time.time() - 25 * 3600)
    tenant, sid = uuid4(), uuid4()

    async def upload():
        async def stream():
            yield b"Synthetic orphan"

        return await store.receive(
            stream(), tenant, sid, 1, hashlib.sha256(b"Synthetic orphan").hexdigest()
        )

    pending = asyncio.run(upload())
    os.utime(tmp_path / pending.key, (time.time() - 25 * 3600,) * 2)
    assert cleanup_blobs(worker_factory, store) == 0, "in-flight metadata owns the file lock"
    pending.release()
    receipt = tmp_path / (pending.key + ".receipt")
    original = receipt.read_bytes()
    changed = json.loads(original)
    changed["payload"]["tenant_id"] = uuid4().hex
    receipt.write_text(json.dumps(changed))
    assert cleanup_blobs(worker_factory, store) == 0
    receipt.write_bytes(original)
    assert (
        cleanup_blobs(
            worker_factory, BlobStore(tmp_path, secret="wrong-secret-that-is-long-enough-for-tests")
        )
        == 0
    )
    (tmp_path / pending.key).write_bytes(b"Tampered contents")
    assert cleanup_blobs(worker_factory, store) == 0
    (tmp_path / pending.key).write_bytes(b"Synthetic orphan")
    os.utime(tmp_path / pending.key, (time.time() - 25 * 3600,) * 2)
    assert cleanup_blobs(worker_factory, store) == 0
    assert cleanup_blobs(worker_factory, store, now=datetime.now(UTC) + timedelta(hours=25)) == 1
    assert not receipt.exists() and not (tmp_path / pending.key).exists()


def test_upload_lock_is_held_until_outer_sql_transaction_finishes(
    owner, tmp_path, worker_factory, monkeypatch
):
    import knowledge.routes as routes
    from knowledge_worker.cleanup import cleanup_blobs

    base = setup_api(owner, tmp_path)
    original = routes.sources.mutate

    def inspect(db, *args, **kwargs):
        result = original(db, *args, **kwargs)
        store = BlobStore(tmp_path, secret="isolated-blob-ownership-secret-for-tests-only")
        assert (
            cleanup_blobs(worker_factory, store, now=datetime.now(UTC) + timedelta(hours=25)) == 0
        )
        return result

    monkeypatch.setattr(routes.sources, "mutate", inspect)
    data = b"Committed test document"
    result = owner.post(
        base + "/sources/upload?name=File&format=txt",
        content=data,
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert result.status_code == 201
    assert len(list(tmp_path.glob("*.blob"))) == 1


def test_unknown_sql_outcome_server_lock_blocks_orphan_cleanup(tmp_path, worker_factory, database):
    import asyncio

    from knowledge_worker.cleanup import cleanup_blobs

    store = BlobStore(tmp_path, secret="isolated-blob-ownership-secret-for-tests-only")

    async def upload():
        async def stream():
            yield b"Synthetic uncertain commit"

        return await store.receive(
            stream(), uuid4(), uuid4(), 1, hashlib.sha256(b"Synthetic uncertain commit").hexdigest()
        )

    pending = asyncio.run(upload())
    with database.begin() as db:
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,7241))"), {"key": pending.key}
        )
        pending.release()  # simulate client disappearing while the server transaction remains alive
        assert (
            cleanup_blobs(worker_factory, store, now=datetime.now(UTC) + timedelta(hours=25)) == 0
        )
    assert cleanup_blobs(worker_factory, store, now=datetime.now(UTC) + timedelta(hours=25)) == 0
    assert cleanup_blobs(worker_factory, store, now=datetime.now(UTC) + timedelta(hours=50)) == 1


def test_metadata_rollback_leaves_proven_orphan_for_later_cleanup(
    owner, tmp_path, worker_factory, monkeypatch
):
    import knowledge.routes as routes
    from knowledge_worker.cleanup import cleanup_blobs
    from scheduler.errors import AppError

    base = setup_api(owner, tmp_path)
    original = routes.sources.mutate

    def failing(*args, **kwargs):
        original(*args, **kwargs)
        raise AppError("synthetic_rollback", 409)

    monkeypatch.setattr(routes.sources, "mutate", failing)
    data = b"Synthetic failed upload commit"
    response = owner.post(
        base + "/sources/upload?name=File&format=txt",
        content=data,
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert response.status_code == 409
    assert len(list(tmp_path.glob("*.blob"))) == 1
    store = BlobStore(tmp_path, secret="isolated-blob-ownership-secret-for-tests-only")
    assert cleanup_blobs(worker_factory, store, now=datetime.now(UTC) + timedelta(hours=25)) == 0
    assert cleanup_blobs(worker_factory, store, now=datetime.now(UTC) + timedelta(hours=50)) == 1


def test_heartbeat_renews_during_blocked_embedding(
    owner, tmp_path, seed, worker_factory, monkeypatch
):
    _, row = source(owner, tmp_path)
    lease = claim(worker_factory, seed["tenant_a"])
    original = lease["lease_until"]

    def barrier():
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            with worker_factory() as db, db.begin():
                tenant_context(db, seed["tenant_a"])
                if db.get(Job, lease["job_id"]).lease_until > original:
                    return
            time.sleep(0.01)
        pytest.fail("lease was not renewed during external work")

    assert process(worker_factory, BlobStore(tmp_path), Embeddings(barrier), lease) == "ready"


def test_recent_retirement_and_reappeared_reference_reset_cleanup_grace(
    owner, tmp_path, seed, worker_factory, database
):
    import asyncio

    from knowledge_worker.cleanup import cleanup_blobs

    _, row = source(owner, tmp_path)
    store = BlobStore(
        tmp_path,
        secret="isolated-blob-ownership-secret-for-tests-only",
        clock=lambda: time.time() - 10 * 86400,
    )

    async def upload():
        async def stream():
            yield b"Old document"

        return await store.receive(
            stream(),
            seed["tenant_a"],
            UUID(row["id"]),
            1,
            hashlib.sha256(b"Old document").hexdigest(),
        )

    pending = asyncio.run(upload())
    pending.release()
    os.utime(tmp_path / pending.key, (time.time() - 10 * 86400,) * 2)
    start = datetime.now(UTC)
    assert cleanup_blobs(worker_factory, store, now=start) == 0
    assert cleanup_blobs(worker_factory, store, now=start + timedelta(hours=23)) == 0
    with database.begin() as db:
        db.execute(
            text("UPDATE knowledge_sources SET blob_key=:key WHERE id=:id"),
            {"key": pending.key, "id": row["id"]},
        )
    assert cleanup_blobs(worker_factory, store, now=start + timedelta(hours=25)) == 0
    with database.begin() as db:
        db.execute(
            text("UPDATE knowledge_sources SET blob_key=NULL,updated_at=:now WHERE id=:id"),
            {"now": start + timedelta(hours=26), "id": row["id"]},
        )
    assert cleanup_blobs(worker_factory, store, now=start + timedelta(hours=26)) == 0
    assert cleanup_blobs(worker_factory, store, now=start + timedelta(hours=49)) == 0
    assert cleanup_blobs(worker_factory, store, now=start + timedelta(hours=51)) == 1


def test_transient_url_failure_backs_off_and_stops_after_three_claims(
    owner, tmp_path, seed, worker_factory, database, monkeypatch
):
    from knowledge.parsing import IngestionError
    from knowledge_worker import runner

    base = setup_api(owner, tmp_path)
    write(
        owner,
        base + "/sources",
        {"kind": "url", "name": "Hours", "url": "https://example.com/hours"},
    )

    def timeout(_):
        raise IngestionError("url_timeout")

    monkeypatch.setattr(runner, "fetch_url", timeout)
    for attempt, expected in enumerate(("queued", "queued", "failed"), 1):
        lease = claim(worker_factory, seed["tenant_a"])
        assert process(worker_factory, BlobStore(tmp_path), Embeddings(), lease) == expected
        with database.begin() as db:
            job = db.execute(
                text(
                    "SELECT attempts,available_at > clock_timestamp() FROM knowledge_jobs WHERE id=:id"
                ),
                {"id": lease["job_id"]},
            ).one()
            assert tuple(job) == (attempt, True)
            db.execute(
                text(
                    "UPDATE knowledge_jobs SET available_at=clock_timestamp()-interval '1 second' WHERE id=:id"
                ),
                {"id": lease["job_id"]},
            )
    with worker_factory() as db, db.begin():
        assert discover_jobs(db) == []


def test_actual_upload_cancellation_waits_for_metadata_sql_and_releases_resources(
    owner, tmp_path, seed, worker_factory, database, monkeypatch
):
    import asyncio
    import fcntl

    import httpx2
    from sqlalchemy import event

    import knowledge.routes as routes
    from knowledge_worker.cleanup import cleanup_blobs

    base = setup_api(owner, tmp_path)
    engine = owner.app.state.engine
    original_factory = owner.app.state.session_factory
    sessions = []
    outer_ended = threading.Event()
    metadata_entered = threading.Event()
    pid = []
    mutation_done = threading.Event()
    mutation_errors = []
    actual_mutate = routes.sources.mutate

    def observe_mutation(*args, **kwargs):
        try:
            return actual_mutate(*args, **kwargs)
        except BaseException as exc:
            mutation_errors.append(exc)
            raise
        finally:
            mutation_done.set()

    monkeypatch.setattr(routes.sources, "mutate", observe_mutation)

    def track_session():
        session = original_factory()
        sessions.append(session)

        def ended(db, transaction):
            if transaction.parent is None:
                outer_ended.set()

        event.listen(session, "after_transaction_end", ended)
        return session

    monkeypatch.setattr(owner.app.state, "session_factory", track_session)

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if "FROM tenants" in statement and "FOR UPDATE" in statement:
            pid.append(connection.connection.driver_connection.info.backend_pid)
            metadata_entered.set()

    event.listen(engine, "before_cursor_execute", before_execute)
    barrier = database.connect()
    transaction = barrier.begin()
    barrier.execute(
        text("SELECT id FROM tenants WHERE id=:id FOR UPDATE"), {"id": seed["tenant_a"]}
    )
    store = BlobStore(tmp_path, secret="isolated-blob-ownership-secret-for-tests-only")
    data = b"Synthetic cancelled upload"
    headers = {
        "Authorization": owner.headers["Authorization"],
        "Idempotency-Key": "actual-cancelled-upload",
        "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
    }
    url = base + "/sources/upload?name=Cancelled&format=txt"

    async def wait_for(predicate, message, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        pytest.fail(message)

    def blocked_metadata():
        if not metadata_entered.is_set():
            return False
        with database.connect() as db:
            return (
                db.scalar(
                    text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": pid[0]},
                )
                is True
            )

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=owner.app), base_url="http://test"
        ) as api:
            task = asyncio.create_task(api.post(url, content=data, headers=headers))
            try:
                await wait_for(
                    blocked_metadata, "real metadata SQL did not reach the row-lock barrier"
                )
                with database.connect() as db:
                    assert (
                        db.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE pid=:pid AND locktype='advisory' AND granted"
                            ),
                            {"pid": pid[0]},
                        )
                        == 1
                    )
                key = next(tmp_path.glob("*.blob")).name
                task.cancel()
                # Two future sweeps would start AND exhaust retirement if the real fences were absent.
                # This cannot pass just because receipt creation/first-unreferenced grace is fresh.
                for hours in (25, 50):
                    assert (
                        await asyncio.to_thread(
                            cleanup_blobs,
                            worker_factory,
                            store,
                            now=datetime.now(UTC) + timedelta(hours=hours),
                        )
                        == 0
                    )
                    assert (tmp_path / key).exists()
                assert blocked_metadata(), (
                    "metadata SQL must still be outstanding during cancellation proof"
                )
                assert not outer_ended.is_set(), (
                    "outer SQL transaction ended before the blocked mutation settled"
                )
            finally:
                transaction.rollback()
            try:
                await asyncio.wait_for(task, timeout=10)
            except asyncio.CancelledError:
                pass
            await wait_for(
                lambda: (
                    mutation_done.is_set()
                    and outer_ended.is_set()
                    and engine.pool.checkedout() == 0
                ),
                "cancelled session or SQL connection did not settle",
            )
            assert not mutation_errors, [repr(exc) for exc in mutation_errors]
            assert task.done() and task.cancelled()
            assert all(not session.in_transaction() for session in sessions)
            with database.connect() as db:
                assert (
                    db.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE pid=:pid AND locktype='advisory' AND granted"
                        ),
                        {"pid": pid[0]},
                    )
                    == 0
                )
            for path in tmp_path.glob("*.blob"):
                fd = os.open(path, os.O_RDONLY)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            with database.connect() as db:
                assert db.scalar(text("SELECT count(*) FROM knowledge_sources")) == 0
                assert db.scalar(text("SELECT count(*) FROM knowledge_jobs")) == 0
            # The actual cancelled dependency rolled back; retry settles to one durable source/job.
            result = await api.post(url, content=data, headers=headers)
            assert result.status_code == 201, result.text
            replay = await api.post(url, content=data, headers=headers)
            assert replay.status_code == 201 and replay.json() == result.json()
            with database.connect() as db:
                assert db.scalar(text("SELECT count(*) FROM knowledge_sources")) == 1
                assert db.scalar(text("SELECT count(*) FROM knowledge_jobs")) == 1
                current = db.scalar(text("SELECT blob_key FROM knowledge_sources"))
            for hours in (75, 100):
                await asyncio.to_thread(
                    cleanup_blobs,
                    worker_factory,
                    store,
                    now=datetime.now(UTC) + timedelta(hours=hours),
                )
            assert [p.name for p in tmp_path.glob("*.blob")] == [current]
            assert engine.pool.checkedout() == 0

    try:
        asyncio.run(run())
    finally:
        if transaction.is_active:
            transaction.rollback()
        barrier.close()
        event.remove(engine, "before_cursor_execute", before_execute)
