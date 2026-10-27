"""Validate prepared evaluation assets; do not claim resolver or audio accuracy."""

from collections import Counter
from datetime import date, datetime
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from .flows import FIXTURES, load_probes

ROOT = Path(__file__).resolve().parents[1]


def envelope_validator() -> Draft202012Validator:
    schema = json.loads((ROOT / 'packages/schemas/tool-result.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_envelope(value: dict) -> bool:
    return envelope_validator().is_valid(value)


def validate_assets() -> dict:
    probes = load_probes()
    tools = json.loads((FIXTURES / 'flow_tools.json').read_text())
    validators = {}
    for tool in tools:
        definition = tool['function']
        Draft202012Validator.check_schema(definition['parameters'])
        validators[definition['name']] = Draft202012Validator(definition['parameters'])
    for probe in probes:
        if probe['suite'] == 'actions':
            validators[probe['expected']['name']].validate(probe['expected']['arguments'])
    normalizers = json.loads((FIXTURES / 'normalizer_golden.json').read_text())
    numbers = normalizers['numbers_0_100']
    if [row['value'] for row in numbers] != list(range(101)):
        raise ValueError('Number lookup must cover every integer from zero through one hundred')
    for row in numbers:
        if not row['say_ur'].strip() or re.search(r'[\dA-Za-z\u0900-\u097f]', row['say_ur']):
            raise ValueError('Number expectation must be digit-free Urdu script')
    for row in normalizers['cases']:
        if not any(key in row for key in ['say_ur', 'say_en', 'expected_error']):
            raise ValueError('Missing normalizer expectation')
        for key in ['say_ur', 'say_en']:
            if key in row and re.search(r'\d', row[key]):
                raise ValueError('Spoken expectation contains raw digits')
    resolver = json.loads((FIXTURES / 'resolver_golden.json').read_text())['cases']
    if len({case['id'] for case in resolver}) != len(resolver):
        raise ValueError('Duplicate resolver case identifiers')
    for case in resolver:
        now = datetime.fromisoformat(case['now'])
        if now.tzinfo is None:
            raise ValueError('Golden resolver time must be timezone-aware')
        today = now.astimezone(ZoneInfo(case['timezone'])).date()
        expected = case['expected']
        for key in ['date', 'date_from', 'date_to']:
            if key in expected:
                resolved = date.fromisoformat(expected[key])
                if case['context'] == 'booking' and resolved < today:
                    raise ValueError('Booking date expectation points into the past')
        if 'date_from' in expected and expected['date_to'] < expected['date_from']:
            raise ValueError('Golden date range is reversed')
    manifest = json.loads((FIXTURES / 'recording_manifest.template.json').read_text())['utterances']
    if len({row['id'] for row in manifest}) != len(manifest):
        raise ValueError('Duplicate recording identifiers')
    counts = dict(Counter(row['lang'] for row in manifest))
    if counts != {'ur': 200, 'mixed': 50} or len({row['speaker_slot'] for row in manifest}) != 10:
        raise ValueError('Recording plan needs two hundred Urdu, fifty mixed prompts and ten speaker slots')
    if any(row['audio_path'] is not None or row['reference_transcript'] is not None
           or row['consent_recorded'] is not False for row in manifest):
        raise ValueError('Public recording template must remain unrecorded and contain no consent claims')
    validator = envelope_validator()
    examples = json.loads((FIXTURES / 'envelope_examples.json').read_text())
    for example in examples:
        if validator.is_valid(example['value']) is not example['valid']:
            raise ValueError('Tool result envelope example disagrees with its expected validity')
    return {'action_probes': 60, 'language_probes': 12, 'number_values': len(numbers),
            'normalizer_cases': len(normalizers['cases']), 'resolver_cases': len(resolver),
            'recording_prompts': counts, 'speakers': 10, 'recorded_audio': 0,
            'envelope_examples': len(examples), 'native_review': 'pending',
            'resolver_implementation': 'not implemented', 'normalizer_implementation': 'not implemented'}


def main() -> int:
    try:
        print(json.dumps(validate_assets()))
    except Exception as exc:
        # Fixtures are public synthetic data; never print environment or provider error bodies.
        print('Asset validation failed: ' + type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
