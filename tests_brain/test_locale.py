"""Golden speech contracts plus invalid-input boundaries; no audio quality claim."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "evaluation/fixtures/normalizer_golden.json").read_text()
)


@pytest.mark.parametrize("row", FIXTURE["numbers_0_100"], ids=lambda r: str(r["value"]))
def test_reviewed_number_lookup(row):
    from brain.normalization import number_ur

    assert number_ur(row["value"]) == row["say_ur"]


@pytest.mark.parametrize("row", FIXTURE["cases"], ids=lambda r: r["id"])
def test_normalization_golden(row):
    from brain import normalization as n

    kind = row["kind"]
    value = row["input"]

    def speak(lang):
        if kind == "name":
            return n.say_name(value["name_latin"], value["name_ur"], lang=lang)
        if kind == "phone":
            return n.say_phone(
                value, lang=lang, read_digits_in_english=row["read_digits_in_english"]
            )
        if kind == "number":
            return n.number_ur(value)
        return getattr(n, "say_" + kind)(value, lang=lang)

    if "expected_error" in row:
        with pytest.raises(n.LocaleError) as exc:
            speak("ur")
        assert exc.value.code == row["expected_error"]
    else:
        for lang in ("ur", "en"):
            if "say_" + lang in row:
                assert speak(lang) == row["say_" + lang]


@pytest.mark.parametrize("value", [True, -1, 101, 1.5, "1"])
def test_lookup_rejects_non_integer_and_out_of_range(value):
    from brain.normalization import LocaleError, number_ur

    with pytest.raises(LocaleError):
        number_ur(value)


def test_english_ordinals_and_extended_currency():
    from brain.normalization import number_en, say_currency, say_date, say_time

    assert number_en(21, ordinal=True) == "twenty-first"
    assert number_en(100) == "one hundred"
    assert say_currency(Decimal("12001"), lang="ur") == "بارہ ہزار ایک روپے"
    assert say_time("13:30") == "دوپہر ڈیڑھ بجے"
    assert say_time("14:30") == "دوپہر ڈھائی بجے"
    assert say_time("09:45") == "صبح پونے دس بجے"
    assert say_time("10:15") == "صبح سوا دس بجے"
    assert say_time("10:05") == "صبح دس بج کر پانچ منٹ"
    assert say_date("2026-10-29", include_year=True) == "جمعرات، انتیس اکتوبر دو ہزار چھبیس"


@pytest.mark.parametrize(
    "kind,value",
    [
        ("time", "25:00"),
        ("time", "10:30:01"),
        ("date", "2026-02-30"),
        ("currency", "NaN"),
        ("currency", "-1"),
        ("phone", "0300bad0000001"),
    ],
)
def test_invalid_speech_input_fails_closed(kind, value):
    from brain import normalization as n

    with pytest.raises(n.LocaleError):
        getattr(n, "say_" + kind)(value)


@pytest.mark.parametrize("name_ur", ["؟", "۱۲۳", 123, True, "Dr سارہ"])
def test_name_requires_reviewed_script_letters(name_ur):
    from brain.normalization import LocaleError, say_name

    with pytest.raises(LocaleError) as exc:
        say_name("Dr Sara Test", name_ur)
    assert exc.value.code == "pronunciation_review_required"


def test_phone_international_groups_and_english_time():
    from brain.normalization import say_phone, say_time

    assert (
        say_phone("+92 300 000 0001", lang="en")
        == "nine two, three zero zero, zero zero zero, zero zero zero one"
    )
    assert say_time("10:30", lang="en") == "ten thirty in the morning"
