"""Deterministic local-calendar scheduling; all returned instants are UTC."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ScheduleRule:
    weekday: int
    start: time
    end: time
    step: int


@dataclass(frozen=True)
class DateException:
    first: date
    last: date
    kind: str
    start: time | None = None
    end: time | None = None


@dataclass(frozen=True)
class Reservation:
    start: datetime
    end: datetime


def local_instants(value: datetime, zone: ZoneInfo) -> list[datetime]:
    return sorted(
        {
            candidate
            for fold in (0, 1)
            if (candidate := value.replace(tzinfo=zone, fold=fold).astimezone(UTC))
            .astimezone(zone)
            .replace(tzinfo=None)
            == value
        }
    )


def candidate_slots(
    *,
    day: date,
    timezone: str,
    rules: list[ScheduleRule],
    exceptions: list[DateException],
    duration: int,
    before: int,
    after: int,
    busy: list[Reservation],
    now: datetime,
    notice: int,
    horizon: int,
) -> list[datetime]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("An aware clock is required")
    if duration < 1 or min(before, after, notice, horizon) < 0:
        raise ValueError("Invalid scheduling policy")
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    if day < today or day > today + timedelta(days=horizon):
        return []
    matching = [item for item in exceptions if item.first <= day <= item.last]
    if any(item.kind == "closed" for item in matching):
        return []
    custom = [item for item in matching if item.kind == "custom"]
    # Custom windows inherit the smallest weekly step; a clinic without weekly rules uses 15m.
    step = min((rule.step for rule in rules), default=15)
    windows = (
        [(item.start, item.end, step) for item in custom]
        if custom
        else [
            (rule.start, rule.end, rule.step) for rule in rules if rule.weekday == day.isoweekday()
        ]
    )
    candidates: set[datetime] = set()
    real_windows: list[Reservation] = []
    for start, end, increment in windows:
        if start is None or end is None or start >= end or increment < 1:
            raise ValueError("Invalid availability window")
        first, last = datetime.combine(day, start), datetime.combine(day, end)
        for left in local_instants(first, zone):
            for right in local_instants(last, zone):
                if left < right:
                    real_windows.append(Reservation(left, right))
        current = first
        while current < last:
            candidates.update(local_instants(current, zone))
            current += timedelta(minutes=increment)
    merged: list[Reservation] = []
    for window in sorted(real_windows, key=lambda item: item.start):
        if merged and window.start <= merged[-1].end:
            merged[-1] = Reservation(merged[-1].start, max(merged[-1].end, window.end))
        else:
            merged.append(window)
    result = []
    for start in sorted(candidates):
        occupied_start = start - timedelta(minutes=before)
        occupied_end = start + timedelta(minutes=duration + after)
        if start < now + timedelta(minutes=notice):
            continue
        if not any(
            window.start <= occupied_start and occupied_end <= window.end for window in merged
        ):
            continue
        if any(occupied_start < booking.end and occupied_end > booking.start for booking in busy):
            continue
        result.append(start)
    return result
