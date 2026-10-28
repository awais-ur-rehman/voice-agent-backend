"""Real Kokoro CPU inference, completed segments (not native frame streaming)."""

from voice_runtime.speech.adapter import SpeechAdapter


class KokoroAdapter(SpeechAdapter):
    engine = "kokoro"
    language = "en"
    sample_rate = 24000
    streaming = "completed-segment"
