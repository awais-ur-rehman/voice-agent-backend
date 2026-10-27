"""Sequential synthetic NIM evaluation. No GPU, patient data, or hidden retries."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

from .scoring import TOOLS, percentile, score
from .stream import read_stream


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())
ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_name('cases.json')
SYSTEM = (
    'You are a clinic receptionist in a synthetic evaluation. Use exactly one provided tool '
    'to answer the caller request. Record date expressions exactly as supplied; code resolves dates. '
    'Use get_clinic_info for hours, address, fees, doctors, or services. '
    'Do not invent patient information, availability, or confirmations. '
    'If you include Urdu prose, use Urdu script, never Devanagari or Roman Urdu. '
    'No reasoning or thinking text. These tool definitions are experimental evaluation contracts.'
)


def build_payload(model: str, text: str) -> dict:
    payload = {'model': model, 'messages': [{'role': 'system', 'content': SYSTEM},
               {'role': 'user', 'content': text}], 'tools': TOOLS,
               'tool_choice': 'auto', 'stream': True, 'temperature': 0.3, 'max_tokens': 300}
    canonical_model = model.removeprefix('stg/')
    if canonical_model == 'nvidia/nemotron-3.5-bolt-e4b':
        payload.update(temperature=1.0, top_p=0.95, parallel_tool_calls=False,
                       chat_template_kwargs={'enable_thinking': False})
    elif canonical_model == 'minimaxai/minimax-m3.1-flash':
        # No thinking-off control is documented for this hosted endpoint.
        payload.update(temperature=1.0, top_p=0.95)
    elif canonical_model == 'meta/muse-glimmer-30b':
        payload.update(temperature=0.95, top_p=1.0, reasoning_effort='none')
    elif canonical_model == 'google/diffusiongemma-26b-a4b-it':
        payload.update(temperature=1.0, top_p=0.95,
                       chat_template_kwargs={'enable_thinking': False})
    return payload


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not key.strip().replace('_', '').isalnum():
            raise ValueError('Invalid .env assignment; use KEY=value without shell expressions')
        value = value.strip()
        if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def validate_endpoint(value: str) -> str:
    parts = urlsplit(value)
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.scheme not in {'http', 'https'}
            or (parts.scheme == 'http' and parts.hostname not in {'localhost', '127.0.0.1', '::1'})):
        raise ValueError('Use an HTTPS endpoint without credentials, query, or fragment; HTTP is loopback-only')
    return value.rstrip('/')


def request_stream(base_url: str, key: str, payload: dict, timeout: float) -> dict:
    request = Request(base_url + '/chat/completions', method='POST',
                      data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                               'Accept': 'text/event-stream'})
    started = time.perf_counter()
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return read_stream(response, started)
    except HTTPError as exc:
        raise RuntimeError(f'Provider returned HTTP {exc.code}; no retry attempted') from None
    except TimeoutError:
        raise RuntimeError('Provider timed out; no retry attempted') from None
    except ValueError:
        raise RuntimeError('Provider returned an invalid or incomplete stream; no retry attempted') from None
    except (URLError, OSError):
        raise RuntimeError('Provider connection failure; no retry attempted') from None


def load_cases() -> tuple[list[dict], str]:
    raw = CASES.read_bytes()
    cases = json.loads(raw)
    if Counter(case['lang'] for case in cases) != {'ur': 20, 'en': 10, 'mixed': 10}:
        raise ValueError('Fixture suite must contain 20 Urdu, 10 English, and 10 mixed turns')
    if len({case['id'] for case in cases}) != 40:
        raise ValueError('Fixture identifiers must be unique')
    for case in cases:
        expected = case['expected']
        if not score(case, {'content': '', 'tools': [{'name': expected['name'],
                     'arguments': json.dumps(expected['arguments'])}]})['passed']:
            raise ValueError('Invalid expected tool arguments in fixture suite')
    return cases, hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Validate fixtures offline; no HTTP requests')
    parser.add_argument('--model', help='Exact accessible model identifier; no implicit model selection')
    parser.add_argument('--limit', type=int, help='Run only the first N cases (smoke test, not full bake-off)')
    parser.add_argument('--interval', type=float, default=2, help='Seconds between requests, default 2')
    parser.add_argument('--timeout', type=float, default=30, help='Socket timeout in seconds, default 30')
    parser.add_argument('--output', type=Path, help='New report path under ignored results/; never overwrite')
    args = parser.parse_args(argv)
    try:
        cases, digest = load_cases()
        if args.limit is not None and not 1 <= args.limit <= len(cases):
            raise ValueError('--limit must be between 1 and 40')
        if not math.isfinite(args.interval) or args.interval < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError('--interval must be nonnegative and --timeout positive; both must be finite')
        if args.dry_run:
            print(json.dumps({'cases': len(cases), 'languages': dict(Counter(case['lang'] for case in cases)),
                              'sha256': digest, 'network_requests': 0}))
            return 0
        load_env(ROOT / '.env')
        key = os.environ.get('LLM_API_KEY', '').strip()
        model = (args.model or os.environ.get('LLM_MODEL', '')).strip()
        if not key:
            raise ValueError('Set LLM_API_KEY in backend/.env; never paste it into a report or commit')
        if not model:
            raise ValueError('Set --model to an identifier available in your NVIDIA API Catalog account')
        endpoint = validate_endpoint(os.environ.get('LLM_BASE_URL', 'https://integrate.api.nvidia.com/v1'))
        output = args.output or ROOT / 'results' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        output = output.resolve()
        if not output.is_relative_to((ROOT / 'results').resolve()):
            raise ValueError('--output must be inside backend/results/ (ignored by Git)')
        if output.exists():
            raise ValueError('Output already exists; choose a new report path')
        rows = []
        for index, case in enumerate(cases[:args.limit]):
            if index:
                time.sleep(args.interval)
            payload = build_payload(model, case['text'])
            row = {'id': case['id'], 'lang': case['lang']}
            try:
                response = request_stream(endpoint, key, payload, args.timeout)
                row.update(status='ok', response=response, scores=score(case, response))
            except RuntimeError as exc:
                row.update(status='error', error=str(exc))
            rows.append(row)
            print(f'{index + 1}/{min(args.limit or 40, 40)} {case["id"]}: {row["status"]}', file=sys.stderr)
            # Stop on provider errors rather than consuming the remaining rate budget.
            if row['status'] == 'error':
                break
        successful = [row for row in rows if row['status'] == 'ok']
        summary = {'requested': min(args.limit or 40, 40), 'attempted': len(rows),
                   'completed': len(successful), 'passed': sum(row['scores']['passed'] for row in successful)}
        for metric in ['first_output_ms', 'first_content_ms', 'first_usable_content_ms', 'total_ms']:
            values = [row['response'][metric] for row in successful if row['response'][metric] is not None]
            summary[metric] = {'samples': len(values), 'p50': percentile(values, 50), 'p95': percentile(values, 95)}
        report = {'created_at': datetime.now(timezone.utc).isoformat(), 'synthetic_only': True,
                  'model': model, 'endpoint': endpoint, 'fixture_sha256': digest,
                  'python': platform.python_version(), 'platform': platform.platform(),
                  'interval_seconds': args.interval, 'socket_timeout_seconds': args.timeout,
                  'generation_config': {key: value for key, value in payload.items()
                                        if key not in {'model', 'messages', 'tools'}},
                  'reasoning_control': ('request: enable_thinking=false'
                                        if payload.get('chat_template_kwargs', {}).get('enable_thinking') is False
                                        else 'request: reasoning_effort=none' if payload.get('reasoning_effort') == 'none'
                                        else 'prompt_only; model-specific controls not verified'),
                  'human_urdu_quality': None, 'gpu_voice_gate': 'pending',
                  'summary': summary, 'cases': rows}
        # Defense in depth: a provider may echo the credential in generated content.
        serialized = json.dumps(report, ensure_ascii=False, indent=2).replace(key, '[REDACTED]')
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(serialized + '\n')
        print(json.dumps({'report': str(output), 'summary': summary}))
        return 0 if len(successful) == len(rows) and summary['passed'] == len(rows) else 1
    except (ValueError, OSError) as exc:
        # Configuration errors contain no environment values or provider error bodies.
        print(f'Configuration error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
