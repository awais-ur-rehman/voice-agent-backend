"""Staff projections use real RLS, exact SQL quantiles, and manager-only text."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests_analytics.test_rollups import NOW, populate
from tests_api.conftest import client, owner  # noqa: F401


def test_review_filters_all_channels_outcome_flags_and_hour_series(database, analytics_seed, owner):  # noqa: F811
    from analytics.aggregation import run_once
    from tests_analytics.test_rollups import worker_factory

    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed, ended_at=NOW, status="ended", outcome="abandoned")
    with database.begin() as db:
        db.execute(text("UPDATE analytics_calls SET quality_flags='[\"handoff\"]'"))
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    params = {"start": "2026-11-08", "end": "2026-11-08", "channel": "all"}
    response = owner.get("/dashboard/analytics/overview", params=params)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["sessions"] == data["text_sessions"] == 1 and data["voice_sessions"] == 0
    assert data["hours"]["unavailable_reason"] is None
    assert sum(x["sessions"] for x in data["hours"]["items"]) == 1
    for filters, expected in [
        ({}, 1),
        ({"outcome": "booked"}, 0),
        ({"review_flag": "interruptions"}, 0),
        ({"outcome": "abandoned", "review_flag": "handoff"}, 1),
    ]:
        response = owner.get("/dashboard/calls", params=params | filters)
        assert response.status_code == 200, response.text
        assert response.json()["total"] == len(response.json()["items"]) == expected
    for field in ["channel", "outcome", "review_flag"]:
        assert owner.get("/dashboard/calls", params=params | {field: "invalid"}).status_code == 422
    stages = owner.get("/dashboard/analytics/latency", params=params).json()["stages"]
    assert next(x for x in stages if x["stage"] == "stt_completion")["eligible_sessions"] == 0


def test_review_detail_has_safe_trace_usage_measurements_and_privacy(
    database,
    analytics_seed,
    owner,  # noqa: F811
):  # noqa: F811
    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed)
    response = owner.get("/dashboard/calls/" + str(analytics_seed["session_a"]))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["trace_id"] and data["usage"]["invocations"] == 0
    assert data["measurements"]["total"] == 0 and not data["measurements"]["has_more"]
    assert data["privacy"]["reason"] == "off"


def extra_call(database, ids, **overrides):
    """Another owned production admission, with no runtime or private payload."""
    sid = uuid4()
    with database.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent_sessions(id,tenant_id,agent_id,version_id,token_jti_hash,expires_at) VALUES(:s,:t,:a,:v,'synthetic',:e)"
            ),
            dict(
                s=sid,
                t=ids["tenant_a"],
                a=ids["agent_a"],
                v=ids["version_a"],
                e=NOW + timedelta(hours=1),
            ),
        )
    populate(database, ids | {"session_a": sid}, **overrides)
    return sid


def test_combined_channel_filters_total_matches_every_seek_page(database, analytics_seed, owner):  # noqa: F811
    from analytics.aggregation import run_once
    from tests_analytics.test_rollups import worker_factory

    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed, outcome="booked", status="ended", ended_at=NOW)
    voice = extra_call(
        database, analytics_seed, channel="voice", outcome="booked", status="ended", ended_at=NOW
    )
    extra_call(
        database, analytics_seed, channel="voice", outcome="abandoned", status="ended", ended_at=NOW
    )
    extra_call(
        database,
        analytics_seed,
        channel="voice",
        purpose="test",
        outcome="booked",
        status="ended",
        ended_at=NOW,
    )
    with database.begin() as db:
        db.execute(text("UPDATE analytics_calls SET quality_flags='[\"handoff\"]'"))
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    params = {
        "start": "2026-11-08",
        "end": "2026-11-08",
        "channel": "all",
        "outcome": "booked",
        "review_flag": "handoff",
        "limit": 1,
    }
    first = owner.get("/dashboard/calls", params=params).json()
    second = owner.get("/dashboard/calls", params=params | {"after": first["next_cursor"]}).json()
    assert first["total"] == second["total"] == 2
    assert first["has_more"] and not second["has_more"]
    assert {first["items"][0]["session_id"], second["items"][0]["session_id"]} == {
        str(voice),
        str(analytics_seed["session_a"]),
    }
    assert owner.get("/dashboard/calls", params=params | {"channel": "voice"}).json()["total"] == 1
    overall = owner.get(
        "/dashboard/analytics/overview",
        params={k: v for k, v in params.items() if k in {"start", "end", "channel"}},
    ).json()
    assert (overall["sessions"], overall["text_sessions"], overall["voice_sessions"]) == (3, 1, 2)
    stages = owner.get("/dashboard/analytics/latency", params=params).json()["stages"]
    assert next(s for s in stages if s["stage"] == "stt_completion")["eligible_sessions"] == 2
    assert next(s for s in stages if s["stage"] == "command_total")["eligible_sessions"] == 3


def test_hour_series_dst_fold_fractional_offset_and_sealed_history(database, analytics_seed, owner):  # noqa: F811
    from datetime import UTC, datetime

    from analytics.aggregation import run_once
    from analytics.models import DailyRollup, HourlyRollup
    from tests_analytics.test_rollups import worker_factory

    owner.app.state.clock = lambda: NOW
    for hour, offset in [(5, -240), (6, -300)]:
        started = datetime(2026, 11, 1, hour, 30, tzinfo=UTC)
        extra_call(
            database,
            analytics_seed,
            timezone="America/New_York",
            utc_offset_minutes=offset,
            started_at=started,
            expires_at=started + timedelta(hours=1),
            ended_at=started + timedelta(minutes=1),
            status="ended",
            outcome="info_only",
            local_date=started.date(),
        )
    populate(database, analytics_seed, timezone="Asia/Kathmandu", utc_offset_minutes=345)
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    params = {"start": "2026-11-01", "end": "2026-11-08"}
    response = owner.get("/dashboard/analytics/overview", params=params)
    assert response.status_code == 200, response.text
    data = response.json()
    hours = data["hours"]["items"]
    assert (
        sum(x["sessions"] for x in hours)
        == sum(x["sessions"] for x in data["days"])
        == data["sessions"]
        == 3
    )
    folds = [h for h in hours if h["timezone"] == "America/New_York"]
    assert {h["utc_offset_minutes"] for h in folds} == {-240, -300}
    assert len({h["bucket_start"] for h in folds}) == 2
    assert (
        next(h for h in hours if h["timezone"] == "Asia/Kathmandu")["bucket_start"][14:16] == "15"
    )
    old = NOW - timedelta(days=120)
    with Session(database) as db, db.begin():
        key = dict(
            tenant_id=analytics_seed["tenant_a"],
            agent_id=analytics_seed["agent_a"],
            purpose="production",
            channel="voice",
            timezone="Asia/Karachi",
            local_date=old.date(),
            sessions=7,
            as_of=NOW,
            sealed_at=NOW,
        )
        db.add(DailyRollup(**key))
        db.add(HourlyRollup(**key, bucket_start=old.replace(minute=0), utc_offset_minutes=300))
    data = owner.get(
        "/dashboard/analytics/overview", params={"start": str(old.date()), "end": str(old.date())}
    ).json()
    assert data["sessions"] == data["hours"]["items"][0]["sessions"] == 7
    assert data["hours"]["unavailable_reason"] is None


def test_hour_series_over_bound_explicit_without_losing_totals(database, analytics_seed, owner):  # noqa: F811
    owner.app.state.clock = lambda: NOW
    with database.begin() as db:
        values = dict(t=analytics_seed["tenant_a"], a=analytics_seed["agent_a"], now=NOW)
        db.execute(
            text(
                "INSERT INTO analytics_hourly_rollups(tenant_id,agent_id,purpose,channel,timezone,local_date,bucket_start,utc_offset_minutes,sessions,as_of) SELECT :t,:a,'production','voice','Asia/Karachi','2026-11-08',:now + i*interval '1 minute',300,1,:now FROM generate_series(1,2001) i"
            ),
            values,
        )
        db.execute(
            text(
                "INSERT INTO analytics_daily_rollups(tenant_id,agent_id,purpose,channel,timezone,local_date,sessions,as_of) VALUES(:t,:a,'production','voice','Asia/Karachi','2026-11-08',2001,:now)"
            ),
            values,
        )
    data = owner.get(
        "/dashboard/analytics/overview", params={"start": "2026-11-08", "end": "2026-11-08"}
    ).json()
    assert data["sessions"] == 2001 and data["hours"]["items"] == []
    assert data["hours"]["unavailable_reason"] == "series_limit"


def test_detail_measurement_paging_turn_association_and_usage_nulls(
    database,
    analytics_seed,
    owner,  # noqa: F811
):  # noqa: F811
    from analytics.models import StageMetric, TurnEvent, UsageInvocation

    owner.app.state.clock = lambda: NOW
    populate(database, analytics_seed)
    with Session(database) as db, db.begin():
        event = TurnEvent(
            tenant_id=analytics_seed["tenant_a"],
            session_id=analytics_seed["session_a"],
            event_id=uuid4(),
            kind="turn.assistant",
            language="en",
            occurred_at=NOW,
            safe_metadata={},
        )
        db.add(event)
        db.flush()
        ordinal = event.ordinal
        for i in range(3):
            db.add(
                StageMetric(
                    tenant_id=event.tenant_id,
                    session_id=event.session_id,
                    event_id=uuid4(),
                    turn_event_id=event.event_id if i else None,
                    stage="llm_completion",
                    language="en",
                    origin="server",
                    duration_ms=10 + i,
                    observed_at=NOW,
                )
            )
        db.add(
            UsageInvocation(
                tenant_id=event.tenant_id,
                session_id=event.session_id,
                invocation_id=uuid4(),
                provider="nim",
                model="synthetic",
                status="success",
                started_at=NOW,
                completed_at=NOW,
                input_tokens=3,
            )
        )
    path = "/dashboard/calls/" + str(analytics_seed["session_a"])
    first = owner.get(path, params={"limit": 2}).json()
    tail = owner.get(
        path, params={"limit": 2, "measurement_after": first["measurements"]["next_cursor"]}
    ).json()
    assert first["measurements"]["total"] == tail["measurements"]["total"] == 3
    assert first["measurements"]["has_more"] and not tail["measurements"]["has_more"]
    samples = first["measurements"]["items"] + tail["measurements"]["items"]
    assert len({s["id"] for s in samples}) == 3 and {s["turn_ordinal"] for s in samples} == {
        None,
        ordinal,
    }
    assert first["usage"]["invocations"] == 1 and first["usage"]["usage_known"] == 0
    assert first["usage"]["input_tokens"] is first["usage"]["estimated_usd"] is None
    assert owner.get(path, params={"measurement_after": "invalid"}).status_code == 422


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
    assert owner.get(path).json()["privacy"]["reason"] == "available"
    settings = owner.get("/dashboard/analytics/settings").json()
    settings.pop("updated_at")
    changed = owner.put("/dashboard/analytics/settings", json=settings | {"retention_days": 1})
    assert changed.status_code == 200, changed.text
    assert owner.get(path).json()["total"] == 0
    assert owner.get(path).json()["privacy"]["reason"] == "expired"
    expanded = changed.json()
    expanded.pop("updated_at")
    expanded = owner.put("/dashboard/analytics/settings", json=expanded | {"retention_days": 7})
    assert owner.get(path).json()["privacy"]["reason"] == "expired"
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    assert owner.get(path).json()["privacy"]["reason"] == "expired"
    assert owner.delete(path).status_code == 204
    assert owner.get(path).json()["privacy"]["reason"] == "deleted"
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    newer = expanded.json()
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


@pytest.mark.parametrize(
    "captured,consent,enabled,deleted,reason",
    [
        (False, "off", False, False, "off"),
        (True, "pending", True, False, "pending"),
        (True, "declined", True, False, "declined"),
        (True, "accepted", False, False, "live_disabled"),
        (True, "accepted", True, False, "no_captured"),
        (True, "declined", False, True, "deleted"),
    ],
)
def test_manager_privacy_reasons_and_staff_explicit_restriction(
    database,
    analytics_seed,
    owner,  # noqa: F811
    captured,
    consent,
    enabled,
    deleted,
    reason,  # noqa: F811
):  # noqa: F811
    from analytics.transcripts import settings_row

    owner.app.state.clock = lambda: NOW
    populate(
        database,
        analytics_seed,
        capture_enabled_at_start=captured,
        consent=consent,
        transcript_deleted_at=NOW if deleted else None,
    )
    with Session(database) as db, db.begin():
        settings_row(db, analytics_seed["tenant_a"]).capture_enabled = enabled
    path = "/dashboard/calls/" + str(analytics_seed["session_a"])
    privacy = owner.get(path + "/transcript").json()["privacy"]
    assert privacy == {"reason": reason, "availability": "unavailable", "text_access": "manager"}
    detail = owner.get(path).json()["privacy"]
    assert detail["reason"] == reason and detail["availability"] == "unavailable_to_role"


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


def test_capture_marker_is_transactional_and_never_set_for_rejected_text(database, analytics_seed):
    from analytics.models import CallRecord, TurnEvent
    from analytics.transcripts import append_text, settings_row

    populate(database, analytics_seed, capture_enabled_at_start=True, consent="accepted")
    with Session(database) as db, db.begin():
        settings_row(db, analytics_seed["tenant_a"]).capture_enabled = True
        call = db.get(CallRecord, analytics_seed["session_a"])
        event = TurnEvent(
            tenant_id=call.tenant_id,
            session_id=call.session_id,
            event_id=uuid4(),
            kind="turn.user",
            language="en",
            occurred_at=NOW,
            safe_metadata={},
        )
        db.add(event)
        db.flush()
        args = dict(speaker="user", literals=(), occurred_at=NOW, now=NOW, event_id=event.event_id)
        append_text(db, call.session_id, value="", **args)
        assert call.transcript_captured_at is None
        with pytest.raises(ValueError), db.begin_nested():
            append_text(db, call.session_id, value="synthetic", **args)
            assert call.transcript_captured_at == NOW
            raise ValueError("rollback")
        assert call.transcript_captured_at is None
        append_text(db, call.session_id, value="synthetic", **args)
        assert call.transcript_captured_at == NOW
    with Session(database) as db:
        assert db.get(CallRecord, analytics_seed["session_a"]).transcript_captured_at == NOW
