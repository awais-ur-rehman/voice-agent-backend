"""Bilingual code output; critical fields never pass through a language model."""

MESSAGES = {
    "service": ("Which service would you like?", "آپ کو کون سی خدمت چاہیے؟"),
    "date": ("Which date and time would you prefer?", "آپ کس تاریخ اور وقت پر آنا چاہیں گے؟"),
    "search_ready": (
        "Shall I show the available appointment times?",
        "کیا دستیاب اپوائنٹمنٹ کے اوقات دکھاؤں؟",
    ),
    "find_ready": (
        "Shall I find the patient’s upcoming appointments?",
        "کیا مریض کی آنے والی اپوائنٹمنٹس دکھاؤں؟",
    ),
    "choose_time": ("Which offered time would you prefer?", "آپ کون سا دستیاب وقت پسند کریں گے؟"),
    "choose_appointment": (
        "Which appointment would you like to change?",
        "آپ کون سی اپوائنٹمنٹ تبدیل کرنا چاہیں گے؟",
    ),
    "no_appointments": (
        "No upcoming booked appointments were found.",
        "آنے والی کوئی بک شدہ اپوائنٹمنٹ نہیں ملی۔",
    ),
    "silence": ("Are you still there?", "کیا آپ ابھی بھی موجود ہیں؟"),
    "ask": ("How can I help with your appointment?", "آپ کو اپوائنٹمنٹ کے بارے میں کیا مدد چاہیے؟"),
    "phone": ("What is the patient’s phone number?", "مریض کا فون نمبر کیا ہے؟"),
    "factor": (
        "Please give the patient’s exact name or date of birth.",
        "براہ کرم مریض کا پورا نام یا تاریخ پیدائش بتائیں۔",
    ),
    "verified": (
        "Identity verified. What appointment do you need?",
        "شناخت کی تصدیق ہو گئی۔ آپ کو کون سی اپوائنٹمنٹ چاہیے؟",
    ),
    "new": (
        "No matching record was found. What is the new patient’s name?",
        "متعلقہ ریکارڈ نہیں ملا۔ نئے مریض کا نام کیا ہے؟",
    ),
    "confirmed": ("Your appointment is confirmed.", "آپ کی اپوائنٹمنٹ کی تصدیق ہو گئی ہے۔"),
    "cancelled": ("Your appointment is cancelled.", "آپ کی اپوائنٹمنٹ منسوخ ہو گئی ہے۔"),
    "patient_created": (
        "The new patient record is ready. Which service do you need?",
        "نئے مریض کا ریکارڈ بن گیا ہے۔ آپ کو کون سی خدمت چاہیے؟",
    ),
    "updated": ("I have updated your request.", "آپ کی درخواست تبدیل کر دی گئی ہے۔"),
    "hold": (
        "The time is held briefly. Shall I read back the details?",
        "وقت عارضی طور پر محفوظ ہے۔ کیا تفصیل دہرا دوں؟",
    ),
    "resume": ("Let’s continue your previous request.", "آئیے آپ کی پچھلی درخواست جاری رکھیں۔"),
    "handoff": (
        "I have queued a callback request for the clinic team.",
        "کلینک کی ٹیم کے لیے واپسی کال کی درخواست درج کر دی گئی ہے۔",
    ),
    "emergency": (
        "If this is an emergency, contact local emergency services now. This scheduler cannot provide medical advice.",
        "اگر یہ ہنگامی حالت ہے تو فوراً مقامی ہنگامی خدمات سے رابطہ کریں۔ یہ نظام طبی مشورہ نہیں دے سکتا۔",
    ),
    "end": ("Thank you. Goodbye.", "شکریہ۔ خدا حافظ۔"),
    "validation_error": ("Please clarify that request.", "براہ کرم اپنی درخواست واضح کریں۔"),
    "not_verified": (
        "Please verify the patient’s identity first.",
        "براہ کرم پہلے مریض کی شناخت کی تصدیق کریں۔",
    ),
    "verification_failed": (
        "Those details could not be verified. Please try again.",
        "ان تفصیلات کی تصدیق نہیں ہوئی۔ براہ کرم دوبارہ بتائیں۔",
    ),
    "patient_not_found": ("No matching patient was found.", "متعلقہ مریض نہیں ملا۔"),
    "slot_unavailable": (
        "That time is no longer available. Please choose another time.",
        "وہ وقت اب دستیاب نہیں۔ براہ کرم دوسرا وقت منتخب کریں۔",
    ),
    "outside_policy": (
        "That request is outside the clinic’s scheduling policy.",
        "یہ درخواست کلینک کی اپوائنٹمنٹ پالیسی کے مطابق نہیں ہے۔",
    ),
    "not_owner": (
        "That appointment cannot be accessed in this session.",
        "اس نشست میں اس اپوائنٹمنٹ تک رسائی نہیں ہے۔",
    ),
    "rate_limited": (
        "Too many attempts. Please contact the clinic team.",
        "کوششوں کی حد پوری ہو گئی۔ براہ کرم کلینک کی ٹیم سے رابطہ کریں۔",
    ),
    "upstream_timeout": (
        "The service is temporarily unavailable. Please try again.",
        "سروس عارضی طور پر دستیاب نہیں۔ براہ کرم دوبارہ کوشش کریں۔",
    ),
    "no_answer": (
        "I do not have a verified answer. Please ask the clinic team.",
        "میرے پاس تصدیق شدہ جواب نہیں۔ براہ کرم کلینک کی ٹیم سے پوچھیں۔",
    ),
}


def result(key="updated", data=None, *, error=None, field=None, say=None):
    pair = MESSAGES[error or key]
    failure = (
        {"code": error, "retryable": error in {"slot_unavailable", "upstream_timeout"}}
        if error
        else None
    )
    if failure and field:
        failure["field"] = field
    return {
        "ok": not error,
        "data": None if error else data or {},
        "say": say or dict(zip(("en", "ur"), pair)),
        "error": failure,
    }


def toned_question(key, tone):
    # Only a general invitation varies by tone; critical bilingual templates stay fixed.
    variants = {
        "friendly": (
            "How can I help with your appointment?",
            "آپ کو اپوائنٹمنٹ کے بارے میں کیا مدد چاہیے؟",
        ),
        "formal": (
            "How may I assist with your appointment?",
            "میں آپ کی اپوائنٹمنٹ کے سلسلے میں کیا مدد کر سکتا ہوں؟",
        ),
        "concise": ("What appointment do you need?", "آپ کو کون سی اپوائنٹمنٹ چاہیے؟"),
    }
    return (
        result(key, say=dict(zip(("en", "ur"), variants.get(tone, variants["friendly"]))))
        if key == "ask"
        else result(key)
    )
