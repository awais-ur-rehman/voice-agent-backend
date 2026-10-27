"""Collect an OpenAI-compatible server-sent event stream without timing metadata."""

import json
import re
import time
from typing import BinaryIO, Callable


def read_stream(source: BinaryIO, started: float, clock: Callable[[], float] = time.perf_counter) -> dict:
    content: list[str] = []
    tools: dict[int, dict] = {}
    first_output = first_content = None
    finish = None
    done = False
    data: list[str] = []
    usage = None
    reasoning_chars = 0
    for raw in source:
        line = raw.decode('utf-8').rstrip('\r\n')
        if line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
            continue
        if line or not data:
            continue
        payload = '\n'.join(data)
        data.clear()
        elapsed = (clock() - started) * 1000
        if payload == '[DONE]':
            done = True
            break
        try:
            event = json.loads(payload)
            if not isinstance(event, dict) or 'error' in event:
                raise ValueError('Provider reported a stream error')
            usage = event.get('usage') or usage
            for choice in event.get('choices', []):
                if choice.get('index', 0) != 0:
                    continue
                finish = choice.get('finish_reason') or finish
                delta = choice.get('delta') or {}
                reasoning = delta.get('reasoning_content') or delta.get('reasoning') or ''
                if not isinstance(reasoning, str):
                    raise ValueError('Malformed reasoning delta')
                reasoning_chars += len(reasoning)
                text = delta.get('content') or ''
                if not isinstance(text, str):
                    raise ValueError('Malformed content delta')
                if text:
                    content.append(text)
                    if first_content is None:
                        first_content = elapsed
                    if first_output is None:
                        first_output = elapsed
                for chunk in delta.get('tool_calls') or []:
                    index = chunk['index']
                    if type(index) is not int or index < 0:
                        raise ValueError('Malformed tool index')
                    function = chunk.get('function') or {}
                    tool = tools.setdefault(index, {'name': '', 'arguments': ''})
                    tool['name'] += function.get('name') or ''
                    tool['arguments'] += function.get('arguments') or ''
                    if (function.get('name') or function.get('arguments')) and first_output is None:
                        first_output = elapsed
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise ValueError('Malformed provider stream') from exc
    if not done or finish not in {'stop', 'tool_calls'} or first_output is None:
        raise ValueError('Incomplete or truncated provider stream')
    text = ''.join(content)
    reasoning_leak = bool(re.search(r'</?(?:think|analysis)\b', text, re.IGNORECASE))
    return {'content': text, 'tools': [tools[key] for key in sorted(tools)],
            'first_output_ms': first_output, 'first_content_ms': first_content,
            'reasoning_leak': reasoning_leak,
            'reasoning_chars': reasoning_chars,
            'first_usable_content_ms': None if reasoning_leak else first_content,
            'total_ms': elapsed, 'finish_reason': finish, 'usage': usage}
