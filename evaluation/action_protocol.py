"""Constrained JSON control channel; never salvage function-like speech as a call."""

from functools import lru_cache
import json
from pathlib import Path
import re
import time

from jsonschema import Draft202012Validator

from .flow_scoring import no_reasoning
from .scoring import unique_object


def action_schema() -> dict:
    tools = json.loads((Path(__file__).with_name('fixtures') / 'flow_tools.json').read_text())
    return {'anyOf': [
        {'type': 'object', 'properties': {
            'name': {'type': 'string', 'const': tool['function']['name']},
            'arguments': tool['function']['parameters']},
         'required': ['name', 'arguments'], 'additionalProperties': False}
        for tool in tools]}


@lru_cache(maxsize=1)
def action_validator() -> Draft202012Validator:
    schema = action_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def reject_constant(value: str):
    raise ValueError('Non-finite JSON numbers are not allowed')


def decode_action_response(response: dict, allowed_schema: dict | None = None) -> dict:
    started = time.perf_counter()
    if response.get('tools') or not no_reasoning(response):
        raise ValueError('Mixed action protocol or reasoning output')
    raw = response.get('content', '')
    try:
        action = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (TypeError, ValueError) as exc:
        raise ValueError('Action is not one complete JSON document') from exc
    if not action_validator().is_valid(action):
        raise ValueError('Action does not satisfy the allowed action schema')
    if allowed_schema is not None:
        Draft202012Validator.check_schema(allowed_schema)
        if not Draft202012Validator(allowed_schema).is_valid(action):
            raise ValueError('Action is not eligible for the supplied state and input')
    if re.search(r'[\u0900-\u097f]', json.dumps(action, ensure_ascii=False)):
        raise ValueError('Unexpected Devanagari in action arguments')
    return {**response, 'raw_content': raw, 'content': '',
            'tools': [{'name': action['name'],
                       'arguments': json.dumps(action['arguments'], ensure_ascii=False, separators=(',', ':'))}],
            'action_protocol': 'json_schema', 'protocol_valid': True,
            'first_control_content_ms': response.get('first_content_ms'),
            'first_content_ms': None, 'first_usable_content_ms': None,
            'action_ready_ms': response['total_ms'] + (time.perf_counter() - started) * 1000}
