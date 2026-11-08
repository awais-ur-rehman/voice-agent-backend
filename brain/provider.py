"""Bounded hosted NIM adapter. No provider text becomes caller output."""

import json
import re
import time

import httpx2
from pydantic import ValidationError

from .contracts import ARGUMENTS, parse_action
from .dialogue import allowed_next
from .prompts import messages, sanitize_text


class ProviderError(Exception):
    def __init__(self, message, requests=0):
        super().__init__(message)
        self.requests = requests


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def decode_plan(content, allowed):
    try:
        value = json.loads(content, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        raise ProviderError("malformed_json") from None
    try:
        action = parse_action(value)
    except (ValueError, TypeError, ValidationError) as exc:
        fields = [str(e["loc"]) for e in exc.errors()] if isinstance(exc, ValidationError) else []
        raise ProviderError("invalid_action_fields:" + ",".join(fields)) from None
    if action.name not in allowed:
        raise ProviderError("ineligible_action:" + action.name)
    return action


def action_schema(state, text=""):
    text = sanitize_text(text)
    branches = []
    for name in allowed_next(state):
        if name == "find_patient" and not re.search(r"\d{10,}", text):
            continue
        arguments = ARGUMENTS[name].model_json_schema()
        if name == "update_dialogue_state":
            spans = re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", text)
            if not any(
                cue in text.casefold()
                for cue in (
                    "my child",
                    "my mother",
                    "my father",
                    "my son",
                    "my daughter",
                    "someone else",
                    "myself",
                    "for me",
                    "my own",
                    "بچے",
                    "بیٹی",
                    "بیٹے",
                    "والد",
                    "کسی اور",
                    "اپنے بجائے",
                    "میرے لیے",
                    "اپنے لیے",
                )
            ):
                arguments["properties"].pop("subject", None)
            temporal = bool(
                spans
                or re.search(
                    r"(?i)\b(today|tomorrow|yesterday|kal|parson|aaj|morning|evening|afternoon|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|baje|am|pm|jummah|derh|dhai|half|quarter|saarhe|paune|sawa)\b|ساڑھے|پونے|سوا|ڈیڑھ|ڈھائی|آج|کل|پرسوں|صبح|دوپہر|شام|رات|بجے|پیر|منگل|بدھ|جمعرات|جمعہ|ہفتہ|اتوار|نماز|\d{1,2}:\d{2}",
                    text,
                )
            )
            if not temporal:
                arguments["properties"].pop("date_phrase", None)
            if not re.search(
                r"(?i)book|cancel|reschedul|appointment|بک|منسوخ|اپوائنٹمنٹ|وقت تبدیل", text
            ):
                arguments["properties"].pop("task", None)
            if len(set(spans)) == 1:
                arguments["required"] = ["date_phrase"]
        if name in ("hold_slot", "select_appointment"):
            source = state.get("offers" if name == "hold_slot" else "appointments", [])
            arguments["properties"]["ref"]["enum"] = [v["ref"] for v in source]
        branches.append(
            {
                "type": "object",
                "properties": {"name": {"const": name}, "arguments": arguments},
                "required": ["name", "arguments"],
                "additionalProperties": False,
            }
        )
    return {"anyOf": branches}


class Nemotron:
    def __init__(self, settings, transport=None, *, clock=time.monotonic):
        from .provider_router import ProviderRouter

        self.settings = settings
        self.client = httpx2.Client(timeout=settings.llm_timeout_seconds, transport=transport)
        self.router = ProviderRouter(settings, self.client, clock=clock)

    @property
    def requests(self):
        return self.router.requests

    @property
    def last_failure(self):
        return self.router.last_failure

    def close(self):
        self.router.close()

    def drain(self, timeout: float) -> bool:
        return self.router.drain(timeout)

    def plan(self, state, text, config, facts):
        return self._structured(
            messages(state, text, config, facts),
            action_schema(state, text),
            "next_action",
            lambda content: decode_plan(content, allowed_next(state)),
        )

    def ground(self, query, language, chunks, *, deadline):
        from knowledge.grounding import answer_messages, answer_schema

        return self._structured(
            answer_messages(query, language, chunks),
            answer_schema(language),
            "grounded_answer",
            lambda content: content,
            deadline=deadline,
        )

    def _structured(self, prompt, schema, name, decode, *, deadline=None):
        return self.router.invoke(
            {
                "messages": prompt,
                "temperature": 0,
                "top_p": 1,
                "max_tokens": 512,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": schema},
                },
            },
            decode,
            deadline=deadline,
        )
