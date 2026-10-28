import pytest


@pytest.mark.parametrize(
    "text", ["yes", "ok", "جی", "appointment", "doctor", "cancel", "thanks", "12345"]
)
def test_neutral_turn_does_not_switch(text):
    from brain.language import language_transition

    assert language_transition("ur", "en", 1, text) == ("ur", None, 0)


def test_two_dominant_turns_are_required_and_streak_resets():
    from brain.language import language_transition

    assert language_transition("ur", None, 0, "Can you help me book?") == (
        "ur",
        "en",
        1,
    )
    assert language_transition("ur", "en", 1, "Please book an appointment for me") == (
        "en",
        None,
        0,
    )
    assert language_transition("en", None, 0, "کیا آپ میری مدد کریں گے؟") == (
        "en",
        "ur",
        1,
    )
    assert language_transition("en", "ur", 1, "مجھے کل وقت چاہیے") == ("ur", None, 0)
    assert language_transition("en", "ur", 1, "I need help booking") == ("en", None, 0)


@pytest.mark.parametrize("mode", ["ur", "en"])
def test_fixed_language_ignores_turn(mode):
    from brain.language import language_transition

    assert language_transition("ur", "en", 1, "Please book an appointment", mode=mode) == (
        mode,
        None,
        0,
    )


def test_loanwords_do_not_outweigh_urdu():
    from brain.language import detect_language

    assert detect_language("appointment چاہیے۔", "ur") == "ur"
    assert detect_language("अगले منگل", "ur") is None


@pytest.mark.parametrize("text", ["جی۔", "جی،", "ہاں؟", "نہیں۔", "شکریہ!"])
def test_punctuated_acknowledgment_is_neutral_in_english_session(text):
    from brain.language import detect_language, language_transition

    assert detect_language(text, "en") is None
    assert language_transition("en", "ur", 1, text) == ("en", None, 0)


def test_audio_language_uses_trusted_asr_two_turns_and_rejects_uncertainty():
    from brain.language import audio_language_transition

    assert audio_language_transition("en", None, 0, "ur", 0.9, False) == ("en", "ur", 1)
    assert audio_language_transition("en", "ur", 1, "ur", 0.9, False) == ("ur", None, 0)
    assert audio_language_transition("en", "ur", 1, "ur", 0.9, True) == ("en", None, 0)
    assert audio_language_transition("en", "ur", 1, "ur", 0.2, False) == ("en", None, 0)
    assert audio_language_transition("en", None, 0, "ur", 0.9, False, "en") == ("en", None, 0)
