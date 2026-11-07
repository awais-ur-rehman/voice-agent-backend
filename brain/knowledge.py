"""Local public-question routing and the prepared-answer tool bridge."""

import re

from sqlalchemy import select

from knowledge.contracts import KnowledgeTrace
from knowledge.runtime import validate_prepared
from scheduler.models import Patient

from .dialogue import subject_change
from .templates import result

QUESTION = re.compile(
    r"(?i)^(what|where|when|how|why|which|can|could|should|do|does|is|are)\b|^(کیا|کب|کہاں|کیسے|کون|کتنے|کتنی)"
)
QUESTION_PART = re.compile(r"(?i)\b(what|where|when|how|why|which)\b|کیا|کہاں|کیسے|کب|[?؟]")
IDENTITY = re.compile(
    r"(?i)\d{4}-\d{2}-\d{2}|\d[\d ()+-]{7,}\d|\b(dob|date of birth|my name|patient name|phone|cnic)\b|تاریخ پیدائش|میرا نام|شناختی کارڈ|فون نمبر"
)
BOOKING = re.compile(
    r"(?i)\b(book|booking|cancel|reschedul\w*|appointment|move)\b|اپوائنٹمنٹ|بکنگ|منسوخ|وقت تبدیل"
)


def identity_literals(db, state):
    identities = state.get("candidates", []) + ([state["subject"]] if state.get("subject") else [])
    values: list[str] = []
    if identities:
        for patient in db.scalars(select(Patient).where(Patient.id.in_(identities))):
            values.extend(
                filter(
                    None,
                    [
                        patient.name,
                        patient.name_ur,
                        patient.phone,
                        patient.dob.isoformat() if patient.dob else None,
                    ],
                )
            )
    values.extend(
        filter(None, [state.get("lookup_phone"), state.get("pending_patient", {}).get("name")])
    )
    return tuple(" ".join(v.casefold().split()) for v in values)


def local_route(state, text, literals=()):
    normalized = " ".join(text.casefold().split())
    if not QUESTION_PART.search(text) and not QUESTION.search(text):
        return None
    mixed = (
        IDENTITY.search(text)
        or subject_change(text)
        or BOOKING.search(text)
        or any(v in normalized for v in literals)
    )
    if mixed:
        return "clarify"
    if QUESTION.search(text):
        return "faq"
    if state.get("candidates") or state.get("lookup_empty"):
        return "clarify"
    return None


def local_input_action(state, text, config, literals=()):
    """The same privacy/emergency gate applies to live and explicit KB actions."""
    text = text.strip()
    if any(
        word in text.casefold()
        for word in (
            "emergency",
            "chest pain",
            "cannot breathe",
            "can't breathe",
            "difficulty breathing",
            "سانس نہیں",
            "سینے میں درد",
            "ہنگامی",
        )
    ):
        return {"name": "request_handoff", "arguments": {"reason": "emergency"}}, False
    route = local_route(state, text, literals)
    if route == "clarify":
        return {"name": "ask_caller", "arguments": {}}, True
    topic = structured_topic(text, config)
    if topic:
        return {"name": "get_clinic_info", "arguments": {"topic": topic}}, False
    if route == "faq":
        return {"name": "search_knowledge", "arguments": {}}, False
    subject = subject_change(text)
    if subject:
        return {"name": "update_dialogue_state", "arguments": {"subject": subject}}, False
    if (
        state.get("candidates")
        and not state.get("verified")
        and not any(word in text.casefold() for word in ("cancel", "reschedule", "منسوخ", "تبدیل"))
    ):
        return {"name": "verify_patient", "arguments": {"factor": text}}, False
    if re.fullmatch(r"[+\d ()-]{9,40}", text) and len(re.sub(r"\D", "", text)) >= 10:
        return {"name": "find_patient", "arguments": {"phone": text}}, False
    return None, False


def structured_topic(text, config):
    lower = text.casefold()
    if re.search(r"\bfees?\b|\bprice\b|\bcost\b|فیس|قیمت", lower):
        return "fees"
    # Match clinic fact domains, not arbitrary occurrences of "open" or "where".
    # An address in either locale owns the domain even if the caller's locale
    # needs a code-owned no-answer response. Documents cannot fill that gap.
    if re.search(
        r"\b(?:opening|closing|clinic|your) (?:hours|times?)\b"
        r"|\b(?:you|clinic) (?:open|close|opens|closes)\b"
        r"|\b(?:you|clinic) (?:is |are )?(?:open|closed)\b"
        r"|\b(?:are you|is (?:the )?clinic) still (?:open|closed)\b"
        r"|اوقات|کھلنے|بند ہونے|(?:کلینک|آپ).*(?:بند|کھل)",
        lower,
    ):
        return "hours"
    profile = config.get("clinic_profile", {})
    for topic, prefix, pattern in (
        (
            "location",
            "address",
            r"\b(?:your|clinic|the) (?:address|location)\b"
            r"|\bwhere (?:is|are) (?:the clinic|you)\b"
            r"|\bclinic (?:is )?located\b"
            r"|\b(?:address|location) of (?:the )?clinic\b"
            r"|پتہ|کلینک.*کہاں|آپ.*کہاں",
        ),
        (
            "preparation",
            "preparation",
            r"\b(?:how (?:should|do|can) I prepare|preparation)\b"
            r"|\bwhat(?: documents| papers)? (?:should|do|must|can) I bring\b"
            r"|\bwhat documents (?:are|do I) need\b"
            r"|\bdo I need to bring (?:any |my |the )?(?:documents|papers)\b"
            r"|لاؤں|لانا|تیاری|دستاویزات",
        ),
    ):
        if any(profile.get(prefix + "_" + lang) for lang in ("en", "ur")) and re.search(
            pattern, lower, re.IGNORECASE
        ):
            return topic
    return None


def clarification(state):
    state["readback"] = None
    return result(
        say={
            "en": "Please give the identity detail and clinic question separately.",
            "ur": "براہ کرم شناخت کی تفصیل اور کلینک کا سوال الگ الگ بتائیں۔",
        }
    )


def answer(engine, state):
    # A FAQ acknowledgment cannot authorize an old appointment/patient readback.
    state["readback"] = None
    prepared = engine.prepared_knowledge
    if prepared is None:
        response = result(error="no_answer")
        response["knowledge"] = KnowledgeTrace(status="service_unavailable").model_dump(mode="json")
        return response
    final = validate_prepared(
        engine.db,
        engine.actor,
        prepared,
        command_id=engine.command_id,
        expected_revision=state["revision"],
        language=state.get("lang", "en"),
        now=engine.clock(),
    )
    trace = KnowledgeTrace(
        status=final.result.reason, citations=list(final.answer.citations) if final.answer else []
    )
    if final.answer:
        say = dict(result(error="no_answer")["say"])
        say[state.get("lang", "en")] = final.answer.answer
        response = result(say=say)
    else:
        technical = final.result.reason == "service_unavailable"
        response = result(
            error="upstream_timeout" if technical else "no_answer",
            say={
                "en": (
                    "Knowledge is temporarily unavailable. "
                    if technical
                    else "I do not have a verified answer. "
                )
                + "Would you like to request a callback from the clinic team?",
                "ur": (
                    "معلومات کی سروس عارضی طور پر دستیاب نہیں۔ "
                    if technical
                    else "میرے پاس تصدیق شدہ جواب نہیں۔ "
                )
                + "کیا آپ کلینک کی ٹیم سے واپسی کال کی درخواست کرنا چاہیں گے؟",
            },
        )
    if final.answer and (
        state.get("hold") or state.get("pending_patient") or state.get("candidates")
    ):
        for lang in ("en", "ur"):
            response["say"][lang] += " " + (
                "We can continue your appointment request."
                if lang == "en"
                else "ہم آپ کی اپوائنٹمنٹ کی درخواست جاری رکھ سکتے ہیں۔"
            )
    response["knowledge"] = trace.model_dump(mode="json")
    return response
