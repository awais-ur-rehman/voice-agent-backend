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


def real_open_intervals(first: datetime, last: datetime, zone: ZoneInfo) -> list[Reservation]:
    """Intersect UTC offset segments with the wall-clock window, without bridging folds.

    Scan bounded UTC minute segments, splitting any offset transition to microsecond
    precision. Rule seconds and gap/fold endpoints are preserved. No nonexistent
    wall-clock endpoint is silently used as an instant.
    """
    cursor = min(first.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1))
    finish = max(last.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1))
    result: list[Reservation] = []
    epsilon = timedelta(microseconds=1)
    while cursor < finish:
        end = min(cursor + timedelta(minutes=1), finish)
        offset = cursor.astimezone(zone).utcoffset()
        if (end - epsilon).astimezone(zone).utcoffset() != offset:
            left, right = cursor, end
            while right - left > epsilon:
                middle = left + (right - left) // 2
                if middle.astimezone(zone).utcoffset() == offset:
                    left = middle
                else:
                    right = middle
            end = right
        local_start = cursor.astimezone(zone).replace(tzinfo=None)
        local_end = local_start + (end - cursor)
        wall_start, wall_end = max(first, local_start), min(last, local_end)
        if wall_start < wall_end:
            start_utc = cursor + (wall_start - local_start)
            end_utc = cursor + (wall_end - local_start)
            if result and result[-1].end == start_utc:
                result[-1] = Reservation(result[-1].start, end_utc)
            else:
                result.append(Reservation(start_utc, end_utc))
        cursor = end
    return result


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
    # Custom windows inherit the smallest matching-weekday step; a clinic without weekly rules uses 15m.
    step = min((rule.step for rule in rules if rule.weekday == day.isoweekday()), default=15)
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
        real_windows.extend(real_open_intervals(first, last, zone))
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
    for slot_start in sorted(candidates):
        occupied_start = slot_start - timedelta(minutes=before)
        occupied_end = slot_start + timedelta(minutes=duration + after)
        if slot_start < now + timedelta(minutes=notice):
            continue
        if not any(
            window.start <= occupied_start and occupied_end <= window.end for window in merged
        ):
            continue
        if any(occupied_start < booking.end and occupied_end > booking.start for booking in busy):
            continue
        result.append(slot_start)
    return result
