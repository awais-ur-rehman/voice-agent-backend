import importlib.util

import pytest


def test_contract_implementation_exists():
    assert importlib.util.find_spec("voice_runtime.speech.contracts") is not None


def test_pcm_rejects_malformed_stereo_and_unbounded_audio():
    from voice_runtime.speech.contracts import PCMChunk

    for args in [
        (b"x", 16000, 1),
        (b"xx", 16000, 2),
        (b"xx", 0, 1),
        (b"\0\0" * 16000 * 31, 16000, 1),
        (b"", 16000, 1),
    ]:
        with pytest.raises(ValueError):
            PCMChunk(*args)
    pcm = PCMChunk(b"\0\0" * 160, 16000, 1)
    assert pcm.duration_seconds == 0.01
    with pytest.raises(Exception):
        pcm.channels = 2


def test_asr_rejects_invalid_script_language_and_confidence():
    from voice_runtime.speech.contracts import ASRResult

    for args in [
        ("नमस्ते", "ur", 0.9, False),
        ("Hello", "hi", 0.9, False),
        ("Hello", "en", 2, False),
        ("hello", "ur", 0.9, False),
    ]:
        with pytest.raises(ValueError):
            ASRResult(*args)
    assert ASRResult("السلام علیکم", "ur", None, True).uncertain
