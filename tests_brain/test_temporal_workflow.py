"""Temporal constraints survive resolver -> actual slot offers -> appointment."""

from datetime import UTC, date, datetime, time
from uuid import UUID

import pytest
from sqlalchemy import select

from scheduler.models import (
    Agent,
    AgentVersion,
    Appointment,
    ExceptionDay,
    Provider,
    ProviderService,
    Schedule,
)
from tests_brain import test_tool_workflows

workflow = test_tool_workflows.workflow


def setup(turn, seed, phrase="2026-11-03"):
    test_tool_workflows.identify(turn)
    return turn(
        "update_dialogue_state",
        {
            "service_id": str(seed["service_a"]),
            "provider_id": str(seed["provider_a"]),
            "date_phrase": phrase,
        },
    )


def local_offers(state):
    from zoneinfo import ZoneInfo

    return [
        datetime.fromisoformat(row["starts_at"])
        .astimezone(ZoneInfo("Asia/Karachi"))
        .strftime("%H:%M")
        for row in state["offers"]
    ]


@pytest.mark.parametrize(
    "phrase,want",
    [
        ("2026-11-03 at 3 pm", ["15:00"]),
        ("2026-11-03 صبح", ["09:00", "09:30", "10:00"]),
        ("2026-11-03 دوپہر", ["12:00", "12:30", "13:00"]),
        ("2026-11-03 شام", ["15:00", "15:30", "16:00"]),
    ],
)
def test_date_and_clock_or_window_control_real_offers(workflow, seed, phrase, want):
    turn, state, db, *_ = workflow
    assert setup(turn, seed, phrase)["ok"]
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == want
    assert turn("hold_slot", {"ref": state["offers"][0]["ref"]})["ok"]
    assert turn("read_back")["ok"]
    assert turn("book_appointment", text="yes")["ok"]
    booked = db.scalar(select(Appointment))
    assert booked.starts_at == datetime(
        2026, 11, 3, int(want[0][:2]) - 5, int(want[0][3:]), tzinfo=UTC
    )


def test_standalone_time_correction_keeps_date_and_invalidates_confirmation(workflow, seed):
    turn, state, *_ = workflow
    assert setup(turn, seed)["ok"]
    assert turn("get_available_slots")["ok"]
    assert turn("hold_slot", {"ref": state["offers"][0]["ref"]})["ok"]
    assert turn("read_back")["ok"]
    corrected = turn("update_dialogue_state", {"date_phrase": "ساڑھے تین"})
    assert corrected["ok"], corrected
    assert state["fields"]["date"] == "2026-11-03"
    assert not state.get("hold") and not state.get("readback")
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == ["15:30"]


def test_date_only_correction_retains_known_exact_time(workflow, seed):
    turn, state, *_ = workflow
    assert setup(turn, seed, "2026-11-03 at 3 pm")["ok"]
    assert turn("update_dialogue_state", {"date_phrase": "2026-11-04"})["ok"]
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == ["15:00"]
    assert state["fields"]["date"] == "2026-11-04"


def test_new_window_replaces_exact_time_and_excludes_end_boundary(workflow, seed):
    turn, state, *_ = workflow
    assert setup(turn, seed, "2026-11-03 at 3 pm")["ok"]
    assert turn("update_dialogue_state", {"date_phrase": "صبح"})["ok"]
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == ["09:00", "09:30", "10:00"]
    # Move opening to 11 so the third candidate at noon would violate the window.
    _, _, db, *_ = workflow
    rule = db.scalar(
        select(Schedule).where(Schedule.provider_id == seed["provider_a"], Schedule.weekday == 2)
    )
    rule.start_time = time(11)
    db.flush()
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == ["11:00", "11:30"]


@pytest.mark.parametrize("phrase", ["tomorrow at 10", "tomorrow nonsense", "अगले منگل"])
def test_ambiguous_or_invalid_correction_cannot_reuse_old_date(workflow, seed, phrase):
    turn, state, db, *_ = workflow
    assert setup(turn, seed)["ok"]
    # Both ten AM and ten PM survive this provider's Monday opening rules.
    rule = db.scalar(
        select(Schedule).where(Schedule.provider_id == seed["provider_a"], Schedule.weekday == 1)
    )
    rule.end_time = time(23)
    db.flush()
    assert not turn("update_dialogue_state", {"date_phrase": phrase})["ok"]
    assert not turn("get_available_slots")["ok"]
    assert state["offers"] == []
    assert turn("update_dialogue_state", {"date_phrase": "2026-11-03 at 3 pm"})["ok"]
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == ["15:00"]


def test_am_pm_uses_selected_provider_not_other_provider_hours(workflow, seed):
    turn, state, db, *_ = workflow
    evening = Provider(tenant_id=seed["tenant_a"], name="Evening Test", name_ur="شام ٹیسٹ")
    db.add(evening)
    db.flush()
    db.add(
        ProviderService(
            tenant_id=seed["tenant_a"],
            provider_id=evening.id,
            service_id=seed["service_a"],
        )
    )
    db.add(
        Schedule(
            tenant_id=seed["tenant_a"],
            provider_id=evening.id,
            weekday=2,
            start_time=time(20),
            end_time=time(23),
            slot_min=30,
        )
    )
    db.flush()
    assert setup(turn, seed, "2026-11-03 das baje")["ok"]
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == ["10:00"]


def test_am_pm_does_not_bridge_split_shift_or_wrong_weekday(workflow, seed):
    turn, state, db, *_ = workflow
    rule = db.scalar(
        select(Schedule).where(Schedule.provider_id == seed["provider_a"], Schedule.weekday == 2)
    )
    rule.start_time = time(8)
    rule.end_time = time(9)
    db.add(
        Schedule(
            tenant_id=seed["tenant_a"],
            provider_id=seed["provider_a"],
            weekday=2,
            start_time=time(21),
            end_time=time(23),
            slot_min=30,
        )
    )
    db.flush()
    assert setup(turn, seed, "2026-11-03 das baje")["ok"]
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == ["22:00"]


def test_custom_hours_override_controls_am_pm(workflow, seed):
    turn, state, db, *_ = workflow
    db.add(
        ExceptionDay(
            tenant_id=seed["tenant_a"],
            provider_id=seed["provider_a"],
            start_date=date(2026, 11, 3),
            end_date=date(2026, 11, 3),
            kind="custom",
            start_time=time(20),
            end_time=time(23),
        )
    )
    db.flush()
    assert setup(turn, seed, "2026-11-03 das baje")["ok"]
    assert turn("get_available_slots")["ok"]
    assert local_offers(state) == ["22:00"]


def test_prayer_relative_request_uses_published_version_and_friday_break(workflow, seed):
    turn, state, db, store, actor = workflow
    # Publish a new immutable test version rather than UPDATE a frozen version.
    version = AgentVersion(
        tenant_id=actor.tenant_id,
        agent_id=actor.agent_id,
        config={"prayer_break": ["12:45", "14:30"]},
    )
    db.add(version)
    db.flush()
    from brain.security import RuntimeActor
    from brain.sessions import create_session
    from brain.tools import ToolEngine
    from scheduler.config import Settings
    from scheduler.db import Principal
    from tests_api.conftest import APP_URL, NOW, SECRET

    db.get(Agent, actor.agent_id).active_version_id = version.id
    created = create_session(
        db,
        Principal(seed["owner_a"], actor.tenant_id, "owner-a@example.com", "owner"),
        actor.agent_id,
        NOW,
        Settings(
            database_url=APP_URL,
            jwt_secret=SECRET,
            runtime_secret="different-runtime-test-secret-32-characters",
            _env_file=None,
        ),
    )
    actor = RuntimeActor(UUID(created["session_id"]), actor.tenant_id, actor.agent_id, version.id)
    state = created["state"]
    engine = ToolEngine(db, actor, store, NOW)

    def configured(name, args=None, text=""):
        r = engine.execute(state, {"name": name, "arguments": args or {}}, text)
        state["revision"] += 1
        return r

    assert setup(configured, seed, "2026-11-06")["ok"]
    assert configured("update_dialogue_state", {"date_phrase": "Jummah ke baad"})["ok"]
    assert configured("get_available_slots")["ok"]
    assert local_offers(state) == ["14:30", "15:00", "15:30"]


def test_time_filter_sees_late_options_beyond_first_hundred_raw_slots(workflow, seed):
    _, state, db, store, actor = workflow
    from brain.tools import ToolEngine

    engine = ToolEngine(db, actor, store, datetime(2026, 11, 2, tzinfo=UTC))

    def turn(name, args=None, text=""):
        response = engine.execute(state, {"name": name, "arguments": args or {}}, text)
        state["revision"] += 1
        return response

    db.add(
        ExceptionDay(
            tenant_id=actor.tenant_id,
            provider_id=seed["provider_a"],
            start_date=date(2026, 11, 8),
            end_date=date(2026, 11, 8),
            kind="custom",
            start_time=time(20),
            end_time=time(23),
        )
    )
    db.flush()
    assert setup(turn, seed, "this week")["ok"]
    assert turn("update_dialogue_state", {"date_phrase": "10 pm"})["ok"]
    response = turn("get_available_slots")
    assert response["ok"], response
    assert local_offers(state) == ["22:00"]
    assert datetime.fromisoformat(state["offers"][0]["starts_at"]).date() == date(2026, 11, 8)
