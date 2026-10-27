"""Deterministic checks for synthetic tool-selection cases, not language quality."""

import json
import math
import re


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'get_clinic_info', 'description': 'Look up a structured clinic fact.',
        'parameters': {'type': 'object', 'properties': {'field': {'type': 'string',
                       'enum': ['hours', 'address', 'fees', 'doctors', 'services']}},
                       'required': ['field'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {
        'name': 'update_dialogue_state',
        'description': 'Record the raw date phrase supplied by the caller. Do not resolve dates.',
        'parameters': {'type': 'object', 'properties': {'date_phrase': {'type': 'string', 'minLength': 1}},
                       'required': ['date_phrase'], 'additionalProperties': False}}},
]


def unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def score(case: dict, response: dict) -> dict:
    calls = response['tools']
    valid = tool_match = arguments_match = False
    if len(calls) == 1:
        call = calls[0]
        tool_match = call['name'] == case['expected']['name']
        try:
            args = json.loads(call['arguments'], object_pairs_hook=unique_object)
            schema = next(tool['function']['parameters'] for tool in TOOLS
                          if tool['function']['name'] == call['name'])
            field = schema['required'][0]
            value = args.get(field) if isinstance(args, dict) else None
            valid = (isinstance(args, dict) and set(args) == {field}
                     and isinstance(value, str) and bool(value.strip())
                     and ('enum' not in schema['properties'][field]
                          or value in schema['properties'][field]['enum']))
            arguments_match = valid and args == case['expected']['arguments']
        except (ValueError, TypeError, StopIteration):
            pass
    text = response['content']
    script_ok = None
    if case['lang'] == 'ur' and text.strip():
        script_ok = bool(re.search(r'[\u0621-\u064a\u066e-\u06d3]', text)) and not bool(re.search(r'[\u0900-\u097f]', text))
    reasoning_leak = bool(re.search(r'</?(?:think|analysis)\b', text, re.IGNORECASE))
    return {'schema_valid': bool(valid), 'tool_match': tool_match,
            'arguments_match': bool(arguments_match), 'urdu_script_ok': script_ok,
            'reasoning_leak': reasoning_leak,
            'passed': bool(valid and tool_match and arguments_match and script_ok is not False and not reasoning_leak)}


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * percent / 100) - 1)]
