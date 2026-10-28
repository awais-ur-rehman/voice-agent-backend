"""Bounded, immutable signed 16-bit little-endian mono audio contracts."""

import math
from dataclasses import dataclass
from typing import Literal

Language = Literal["en", "ur"]


@dataclass(frozen=True)
class PCMChunk:
    samples: bytes
    sample_rate: int
    channels: int = 1
    text_span: tuple[int, int] | None = None
    encoding: Literal["s16le"] = "s16le"

    def __post_init__(self):
        if (
            not isinstance(self.samples, bytes)
            or not self.samples
            or len(self.samples) % 2
            or self.sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000)
            or self.channels != 1
            or self.encoding != "s16le"
        ):
            raise ValueError("expected nonempty s16le mono PCM at a supported rate")
        if self.duration_seconds > 30:
            raise ValueError("PCM exceeds 30 seconds")
        if self.text_span is not None and (
            len(self.text_span) != 2 or not 0 <= self.text_span[0] < self.text_span[1]
        ):
            raise ValueError("invalid text span")

    @property
    def duration_seconds(self) -> float:
        return len(self.samples) / (2 * self.sample_rate)


@dataclass(frozen=True)
class ASRResult:
    text: str
    language: Language
    confidence: float | None
    uncertain: bool

    def __post_init__(self):
        if self.language not in ("en", "ur"):
            raise ValueError("unsupported language")
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise ValueError("invalid confidence")
        letters = [c for c in self.text if c.isalpha()]
        if any("\u0900" <= c <= "\u097f" for c in self.text):
            raise ValueError("invalid script")
        if (
            self.language == "ur"
            and letters
            and not any("\u0600" <= c <= "\u06ff" for c in letters)
        ):
            raise ValueError("Urdu result lacks Urdu script")
