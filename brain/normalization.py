"""Deterministic text for speech; reviewed names never undergo transliteration.

The Urdu spellings are proposed canonical text pending native-speaker review.
English digit reading is returned as English text for the transport to route to
its English voice; this module does not synthesize or mark up audio.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

_UR: list[str] = json.loads((Path(__file__).parent / "locales/ur_numbers.json").read_text())
_EN_SMALL = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_EN_TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
_UR_MONTHS = "جنوری فروری مارچ اپریل مئی جون جولائی اگست ستمبر اکتوبر نومبر دسمبر".split()
_UR_DAYS = ["پیر", "منگل", "بدھ", "جمعرات", "جمعہ", "ہفتہ", "اتوار"]
_EN_MONTHS = (
    "January February March April May June July August September October November December".split()
)
_EN_DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()


class LocaleError(ValueError):
    """A stable, transport-independent validation reason."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _lang(lang: str) -> None:
    if lang not in ("ur", "en"):
        raise LocaleError("validation_error")


def number_ur(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LocaleError("validation_error")
    if not 0 <= value <= 100:
        raise LocaleError("outside_lookup_range")
    return _UR[value]


def number_en(value: int, ordinal: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LocaleError("validation_error")
    if value > 999_999_999:
        raise LocaleError("outside_lookup_range")
    if value < 20:
        result = _EN_SMALL[value]
    elif value < 100:
        result = _EN_TENS[value // 10] + ("-" + _EN_SMALL[value % 10] if value % 10 else "")
    else:
        scale, word = next(
            (s, w)
            for s, w in [(1_000_000, "million"), (1000, "thousand"), (100, "hundred")]
            if value >= s
        )
        result = number_en(value // scale) + " " + word
        if value % scale:
            result += " " + number_en(value % scale)
    if ordinal:
        head, separator, last = result.rpartition("-" if "-" in result.rsplit(" ", 1)[-1] else " ")
        special = {
            "zero": "zeroth",
            "one": "first",
            "two": "second",
            "three": "third",
            "five": "fifth",
            "eight": "eighth",
            "nine": "ninth",
            "twelve": "twelfth",
        }
        last = special.get(last, last[:-1] + "ieth" if last.endswith("y") else last + "th")
        result = head + separator + last
    return result


def _large_ur(value: int) -> str:
    if value <= 100:
        return number_ur(value)
    if value > 999_999:
        raise LocaleError("outside_lookup_range")
    scale, word = next(
        (s, w) for s, w in [(100_000, "لاکھ"), (1000, "ہزار"), (100, "سو")] if value >= s
    )
    result = number_ur(value // scale) + " " + word
    return result + (" " + _large_ur(value % scale) if value % scale else "")


def say_time(value: time | str, lang: str = "ur") -> str:
    _lang(lang)
    try:
        parsed = time.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, time) or parsed.second or parsed.microsecond:
            raise ValueError
    except (TypeError, ValueError):
        raise LocaleError("validation_error") from None
    hour, minute = parsed.hour, parsed.minute
    h = hour % 12 or 12
    if lang == "en":
        period_en = (
            "morning"
            if 5 <= hour < 12
            else "afternoon"
            if 12 <= hour < 17
            else "evening"
            if 17 <= hour < 19
            else "night"
        )
        clock_en = number_en(h)
        if minute:
            clock_en += " " + ("oh " if minute < 10 else "") + number_en(minute)
        return clock_en + (" at night" if period_en == "night" else " in the " + period_en)
    period = (
        "صبح"
        if 5 <= hour < 12
        else "دوپہر"
        if 12 <= hour < 15
        else "سہ پہر"
        if 15 <= hour < 17
        else "شام"
        if 17 <= hour < 19
        else "رات"
    )
    if minute == 30:
        clock = "ڈیڑھ" if h == 1 else "ڈھائی" if h == 2 else "ساڑھے " + number_ur(h)
    elif minute == 15:
        clock = "سوا " + number_ur(h)
    elif minute == 45:
        clock = "پونے " + number_ur(h % 12 + 1)
    elif minute:
        return period + " " + number_ur(h) + " بج کر " + number_ur(minute) + " منٹ"
    else:
        clock = number_ur(h)
    return period + " " + clock + " بجے"


def say_date(value: date | str, lang: str = "ur", include_year: bool = False) -> str:
    _lang(lang)
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, date) or isinstance(parsed, datetime):
            raise ValueError
    except (TypeError, ValueError):
        raise LocaleError("validation_error") from None
    if lang == "en":
        result = f"{_EN_DAYS[parsed.weekday()]}, {_EN_MONTHS[parsed.month - 1]} {number_en(parsed.day, ordinal=True)}"
        return result + (" " + number_en(parsed.year) if include_year else "")
    result = f"{_UR_DAYS[parsed.weekday()]}، {number_ur(parsed.day)} {_UR_MONTHS[parsed.month - 1]}"
    return result + (" " + _large_ur(parsed.year) if include_year else "")


def say_currency(value: Decimal | str | int, lang: str = "ur") -> str:
    _lang(lang)
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise LocaleError("validation_error")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise LocaleError("validation_error")
        if amount != amount.to_integral_value():
            raise LocaleError("fractional_currency_policy_required")
        if amount > (999_999 if lang == "ur" else 999_999_999):
            raise LocaleError("outside_lookup_range")
    except InvalidOperation:
        raise LocaleError("validation_error") from None
    integer = int(amount)
    return (
        (_large_ur(integer) + " روپے")
        if lang == "ur"
        else (number_en(integer) + " Pakistani rupees")
    )


def say_phone(value: str, lang: str = "ur", read_digits_in_english: bool = False) -> str:
    _lang(lang)
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9\s()\-]+", value):
        raise LocaleError("validation_error")
    digits = re.sub(r"\D", "", value)
    international = digits.startswith("92") and len(digits) == 12
    if international:
        local = "0" + digits[2:]
    else:
        local = digits
    if not re.fullmatch(r"03\d{9}", local):
        raise LocaleError("validation_error")
    groups = [local[:4], local[4:7], local[7:]]
    if international:
        groups = ["92", digits[2:5], digits[5:8], digits[8:]]
    speak = number_en if lang == "en" or read_digits_in_english else number_ur
    separator = ", " if lang == "en" or read_digits_in_english else "، "
    return separator.join(" ".join(speak(int(digit)) for digit in group) for group in groups)


def say_name(name: str, name_ur: str | None, lang: str = "ur") -> str:
    _lang(lang)
    if lang == "en":
        if not isinstance(name, str) or not name.strip():
            raise LocaleError("validation_error")
        return name.strip()
    if (
        not isinstance(name_ur, str)
        or not re.search(r"[\u0621-\u063a\u0641-\u064a\u066e-\u06d3\u06fa-\u06ff]", name_ur)
        or re.search(r"[A-Za-z0-9\u0660-\u0669\u06f0-\u06f9\u0900-\u097f]", name_ur)
    ):
        raise LocaleError("pronunciation_review_required")
    return name_ur.strip()
