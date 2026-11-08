"""Observed rolling alerts, minimum samples, deduplication and recovery."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from analytics.models import AnalyticsAlert
from tests_analytics.test_rollups import NOW, populate, worker_factory


def test_latency_alert_requires_five_canonical_samples_recovers_and_reopens(
    database, analytics_seed
):
    import analytics.aggregation as aggregation

    populate(database, analytics_seed, channel="voice")
    factory = worker_factory(database)

    def sample(stage="end_of_speech_to_audio", language="en", value=1600):
        with database.begin() as db:
            db.execute(
                text(
                    "INSERT INTO analytics_stage_metrics(tenant_id,session_id,event_id,stage,language,origin,duration_ms,observed_at) VALUES(:t,:s,:e,:stage,:lang,'native',:value,:now)"
                ),
                dict(
                    t=analytics_seed["tenant_a"],
                    s=analytics_seed["session_a"],
                    e=uuid4(),
                    stage=stage,
                    lang=language,
                    value=value,
                    now=NOW,
                ),
            )

    for _ in range(5):
        sample("end_of_speech_to_audio_queue", value=5000)
    for _ in range(4):
        sample()
    aggregation.run_once(factory, clock=lambda: NOW)
    with Session(database) as db:
        assert db.scalar(select(AnalyticsAlert)) is None
    sample()
    aggregation.run_once(factory, clock=lambda: NOW)
    aggregation.run_once(factory, clock=lambda: NOW + timedelta(seconds=1))
    with Session(database) as db:
        rows = list(db.scalars(select(AnalyticsAlert)))
        assert len(rows) == 1 and rows[0].state == "open" and rows[0].denominator == 5
    aggregation.run_once(factory, clock=lambda: NOW + timedelta(minutes=16))
    with Session(database) as db:
        assert db.scalar(select(AnalyticsAlert)).state == "resolved"


def test_queue_only_tenant_discovery_per_agent_no_content(database, analytics_seed):
    with database.begin() as db:
        vals = dict(
            t=analytics_seed["tenant_a"],
            a=analytics_seed["agent_a"],
            s=uuid4(),
            now=NOW - timedelta(minutes=20),
        )
        db.execute(
            text(
                "INSERT INTO knowledge_sources(id,tenant_id,agent_id,kind,name) VALUES(:s,:t,:a,'text','private synthetic')"
            ),
            vals,
        )
        db.execute(
            text(
                "INSERT INTO knowledge_jobs(tenant_id,agent_id,source_id,source_revision,created_at) VALUES(:t,:a,:s,1,:now)"
            ),
            vals,
        )
        db.execute(text("SET LOCAL ROLE analytics_worker"))
        ids = db.execute(text("SELECT * FROM analytics_discover_tenants(NULL,50)")).scalars().all()
        assert analytics_seed["tenant_a"] in ids, "queue-only tenants must be discoverable"
        db.execute(
            text("SELECT set_config('app.tenant_id',:t,true)"),
            {"t": str(analytics_seed["tenant_a"])},
        )
        rows = (
            db.execute(text("SELECT * FROM analytics_agent_backlog(:now,NULL,100)"), {"now": NOW})
            .mappings()
            .all()
        )
        assert rows == [
            {"agent_id": analytics_seed["agent_a"], "queued_count": 1, "oldest_age_seconds": 1200}
        ]


@pytest.mark.parametrize(
    "language,values,expected",
    [
        ("en", [1500] * 5, False),
        ("en", [1501] * 5, True),
        ("ur", [2000] * 5, False),
        ("ur", [2001] * 5, True),
        ("ur", [4000] * 4, False),
    ],
)
def test_latency_thresholds_strict_and_language_specific(
    database, analytics_seed, language, values, expected
):
    from analytics.aggregation import run_once

    populate(database, analytics_seed, channel="voice")
    with database.begin() as db:
        for value in values:
            db.execute(
                text(
                    "INSERT INTO analytics_stage_metrics(tenant_id,session_id,event_id,stage,language,origin,duration_ms,observed_at) VALUES(:t,:s,gen_random_uuid(),'end_of_speech_to_audio',:lang,'native',:v,:now)"
                ),
                {
                    "t": analytics_seed["tenant_a"],
                    "s": analytics_seed["session_a"],
                    "lang": language,
                    "v": value,
                    "now": NOW,
                },
            )
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    with Session(database) as db:
        row = db.scalar(select(AnalyticsAlert).where(AnalyticsAlert.kind == "latency"))
        assert (row is not None) == expected
        if expected:
            assert row.language == language and row.denominator == 5


@pytest.mark.parametrize(
    "failures,total,expected", [(1, 4, False), (1, 10, False), (1, 5, True), (2, 10, True)]
)
def test_tool_failure_rate_requires_five_and_strictly_more_than_ten_percent(
    database, analytics_seed, failures, total, expected
):
    from analytics.aggregation import run_once

    populate(database, analytics_seed)
    with database.begin() as db:
        for i in range(total):
            db.execute(
                text(
                    "INSERT INTO analytics_turns(tenant_id,session_id,event_id,kind,language,tool_name,tool_status,occurred_at) VALUES(:t,:s,gen_random_uuid(),'tool.result','en','ask_caller',:status,:now)"
                ),
                {
                    "t": analytics_seed["tenant_a"],
                    "s": analytics_seed["session_a"],
                    "status": "failure" if i < failures else "success",
                    "now": NOW,
                },
            )
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    with Session(database) as db:
        row = db.scalar(select(AnalyticsAlert).where(AnalyticsAlert.kind == "tool_failure"))
        assert (row is not None) == expected
        if expected:
            assert row.numerator == failures and row.denominator == total


def test_actual_provider_failure_and_error_end_each_fire_with_one_and_recover_reopen(
    database, analytics_seed
):
    from analytics.aggregation import run_once

    populate(
        database, analytics_seed, ended_at=NOW, status="failed", outcome="error", end_reason="error"
    )
    with database.begin() as db:
        db.execute(
            text(
                "INSERT INTO analytics_usage_invocations(tenant_id,session_id,invocation_id,provider,model,status,started_at,completed_at) VALUES(:t,:s,gen_random_uuid(),'nim','synthetic','failure',:now,:now)"
            ),
            {"t": analytics_seed["tenant_a"], "s": analytics_seed["session_a"], "now": NOW},
        )
    factory = worker_factory(database)
    for stamp in [NOW, NOW + timedelta(seconds=1)]:
        assert run_once(factory, clock=lambda: stamp)["failed"] == 0
    with Session(database) as db:
        rows = list(db.scalars(select(AnalyticsAlert)))
        assert {r.kind for r in rows} == {"provider_failure", "ended_error"}
        assert all(r.numerator == r.denominator == 1 and r.state == "open" for r in rows)
    assert run_once(factory, clock=lambda: NOW + timedelta(minutes=16))["failed"] == 0
    with Session(database) as db:
        assert all(r.state == "resolved" for r in db.scalars(select(AnalyticsAlert)))
    later = NOW + timedelta(minutes=17)
    with database.begin() as db:
        db.execute(
            text(
                "INSERT INTO analytics_usage_invocations(tenant_id,session_id,invocation_id,provider,model,status,started_at,completed_at) VALUES(:t,:s,gen_random_uuid(),'nim','synthetic','timeout',:now,:now)"
            ),
            {"t": analytics_seed["tenant_a"], "s": analytics_seed["session_a"], "now": later},
        )
    assert run_once(factory, clock=lambda: later)["failed"] == 0
    with Session(database) as db:
        rows = list(db.scalars(select(AnalyticsAlert)))
        assert len(rows) == 3 and sum(r.state == "open" for r in rows) == 1


def test_queue_only_alert_no_channel_fabrication_and_recovery(database, analytics_seed):
    from analytics.aggregation import run_once

    source = uuid4()
    with database.begin() as db:
        values = {
            "t": analytics_seed["tenant_a"],
            "a": analytics_seed["agent_a"],
            "s": source,
            "now": NOW - timedelta(minutes=16),
        }
        db.execute(
            text(
                "INSERT INTO knowledge_sources(id,tenant_id,agent_id,kind,name) VALUES(:s,:t,:a,'text','synthetic')"
            ),
            values,
        )
        db.execute(
            text(
                "INSERT INTO knowledge_jobs(tenant_id,agent_id,source_id,source_revision,created_at) VALUES(:t,:a,:s,1,:now)"
            ),
            values,
        )
    factory = worker_factory(database)
    assert run_once(factory, clock=lambda: NOW)["failed"] == 0
    with Session(database) as db:
        rows = list(db.scalars(select(AnalyticsAlert)))
        assert len(rows) == 1
        row = rows[0]
        assert (
            row.kind == "backlog"
            and row.channel is None
            and row.agent_id == analytics_seed["agent_a"]
            and row.tenant_id == analytics_seed["tenant_a"]
        )
    with database.begin() as db:
        db.execute(text("UPDATE knowledge_jobs SET status='cancelled'"))
    assert run_once(factory, clock=lambda: NOW + timedelta(seconds=1))["failed"] == 0
    with Session(database) as db:
        assert db.scalar(select(AnalyticsAlert)).state == "resolved"
