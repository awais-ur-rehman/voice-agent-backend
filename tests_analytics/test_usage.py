"""Actual fake-HTTP invocations, isolated request contexts and late ledger completion."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import httpx2
import pytest

from brain.provider import Nemotron, ProviderError
from scheduler.config import Settings


def model(handler):
    return Nemotron(
        Settings(
            database_url="postgresql://unused",
            jwt_secret="x" * 40,
            llm_api_key="synthetic",
            _env_file=None,
        ),
        transport=httpx2.MockTransport(handler),
    )


def answer(tokens=7, content='{"name":"ask_caller","arguments":{}}'):
    return httpx2.Response(
        200,
        json={
            "usage": {"prompt_tokens": tokens, "completion_tokens": 3},
            "choices": [{"message": {"content": content}}],
        },
    )


def test_actual_usage_is_reported_even_for_malformed_output_without_first_token():
    from analytics.telemetry import MeasurementSink, measurement_scope

    values = []
    sink = MeasurementSink(writer=values.append)
    with measurement_scope(sink):
        with pytest.raises(ProviderError):
            model(lambda req: answer(content="private malformed output")).plan(
                {"task": "book", "fields": {}}, "private question", {}, {}
            )
    assert [v.status for v in values] == ["started", "malformed"]
    assert values[-1].input_tokens == 7 and values[-1].output_tokens == 3
    assert values[-1].cost is None and values[-1].input_rate is None
    assert values[0].invocation_id == values[1].invocation_id
    assert "private" not in repr(values)
    assert [x.stage for x in sink.stages] == ["llm_completion"]


def test_concurrent_requests_never_attribute_shared_counter_differences():
    from analytics.telemetry import MeasurementSink, measurement_scope

    gate = threading.Barrier(2)
    provider = model(
        lambda req: (
            gate.wait(3),
            answer(
                11
                if any(
                    m.get("role") == "user" and "first" in m.get("content", "")
                    for m in __import__("json").loads(req.content)["messages"]
                )
                else 29
            ),
        )[1]
    )

    def run(text):
        values = []
        sink = MeasurementSink(writer=values.append)
        with measurement_scope(sink):
            provider.plan({"task": "book", "fields": {}}, text, {}, {})
        return sink, values

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(run, "first")
        b = pool.submit(run, "second")
        results = [a.result(), b.result()]
    assert [v[-1].input_tokens for _, v in results] == [11, 29]
    assert all(s.requests == 1 for s, _ in results)
    assert results[0][1][-1].invocation_id != results[1][1][-1].invocation_id


def test_grounding_context_and_late_completion_only_update_original_invocation():
    from analytics.telemetry import MeasurementSink, measurement_scope

    entered, release = threading.Event(), threading.Event()
    values = []

    def serve(req):
        entered.set()
        assert release.wait(3)
        return answer(content="{}")

    provider = model(serve)
    sink = MeasurementSink(writer=values.append)
    with measurement_scope(sink):
        with pytest.raises(ProviderError):
            provider.ground("private", "en", [], deadline=time.monotonic() + 0.03)
    assert entered.is_set() and sink.requests == 1
    release.set()
    limit = time.monotonic() + 3
    while len(values) < 2 and time.monotonic() < limit:
        time.sleep(0.01)
    assert len(values) == 2 and values[-1].status == "timeout"
    assert values[-1].input_tokens == 7
    assert sink.stages == []  # closed turn cannot receive a successful late duration


def test_failed_telemetry_does_not_fail_provider_and_unknown_usage_stays_null():
    from analytics.telemetry import MeasurementSink, measurement_scope

    def unavailable(value):
        raise RuntimeError("private database body")

    sink = MeasurementSink(writer=unavailable)
    with measurement_scope(sink):
        assert (
            model(
                lambda req: httpx2.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"content": '{"name":"ask_caller","arguments":{}}'}}
                        ]
                    },
                )
            )
            .plan({"task": "book", "fields": {}}, "hello", {}, {})
            .name
            == "ask_caller"
        )
    assert sink.requests == 1


def test_usage_ledger_survives_separate_domain_rollback(database, analytics_seed):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from analytics.contracts import UsageObservation
    from analytics.models import UsageInvocation
    from analytics.telemetry import database_writer
    from tests_analytics.conftest import insert_call

    ids = analytics_seed
    with database.begin() as db:
        insert_call(db, ids)
    from analytics.prometheus import Registry

    registry = Registry()
    write = database_writer(
        lambda: Session(database, info={"analytics_metrics": registry}),
        ids["tenant_a"],
        ids["session_a"],
    )
    value = UsageObservation(
        invocation_id=uuid4(), model="safe/model", status="started", started_at=datetime.now(UTC)
    )
    write(value)
    write(value)
    with pytest.raises(RuntimeError), Session(database) as db, db.begin():
        raise RuntimeError("domain rollback")
    write(
        value.model_copy(
            update={
                "status": "success",
                "completed_at": datetime.now(UTC),
                "input_tokens": 9,
                "output_tokens": 4,
            }
        )
    )
    with Session(database) as db:
        rows = list(db.scalars(select(UsageInvocation)))
        assert len(rows) == 1 and rows[0].input_tokens == 9
    assert (
        'analytics_stage_events_total{stage="llm_completion",language="unknown",channel="text",status="started"} 1'
        in registry.render()
    )
    assert (
        'analytics_stage_events_total{stage="llm_completion",language="unknown",channel="text",status="success"} 1'
        in registry.render()
    )
    assert "analytics_stage_seconds_count{" not in registry.render()


def test_configured_usd_rates_snapshot_reported_counts_only():
    from decimal import Decimal

    from analytics.telemetry import MeasurementSink, measurement_scope

    values = []
    sink = MeasurementSink(writer=values.append)
    provider = model(lambda req: answer(tokens=10))
    provider.settings.llm_input_rate_usd_per_million = Decimal("2.5")
    provider.settings.llm_output_rate_usd_per_million = Decimal("5")
    with measurement_scope(sink):
        provider.plan({"task": "book", "fields": {}}, "hi", {}, {})
    assert values[-1].cost == Decimal("0.000040")
    assert values[0].input_rate == Decimal("2.5") and values[0].cost is None


def test_budget_refusal_has_no_invocation_and_invalid_reported_counts_are_unknown():
    from analytics.telemetry import MeasurementSink, measurement_scope

    values = []
    sink = MeasurementSink(writer=values.append)
    provider = model(
        lambda req: httpx2.Response(
            200,
            json={
                "usage": {"prompt_tokens": True, "completion_tokens": -1},
                "choices": [{"message": {"content": '{"name":"ask_caller","arguments":{}}'}}],
            },
        )
    )
    provider.settings.llm_request_budget = 1
    with measurement_scope(sink):
        provider.plan({"task": "book", "fields": {}}, "hi", {}, {})
        with pytest.raises(ProviderError):
            provider.plan({"task": "book", "fields": {}}, "hi", {}, {})
    assert sink.requests == 1 and len(values) == 2
    assert values[-1].input_tokens is None and values[-1].output_tokens is None


def test_http_completion_clock_excludes_decode_and_ledger_writes(monkeypatch):
    import analytics.telemetry as telemetry
    from analytics.telemetry import MeasurementSink, measurement_scope

    clock = [0.0]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: clock[0])

    def write(value):
        clock[0] += 10

    def serve(request):
        clock[0] += 0.005
        return answer()

    def decode(content):
        clock[0] += 3
        return "decoded"

    sink = MeasurementSink(writer=write)
    with measurement_scope(sink):
        assert model(serve)._structured([], {}, "next_action", decode) == "decoded"
    assert sink.stages[0].duration_ms == pytest.approx(5)


def test_http_failure_preserves_actually_reported_usage_without_success_latency():
    from analytics.telemetry import MeasurementSink, measurement_scope

    values = []
    sink = MeasurementSink(writer=values.append)
    provider = model(
        lambda request: httpx2.Response(
            503,
            json={
                "usage": {"prompt_tokens": 17, "completion_tokens": 2},
                "error": "private failure body",
            },
        )
    )
    with measurement_scope(sink), pytest.raises(ProviderError):
        provider.plan({"task": "book", "fields": {}}, "hello", {}, {})
    assert values[-1].status == "failure" and values[-1].input_tokens == 17
    assert sink.stages == [] and "private" not in repr(values)


def test_concurrent_duplicate_terminal_invocations_count_committed_status_once(
    database, analytics_seed
):
    from sqlalchemy.orm import Session

    from analytics.contracts import UsageObservation
    from analytics.prometheus import Registry
    from analytics.telemetry import database_writer
    from tests_analytics.conftest import insert_call

    with database.begin() as db:
        insert_call(db, analytics_seed)
    registry = Registry()
    writer = database_writer(
        lambda: Session(database, info={"analytics_metrics": registry}),
        analytics_seed["tenant_a"],
        analytics_seed["session_a"],
    )
    values = [
        UsageObservation(
            invocation_id=uuid4(), model="synthetic", status="started", started_at=datetime.now(UTC)
        )
        for _ in range(2)
    ]
    for value in values:
        writer(value)
    finals = [
        v.model_copy(update={"status": "failure", "completed_at": datetime.now(UTC)})
        for v in values
    ]
    with ThreadPoolExecutor(4) as pool:
        futures = [pool.submit(writer, value) for value in finals * 2]
        for future in futures:
            future.result()
    assert (
        'analytics_stage_events_total{stage="llm_completion",language="unknown",channel="text",status="failure"} 2'
        in registry.render()
    )
    assert (
        'analytics_stage_events_total{stage="llm_completion",language="unknown",channel="text",status="started"} 2'
        in registry.render()
    )
