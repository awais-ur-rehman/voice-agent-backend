from datetime import UTC, date, datetime, time, timedelta

import pytest

from scheduler.domain import DateException, Reservation, ScheduleRule, candidate_slots

DAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 1, tzinfo=UTC)
RULES = [ScheduleRule(1, time(9), time(12), 30), ScheduleRule(1, time(13), time(17), 30)]


def slots(**changes):
    args = dict(
        day=DAY,
        timezone="Asia/Karachi",
        rules=RULES,
        exceptions=[],
        duration=30,
        before=0,
        after=0,
        busy=[],
        now=NOW,
        notice=120,
        horizon=90,
    )
    return candidate_slots(**(args | changes))


def test_weekly_breaks_grid_and_half_open_reservations():
    found = slots()
    assert len(found) == 14
    assert found[0].hour == 4
    assert datetime(2026, 11, 2, 7, tzinfo=UTC) not in found
    busy = [Reservation(found[0], found[0] + timedelta(minutes=30))]
    assert slots(busy=busy)[0] == found[1]


def test_buffers_fit_within_working_hours_and_respect_existing_buffers():
    found = slots(before=15, after=15)
    assert len(found) == 10
    assert found[0] == datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
    assert (
        slots(
            busy=[Reservation(found[0] - timedelta(minutes=15), found[0] + timedelta(minutes=45))],
            before=15,
            after=15,
        )[0]
        == found[2]
    )


def test_closed_exceptions_win_and_custom_hours_replace_weekly_rules():
    custom = DateException(DAY, DAY, "custom", time(15), time(16))
    assert len(slots(exceptions=[custom])) == 2
    assert slots(exceptions=[custom, DateException(DAY, DAY, "closed")]) == []


def test_notice_horizon_and_naive_time_rejection():
    assert slots(now=datetime(2026, 11, 2, 4, tzinfo=UTC), notice=60)[0].hour == 5
    assert slots(horizon=0) == []
    with pytest.raises(ValueError):
        slots(now=datetime(2026, 11, 1))


def test_dst_gap_skips_nonexistent_times_and_fold_keeps_distinct_instants():
    rules = [ScheduleRule(7, time(1), time(4), 30)]
    gap = slots(
        day=date(2026, 3, 8),
        timezone="America/New_York",
        rules=rules,
        now=datetime(2026, 3, 1, tzinfo=UTC),
        notice=0,
    )
    assert len(gap) == 4
    fold = slots(
        day=date(2026, 11, 1),
        timezone="America/New_York",
        rules=rules,
        now=datetime(2026, 10, 31, tzinfo=UTC),
        notice=0,
    )
    assert len(fold) == 8
    assert len(set(fold)) == 8


def test_custom_hours_inherit_the_matching_weekdays_grid():
    rules = [ScheduleRule(1, time(9), time(12), 30), ScheduleRule(2, time(9), time(12), 15)]
    assert (
        len(slots(rules=rules, exceptions=[DateException(DAY, DAY, "custom", time(15), time(16))]))
        == 2
    )
