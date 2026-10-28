"""Real Indic Parler CPU inference, completed segment output."""

from voice_runtime.speech.adapter import SpeechAdapter


class ParlerAdapter(SpeechAdapter):
    engine = "parler"
    language = "ur"
    sample_rate = 44100
    streaming = "completed-segment"
