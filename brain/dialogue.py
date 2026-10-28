"""Server-owned workflow eligibility and bounded, non-identifying conversation memory."""

import re


def affirmative(text: str) -> bool:
    return re.sub(r"[.!؟،۔?]", "", text.strip().casefold()).strip() in {
        "yes",
        "yes please",
        "confirm",
        "confirmed",
        "جی",
        "ہاں",
        "جی ہاں",
        "جی بالکل",
        "بالکل",
        "haan",
        "ji",
        "ji haan",
    }


def allowed_next(state: dict) -> list[str]:
    if state.get("ended"):
        return []
    common = [
        "find_patient",
        "set_caller_contact",
        "get_clinic_info",
        "search_knowledge",
        "ask_caller",
        "request_handoff",
        "end_call",
    ]
    if state.get("attempts", 0) >= 3 or state.get("handoff"):
        return ["request_handoff", "end_call"]
    common += ["update_dialogue_state"]
    if state.get("parked"):
        common += ["resume_task"]
    if not state.get("verified"):
        common += ["find_patient"]
        if state.get("candidates"):
            common += ["verify_patient"]
        if state.get("lookup_empty"):
            common += ["prepare_patient"]
        if state.get("readback", {}) and state["readback"].get("kind") == "patient":
            common += ["create_patient", "confirm"]
        return common
    if state.get("fields", {}).get("temporal_pending"):
        return sorted(set(common))
    if state.get("task") in ("cancel", "reschedule"):
        common += ["find_appointments"]
        if state.get("appointments") and not state.get("appointment"):
            common += ["select_appointment"]
        if not state.get("appointment"):
            return common
    if state.get("task") != "cancel":
        if all(state.get("fields", {}).get(k) for k in ("service_id", "date")):
            common += ["get_available_slots"]
        if state.get("offers"):
            common += ["hold_slot"]
    if state.get("task") == "cancel" and state.get("appointment") or state.get("hold"):
        common += ["read_back"]
    if state.get("readback") and state["readback"].get("kind") == "appointment":
        common += [
            "confirm",
            {
                "book": "book_appointment",
                "cancel": "cancel_appointment",
                "reschedule": "reschedule_appointment",
            }[state["task"]],
        ]
    return sorted(set(common))


def remember(state: dict, caller: str, reply: str, *, sensitive: bool = False):
    # Patient identifiers/factors never enter retained memory. Only delivered system text and
    # non-identifying intent categories are retained; raw text is used ephemerally by planner.
    state.setdefault("history", []).extend(
        [
            {
                "role": "user",
                "content": "[identity input redacted]" if sensitive else "[caller turn]",
            },
            {
                "role": "assistant",
                "content": "[identity response redacted]" if sensitive else reply,
            },
        ]
    )
    state["history"] = state["history"][-10:]
    state["summary"] = {
        "task": state.get("task"),
        "stage": state.get("stage"),
        "turns": state.get("turns", 0),
    }


def subject_change(text: str) -> str | None:
    """A clear family change immediately removes the previous subject's authority."""
    if re.search(
        r"(?i)\b(?:my|our)\s+(?:child|son|daughter|mother|father|wife|husband|sister|brother)\b|someone else|another patient|بچے|بیٹی|بیٹے|والدہ|والد|بہن|بھائی|شوہر|بیوی|کسی اور",
        text,
    ):
        return "other"
    if re.search(r"(?i)for myself|for me\b|my own appointment|اپنے لیے|میرے لیے", text):
        return "self"
    return None
