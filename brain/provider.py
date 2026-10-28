"""Bounded hosted NIM adapter. No provider text becomes caller output."""

import json
import re
import threading
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
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx2.Client(timeout=settings.llm_timeout_seconds, transport=transport)
        self.lock = threading.Lock()
        self.requests = 0
        self.last_failure = None
        self.failures = 0
        self.open_until = 0.0

    def close(self):
        self.client.close()

    def plan(self, state, text, config, facts):
        with self.lock:
            if (
                not self.settings.llm_api_key
                or not self.settings.llm_api_key.get_secret_value()
                or self.requests >= self.settings.llm_request_budget
                or time.monotonic() < self.open_until
            ):
                raise ProviderError("unavailable")
            self.requests += 1
        try:
            response = self.client.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value()},
                json={
                    "model": self.settings.llm_model,
                    "messages": messages(state, text, config, facts),
                    "temperature": 0,
                    "top_p": 1,
                    "max_tokens": 512,
                    "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "next_action",
                            "strict": True,
                            "schema": action_schema(state, text),
                        },
                    },
                },
            )
            response.raise_for_status()
            data = response.json()
            action = decode_plan(data["choices"][0]["message"]["content"], allowed_next(state))
        except (
            httpx2.HTTPError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            ProviderError,
        ) as exc:
            self.last_failure = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            with self.lock:
                self.failures += 1
                if self.failures >= 2:
                    self.open_until = time.monotonic() + 30
            raise ProviderError("provider_failed", requests=1) from None
        with self.lock:
            self.failures = 0
        return action
