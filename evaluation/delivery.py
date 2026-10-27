"""Select authoritative, speakable tool text without an LLM round trip."""

from functools import lru_cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator


@lru_cache(maxsize=1)
def result_validator() -> Draft202012Validator:
    path = Path(__file__).resolve().parents[1] / 'packages/schemas/tool-result.v1.schema.json'
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def confirmation_text(tool_result: dict, language: str) -> str:
    if language not in {'en', 'ur'} or not result_validator().is_valid(tool_result):
        raise ValueError('Invalid language or authoritative tool result')
    text = tool_result['say'][language]
    if not text.strip():
        raise ValueError('Authoritative say text is blank')
    return text
