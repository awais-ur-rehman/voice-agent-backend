"""Frozen, server-owned voice candidates. No profile has qualified speech evidence yet."""

import json
from pathlib import Path

_MANIFEST = Path(__file__).resolve().parents[1] / "voice/catalog.json"


def profile(identity: str) -> dict:
    for record in json.loads(_MANIFEST.read_text()):
        if record["id"] == identity:
            return record
    raise ValueError("Unknown voice profile")


def catalog() -> list[dict]:
    return [
        {k: v for k, v in row.items() if k != "config"}
        | {
            "readiness": "provisional",
            "preview_available": False,
            "preview_url": None,
            "unavailable_reason": "Exact profile speech acceptance and pre-rendered preview are unavailable.",
        }
        for row in json.loads(_MANIFEST.read_text())
    ]
