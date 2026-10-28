"""Validate frozen server profile snapshots without importing API dependencies."""

import json

from voice_runtime.config import BACKEND, FROZEN_VOICES


def selected_config(language, selected=None):
    if selected in (None, {}):
        return dict(FROZEN_VOICES[language])
    records = json.loads((BACKEND / "voice/catalog.json").read_text())
    if (
        not isinstance(selected, dict)
        or selected not in records
        or selected["language"] != language
    ):
        raise ValueError("Voice snapshot does not match frozen catalog")
    return dict(selected["config"])
