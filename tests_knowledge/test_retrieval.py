"""Finite fixture vectors test SQL/control semantics, never real BGE model quality."""

import math
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge.models import Chunk, Generation, QueryEvent, Source
from knowledge.profile import PROFILE
from tests_api.conftest import SECRET
from tests_knowledge.test_sources import setup_api, write


def vector(cosine=1.0):
    return [cosine, math.sqrt(1 - cosine * cosine)] + [0.0] * 1022


class Embeddings:
    def embed(self, texts, *, deadline):
        return [vector()]


def corpus(database, tenant, agent, texts, *, cosines=None, profile=None, sealed=True):
    """Only the guarded disposable test database receives synthetic corpus rows."""
    assert database.url.database == "voice_agent_test"
    with Session(database) as db, db.begin():
        from scheduler.db import tenant_context

        tenant_context(db, tenant)
        source = Source(
            tenant_id=tenant,
            agent_id=agent,
            kind="text",
            name="Synthetic hours",
            content={"text": "synthetic"},
            status="ready",
        )
        db.add(source)
        db.flush()
        gen = Generation(
            tenant_id=tenant,
            agent_id=agent,
            source_id=source.id,
            source_revision=1,
            profile=profile or PROFILE.fingerprint,
            chunk_count=len(texts),
        )
        db.add(gen)
        db.flush()
        chunks = []
        for i, value in enumerate(texts):
            chunk = Chunk(
                tenant_id=tenant,
                agent_id=agent,
                source_id=source.id,
                generation_id=gen.id,
                ordinal=i,
                language="ur" if "ا" in value else "en",
                text=value,
                token_count=4,
                embedding=vector((cosines or [1.0] * len(texts))[i]),
            )
            db.add(chunk)
            chunks.append(chunk)
        db.flush()
        if sealed:
            db.execute(
                text("UPDATE knowledge_generations SET sealed_at=now() WHERE id=:id"),
                {"id": gen.id},
            )
        source.active_generation_id = gen.id
        return source.id, gen.id, [c.id for c in chunks]


def search(owner, base, query="hours", query_id=None, **extra):
    return owner.post(
        base + "/search",
        json={"query": query, "language": "en", "query_id": str(query_id or uuid4()), **extra},
    )


def test_manager_preview_hybrid_scores_and_current_revision(owner, tmp_path, database, seed):
    base = setup_api(owner, tmp_path)
    agent = UUID(base.split("/")[3])
    source_id, gen_id, ids = corpus(
        database,
        seed["tenant_a"],
        agent,
        ["hours hours", "weekdays", "hours", "other", "irrelevant hours"],
        cosines=[0.8, 0.95, 0.7, 0.9, 0.2],
    )
    owner.app.state.knowledge_embeddings = Embeddings()
    response = search(owner, base)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["reason"] == "ready"
    assert [c["chunk_id"] for c in result["chunks"]] == [
        str(ids[0]),
        str(ids[2]),
        str(ids[1]),
        str(ids[3]),
    ]
    assert result["chunks"][0]["cosine"] == pytest.approx(0.8)
    assert result["chunks"][0]["score"] == pytest.approx(1 / 63 + 1 / 61)
    assert "source_revision" not in result["chunks"][0]  # private validation identity
    assert "answer" not in result


def test_empty_pending_unavailable_and_threshold_are_distinct(owner, tmp_path, database, seed):
    base = setup_api(owner, tmp_path)
    assert search(owner, base).status_code == 200
    assert search(owner, base).json()["reason"] == "no_sources"
    row = write(
        owner,
        base + "/sources",
        {"kind": "text", "name": "Pending", "text": "Hours", "format": "txt"},
    ).json()
    assert search(owner, base).json()["reason"] == "processing_pending"
    corpus(database, seed["tenant_a"], UUID(base.split("/")[3]), ["hours"], cosines=[0.54])
    assert search(owner, base).json()["reason"] == "service_unavailable"
    owner.app.state.knowledge_embeddings = Embeddings()
    assert search(owner, base).json()["reason"] == "below_threshold"
    assert row["status"] == "queued"


def test_preview_scope_validation_and_event_dedup(owner, tmp_path, database, seed):
    base = setup_api(owner, tmp_path)
    owner.app.state.knowledge_embeddings = Embeddings()
    corpus(database, seed["tenant_a"], UUID(base.split("/")[3]), ["hours"])
    query_id = uuid4()
    query = (
        "Name: Synthetic Patient DOB: 2001-02-03 phone: +923001234567 CNIC: 12345-1234567-1 hours"
    )
    assert search(owner, base, query, query_id).status_code == 200
    assert search(owner, base, query, query_id).status_code == 200
    assert search(owner, base, "different", query_id).status_code == 409
    assert search(owner, base, tenant_id=str(seed["tenant_b"])).status_code == 422
    with Session(database) as db:
        events = list(db.scalars(select(QueryEvent)))
        assert len(events) == 1
        event = events[0]
        assert event.redacted_question is None
        assert len(event.query_hash) == 64 and event.language == "en" and event.purpose == "preview"
        assert "Synthetic" not in str(event.refs) and "hours" not in str(event.refs)


def test_embedding_has_no_open_sql_transaction_and_old_ready_refs(owner, tmp_path, database, seed):
    from knowledge.retrieval import KnowledgeScope
    from knowledge.service import KnowledgeService

    base = setup_api(owner, tmp_path)
    agent = UUID(base.split("/")[3])
    sid, gid, _ = corpus(database, seed["tenant_a"], agent, ["hours"])
    assert write(owner, base + f"/sources/{sid}/reindex", {"revision": 1}).status_code == 200

    class Probe(Embeddings):
        def embed(self, texts, *, deadline):
            with database.connect() as conn:
                # Auth and corpus reads must both be closed before external work.
                assert (
                    conn.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE usename='scheduler_app' AND datname='voice_agent_test' AND state='idle in transaction'"
                        )
                    )
                    == 0
                )
            return super().embed(texts, deadline=deadline)

    owner.app.state.knowledge_embeddings = Probe()
    assert search(owner, base).status_code == 200
    result = KnowledgeService(
        owner.app.state.session_factory, Probe(), audit_secret=SECRET
    ).retrieve(
        KnowledgeScope(seed["tenant_a"], agent, "preview"),
        "hours",
        "en",
        query_id=uuid4(),
        deadline=time.monotonic() + 10,
    )
    assert result.reason == "ready"
    assert result.chunks[0].source_revision == 2
    assert result.chunks[0].generation_source_revision == 1
    assert result.chunks[0].generation_id == gid


@pytest.mark.parametrize(
    "language,query,content",
    [
        ("en", "hours", "hours weekdays"),
        ("ur", "اوقات", "کلینک کے اوقات"),
        ("ur", "اوقات", "English opening times"),
    ],
)
def test_simple_fts_and_cross_language_fixture_retrieval(
    owner, tmp_path, database, seed, language, query, content
):
    base = setup_api(owner, tmp_path)
    owner.app.state.knowledge_embeddings = Embeddings()
    corpus(database, seed["tenant_a"], UUID(base.split("/")[3]), [content])
    result = search(owner, base, query, language=language).json()
    assert result["reason"] == "ready"
    assert result["chunks"][0]["text"] == content
    assert result["chunks"][0]["score"] == pytest.approx(
        1 / 61 if language == "ur" and content.startswith("English") else 2 / 61
    )


@pytest.mark.parametrize("excluded", ["deleted", "unsealed", "profile", "old_generation", "failed"])
def test_excludes_nonserving_generations(owner, tmp_path, database, seed, excluded):
    base = setup_api(owner, tmp_path)
    agent = UUID(base.split("/")[3])
    owner.app.state.knowledge_embeddings = Embeddings()
    good, _, _ = corpus(database, seed["tenant_a"], agent, ["hours authorized"], cosines=[0.7])
    sid, gid, _ = corpus(
        database,
        seed["tenant_a"],
        agent,
        ["hours excluded"],
        sealed=excluded != "unsealed",
        profile="0" * 64 if excluded == "profile" else None,
    )
    with Session(database) as db, db.begin():
        if excluded == "deleted":
            db.execute(
                text("UPDATE knowledge_sources SET status='deleted',deleted_at=now() WHERE id=:id"),
                {"id": sid},
            )
        elif excluded in {"old_generation", "failed"}:
            db.execute(
                text(
                    "UPDATE knowledge_sources SET active_generation_id=NULL,status=:status WHERE id=:id"
                ),
                {"id": sid, "status": "failed" if excluded == "failed" else "queued"},
            )
    result = search(owner, base).json()
    assert [c["source_id"] for c in result["chunks"]] == [str(good)]


def test_foreign_tenant_and_agent_vectors_cannot_starve_authorized_recall(
    owner, tmp_path, database, seed
):
    from scheduler.models import Agent

    base = setup_api(owner, tmp_path)
    agent = UUID(base.split("/")[3])
    owner.app.state.knowledge_embeddings = Embeddings()
    own, _, _ = corpus(database, seed["tenant_a"], agent, ["hours authorized"], cosines=[0.6])
    foreign_agents = []
    with Session(database) as db, db.begin():
        for tenant in [seed["tenant_a"], seed["tenant_b"]]:
            other = Agent(tenant_id=tenant, name="Synthetic foreign")
            db.add(other)
            db.flush()
            foreign_agents.append((tenant, other.id))
    for tenant, other in foreign_agents:
        for _ in range(4):
            corpus(database, tenant, other, ["hours foreign"] * 128)
    result = search(owner, base).json()
    assert result["reason"] == "ready"
    assert [c["source_id"] for c in result["chunks"]] == [str(own)]
    denied = base.replace(str(agent), str(foreign_agents[1][1]))
    assert search(owner, denied).status_code == 404


def test_dense20_keyword20_and_top4_boundaries(owner, tmp_path, database, seed):
    base = setup_api(owner, tmp_path)
    owner.app.state.knowledge_embeddings = Embeddings()
    # Dense top20 excludes ordinal20. Its keyword rank1 must still enter and gate.
    content = ["neutral"] * 20 + ["hours hours hours hours"] + ["hours"] * 20
    cosines = [0.99 - i * 0.005 for i in range(20)] + [0.8] + [0.1] * 20
    _, _, ids = corpus(
        database, seed["tenant_a"], UUID(base.split("/")[3]), content, cosines=cosines
    )
    result = search(owner, base).json()
    assert len(result["chunks"]) == 4
    assert [c["chunk_id"] for c in result["chunks"]] == [
        str(ids[0]),
        str(ids[20]),
        str(ids[1]),
        str(ids[2]),
    ]
    assert result["chunks"][1]["score"] == pytest.approx(1 / 61)
    assert all(c["cosine"] >= 0.55 for c in result["chunks"])
    assert all(c["score"] <= 2 / 61 for c in result["chunks"])


def test_query_event_concurrent_dedup_and_no_sensitive_logs(
    owner, tmp_path, database, seed, caplog
):
    from concurrent.futures import ThreadPoolExecutor

    from knowledge.retrieval import KnowledgeScope
    from knowledge.service import KnowledgeService

    base = setup_api(owner, tmp_path)
    agent = UUID(base.split("/")[3])
    corpus(database, seed["tenant_a"], agent, ["hours"])
    service = KnowledgeService(owner.app.state.session_factory, Embeddings(), audit_secret=SECRET)
    scope = KnowledgeScope(seed["tenant_a"], agent, "preview")
    query_id = uuid4()

    def retrieve():
        return service.retrieve(
            scope,
            "Synthetic Person DOB 2001-02-03 شناختی کارڈ 12345-1234567-1",
            "en",
            query_id=query_id,
            deadline=time.monotonic() + 10,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: retrieve(), range(2)))
    assert all(r.reason == "ready" for r in results)
    with Session(database) as db:
        assert len(list(db.scalars(select(QueryEvent)))) == 1
    assert "Synthetic Person" not in caplog.text and "2001-02-03" not in caplog.text


@pytest.mark.parametrize("failure", ["provider", "nan", "zero", "deadline"])
def test_embedding_failure_never_silently_returns_keyword_results(
    owner, tmp_path, database, seed, caplog, failure
):
    from knowledge.embedding import EmbeddingUnavailable

    base = setup_api(owner, tmp_path)
    corpus(database, seed["tenant_a"], UUID(base.split("/")[3]), ["hours"])

    class Failing:
        def embed(self, texts, *, deadline):
            if failure == "provider":
                raise EmbeddingUnavailable("PRIVATE PROVIDER BODY Synthetic Patient")
            if failure == "nan":
                return [[float("nan")] * 1024]
            if failure == "zero":
                return [[0.0] * 1024]
            raise EmbeddingUnavailable("deadline")

    owner.app.state.knowledge_embeddings = Failing()
    result = search(owner, base).json()
    assert result == {"reason": "service_unavailable", "chunks": []}
    assert "PRIVATE PROVIDER BODY" not in caplog.text


def test_preview_manager_authority_and_disabled_gate(owner, tmp_path):
    from tests_api.conftest import PASSWORD

    base = setup_api(owner, tmp_path)
    owner.app.state.settings.knowledge_enabled = False
    assert search(owner, base).status_code == 503
    owner.app.state.settings.knowledge_enabled = True
    token = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()["access_token"]
    owner.headers["Authorization"] = "Bearer " + token
    assert search(owner, base).status_code == 403
    owner.headers.pop("Authorization")
    assert search(owner, base).status_code == 401


def test_same_query_id_language_change_conflicts(owner, tmp_path):
    base = setup_api(owner, tmp_path)
    query_id = uuid4()
    assert search(owner, base, query_id=query_id).status_code == 200
    assert search(owner, base, query_id=query_id, language="ur").status_code == 409


def test_query_fingerprint_is_not_a_public_identity_dictionary_oracle(
    owner, tmp_path, database, seed
):
    import hashlib
    import json

    base = setup_api(owner, tmp_path)
    query = "Synthetic Patient DOB 2001-02-03"
    query_id = uuid4()
    assert search(owner, base, query, query_id).status_code == 200
    public_digest = hashlib.sha256(
        json.dumps(
            [query, "en", "preview", "", ""], ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    with Session(database) as db:
        event = db.scalar(select(QueryEvent))
        assert event.query_hash != public_digest
        assert event.redacted_question is None


def test_keyed_fingerprint_binds_scope_and_key_without_storing_names():
    import hashlib
    import hmac
    import json

    from knowledge.privacy import derive_query_key, query_fingerprint
    from knowledge.retrieval import KnowledgeScope

    scope = KnowledgeScope(UUID(int=1), UUID(int=2), "preview")
    key = hmac.new(SECRET.encode(), b"knowledge-query-key-v1", hashlib.sha256).digest()
    payload = json.dumps(
        [str(UUID(int=1)), str(UUID(int=2)), "Synthetic Person", "en", "preview", "", ""],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    expected = hmac.new(key, b"knowledge-query-v1\0" + payload, hashlib.sha256).hexdigest()
    assert (
        query_fingerprint(scope, "Synthetic Person", "en", key=derive_query_key(SECRET)) == expected
    )
    assert (
        query_fingerprint(
            scope,
            "Synthetic Person",
            "en",
            key=derive_query_key("different-server-secret-for-synthetic-tests"),
        )
        != expected
    )
    assert (
        query_fingerprint(
            KnowledgeScope(UUID(int=1), UUID(int=3), "preview"), "Synthetic Person", "en", key=key
        )
        != expected
    )


def test_audit_reference_keeps_bounded_scores_without_source_content():
    from knowledge.retrieval import RetrievedChunk

    chunk = RetrievedChunk(
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        PROFILE.fingerprint,
        2,
        1,
        "Synthetic heading",
        "Secret source heading",
        "en",
        "Secret source body",
        0.8,
        0.03,
    )
    refs = chunk.reference()
    assert refs["cosine"] == 0.8 and refs["score"] == 0.03
    assert "Secret" not in str(refs)


@pytest.mark.parametrize("entry", ["public", "service"])
def test_query_1001_rejected_before_embedding_or_event(owner, tmp_path, database, seed, entry):
    from knowledge.retrieval import KnowledgeScope
    from knowledge.service import KnowledgeService
    from scheduler.errors import AppError

    base = setup_api(owner, tmp_path)
    agent = UUID(base.split("/")[3])
    corpus(database, seed["tenant_a"], agent, ["hours"])
    calls = []

    class Observed(Embeddings):
        def embed(self, texts, *, deadline):
            calls.extend(texts)
            return super().embed(texts, deadline=deadline)

    embeddings = Observed()
    owner.app.state.knowledge_embeddings = embeddings
    if entry == "public":
        result = search(owner, base, "x" * 1001)
        assert result.status_code == 422, result.text
    else:
        with pytest.raises(AppError) as error:
            KnowledgeService(
                owner.app.state.session_factory, embeddings, audit_secret=SECRET
            ).retrieve(
                KnowledgeScope(seed["tenant_a"], agent, "preview"),
                "x" * 1001,
                "en",
                query_id=uuid4(),
                deadline=time.monotonic() + 10,
            )
        assert error.value.status == 422
    assert calls == []
    with Session(database) as db:
        assert list(db.scalars(select(QueryEvent))) == []


@pytest.mark.parametrize("entry", ["public", "service"])
def test_query_trimmed_1000_boundary_reaches_embedding(owner, tmp_path, database, seed, entry):
    from knowledge.retrieval import KnowledgeScope
    from knowledge.service import KnowledgeService

    base = setup_api(owner, tmp_path)
    agent = UUID(base.split("/")[3])
    corpus(database, seed["tenant_a"], agent, ["hours"])
    calls = []

    class Observed(Embeddings):
        def embed(self, texts, *, deadline):
            calls.extend(texts)
            return super().embed(texts, deadline=deadline)

    embeddings = Observed()
    owner.app.state.knowledge_embeddings = embeddings
    query = " \n" + "x" * 1000 + "\t "
    if entry == "public":
        response = search(owner, base, query)
        assert response.status_code == 200, response.text
        assert response.json()["reason"] == "ready"
    else:
        result = KnowledgeService(
            owner.app.state.session_factory, embeddings, audit_secret=SECRET
        ).retrieve(
            KnowledgeScope(seed["tenant_a"], agent, "preview"),
            query,
            "en",
            query_id=uuid4(),
            deadline=time.monotonic() + 10,
        )
        assert result.reason == "ready"
    assert calls == ["x" * 1000]
    with Session(database) as db:
        assert len(list(db.scalars(select(QueryEvent)))) == 1


def test_generated_search_contract_retains_established_query_limit():
    import json

    from scripts.export_dashboard_contracts import render

    query = json.loads(render())["components"]["schemas"]["SearchQuery"]["properties"]["query"]
    assert query["minLength"] == 1
    assert query["maxLength"] == 1000
