import pytest


def test_dynamic_or_patient_speech_cannot_enter_shared_cache(tmp_path):
    from voice_runtime.cache import PublicClipCache
    from voice_runtime.speech.contracts import PCMChunk

    cache = PublicClipCache(tmp_path)
    with pytest.raises(ValueError):
        cache.put("Your appointment for Jane is confirmed.", "en", PCMChunk(b"\1\0" * 160, 24000))
    cache.put("Hello. How can I help you today?", "en", PCMChunk(b"\1\0" * 160, 24000))
    assert cache.get("Hello. How can I help you today?", "en").samples == b"\1\0" * 160


def test_public_cache_key_changes_with_the_frozen_voice_configuration(tmp_path, monkeypatch):
    import voice_runtime.cache as module

    cache = module.PublicClipCache(tmp_path)
    text = "السلام علیکم۔ آپ کیسے ہیں؟"
    before = cache.key(text, "ur")
    monkeypatch.setitem(
        module.FROZEN_VOICES, "ur", {"description": "changed synthetic voice", "seed": 42}
    )
    assert cache.key(text, "ur") != before
