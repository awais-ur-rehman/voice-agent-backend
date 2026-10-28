"""Native worker configuration; secrets are loaded only by explicit download."""

from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
LOCAL = BACKEND / ".local" / "voice"


FROZEN_VOICES = {
    "en": {"name": "af_heart", "seed": 42, "speed": 1},
    "ur": {
        "description": "A male speaker delivers clear Urdu speech at a moderate speed, close recording with no background noise.",
        "seed": 42,
        "do_sample": False,
        "max_new_tokens": 1000,
    },
}


@dataclass(frozen=True)
class Settings:
    inference_python: Path = BACKEND / "voice" / "inference" / ".venv" / "bin" / "python"
    cache_dir: Path = LOCAL / "huggingface"
    memory_bytes: int = 3584 * 1024**2
    timeout_seconds: float = 180
    threads: int = 2

    def __post_init__(self):
        if not self.cache_dir.resolve().is_relative_to((BACKEND / ".local").resolve()):
            raise ValueError("model cache must remain under backend/.local")
        if self.memory_bytes <= 0 or self.timeout_seconds <= 0 or not 1 <= self.threads <= 4:
            raise ValueError("invalid resource limits")

    def environment(self):
        import os

        # Whitelist process essentials; URLs and arbitrary provider variables can
        # contain secrets even when their key has no TOKEN/PASSWORD suffix.
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env["PYTHONPATH"] = str(BACKEND / "voice" / "src")
        env.update(
            HF_HOME=str(self.cache_dir),
            XDG_CACHE_HOME=str(LOCAL / "cache"),
            TORCH_HOME=str(LOCAL / "torch"),
            MPLCONFIGDIR=str(LOCAL / "matplotlib"),
            NUMBA_CACHE_DIR=str(LOCAL / "numba"),
            PYTHONNOUSERSITE="1",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            OMP_NUM_THREADS=str(self.threads),
            MKL_NUM_THREADS=str(self.threads),
            HF_HUB_DISABLE_PROGRESS_BARS="1",
            TOKENIZERS_PARALLELISM="false",
        )
        return env


def require_headroom(engine: str, *, available_bytes: int):
    """Conservative FP32 preflight; RSS supervision still applies during the job."""
    from voice_runtime.speech.process import JobFailure

    # Parler has 937,803,241 parameters: 3.49GiB FP32 weights before activations.
    minimum = {"parler": 4608, "kokoro": 1536, "whisper": 1536}[engine] * 1024**2
    if available_bytes < minimum:
        raise JobFailure(f"insufficient physical memory headroom for {engine}")
