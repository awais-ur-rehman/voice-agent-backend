"""Strict native provenance and measurement bounds."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests_analytics.conftest import insert_call
from tests_api.conftest import NOW


def test_stage_metrics_publish_only_committed_native_insert_once(database, analytics_seed):
    from analytics.prometheus import Registry
    from analytics.telemetry import NativeBatch, record_native
    from scheduler.models import AgentSession

    registry = Registry()
    with database.begin() as db:
        insert_call(
            db,
            analytics_seed,
            channel="voice",
            started_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
        )
    payload = NativeBatch.model_validate(
        {"generation": 1, "revision": 0, "observations": [sample()]}
    )
    with Session(database, info={"analytics_metrics": registry}) as db:
        with db.begin():
            row = db.get(AgentSession, analytics_seed["session_a"])
            row.transport_mode = "voice"
            record_native(db, row, payload, NOW)
            assert "analytics_stage_seconds_count{" not in registry.render()
        record_native(db, row, payload, NOW)
        db.commit()
        with pytest.raises(ValueError), db.begin():
            record_native(
                db,
                row,
                NativeBatch.model_validate(
                    {"generation": 1, "revision": 0, "observations": [sample()]}
                ),
                NOW,
            )
            raise ValueError("rollback")
        with db.begin():
            with pytest.raises(ValueError), db.begin_nested():
                record_native(
                    db,
                    row,
                    NativeBatch.model_validate(
                        {"generation": 1, "revision": 0, "observations": [sample()]}
                    ),
                    NOW,
                )
                raise ValueError("savepoint rollback")
    line = 'analytics_stage_seconds_count{stage="stt_completion",language="en",channel="voice",status="observed"} 1'
    assert line in registry.render()


def test_metric_savepoint_descendants_close_reuse_and_observer_failure(database, analytics_seed):
    from analytics.models import CallRecord
    from analytics.prometheus import Registry, committed_observation
    from tests_analytics.test_rollups import populate

    populate(database, analytics_seed)
    registry = Registry()
    db = Session(database, info={"analytics_metrics": registry})

    def emit():
        committed_observation(
            db,
            db.get(CallRecord, analytics_seed["session_a"]),
            stage="tool",
            language="en",
            duration_ms=2,
        )

    with db.begin():
        with pytest.raises(ValueError), db.begin_nested():
            with db.begin_nested():
                emit()
            raise ValueError("parent savepoint rollback")
        with db.begin_nested():
            emit()
    assert (
        'analytics_stage_seconds_count{stage="tool",language="en",channel="text",status="observed"} 1'
        in registry.render()
    )
    emit()
    db.close()
    with db.begin():
        emit()
    assert (
        'analytics_stage_seconds_count{stage="tool",language="en",channel="text",status="observed"} 2'
        in registry.render()
    )
    registry.observe_stage = lambda *args: (_ for _ in ()).throw(RuntimeError("unavailable"))
    with db.begin():
        emit()
    db.close()


def test_stage_registry_labels_are_bounded_absence_is_not_a_zero_sample():
    from analytics.prometheus import Registry

    registry = Registry()
    for labels in [
        ("private", "en", "voice", "observed"),
        ("tool", "private", "text", "success"),
        ("tool", "en", "private", "failure"),
        ("tool", "en", "text", "private"),
    ]:
        registry.observe_stage(*labels, 3)
    registry.observe_stage("tool", "en", "text", "success", float("nan"))
    assert "analytics_stage_seconds_count{" not in registry.render()
    registry.observe_stage("tool", "en", "text", "failure", 15)
    assert 'status="failure"} 1' in registry.render()
    assert 'stage="tts_first_byte"' not in registry.render()
    assert "analytics_stage_seconds_count{" not in Registry().render()


def sample(**changes):
    return (
        dict(
            event_id=str(uuid4()),
            stage="stt_completion",
            language="en",
            duration_ms=12.5,
            observed_at=NOW.isoformat(),
            metadata={"stt_confidence": 0.4, "uncertain": True},
            output_kind="input",
        )
        | changes
    )


def test_native_contract_forbids_unmeasured_canonical_and_foreign_identity():
    from analytics.telemetry import NativeBatch

    for change in [
        {"stage": "tts_first_byte"},
        {"stage": "llm_completion"},
        {"duration_ms": -1},
        {"duration_ms": float("nan")},
        {"tenant_id": str(uuid4())},
        {"metadata": {"text": "private"}},
        {"output_kind": "input", "stage": "audio_queue"},
        {"duration_ms": True},
    ]:
        with pytest.raises(ValidationError):
            NativeBatch.model_validate(
                {"generation": 1, "revision": 0, "observations": [sample(**change)]}
            )
    for values in [[], [sample()] * 33]:
        with pytest.raises(ValidationError):
            NativeBatch.model_validate({"generation": 1, "revision": 0, "observations": values})


def test_native_storage_dedup_conflict_clock_text_and_revision_fences(database, analytics_seed):
    from analytics.models import CallRecord, StageMetric, TurnEvent
    from analytics.telemetry import NativeBatch, record_native
    from scheduler.errors import AppError
    from scheduler.models import AgentSession

    ids = analytics_seed
    with database.begin() as db:
        insert_call(
            db,
            ids,
            channel="voice",
            started_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
        )
    payload = {"generation": 1, "revision": 0, "observations": [sample()]}
    with Session(database) as db, db.begin():
        row = db.get(AgentSession, ids["session_a"])
        row.transport_mode = "voice"
        batch = NativeBatch.model_validate(payload)
        record_native(db, row, batch, NOW)
        record_native(db, row, batch, NOW)
        assert len(list(db.scalars(select(StageMetric)))) == 1
        assert len(list(db.scalars(select(TurnEvent)))) == 1
        assert db.get(CallRecord, row.id).quality_flags == ["low_stt_confidence"]
        assert db.scalar(select(TurnEvent)).kind == "metric.turn"
        for altered in [
            sample(event_id=payload["observations"][0]["event_id"], duration_ms=13),
            sample(observed_at=(NOW + timedelta(seconds=6)).isoformat()),
            sample(observed_at=(NOW - timedelta(minutes=11)).isoformat()),
            sample(observed_at=(NOW - timedelta(minutes=2)).isoformat()),
        ]:
            with pytest.raises(AppError), db.begin_nested():
                record_native(
                    db, row, NativeBatch.model_validate(payload | {"observations": [altered]}), NOW
                )
        with pytest.raises(AppError):
            record_native(db, row, batch.model_copy(update={"revision": 1}), NOW)
        row.transport_mode = "text"
        with pytest.raises(AppError):
            record_native(db, row, batch, NOW)


def test_tracing_is_allowlisted_correlated_and_never_records_private_exceptions():
    from analytics.tracing import safe_span

    calls = []

    class Span:
        def end(self):
            calls.append("end")

        def set_status(self, status):
            calls.append(status.description)

    class Tracer:
        def start_span(self, name, **kwargs):
            calls.append((name, kwargs))
            return Span()

    identity = uuid4()
    with pytest.raises(RuntimeError):
        with safe_span("provider.invoke", identity, tracer=Tracer()):
            raise RuntimeError("private text")
    name, kwargs = calls[0]
    assert name == "provider.invoke"
    assert kwargs["record_exception"] is False and kwargs["set_status_on_exception"] is False
    assert kwargs["attributes"] == {"component": "nim"}
    assert "private" not in repr(calls)
    with pytest.raises(ValueError):
        with safe_span("private body", identity):
            pass


def test_native_provenance_rejects_cache_as_synthesis_and_input_output_confusion():
    from analytics.telemetry import NativeBatch

    values = [
        sample(
            stage="tts_buffered_generation",
            output_kind="greeting",
            metadata={"cache_hit": True, "output_state": "generated"},
        ),
        sample(stage="audio_queue", output_kind="greeting", metadata={"output_state": "generated"}),
        sample(metadata={"cache_hit": True}),
        sample(observed_at="2026-11-01T00:00:00"),
    ]
    for value in values:
        with pytest.raises(ValidationError):
            NativeBatch.model_validate({"generation": 1, "revision": 0, "observations": [value]})


def test_tracing_default_has_no_export_and_injected_export_failure_is_harmless(monkeypatch):
    import socket

    from opentelemetry import trace

    from analytics.tracing import safe_span

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external telemetry")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    with safe_span("runtime.command", uuid4()):
        assert not trace.get_current_span().is_recording()

    class Broken:
        def start_span(self, *args, **kwargs):
            raise RuntimeError("private exporter exception")

    with safe_span("provider.invoke", uuid4(), tracer=Broken()):
        pass

    class Span:
        def end(self):
            raise RuntimeError("private exporter exception")

    class Tracer:
        def start_span(self, *args, **kwargs):
            return Span()

    with safe_span("provider.invoke", uuid4(), tracer=Tracer()):
        pass
