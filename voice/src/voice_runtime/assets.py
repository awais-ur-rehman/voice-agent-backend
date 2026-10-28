"""Explicit pinned artifact download; no implicit network in speech adapters."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from voice_runtime.config import BACKEND, LOCAL, Settings


@dataclass(frozen=True)
class Artifact:
    repo: str
    revision: str
    patterns: tuple[str, ...]


ARTIFACTS = {
    "description": Artifact(
        "google/flan-t5-large",
        "0613663d0d48ea86ba8cb3d7a44f0f65dc596a2a",
        (
            "tokenizer*",
            "spiece.model",
            "special_tokens_map.json",
            "config.json",
            "LICENSE*",
            "README.md",
        ),
    ),
    "whisper": Artifact(
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
        ("*.json", "*.bin", "*.txt", "LICENSE*", "README.md"),
    ),
    "kokoro": Artifact(
        "hexgrad/Kokoro-82M",
        "f3ff3571791e39611d31c381e3a41a3af07b4987",
        ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt", "LICENSE*", "README.md"),
    ),
    "parler": Artifact(
        "ai4bharat/indic-parler-tts",
        "660bc53c64ae83150613264111708a0465211237",
        ("*.json", "*.safetensors", "*.model", "*.txt", "LICENSE*", "README.md"),
    ),
}


def snapshot(name: str, settings: Settings | None = None) -> Path:
    settings = settings or Settings()
    pin = ARTIFACTS[name]
    path = (
        settings.cache_dir
        / "hub"
        / ("models--" + pin.repo.replace("/", "--"))
        / "snapshots"
        / pin.revision
    )
    if not path.is_dir() or not (LOCAL / f"{name}-manifest.json").is_file():
        raise FileNotFoundError(f"{name} assets unavailable; explicitly run download")
    manifest = json.loads((LOCAL / f"{name}-manifest.json").read_text())
    if (
        manifest.get("repo") != pin.repo
        or manifest.get("revision") != pin.revision
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        raise ValueError("artifact manifest does not match pin")
    for record in manifest["files"]:
        relative = Path(record["file"])
        file = path / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not file.resolve().is_relative_to(settings.cache_dir.resolve())
            or not file.is_file()
            or file.stat().st_size != record["bytes"]
        ):
            raise ValueError("artifact manifest is incomplete")
    return path


def download(name: str):
    import os

    os.environ["HF_HOME"] = str(Settings().cache_dir)
    os.environ["HF_XET_CACHE"] = str(Settings().cache_dir / "xet")
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from dotenv import dotenv_values
    from huggingface_hub import snapshot_download

    token = dotenv_values(BACKEND / ".env").get("HF_TOKEN")
    if not token:
        raise ValueError("HF_TOKEN is missing from backend/.env")
    pin = ARTIFACTS[name]
    settings = Settings()
    path = Path(
        snapshot_download(
            pin.repo,
            revision=pin.revision,
            token=token,
            cache_dir=settings.cache_dir / "hub",
            allow_patterns=list(pin.patterns),
            max_workers=1,
        )
    )
    records = []
    for file in sorted(path.rglob("*")):
        if file.is_file():
            with file.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            records.append(
                {
                    "file": str(file.relative_to(path)),
                    "sha256": digest,
                    "bytes": file.stat().st_size,
                }
            )
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / f"{name}-manifest.json").write_text(
        json.dumps({"repo": pin.repo, "revision": pin.revision, "files": records}, indent=2)
    )
    return {
        "artifact": name,
        "revision": pin.revision,
        "files": len(records),
        "bytes": sum(item["bytes"] for item in records),
    }
