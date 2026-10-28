"""Conservative phone extraction and exact reviewed Urdu identity factors."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from brain.tools import ToolEngine


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("zero three zero zero one two three four five six seven", "03001234567"),
        ("صفر تین صفر صفر ایک دو تین چار پانچ چھ سات", "03001234567"),
        ("+۹۲ (۳۰۰) ۱۲۳-۴۵۶۷", "+92 (300) 123-4567"),
        ("+٩٢ (٣٠٠) ١٢٣-٤٥٦٧", "+92 (300) 123-4567"),
        ("plus nine two three zero zero one two three four five six seven", "+923001234567"),
        ("پلس نو دو تین صفر صفر ایک دو تین چار پانچ چھ سات", "+923001234567"),
        ("ZERO three 0 0 ایک دو three four five six seven", "03001234567"),
        ("0300-1234567", "0300-1234567"),
    ],
)
def test_explicit_digit_sequence_and_canonical_numerals(spoken, expected):
    from brain.voice_input import normalize_phone_input

    assert normalize_phone_input(spoken) == expected


@pytest.mark.parametrize(
    "spoken",
    [
        "double zero three hundred",
        "oh three zero",
        "thirty zero one",
        "تین سو ایک",
        "my number is zero three zero",
        "zero, three, zero",
        "one or two",
        "ایک یا دو",
        "",
        "   ",
        "1+2",
        "+ +92",
        "03001234567 ext 1",
        "१२३",
        "Ⅲ",
        "²",
        "0" * 129,
        "one " * 129,
    ],
)
def test_ambiguous_or_unbounded_numbers_need_repetition(spoken):
    from brain.voice_input import normalize_phone_input

    assert normalize_phone_input(spoken) is None


class CandidateDatabase:
    """Only the query result is substituted; verification runs real tool logic."""

    def __init__(self, candidates):
        self.candidates = candidates

    def scalar(self, _query):
        return SimpleNamespace(id=uuid4())

    def scalars(self, _query):
        return self.candidates


def verify(factor, names):
    candidates = [
        SimpleNamespace(
            id=uuid4(),
            name_key="synthetic patient",
            revision=1,
            name_ur=name,
            dob=None,
        )
        for name in names
    ]
    current = {
        "candidates": [str(p.id) for p in candidates],
        "lookup_phone": "+923001234567",
        "attempts": 0,
        "verified": False,
    }
    engine = ToolEngine(
        CandidateDatabase(candidates), SimpleNamespace(tenant_id=uuid4()), None, datetime.now(UTC)
    )
    response = engine._execute(current, "verify_patient", {"factor": factor}, "")
    return response, current


def test_exact_reviewed_urdu_factor_can_verify_unique_phone_candidate():
    response, current = verify("  مصنوعی   مریض ", ["مصنوعی مریض"])
    assert response["ok"]
    assert current["verified"]


@pytest.mark.parametrize("names", [["مصنوعی مریض", "مصنوعی مریض"], ["مصنوعی مریضہ"], [None]])
def test_urdu_factor_is_not_fuzzy_and_shared_phone_is_not_unique(names):
    response, current = verify("مصنوعی مریض", names)
    assert not response["ok"]
    assert not current["verified"]
    assert current["attempts"] == 1
