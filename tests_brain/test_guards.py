import pytest
from pydantic import ValidationError


def test_model_cannot_assign_authorization_or_unknown_fields():
    from brain.contracts import parse_action

    for name, args in [
        ("update_dialogue_state", {"verified": True}),
        ("book_appointment", {"confirmed": True}),
        ("find_patient", {"phone": "03001234567", "tenant_id": "x"}),
    ]:
        with pytest.raises(ValidationError):
            parse_action({"name": name, "arguments": args})


@pytest.mark.parametrize(
    "text",
    ["yes but change the time", "not yes", "maybe", "جی لیکن وقت بدل دیں", "نہیں", "yes tomorrow"],
)
def test_qualified_or_negative_reply_never_confirms(text):
    from brain.dialogue import affirmative

    assert not affirmative(text)


@pytest.mark.parametrize("text", ["Yes.", "confirm", "جی ہاں", "ہاں", "جی"])
def test_unqualified_explicit_affirmative(text):
    from brain.dialogue import affirmative

    assert affirmative(text)


def test_unverified_state_cannot_request_private_or_mutating_tools():
    from brain.dialogue import allowed_next

    state = {
        "task": "cancel",
        "stage": "identify",
        "verified": False,
        "attempts": 0,
        "fields": {},
        "offers": [],
        "readback": None,
    }
    allowed = allowed_next(state)
    assert "find_patient" in allowed
    assert not set(allowed) & {
        "find_appointments",
        "cancel_appointment",
        "reschedule_appointment",
        "book_appointment",
    }


def test_redacted_history_does_not_retain_identity_factor():
    from brain.dialogue import remember

    state = {"history": [], "summary": {}}
    remember(state, "Synthetic Patient 1990-02-03", "Verified.", sensitive=True)
    assert "1990" not in str(state) and "Synthetic" not in str(state)
    for i in range(30):
        remember(state, str(i), "Continue.")
    assert len(state["history"]) == 10


@pytest.mark.parametrize(
    "text",
    [
        "Book for my child instead of myself.",
        "اپنے بچے کے لیے بک کرنا چاہتا ہوں۔",
        "This appointment is for my mother.",
    ],
)
def test_explicit_family_subject_change_is_detected_before_planning(text):
    from brain.dialogue import subject_change

    assert subject_change(text) == "other"


def test_resume_is_only_eligible_when_a_task_is_parked():
    from brain.dialogue import allowed_next

    state = {"task": "book", "verified": True, "fields": {}, "parked": []}
    assert "resume_task" not in allowed_next(state)
    state["parked"] = [{"task": "cancel", "fields": {}}]
    assert "resume_task" in allowed_next(state)
