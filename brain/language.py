"""Conservative text-language adapter for Phase 2, not Whisper language ID."""

from __future__ import annotations

import re

_NEUTRAL = {
    "yes",
    "no",
    "ok",
    "okay",
    "thanks",
    "thank",
    "you",
    "please",
    "ji",
    "jee",
    "haan",
    "han",
    "nahi",
    "appointment",
    "doctor",
    "cancel",
    "tuesday",
    "جی",
    "ہاں",
    "نہیں",
    "شکریہ",
    "اوکے",
    "اچھا",
}
_ROMAN_UR = {
    "mujhe",
    "chahiye",
    "mera",
    "meri",
    "aap",
    "hain",
    "hai",
    "kar",
    "karein",
    "kal",
    "aaj",
    "baje",
    "ke",
    "liye",
    "mein",
    "ko",
    "book",
    "karna",
}


def detect_language(text: str, current: str) -> str | None:
    """Return a dominant language or None for neutral/corrupt/unknown turns."""
    if not isinstance(text, str) or re.search(r"[\u0900-\u097f]", text):
        return None
    tokens = re.findall(
        r"[A-Za-z]+|[\u0621-\u063a\u0641-\u064a\u066e-\u06d3\u06fa-\u06ff]+",
        text.lower(),
    )
    substantive = [word for word in tokens if word not in _NEUTRAL]
    ur = sum(bool(re.search(r"[\u0621-\u06d3]", word)) for word in substantive)
    latin = [word for word in substantive if word.isascii()]
    roman = sum(word in _ROMAN_UR for word in latin)
    if ur and ur >= len(latin):
        return "ur"
    if roman >= 2 and roman > len(latin) / 2:
        return "ur"
    if len(latin) >= 2 and len(latin) > ur:
        return "en"
    return None


def language_transition(
    current: str, pending: str | None, streak: int, text: str, mode: str = "bilingual"
) -> tuple[str, str | None, int]:
    if mode in ("en", "ur"):
        return mode, None, 0
    if mode != "bilingual" or current not in ("en", "ur"):
        raise ValueError("invalid_language_mode")
    detected = detect_language(text, current)
    if detected is None or detected == current:
        return current, None, 0
    count = streak + 1 if pending == detected else 1
    return (detected, None, 0) if count >= 2 else (current, detected, count)


def audio_language_transition(
    current: str,
    pending: str | None,
    streak: int,
    detected: str,
    confidence: float | None,
    uncertain: bool,
    mode: str = "bilingual",
) -> tuple[str, str | None, int]:
    """Trusted ASR metadata supplies language ID; no text/script guess replaces it."""
    if mode in ("en", "ur"):
        return mode, None, 0
    if mode != "bilingual" or current not in ("en", "ur") or detected not in ("en", "ur"):
        raise ValueError("invalid_language_mode")
    if uncertain or confidence is not None and confidence < 0.5 or detected == current:
        return current, None, 0
    count = streak + 1 if pending == detected else 1
    return (detected, None, 0) if count >= 2 else (current, detected, count)
