import json
from datetime import datetime
from pathlib import Path

import pytest

ROWS = json.loads(
    (Path(__file__).parents[1] / "evaluation/fixtures/resolver_golden.json").read_text()
)["cases"]


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["id"])
def test_resolver_golden(row):
    from brain.resolver import resolve

    assert (
        resolve(
            row["phrase"],
            now=datetime.fromisoformat(row["now"]),
            timezone=row["timezone"],
            context=row["context"],
            clinic_hours=row["clinic_hours"],
            prayer_break=row.get("prayer_break"),
        )
        == row["expected"]
    )


@pytest.mark.parametrize(
    "phrase,expected",
    [
        (
            "kal subah das baje",
            {"date": "2026-10-28", "time": "10:00", "read_back_absolute": True},
        ),
        (
            "tomorrow at 3 pm",
            {"date": "2026-10-28", "time": "15:00", "read_back_absolute": True},
        ),
        ("next Friday", {"date": "2026-10-30", "read_back_absolute": True}),
        ("2026-02-30", {"error": "invalid_date"}),
        ("2025-01-01", {"error": "past_booking_date"}),
        ("25:90", {"error": "invalid_time"}),
        ("do not book tomorrow", {"clarify": "date_or_time"}),
        ("today or tomorrow", {"clarify": "date_or_time"}),
        ("2026-10-29", {"date": "2026-10-29", "read_back_absolute": True}),
    ],
)
def test_composition_and_unsafe_expressions(phrase, expected):
    from brain.resolver import resolve

    assert (
        resolve(
            phrase,
            now=datetime.fromisoformat("2026-10-27T09:00:00+05:00"),
            timezone="Asia/Karachi",
            clinic_hours=["08:00", "18:00"],
        )
        == expected
    )
