"""Export the versioned transport schema without connecting to any service."""

import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from brain.voice_contracts import (
    CallControl,
    CaptionMessage,
    InvalidateMessage,
    MediaGrant,
    PlaybackAck,
    SegmentEndMessage,
    SegmentMessage,
    VoiceCreate,
    VoiceSnapshot,
    VoiceStateMessage,
)

ROOT = Path(__file__).resolve().parents[1]


def render() -> str:
    _, schema = models_json_schema(
        [
            (model, "validation")
            for model in (
                VoiceCreate,
                MediaGrant,
                VoiceSnapshot,
                PlaybackAck,
                CallControl,
                VoiceStateMessage,
                CaptionMessage,
                SegmentMessage,
                SegmentEndMessage,
                InvalidateMessage,
            )
        ],
        title="Voice transport v1",
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "packages/schemas/voice.v1.json"
    content = render()
    if args.check:
        if not target.exists() or target.read_text() != content:
            raise SystemExit("Voice schema is stale; run python -m scripts.export_voice_contracts")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
