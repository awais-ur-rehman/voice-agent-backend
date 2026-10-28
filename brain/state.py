"""Versioned dialogue state and a transcript-free SQL recovery projection."""

from copy import deepcopy
from uuid import UUID


def initial_state(session_id: UUID, agent_id: UUID, version_id: UUID) -> dict:
    return {
        "schema_version": 1,
        "session_id": str(session_id),
        "agent_id": str(agent_id),
        "version_id": str(version_id),
        "revision": 0,
        "task": "book",
        "stage": "start",
        "subject": None,
        "verified": False,
        "attempts": 0,
        "fields": {},
        "offers": [],
        "selection": None,
        "readback": None,
        "parked": [],
        "history": [],
        "summary": "",
        "lang": "en",
        "lang_pending": None,
        "lang_streak": 0,
    }


def recovery_checkpoint(state: dict) -> dict:
    """Never persist raw dialogue or verification factors to PostgreSQL."""
    excluded = {
        "history",
        "summary",
        "factor",
        "dob",
        "verification_name",
        "raw_text",
        "transcript",
        "utterance",
        "text",
        "raw",
    }

    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in excluded}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return deepcopy(value)

    result = clean(state)
    result.update(history=[], summary="")
    return result
