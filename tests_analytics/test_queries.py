"""Staff projections use real RLS, exact SQL quantiles, and manager-only text."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests_analytics.test_rollups import NOW, populate
from tests_api.conftest import client, owner  # noqa: F401


def test_staff_routes_bounds_empty_coverage_and_private_allowlist(owner, analytics_seed):  # noqa: F811
    owner.app.state.clock = lambda: NOW
    base = "/dashboard/analytics/"
    params = {"start": "2026-11-08", "end": "2026-11-08", "channel": "voice"}
    response = owner.get(base + "overview", params=params)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["booking_conversion"] == {"numerator": 0, "denominator": 0, "value": None}
    assert data["usage"]["estimated_usd"] is None and data["freshness"]["stale"]
    assert owner.get(base + "overview", params=params | {"start": "2026-09-01"}).status_code == 422
    assert owner.get("/dashboard/calls", params=params | {"limit": 101}).status_code == 422
    import json

    from scripts.export_dashboard_contracts import render

    paths = json.loads(render())["paths"]
    assert base + "overview" in paths
    assert not any("private" in p or "worker" in p for p in paths)


def test_exact_latency_null_voice_coverage_and_test_exclusion(database, analytics_seed, owner):  # noqa: F811
    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed, channel="voice")
    with database.begin() as db:
        for i, value in enumerate([10, 20, 30, 40, 500]):
            db.execute(
                text(
                    "INSERT INTO analytics_stage_metrics(tenant_id,session_id,event_id,stage,language,origin,duration_ms,observed_at) VALUES(:t,:s,:e,'llm_completion','en','server',:value,:now)"
                ),
                dict(
                    t=analytics_seed["tenant_a"],
                    s=analytics_seed["session_a"],
                    e=uuid4(),
                    value=value,
                    now=NOW,
                ),
            )
    params = {"start": "2026-11-08", "end": "2026-11-08", "channel": "voice"}
    response = owner.get("/dashboard/analytics/latency", params=params)
    assert response.status_code == 200, response.text
    stages = {r["stage"]: r for r in response.json()["stages"]}
    assert stages["llm_completion"]["samples"] == 5
    assert stages["llm_completion"]["p50_ms"] == 30 and stages["llm_completion"][
        "p95_ms"
    ] == pytest.approx(408)
    assert stages["llm_first_token"]["p95_ms"] is None
    assert (
        owner.get(
            "/dashboard/analytics/latency",
            params=params | {"start": "2026-07-01", "end": "2026-07-05"},
        ).json()["unavailable_reason"]
        == "raw_retention"
    )
    with database.begin() as db:
        db.execute(text("UPDATE analytics_calls SET purpose='test'"))
    assert all(
        s["samples"] == 0
        for s in owner.get("/dashboard/analytics/latency", params=params).json()["stages"]
    )


def test_manager_settings_revision_staff_denial_and_foreign_call(database, analytics_seed, owner):  # noqa: F811
    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed)
    settings = owner.get("/dashboard/analytics/settings")
    assert settings.status_code == 200, settings.text
    body = settings.json()
    body.pop("updated_at")
    response = owner.put("/dashboard/analytics/settings", json=body | {"capture_enabled": True})
    assert response.status_code == 200, response.text
    assert owner.put("/dashboard/analytics/settings", json=body).status_code == 409
    assert owner.get("/dashboard/calls/" + str(analytics_seed["session_b"])).status_code == 404
    from tests_api.conftest import PASSWORD

    token = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()["access_token"]
    owner.headers["Authorization"] = "Bearer " + token
    assert owner.get("/dashboard/analytics/settings").status_code == 403
    assert (
        owner.get(
            "/dashboard/calls/" + str(analytics_seed["session_a"]) + "/transcript"
        ).status_code
        == 403
    )
    assert (
        owner.delete(
            "/dashboard/calls/" + str(analytics_seed["session_a"]) + "/transcript"
        ).status_code
        == 403
    )
    response = owner.get("/dashboard/calls/" + str(analytics_seed["session_a"]))
    assert response.status_code == 200, response.text
    assert "transcript" not in response.text and "text" not in response.json().get("summary", "")


def test_chronological_transcript_seek_total_live_disable(database, analytics_seed, owner):  # noqa: F811
    from analytics.models import TurnEvent
    from analytics.transcripts import append_text, settings_row

    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed, capture_enabled_at_start=True, consent="accepted")
    with Session(database) as db, db.begin():
        settings_row(db, analytics_seed["tenant_a"]).capture_enabled = True
        for i in range(104):
            event_id = uuid4()
            speaker = "user" if i % 2 == 0 else "assistant"
            db.add(
                TurnEvent(
                    tenant_id=analytics_seed["tenant_a"],
                    session_id=analytics_seed["session_a"],
                    event_id=event_id,
                    kind="turn." + speaker,
                    language="en",
                    safe_metadata={},
                    occurred_at=NOW,
                )
            )
            db.flush()
            append_text(
                db,
                analytics_seed["session_a"],
                speaker=speaker,
                value="synthetic " + chr(97 + i // 26) + chr(97 + i % 26),
                literals=(),
                occurred_at=NOW,
                now=NOW,
                event_id=event_id,
            )
    path = "/dashboard/calls/" + str(analytics_seed["session_a"]) + "/transcript"
    first = owner.get(path)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["total"] == 104 and data["has_more"] and len(data["items"]) == 100
    assert [x["text"] for x in data["items"]] == [
        "synthetic " + chr(97 + i // 26) + chr(97 + i % 26) for i in range(100)
    ]
    assert first.headers["cache-control"] == "no-store"
    tail = owner.get(path, params={"after": data["next_cursor"]}).json()
    assert not tail["has_more"] and [x["text"] for x in tail["items"]] == [
        "synthetic " + chr(97 + i // 26) + chr(97 + i % 26) for i in range(100, 104)
    ]
    settings = owner.get("/dashboard/analytics/settings").json()
    settings.pop("updated_at")
    owner.put("/dashboard/analytics/settings", json=settings | {"capture_enabled": False})
    assert owner.get(path).json()["items"] == []
    assert owner.delete(path).status_code == 204
    assert owner.get(path).json()["items"] == []


def test_knowledge_runtime_linkage_is_server_owned_and_preview_stays_unattributed(
    database, analytics_seed
):
    from knowledge.models import QueryEvent
    from knowledge.privacy import record_event
    from knowledge.retrieval import KnowledgeScope, RetrievalResult

    populate(database, analytics_seed)
    with Session(database) as db, db.begin():
        scope = KnowledgeScope(
            analytics_seed["tenant_a"],
            analytics_seed["agent_a"],
            "runtime",
            analytics_seed["session_a"],
        )
        record_event(db, scope, uuid4(), "a" * 64, "en", RetrievalResult("service_unavailable"), 1)
        row = db.query(QueryEvent).one()
        assert getattr(row, "session_id", None) == analytics_seed["session_a"]
        preview = KnowledgeScope(analytics_seed["tenant_a"], analytics_seed["agent_a"], "preview")
        record_event(db, preview, uuid4(), "b" * 64, "en", RetrievalResult("below_threshold"), 1)
        assert db.query(QueryEvent).filter(QueryEvent.purpose == "preview").one().session_id is None


def test_exact_sample_cap_across_stages_never_returns_truncated_quantile(
    database,
    analytics_seed,
    owner,  # noqa: F811
):  # noqa: F811
    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed, channel="voice")
    with database.begin() as db:
        db.execute(
            text("""INSERT INTO analytics_stage_metrics(tenant_id,session_id,event_id,stage,language,origin,duration_ms,observed_at)
          SELECT :tenant,:session,gen_random_uuid(),CASE WHEN i%2=0 THEN 'tool' ELSE 'llm_completion' END,'en','server',i%600001,:now FROM generate_series(1,100000) i"""),
            {
                "tenant": analytics_seed["tenant_a"],
                "session": analytics_seed["session_a"],
                "now": NOW,
            },
        )
    params = {"start": "2026-11-08", "end": "2026-11-08", "channel": "voice"}
    data = owner.get("/dashboard/analytics/latency", params=params).json()
    assert (
        data["unavailable_reason"] is None and sum(r["samples"] for r in data["stages"]) == 100000
    )
    with database.begin() as db:
        db.execute(
            text("""INSERT INTO analytics_stage_metrics(tenant_id,session_id,event_id,stage,language,origin,duration_ms,observed_at)
          VALUES(:tenant,:session,gen_random_uuid(),'audio_queue','en','native',1,:now)"""),
            {
                "tenant": analytics_seed["tenant_a"],
                "session": analytics_seed["session_a"],
                "now": NOW,
            },
        )
    data = owner.get("/dashboard/analytics/latency", params=params).json()
    assert data["unavailable_reason"] == "sample_limit"
    assert all(r["p95_ms"] is None and r["samples"] is None for r in data["stages"])


def test_staff_detail_never_selects_transcript_and_foreign_agent_filter_is_denied(
    database,
    analytics_seed,
    owner,  # noqa: F811
):  # noqa: F811
    from sqlalchemy import event

    from tests_api.conftest import PASSWORD

    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed)
    token = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()["access_token"]
    owner.headers["Authorization"] = "Bearer " + token
    statements = []

    def trace(conn, cursor, statement, *args):
        statements.append(statement.lower())

    event.listen(owner.app.state.engine, "before_cursor_execute", trace)
    try:
        assert owner.get("/dashboard/calls/" + str(analytics_seed["session_a"])).status_code == 200
        assert (
            owner.get(
                "/dashboard/analytics/overview",
                params={
                    "start": "2026-11-08",
                    "end": "2026-11-08",
                    "agent_id": str(analytics_seed["agent_b"]),
                },
            ).status_code
            == 404
        )
        assert (
            owner.get(
                "/dashboard/calls/" + str(analytics_seed["session_a"]) + "/transcript"
            ).status_code
            == 403
        )
    finally:
        event.remove(owner.app.state.engine, "before_cursor_execute", trace)
    assert not any("analytics_transcripts" in sql for sql in statements)


def test_intents_count_sessions_not_turns_or_default_book(database, analytics_seed, owner):  # noqa: F811
    from analytics.models import CallRecord, TurnEvent

    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed)
    with Session(database) as db, db.begin():
        call = db.get(CallRecord, analytics_seed["session_a"])
        for _ in range(4):
            db.add(
                TurnEvent(
                    tenant_id=call.tenant_id,
                    session_id=call.session_id,
                    event_id=uuid4(),
                    kind="tool.result",
                    tool_name="get_clinic_info",
                    tool_status="success",
                    language="en",
                    safe_metadata={},
                    occurred_at=NOW,
                )
            )
    data = owner.get(
        "/dashboard/analytics/intents",
        params={"start": "2026-11-08", "end": "2026-11-08", "channel": "text"},
    ).json()
    counts = {r["intent"]: r["sessions"] for r in data["items"]}
    assert counts == dict(book=0, cancel=0, reschedule=0, information=1, handoff=0)


def test_knowledge_gaps_runtime_cohort_preview_foreign_and_test_exclusion(
    database,
    analytics_seed,
    owner,  # noqa: F811
):  # noqa: F811
    from sqlalchemy.exc import IntegrityError

    from knowledge.privacy import record_event
    from knowledge.retrieval import KnowledgeScope, RetrievalResult

    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed, channel="voice")
    with Session(database) as db, db.begin():
        runtime = KnowledgeScope(
            analytics_seed["tenant_a"],
            analytics_seed["agent_a"],
            "runtime",
            analytics_seed["session_a"],
        )
        for decision in ["service_unavailable", "below_threshold"]:
            record_event(
                db, runtime, uuid4(), decision.ljust(64, "a"), "en", RetrievalResult(decision), 1
            )
        record_event(
            db,
            KnowledgeScope(runtime.tenant_id, runtime.agent_id, "preview"),
            uuid4(),
            "b" * 64,
            "en",
            RetrievalResult("below_threshold"),
            1,
        )
        db.execute(text("UPDATE knowledge_query_events SET created_at=:now"), {"now": NOW})
    with pytest.raises(IntegrityError), Session(database) as db, db.begin():
        record_event(
            db,
            KnowledgeScope(
                runtime.tenant_id, runtime.agent_id, "runtime", analytics_seed["session_b"]
            ),
            uuid4(),
            "c" * 64,
            "en",
            RetrievalResult("ready"),
            1,
        )
    params = {"start": "2026-11-08", "end": "2026-11-08", "channel": "voice"}
    response = owner.get("/dashboard/analytics/knowledge-gaps", params=params)
    assert response.status_code == 200, response.text
    data = response.json()
    assert (
        data["unavailable"] == 1
        and data["irrelevant"] == 1
        and data["total"] == 2
        and data["unattributed_observations"] == 1
    )
    assert (
        owner.get(
            "/dashboard/analytics/knowledge-gaps", params=params | {"channel": "text"}
        ).json()["total"]
        == 0
    )
    with database.begin() as db:
        db.execute(
            text(
                "UPDATE analytics_calls SET purpose='test'; UPDATE knowledge_query_events SET purpose='test' WHERE session_id IS NOT NULL"
            )
        )
    assert owner.get("/dashboard/analytics/knowledge-gaps", params=params).json()["total"] == 0


def test_usage_estimate_exposes_real_coverage_and_zero_rates_are_known(
    database,
    analytics_seed,
    owner,  # noqa: F811
):  # noqa: F811
    from analytics.aggregation import run_once
    from tests_analytics.test_rollups import worker_factory

    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed, ended_at=NOW, status="ended", outcome="info_only")
    with database.begin() as db:
        for values in [
            (10, 20, "1", "2", "0.00005"),
            (3, 7, None, None, None),
            (None, None, None, None, None),
            (100, None, None, None, None),
            (0, 0, "0", "0", "0"),
        ]:
            db.execute(
                text("""INSERT INTO analytics_usage_invocations(tenant_id,session_id,invocation_id,provider,model,status,started_at,completed_at,input_tokens,output_tokens,input_rate,output_rate,cost)
              VALUES(:t,:s,gen_random_uuid(),'nim','synthetic','success',:now,:now,:input,:output,:input_rate,:output_rate,:cost)"""),
                dict(
                    t=analytics_seed["tenant_a"],
                    s=analytics_seed["session_a"],
                    now=NOW,
                    input=values[0],
                    output=values[1],
                    input_rate=values[2],
                    output_rate=values[3],
                    cost=values[4],
                ),
            )
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    params = {"start": "2026-11-08", "end": "2026-11-08", "channel": "text"}
    response = owner.get("/dashboard/analytics/overview", params=params)
    assert response.status_code == 200, response.text
    usage = response.json()["usage"]
    assert (usage["invocations"], usage["usage_known"], usage["cost_known"]) == (5, 3, 2)
    assert (usage["input_tokens"], usage["output_tokens"]) == (13, 27)
    from decimal import Decimal

    assert Decimal(usage["estimated_usd"]) == Decimal("0.00005")
    assert usage["currency"] == "USD"


def test_http_pages_immediate_retention_shortening_and_delete_replay_worker_nonresurrection(
    database,
    analytics_seed,
    owner,  # noqa: F811
):  # noqa: F811
    from analytics.aggregation import run_once
    from analytics.models import TurnEvent
    from analytics.transcripts import append_text, settings_row
    from tests_analytics.test_rollups import worker_factory

    owner.app.state.clock = lambda: NOW
    happened = NOW - timedelta(days=4)
    populate(
        database,
        analytics_seed,
        started_at=happened,
        ended_at=happened + timedelta(minutes=1),
        status="ended",
        outcome="info_only",
        capture_enabled_at_start=True,
        consent="accepted",
        local_date=happened.date(),
    )
    event_id = uuid4()
    with Session(database) as db, db.begin():
        settings_row(db, analytics_seed["tenant_a"]).capture_enabled = True
        db.add(
            TurnEvent(
                tenant_id=analytics_seed["tenant_a"],
                session_id=analytics_seed["session_a"],
                event_id=event_id,
                kind="turn.user",
                language="en",
                safe_metadata={},
                occurred_at=happened,
            )
        )
        db.flush()
        append_text(
            db,
            analytics_seed["session_a"],
            speaker="user",
            value="synthetic message",
            literals=(),
            occurred_at=happened,
            now=NOW,
            event_id=event_id,
        )
    path = "/dashboard/calls/" + str(analytics_seed["session_a"]) + "/transcript"
    assert owner.get(path).json()["total"] == 1
    settings = owner.get("/dashboard/analytics/settings").json()
    settings.pop("updated_at")
    changed = owner.put("/dashboard/analytics/settings", json=settings | {"retention_days": 1})
    assert changed.status_code == 200, changed.text
    assert owner.get(path).json()["total"] == 0
    assert owner.delete(path).status_code == 204
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    newer = changed.json()
    newer.pop("updated_at")
    assert (
        owner.put("/dashboard/analytics/settings", json=newer | {"retention_days": 7}).status_code
        == 200
    )
    with Session(database) as db, db.begin():
        append_text(
            db,
            analytics_seed["session_a"],
            speaker="user",
            value="synthetic message",
            literals=(),
            occurred_at=happened,
            now=NOW,
            event_id=event_id,
        )
    assert owner.get(path).json()["total"] == 0


def test_alerts_and_call_pages_expose_typed_freshness_and_seek_cursor(
    database,
    analytics_seed,
    owner,  # noqa: F811
):  # noqa: F811
    from analytics.aggregation import run_once
    from tests_analytics.test_rollups import worker_factory

    owner.app.state.clock = lambda: NOW
    populate(
        database, analytics_seed, ended_at=NOW, status="failed", outcome="error", end_reason="error"
    )
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    params = {"start": "2026-11-08", "end": "2026-11-08", "channel": "text", "limit": 1}
    alerts = owner.get("/dashboard/analytics/alerts", params=params)
    assert alerts.status_code == 200, alerts.text
    assert (
        alerts.json()["freshness"]["stale"] is False
        and alerts.json()["items"][0]["kind"] == "ended_error"
    )
    calls = owner.get("/dashboard/calls", params=params)
    assert calls.status_code == 200, calls.text
    assert calls.json()["total"] == 1 and not calls.json()["has_more"]
    assert owner.get("/dashboard/calls", params=params | {"after": "invalid"}).status_code == 422


def test_purged_eastern_cohort_keeps_mixed_timezone_date_raw_coverage_unavailable(
    database,
    analytics_seed,
    owner,  # noqa: F811
):
    from datetime import UTC, datetime, timedelta

    from analytics.models import DailyRollup, HourlyRollup

    # At03:00 UTC, this date's western midnight (05:00UTC) is within90days,
    # but its eastern midnight (previous19:00UTC) has already crossed retention.
    now = NOW.replace(hour=3)
    start = (now - timedelta(days=90)).date()
    midnight = datetime.combine(start, datetime.min.time(), UTC)
    owner.app.state.clock = lambda: now
    populate(
        database,
        analytics_seed,
        timezone="America/New_York",
        utc_offset_minutes=-300,
        local_date=start,
        channel="voice",
        started_at=midnight + timedelta(hours=6),
        expires_at=midnight + timedelta(hours=7),
        ended_at=midnight + timedelta(hours=6, minutes=1),
        status="ended",
        outcome="info_only",
    )
    with Session(database) as db, db.begin():
        key = dict(
            tenant_id=analytics_seed["tenant_a"],
            agent_id=analytics_seed["agent_a"],
            purpose="production",
            channel="voice",
            timezone="Asia/Karachi",
            local_date=start,
            sessions=1,
            ended_sessions=1,
            as_of=now,
            sealed_at=now,
        )
        db.add(DailyRollup(**key))
        db.add(
            HourlyRollup(**key, bucket_start=midnight - timedelta(hours=5), utc_offset_minutes=300)
        )
    params = {"start": str(start), "end": str(start), "channel": "voice"}
    counters = owner.get("/dashboard/analytics/overview", params=params)
    assert counters.status_code == 200, counters.text
    assert counters.json()["sessions"] == 1
    response = owner.get("/dashboard/analytics/latency", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["unavailable_reason"] == "raw_retention"
    assert all(r["samples"] is None and r["p95_ms"] is None for r in response.json()["stages"])
    assert owner.get("/dashboard/analytics/overview", params=params).json()["sessions"] == 1
    western = owner.get(
        "/dashboard/analytics/latency", params=params | {"timezone": "America/New_York"}
    )
    assert western.status_code == 200, western.text
    assert western.json()["unavailable_reason"] is None
