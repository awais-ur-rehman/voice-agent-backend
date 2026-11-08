"""Storage boundaries: retries, ownership, safe shape and actual samples."""

import importlib.util
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests_analytics.conftest import insert_call

TABLES = {
    "analytics_settings",
    "analytics_calls",
    "analytics_tasks",
    "analytics_operations",
    "analytics_turns",
    "analytics_transcripts",
    "analytics_stage_metrics",
    "analytics_usage_invocations",
    "analytics_hourly_rollups",
    "analytics_daily_rollups",
    "analytics_alerts",
    "analytics_dirty_buckets",
}


@pytest.mark.parametrize(
    "stage", ["tts_buffered_generation", "audio_queue", "end_of_speech_to_audio_queue"]
)
def test_diagnostics_remain_distinct_actual_samples(stage):
    from analytics.contracts import SafeMetadata, StageObservation

    assert (
        StageObservation(
            event_id=uuid4(), stage=stage, language="ur", origin="native", duration_ms=5
        ).stage
        == stage
    )
    safe = SafeMetadata(stt_confidence=0.4, uncertain=True, cache_hit=False, output_state="queued")
    assert safe.model_dump(exclude_none=True) == dict(
        stt_confidence=0.4, uncertain=True, cache_hit=False, output_state="queued"
    )


def test_analytics_storage_exists_with_forced_rls(database):
    with database.connect() as db:
        protected = set(
            db.execute(
                text(
                    "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relrowsecurity AND relforcerowsecurity"
                )
            ).scalars()
        )
    assert TABLES <= protected


def test_request_local_observation_rejects_text_unknowns_and_nonfinite():
    assert importlib.util.find_spec("analytics") is not None, "typed analytics contracts missing"
    from pydantic import ValidationError

    from analytics.contracts import StageObservation, TurnObservation

    sample = StageObservation(
        event_id=uuid4(), stage="llm_completion", language="en", origin="server", duration_ms=12
    )
    assert TurnObservation(stages=[sample]).stages[0].duration_ms == 12
    for value in (-1, 600001, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            StageObservation(
                event_id=uuid4(),
                stage="llm_completion",
                language="en",
                origin="server",
                duration_ms=value,
            )
    with pytest.raises(ValidationError):
        TurnObservation(caller_text="Synthetic private text")
    with pytest.raises(ValidationError):
        TurnObservation(stages=[sample] * 33)
    with pytest.raises(ValidationError):
        StageObservation(
            event_id=uuid4(), stage="fabricated", language="en", origin="server", duration_ms=1
        )


def test_calls_require_same_tenant_agent_session(database, analytics_seed):
    for override in (
        dict(agent_id=analytics_seed["agent_b"]),
        dict(session_id=analytics_seed["session_b"]),
    ):
        with pytest.raises(IntegrityError) as error, database.begin() as db:
            insert_call(db, analytics_seed, **override)
        assert error.value.orig.sqlstate == "23503"
    another_agent = uuid4()
    with database.begin() as db:
        db.execute(
            text("INSERT INTO agents(id,tenant_id,name) VALUES(:id,:tenant,'Synthetic alternate')"),
            dict(id=another_agent, tenant=analytics_seed["tenant_a"]),
        )
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        insert_call(db, analytics_seed, agent_id=another_agent)
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize(
    "override",
    [
        dict(channel="browser"),
        dict(purpose="training"),
        dict(consent="maybe"),
        dict(status="mystery"),
        dict(outcome="booked-ish"),
        dict(utc_offset_minutes=1000),
        dict(expires_at="2026-11-07T08:00:00Z"),
        dict(ended_at="2026-11-07T08:00:00Z"),
        dict(parked_task_ids="{}"),
    ],
)
def test_invalid_call_snapshots_rejected(database, analytics_seed, override):
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        insert_call(db, analytics_seed, **override)
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("days", [0, 31])
def test_retention_cannot_escape_product_bounds(database, seed, days):
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(
            text("INSERT INTO analytics_settings(tenant_id,retention_days) VALUES(:tenant,:days)"),
            dict(tenant=seed["tenant_a"], days=days),
        )
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("duration", ["-1", "600001", "NaN", "Infinity", "-Infinity"])
def test_stage_samples_are_actual_finite_bounded_durations(database, analytics_seed, duration):
    with database.begin() as db:
        call = insert_call(db, analytics_seed)
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(
            text(
                "INSERT INTO analytics_stage_metrics(tenant_id,session_id,event_id,stage,language,origin,duration_ms,observed_at) VALUES(:tenant_id,:session_id,:event,'stt_completion','ur','native',CAST(:duration AS double precision),now())"
            ),
            call | dict(event=uuid4(), duration=duration),
        )
    assert error.value.orig.sqlstate == "23514"


def test_stable_event_id_cannot_double_count_and_task_owner_is_composite(database, analytics_seed):
    ids, event, task = analytics_seed, uuid4(), uuid4()
    with database.begin() as db:
        call = insert_call(db, ids)
        other = insert_call(db, ids, "b")
        db.execute(
            text(
                "INSERT INTO analytics_tasks(id,tenant_id,session_id,kind,started_at) VALUES(:id,:tenant_id,:session_id,'book',now())"
            ),
            other | dict(id=task),
        )
        db.execute(
            text(
                "INSERT INTO analytics_operations(tenant_id,session_id,event_id,operation,occurred_at) VALUES(:tenant_id,:session_id,:event,'book',now())"
            ),
            call | dict(event=event),
        )
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(
            text(
                "INSERT INTO analytics_operations(tenant_id,session_id,event_id,operation,occurred_at) VALUES(:tenant_id,:session_id,:event,'book',now())"
            ),
            call | dict(event=event),
        )
    assert error.value.orig.sqlstate == "23505"
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(
            text(
                "INSERT INTO analytics_operations(tenant_id,session_id,event_id,task_id,operation,occurred_at) VALUES(:tenant_id,:session_id,:event,:task,'book',now())"
            ),
            call | dict(event=uuid4(), task=task),
        )
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize(
    "metadata",
    [
        "[]",
        '{"question":"Synthetic private text"}',
        '{"prompt_id":"patient Synthetic"}',
        '{"stt_confidence":2}',
        '{"progressed":"yes"}',
    ],
)
def test_safe_event_json_cannot_retain_caller_text_or_unbounded_values(
    database, analytics_seed, metadata
):
    with database.begin() as db:
        call = insert_call(db, analytics_seed)
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(
            text(
                "INSERT INTO analytics_turns(tenant_id,session_id,event_id,kind,language,occurred_at,safe_metadata) VALUES(:tenant_id,:session_id,:event,'turn.user','en',now(),CAST(:metadata AS jsonb))"
            ),
            call | dict(event=uuid4(), metadata=metadata),
        )
    assert error.value.orig.sqlstate == "23514"


def test_unknown_usage_preserved_separately_from_successful_domain_events(database, analytics_seed):
    with database.begin() as db:
        call, invocation = insert_call(db, analytics_seed), uuid4()
        db.execute(
            text(
                "INSERT INTO analytics_usage_invocations(tenant_id,session_id,invocation_id,provider,model,status,started_at) VALUES(:tenant_id,:session_id,:invocation,'nim','configured-model','timeout',now())"
            ),
            call | dict(invocation=invocation),
        )
        assert db.execute(
            text(
                "SELECT input_tokens,output_tokens,input_rate,output_rate,cost FROM analytics_usage_invocations WHERE invocation_id=:invocation"
            ),
            dict(invocation=invocation),
        ).one() == (None, None, None, None, None)
        assert db.execute(text("SELECT count(*) FROM analytics_operations")).scalar_one() == 0


def test_transcript_has_separate_text_and_defaults_off(database, analytics_seed):
    with database.begin() as db:
        insert_call(db, analytics_seed)
        assert db.execute(
            text(
                "SELECT capture_enabled_at_start,consent,transcript_deleted_at FROM analytics_calls"
            )
        ).one() == (False, "off", None)
        columns = db.execute(
            text(
                "SELECT table_name,column_name FROM information_schema.columns WHERE table_name LIKE 'analytics_%' AND data_type IN ('jsonb','text','character varying')"
            )
        ).all()
        assert ("analytics_transcripts", "text") in columns
        assert not any(
            name in {"caller_text", "assistant_text", "question", "raw_arguments", "readback"}
            for _, name in columns
        )


def test_additive_migration_round_trip(database):
    from alembic import command
    from alembic.config import Config

    from tests_api.conftest import ADMIN_URL, ROOT

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", ADMIN_URL.replace("%", "%%"))
    command.downgrade(cfg, "0009")
    with database.connect() as db:
        assert db.execute(text("SELECT to_regclass('analytics_calls')")).scalar_one() is None
        assert db.execute(text("SELECT to_regclass('knowledge_sources')")).scalar_one() is not None
    command.upgrade(cfg, "head")


def test_orm_columns_match_migrated_analytics_storage(database):
    from analytics.models import Base

    with database.connect() as db:
        for name in TABLES:
            actual = set(
                db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:name"
                    ),
                    dict(name=name),
                ).scalars()
            )
            assert actual == set(Base.metadata.tables[name].columns.keys()), name


@pytest.mark.parametrize(
    "column,value", [("stage", "fabricated"), ("language", "patient-name"), ("origin", "browser")]
)
def test_metric_enum_cannot_admit_untrusted_stage_labels(database, analytics_seed, column, value):
    with database.begin() as db:
        call = insert_call(db, analytics_seed)
    values = (
        call
        | dict(event=uuid4(), stage="llm_completion", language="en", origin="server")
        | {column: value}
    )
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(
            text(
                "INSERT INTO analytics_stage_metrics(tenant_id,session_id,event_id,stage,language,origin,duration_ms,observed_at) VALUES(:tenant_id,:session_id,:event,:stage,:language,:origin,10,now())"
            ),
            values,
        )
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "table,extras",
    [
        (
            "analytics_turns",
            "kind,language,occurred_at) VALUES(:tenant_id,:session_id,:event,'turn.user','en',now())",
        ),
        (
            "analytics_stage_metrics",
            "stage,language,origin,duration_ms,observed_at) VALUES(:tenant_id,:session_id,:event,'llm_completion','en','server',1,now())",
        ),
        (
            "analytics_usage_invocations",
            "provider,model,status,started_at) VALUES(:tenant_id,:session_id,:event,'nim','synthetic','failure',now())",
        ),
    ],
)
def test_all_retry_sensitive_ledgers_reject_duplicate_identity(
    database, analytics_seed, table, extras
):
    with database.begin() as db:
        values = insert_call(db, analytics_seed) | dict(event=uuid4())
        identity = "invocation_id" if table == "analytics_usage_invocations" else "event_id"
        statement = text(f"INSERT INTO {table}(tenant_id,session_id,{identity}," + extras)
        db.execute(statement, values)
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(statement, values)
    assert error.value.orig.sqlstate == "23505"


@pytest.mark.parametrize(
    "column,value",
    [("input_tokens", "-1"), ("input_rate", "NaN"), ("output_rate", "Infinity"), ("cost", "1")],
)
def test_usage_rejects_invalid_or_fabricated_cost_coverage(database, analytics_seed, column, value):
    with database.begin() as db:
        values = insert_call(db, analytics_seed) | dict(event=uuid4(), value=value)
    with pytest.raises(DBAPIError) as error, database.begin() as db:
        db.execute(
            text(
                f"INSERT INTO analytics_usage_invocations(tenant_id,session_id,invocation_id,provider,model,status,started_at,{column}) VALUES(:tenant_id,:session_id,:event,'nim','synthetic','failure',now(),:value)"
            ),
            values,
        )
    assert error.value.orig.sqlstate == ("22003" if value == "Infinity" else "23514")


def test_dst_repeated_hour_keeps_two_buckets_but_replay_deduplicates(database, analytics_seed):
    values = dict(tenant=analytics_seed["tenant_a"], agent=analytics_seed["agent_a"])
    statement = text(
        "INSERT INTO analytics_hourly_rollups(tenant_id,agent_id,purpose,channel,timezone,local_date,bucket_start,utc_offset_minutes,as_of) VALUES(:tenant,:agent,'production','voice','America/New_York','2026-11-01',:bucket,:offset,now())"
    )
    with database.begin() as db:
        db.execute(statement, values | dict(bucket="2026-11-01T05:00:00Z", offset=-240))
        db.execute(statement, values | dict(bucket="2026-11-01T06:00:00Z", offset=-300))
        assert db.execute(text("SELECT count(*) FROM analytics_hourly_rollups")).scalar_one() == 2
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(statement, values | dict(bucket="2026-11-01T06:00:00Z", offset=-300))
    assert error.value.orig.sqlstate == "23505"


def test_dirty_work_and_alert_history_have_bounded_deduplicated_shape(database, analytics_seed):
    values = dict(tenant=analytics_seed["tenant_a"], agent=analytics_seed["agent_a"])
    dirty = text(
        "INSERT INTO analytics_dirty_buckets(tenant_id,agent_id,purpose,channel,timezone,local_date,bucket_start,utc_offset_minutes) VALUES(:tenant,:agent,'production','text','Asia/Karachi','2026-11-08','2026-11-08T08:00:00Z',300)"
    )
    with database.begin() as db:
        db.execute(dirty, values)
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(dirty, values)
    assert error.value.orig.sqlstate == "23505"
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        db.execute(
            text(
                "INSERT INTO analytics_alerts(tenant_id,agent_id,channel,kind,state,window_start,window_end,numerator,denominator,opened_at,last_seen_at) VALUES(:tenant,:agent,'voice','latency','open','2026-11-08T08:00:00Z','2026-11-08T09:00:00Z',1,5,now(),now())"
            ),
            values,
        )
    assert error.value.orig.sqlstate == "23514"


def test_native_diagnostic_storage_preserves_safe_uncertainty_without_text(
    database, analytics_seed
):
    with database.begin() as db:
        call = insert_call(db, analytics_seed)
        db.execute(
            text(
                "INSERT INTO analytics_turns(tenant_id,session_id,event_id,kind,language,occurred_at,safe_metadata) VALUES(:tenant_id,:session_id,:event,'turn.user','ur',now(),CAST(:metadata AS jsonb))"
            ),
            call
            | dict(
                event=uuid4(),
                metadata='{"stt_confidence":0.4,"uncertain":true,"cache_hit":false,"output_state":"queued"}',
            ),
        )
        for stage in ("tts_buffered_generation", "audio_queue", "end_of_speech_to_audio_queue"):
            db.execute(
                text(
                    "INSERT INTO analytics_stage_metrics(tenant_id,session_id,event_id,stage,language,origin,duration_ms,observed_at) VALUES(:tenant_id,:session_id,:event,:stage,'ur','native',10,now())"
                ),
                call | dict(event=uuid4(), stage=stage),
            )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM analytics_stage_metrics WHERE stage IN ('tts_first_byte','end_of_speech_to_audio')"
                )
            ).scalar_one()
            == 0
        )


def test_parked_task_stack_cannot_retain_payload_or_exceed_eight(database, analytics_seed):
    for value in (
        '["Synthetic private text"]',
        '["' + str(uuid4()) + '",' + ",".join('"' + str(uuid4()) + '"' for _ in range(8)) + "]",
    ):
        with pytest.raises(IntegrityError) as error, database.begin() as db:
            insert_call(db, analytics_seed, parked_task_ids=value)
        assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "slots",
    ["[null]", '[null,"' + str(uuid4()) + '",null]', "[null,null,null,null,null,null,null,null]"],
)
def test_parked_task_slots_allow_bounded_opaque_placeholders(database, analytics_seed, slots):
    with database.begin() as db:
        insert_call(db, analytics_seed, parked_task_ids=slots)


@pytest.mark.parametrize(
    "slots",
    [
        "[null,null,null,null,null,null,null,null,null]",
        "[null,{}]",
        '[null,"private-frame"]',
        "[null,1]",
    ],
)
def test_parked_task_slots_still_reject_payloads_and_excess_depth(database, analytics_seed, slots):
    with pytest.raises(IntegrityError) as error, database.begin() as db:
        insert_call(db, analytics_seed, parked_task_ids=slots)
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("same_clinic", [True, False])
def test_parked_task_restore_never_adopts_or_mutates_other_session(
    database, analytics_seed, same_clinic
):
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from analytics.models import CallRecord, TaskRecord
    from analytics.tasks import reconcile_task

    now = datetime(2026, 11, 8, 8, tzinfo=UTC)
    with Session(database) as db:
        if same_clinic:
            analytics_seed = dict(analytics_seed)
            other_session = uuid4()
            db.execute(
                text(
                    "INSERT INTO agent_sessions(id,tenant_id,agent_id,version_id,token_jti_hash,expires_at) SELECT :other,tenant_id,agent_id,version_id,'synthetic-other',expires_at FROM agent_sessions WHERE id=:original"
                ),
                {"other": other_session, "original": analytics_seed["session_a"]},
            )
            analytics_seed.update(
                tenant_b=analytics_seed["tenant_a"],
                agent_b=analytics_seed["agent_a"],
                session_b=other_session,
            )
        insert_call(db, analytics_seed, "a")
        insert_call(db, analytics_seed, "b")
        foreign = TaskRecord(
            tenant_id=analytics_seed["tenant_b"],
            session_id=analytics_seed["session_b"],
            kind="book",
            status="parked",
            started_at=now,
        )
        db.add(foreign)
        db.flush()
        call = db.get(CallRecord, analytics_seed["session_a"])
        call.parked_task_ids = [str(foreign.id)]
        assert (
            reconcile_task(
                db,
                call,
                {"task": "cancel", "parked": [{}]},
                {"task": "book", "parked": []},
                {"name": "resume_task"},
                True,
                now,
            )
            is None
        )
        assert call.parked_task_ids == [] and call.active_task_id is None
        assert foreign.status == "parked" and foreign.ended_at is None
        # Dropping an oversized/mismatched association cannot mutate it either.
        call.parked_task_ids = [str(foreign.id)]
        reconcile_task(
            db,
            call,
            {"task": "book", "parked": []},
            {"task": "book", "parked": []},
            {"name": "get_clinic_info"},
            True,
            now,
        )
        assert call.parked_task_ids == [] and foreign.status == "parked"


@pytest.mark.parametrize("has_placeholder", [True, False])
def test_slot_migration_downgrade_refuses_lossy_active_placeholders(
    database, analytics_seed, monkeypatch, has_placeholder
):
    from importlib import import_module
    from types import SimpleNamespace

    migration = import_module("migrations.versions.0012_parked_task_slots")
    with database.connect() as db:
        transaction = db.begin()
        try:
            insert_call(db, analytics_seed, parked_task_ids="[null]" if has_placeholder else "[]")
            monkeypatch.setattr(
                migration, "op", SimpleNamespace(execute=lambda sql: db.execute(text(sql)))
            )
            if has_placeholder:
                with pytest.raises(DBAPIError, match="Cannot downgrade nullable parked task slots"):
                    migration.downgrade()
            else:
                migration.downgrade()
                assert not db.scalar(text("SELECT analytics_uuid_stack('[null]'::jsonb)"))
                migration.upgrade()
                assert db.scalar(text("SELECT analytics_uuid_stack('[null]'::jsonb)"))
        finally:
            transaction.rollback()
