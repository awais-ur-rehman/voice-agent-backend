"""One-shot synthetic capacity/plan observation, not a performance acceptance gate."""

import json
import time
from uuid import UUID

from sqlalchemy import text

from knowledge.profile import PROFILE
from knowledge.retrieval import SEARCH_SQL
from scheduler.db import tenant_context
from tests_knowledge.test_retrieval import Embeddings, corpus, search, vector
from tests_knowledge.test_sources import setup_api


def test_exact_capacity_plan_and_overflow_fail_closed(owner, database, seed, tmp_path):
    base = setup_api(owner, tmp_path)
    agent = UUID(base.split("/")[3])
    owner.app.state.knowledge_embeddings = Embeddings()
    started = time.monotonic()
    # At most 256 x 1024 floats allocated at once; no giant all-corpus Python list.
    for _ in range(64):
        corpus(database, seed["tenant_a"], agent, ["Synthetic hours weekdays"] * 256)
    print("Synthetic seed seconds:", time.monotonic() - started)
    with owner.app.state.session_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        assert db.scalar(text("SELECT current_user")) == "scheduler_app"
        plan = db.execute(
            text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + SEARCH_SQL.text),
            {
                "tenant": seed["tenant_a"],
                "agent": agent,
                "profile": PROFILE.fingerprint,
                "vector": json.dumps(vector()),
                "query": "hours",
            },
        ).scalar_one()
        print("UPPER_BOUND_PLAN:", json.dumps(plan))
    result = search(owner, base).json()
    assert result["reason"] == "ready" and len(result["chunks"]) == 4
    corpus(database, seed["tenant_a"], agent, ["Synthetic overflow"])
    result = search(owner, base).json()
    assert result["reason"] == "service_unavailable", result
