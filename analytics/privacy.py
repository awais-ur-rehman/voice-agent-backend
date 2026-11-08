"""Code-owned product consent and best-effort redaction, never de-identification.

Free text can contain residual sensitive content. Consent is not a legal assurance.
Identity literals live only in the request and must never be logged or serialized.
"""

import re
import unicodedata
from typing import Literal

NOTICE_VERSION = "recording-v1"
NOTICE = (
    "May we save a redacted text transcript for quality review? Sensitive details may remain. "
    "Say yes to allow recording or no to continue without it. "
    "کیا معیار کے جائزے کے لیے شناختی تفصیل چھپا کر گفتگو کا متن ریکارڈ کر سکتے ہیں؟ "
    "حساس معلومات باقی رہ سکتی ہیں۔ اجازت کے لیے ہاں، ورنہ نہیں کہیں۔"
)
ACCEPT = {"yes", "yes please", "i agree", "allow recording", "جی", "جی ہاں", "ہاں", "اجازت ہے"}
DECLINE = {
    "no",
    "no thanks",
    "do not record",
    "don't record",
    "نہیں",
    "ریکارڈ نہ کریں",
    "اجازت نہیں",
}
IDENTITY_KEYS = {
    "name",
    "name_ur",
    "phone",
    "dob",
    "factor",
    "email",
    "lookup_phone",
    "last_name",
    "first_name",
}


def normalized_phrase(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("’", "'")
    value = "".join(
        " " if unicodedata.category(c).startswith("P") and c != "'" else c for c in value
    )
    return " ".join(value.split())


def consent_decision(value: str) -> Literal["accepted", "declined"] | None:
    value = normalized_phrase(value)
    return "accepted" if value in ACCEPT else "declined" if value in DECLINE else None


def safety_action(text: str, action: dict | None = None) -> dict | None:
    from brain.knowledge import local_input_action

    local, _ = local_input_action({}, text, {})
    if local and local["name"] == "request_handoff":
        return local
    if action and action.get("name") in {"request_handoff", "end_call"}:
        return action
    phrase = normalized_phrase(text)
    if phrase in {
        "end call",
        "end the call",
        "end this call",
        "goodbye",
        "bye",
        "خدا حافظ",
        "کال ختم کریں",
    }:
        return {"name": "end_call", "arguments": {}}
    if phrase in {
        "speak to a person",
        "i want to speak to a person",
        "talk to a person",
        "human please",
        "handoff",
        "عملے سے بات کرنی ہے",
        "مجھے کسی انسان سے بات کرنی ہے",
        "میں کسی انسان سے بات کرنا چاہتا ہوں",
        "میں کسی انسان سے بات کرنا چاہتی ہوں",
    }:
        return {"name": "request_handoff", "arguments": {"reason": "requested"}}
    return None


def consent_response(decision: str | None) -> dict:
    from brain.templates import result

    if decision == "accepted":
        return result(
            say={
                "en": "Recording allowed. How can I help?",
                "ur": "ریکارڈنگ کی اجازت مل گئی۔ کیا مدد کر سکتا ہوں؟",
            }
        )
    if decision == "declined":
        return result(
            say={
                "en": "We will continue without recording. How can I help?",
                "ur": "ریکارڈ کیے بغیر بات جاری رکھیں گے۔ کیا مدد کر سکتا ہوں؟",
            }
        )
    return result(say={"en": NOTICE, "ur": NOTICE})


def identity_values(value) -> tuple[str, ...]:
    values = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in IDENTITY_KEYS and isinstance(child, str) and child.strip():
                values.append(child)
            elif key == "patient_snapshot" and isinstance(child, list):
                values.extend(v for v in child if isinstance(v, str))
            elif isinstance(child, (dict, list)):
                values.extend(identity_values(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(identity_values(child))
    return tuple(values)


def redact(value: str, literals: tuple[str, ...]) -> str:
    # Normalize whitespace before matching stored names and Urdu/ASCII variants.
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    for literal in sorted(set(literals), key=len, reverse=True):
        literal = " ".join(unicodedata.normalize("NFKC", literal).split())
        if literal:
            value = re.sub(re.escape(literal), "[redacted]", value, flags=re.IGNORECASE)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[\w-]+", "[redacted]", value)
    # \d covers English, Arabic and Urdu decimal digits. This deliberately
    # over-redacts dates/numbers instead of guessing whether a number is identity.
    value = re.sub(r"\d(?:[\d\s()+./:-]*\d)?", "[redacted]", value)
    return value[:8000]
