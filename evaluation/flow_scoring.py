"""Checks for evaluation-only next-action probes and speech-text review samples."""

import json
from functools import lru_cache
from pathlib import Path
import re
import unicodedata

from jsonschema import Draft202012Validator

from .scoring import unique_object


@lru_cache(maxsize=1)
def tool_validators() -> dict:
    tools = json.loads((Path(__file__).with_name('fixtures') / 'flow_tools.json').read_text())
    return {tool['function']['name']: Draft202012Validator(tool['function']['parameters']) for tool in tools}


def no_reasoning(response: dict) -> bool:
    text = response.get('content', '')
    usage = response.get('usage') or {}
    tokens = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0
    return not (response.get('reasoning_chars', 0) or tokens
                or re.search(r'</?(?:think|analysis)\b|<\|channel>thought', text, re.I))


def score_action(expected: dict, response: dict) -> dict:
    calls = response.get('tools', [])
    parsed = None
    tool_match = False
    if len(calls) == 1:
        tool_match = calls[0].get('name') == expected['name']
        try:
            parsed = json.loads(calls[0]['arguments'], object_pairs_hook=unique_object)
        except (KeyError, TypeError, ValueError):
            pass
    valid_json = isinstance(parsed, dict)
    schema_valid = bool(valid_json and len(calls) == 1 and calls[0].get('name') in tool_validators()
                        and tool_validators()[calls[0]['name']].is_valid(parsed))
    arguments_match = (valid_json and json.dumps(parsed, sort_keys=True, ensure_ascii=False)
                       == json.dumps(expected['arguments'], sort_keys=True, ensure_ascii=False))
    script_ok = not bool(re.search(r'[\u0900-\u097f]', json.dumps(parsed, ensure_ascii=False)))
    checks = {'single_tool': len(calls) == 1, 'valid_json_object': valid_json, 'schema_valid': schema_valid,
              'tool_match': tool_match, 'arguments_match': arguments_match,
              'script_ok': script_ok, 'no_free_prose': not response.get('content', '').strip(),
              'no_reasoning': no_reasoning(response)}
    return {**checks, 'passed': all(checks.values()), 'human_review': 'pending'}


def score_language(case: dict, response: dict) -> dict:
    text = response.get('content', '').strip()
    latin = bool(re.search(r'[A-Za-z]', text))
    arabic = bool(re.search(r'[\u0621-\u064a\u066e-\u06d3]', text))
    script_ok = (arabic and not latin) if case['expected_lang'] == 'ur' else (latin and not arabic)
    script_ok = script_ok and not bool(re.search(r'[\u0900-\u097f]', text))
    exact_say = ('say' not in case or unicodedata.normalize('NFC', text)
                 == unicodedata.normalize('NFC', case['say'].strip()))
    checks = {'nonempty': bool(text), 'script_ok': script_ok,
              'no_raw_digits': not bool(re.search(r'\d', text)),
              'one_question': len(re.findall(r'[?؟]', text)) <= 1,
              'short_reply': len(re.findall(r'[.!?۔؟]', text)) <= 2 and len(text.split()) <= 50,
              'respectful_form': not bool(re.search(r'\b(?:تم|تمہارا|تمہاری|تمہیں)\b', text)),
              'exact_say': exact_say, 'no_tools': not response.get('tools'),
              'no_reasoning': no_reasoning(response)}
    return {**checks, 'passed': all(checks.values()), 'human_review': 'pending'}
