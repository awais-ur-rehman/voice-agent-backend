"""Small deterministic bilingual temporal grammar with conservative rejection.

Windows constrain appointment starts, with an exclusive end. Actual scheduling
rules/exceptions remain authoritative; this resolver never asserts availability.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DATE_WORDS = {
    "day after tomorrow": 2,
    "tomorrow": 1,
    "today": 0,
    "yesterday": -1,
    "پرسوں": 2,
    "parson": 2,
    "آج": 0,
    "aaj": 0,
    "کل": 1,
    "kal": 1,
}
_WEEKDAYS = {
    "monday": 0,
    "پیر": 0,
    "peer": 0,
    "tuesday": 1,
    "منگل": 1,
    "mangal": 1,
    "wednesday": 2,
    "بدھ": 2,
    "budh": 2,
    "thursday": 3,
    "جمعرات": 3,
    "friday": 4,
    "جمعہ": 4,
    "jummah": 4,
    "saturday": 5,
    "ہفتہ": 5,
    "sunday": 6,
    "اتوار": 6,
}
_WINDOWS = {
    "صبح": ("08:00", "12:00"),
    "subah": ("08:00", "12:00"),
    "morning": ("08:00", "12:00"),
    "دوپہر": ("12:00", "15:00"),
    "dopahar": ("12:00", "15:00"),
    "afternoon": ("12:00", "15:00"),
    "سہ پہر": ("15:00", "19:00"),
    "شام": ("15:00", "19:00"),
    "shaam": ("15:00", "19:00"),
    "evening": ("15:00", "19:00"),
    "late afternoon": ("15:00", "19:00"),
    "رات": ("19:00", "24:00"),
    "raat": ("19:00", "24:00"),
    "night": ("19:00", "24:00"),
}
_HOURS = {
    "ایک": 1,
    "ek": 1,
    "one": 1,
    "دو": 2,
    "do": 2,
    "two": 2,
    "تین": 3,
    "teen": 3,
    "three": 3,
    "چار": 4,
    "char": 4,
    "four": 4,
    "پانچ": 5,
    "paanch": 5,
    "five": 5,
    "چھ": 6,
    "chhe": 6,
    "six": 6,
    "سات": 7,
    "saat": 7,
    "seven": 7,
    "آٹھ": 8,
    "aath": 8,
    "eight": 8,
    "نو": 9,
    "nau": 9,
    "nine": 9,
    "دس": 10,
    "das": 10,
    "ten": 10,
    "گیارہ": 11,
    "gyarah": 11,
    "eleven": 11,
    "بارہ": 12,
    "barah": 12,
    "twelve": 12,
}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _minutes(value: str, *, allow_end: bool = False) -> int:
    if allow_end and value == "24:00":
        return 1440
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("invalid_time")
    h, m = map(int, value.split(":"))
    return 60 * h + m


def _range(values: list[str] | None) -> tuple[int, int] | None:
    if values is None:
        return None
    if len(values) != 2:
        raise ValueError("invalid_hours")
    start, end = _minutes(values[0]), _minutes(values[1], allow_end=True)
    if start >= end:
        raise ValueError("invalid_hours")
    return start, end


def _format(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _absolute(value: date, today: date, context: str) -> dict[str, Any]:
    if context == "booking" and value < today:
        return {"error": "past_booking_date"}
    return {"date": value.isoformat(), "read_back_absolute": True}


def _parse_clock(text: str) -> tuple[int, int, bool] | None:
    """Return hour, minute, explicit-24h or raise on a malformed clock."""
    text = re.sub(r"\s*(?:بجے|baje|o.clock)$", "", text).strip()
    if text in ("ڈیڑھ", "derh"):
        return 1, 30, False
    if text in ("ڈھائی", "dhai"):
        return 2, 30, False
    minute = 0
    for prefix, delta in [
        ("ساڑھے ", 30),
        ("saarhe ", 30),
        ("پونے ", -15),
        ("paune ", -15),
        ("سوا ", 15),
        ("sawa ", 15),
        ("half past ", 30),
        ("quarter past ", 15),
        ("quarter to ", -15),
    ]:
        if text.startswith(prefix):
            text, minute = text[len(prefix) :], delta
            break
    if text in _HOURS:
        hour = _HOURS[text]
        explicit = False
    elif re.fullmatch(r"\d{1,2}(?::\d{2})?", text):
        parts = text.split(":")
        hour = int(parts[0])
        if len(parts) == 2:
            if minute:
                raise ValueError("invalid_time")
            minute = int(parts[1])
        if hour > 23 or minute > 59:
            raise ValueError("invalid_time")
        explicit = hour == 0 or hour > 12
    else:
        return None
    if not 0 <= hour <= 23:
        raise ValueError("invalid_time")
    total = hour * 60 + minute
    return (total // 60) % 24, total % 60, explicit


def resolve(
    phrase: str,
    *,
    now: datetime,
    timezone: str,
    context: str = "booking",
    clinic_hours: list[str] | None = None,
    prayer_break: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve only supported expressions, never guess an unknown date/time.

    A timezone-aware fixed clock is mandatory. Grammar permits a date followed
    by a window or clock, e.g. ``kal subah das baje`` or ``tomorrow at 3 pm``.
    Ambiguity/error results contain no partly-resolved authoritative fields.
    """
    if not isinstance(phrase, str):
        return {"error": "validation_error"}
    if re.search(r"[\u0900-\u097f]", phrase):
        return {"error": "invalid_script", "retry_extraction": True}
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return {"error": "invalid_clock"}
    try:
        today = now.astimezone(ZoneInfo(timezone)).date()
        opening = _range(clinic_hours)
        prayer = _range(prayer_break)
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        return {"error": "validation_error"}
    text = " ".join(phrase.lower().translate(_DIGITS).strip(" ۔،,.?!؟").split())
    result: dict[str, Any] = {}
    # Remove only a recognized date prefix, then require the complete rest to
    # match a time grammar. Negation, alternatives and injections stay unknown.
    if text in ("اس ہفتے", "this week", "is haftay", "is hafte"):
        return {
            "date_from": today.isoformat(),
            "date_to": (today + timedelta(days=6 - today.weekday())).isoformat(),
            "read_back_absolute": True,
        }
    for word, offset in _DATE_WORDS.items():
        if text == word or text.startswith(word + " "):
            if word in ("کل", "kal", "پرسوں", "parson") and context != "booking":
                return {"clarify": "past_or_future"}
            result = _absolute(today + timedelta(days=offset), today, context)
            text = text[len(word) :].strip()
            break
    if not result:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+|$)", text)
        if match:
            try:
                day = date.fromisoformat(match[1])
            except ValueError:
                return {"error": "invalid_date"}
            result = _absolute(day, today, context)
            text = text[match.end() :].strip()
        else:
            weekday_text = text
            for prefix in ("next ", "اگلے ", "اگلا ", "aglay "):
                if text.startswith(prefix):
                    weekday_text = text[len(prefix) :]
                    break
            for word, weekday in _WEEKDAYS.items():
                if weekday_text == word or weekday_text.startswith(word + " "):
                    # Prayer-relative expressions are handled below, not as dates.
                    tail = weekday_text[len(word) :].strip()
                    if tail.startswith(("ke baad", "کی نماز", "prayer")):
                        break
                    result = _absolute(
                        today + timedelta(days=(weekday - today.weekday()) % 7 or 7),
                        today,
                        context,
                    )
                    text = tail
                    break
    if "error" in result:
        return result
    if not text:
        return result or {"clarify": "date_or_time"}
    if text in ("jummah ke baad", "جمعہ کی نماز کے بعد", "after friday prayers"):
        if prayer is None:
            return {"clarify": "clinic_prayer_break_required"}
        return {
            **result,
            "time_from": _format(prayer[1]),
            "respect_prayer_break": True,
            "read_back_absolute": True,
        }
    text = re.sub(r"^(?:at |کو )", "", text)
    window: tuple[str, str] | None = None
    # A standalone window remains a preference. With a clock it is an AM/PM
    # constraint, and does not add a second unrelated preference to the result.
    for word in sorted(_WINDOWS, key=len, reverse=True):
        if text == word or text.startswith(word + " "):
            window = _WINDOWS[word]
            text = text[len(word) :].strip()
            break
    if window is not None and not text:
        preference = list(window)
        # Night is an open-ended preference: the slot service applies closing
        # and date-specific rules (the reviewed golden contract defers this).
        if (
            opening
            and window[1] != "24:00"
            and max(opening[0], _minutes(window[0]))
            >= min(opening[1], _minutes(window[1], allow_end=True))
        ):
            return {
                **result,
                "preferred_window": preference,
                "available_window": None,
                "clarify": "alternative_hours",
            }
        return {
            **result,
            "preferred_window": preference,
            "end_exclusive": True,
            "apply_clinic_hours": True,
        }
    meridiem = None
    match = re.search(r"\s*(am|pm)$", text)
    if match:
        meridiem = match[1]
        text = text[: match.start()].strip()
    try:
        clock = _parse_clock(text)
    except ValueError:
        return {"error": "invalid_time"}
    if clock is None:
        return {"clarify": "date_or_time"}
    h, m, explicit = clock
    if meridiem and (h == 0 or h > 12):
        return {"error": "invalid_time"}
    if meridiem:
        candidates = [(h % 12 + (12 if meridiem == "pm" else 0)) * 60 + m]
    elif explicit:
        candidates = [h * 60 + m]
    else:
        candidates = sorted([h % 12 * 60 + m, (h % 12 + 12) * 60 + m])
    if window:
        candidates = [
            t
            for t in candidates
            if _minutes(window[0]) <= t < _minutes(window[1], allow_end=True)
        ]
    if opening:
        candidates = [t for t in candidates if opening[0] <= t < opening[1]]
    if not candidates:
        return {"clarify": "alternative_hours"}
    if len(candidates) > 1:
        return {"clarify": "am_or_pm", "candidates": [_format(t) for t in candidates]}
    return {**result, "time": _format(candidates[0]), "read_back_absolute": True}
