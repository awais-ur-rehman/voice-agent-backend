"""Synthetic blocked I/O only: no external network or model assets accessed."""

import io
import json
import os
import time

import psutil
import pytest

from knowledge.embedding import EmbeddingUnavailable
from scripts import prepare_knowledge_assets as bootstrap_module

ORIGINAL_DOWNLOAD = bootstrap_module._download_asset


def mark_started(stage, phase):
    (stage.parent.parent / "started.json").write_text(
        json.dumps({"pid": os.getpid(), "phase": phase})
    )


def slow_open_download(stage, artifact):
    def slow_open(*args, **kwargs):
        mark_started(stage, "open")
        time.sleep(3)
        return io.BytesIO(b"x")

    bootstrap_module.urllib.request.urlopen = slow_open
    ORIGINAL_DOWNLOAD(stage, artifact)


def slow_stream_download(stage, artifact):
    class SlowResponse(io.BytesIO):
        def read(self, size):
            mark_started(stage, "read")
            time.sleep(3)
            return super().read(size)

    bootstrap_module.urllib.request.urlopen = lambda *a, **kw: SlowResponse(b"x")
    ORIGINAL_DOWNLOAD(stage, artifact)


@pytest.mark.parametrize(
    "download,phase", [(slow_open_download, "open"), (slow_stream_download, "read")]
)
def test_bootstrap_absolute_deadline_kills_blocked_io_and_cleans_stage(
    tmp_path, monkeypatch, download, phase
):
    monkeypatch.setattr(bootstrap_module, "DOWNLOAD_SECONDS", 0.8)
    monkeypatch.setattr(bootstrap_module, "_download_asset", download)
    # The synchronous RED implementation patches urllib in this process; restore it too.
    monkeypatch.setattr(
        bootstrap_module.urllib.request, "urlopen", bootstrap_module.urllib.request.urlopen
    )
    root = tmp_path / "model"
    started = time.monotonic()
    with pytest.raises(EmbeddingUnavailable) as error:
        bootstrap_module.bootstrap(root)
    elapsed = time.monotonic() - started
    assert elapsed < 1.8, f"blocked I/O exceeded deadline: {elapsed}"
    assert str(error.value) == "download_deadline"
    evidence = json.loads((tmp_path / "started.json").read_text())
    assert evidence["phase"] == phase
    assert not psutil.pid_exists(evidence["pid"]), "download worker survived deadline"
    assert not root.exists()
    assert not list(tmp_path.glob("bge-stage-*")), "partial stage survived deadline"


SYNTHETIC_BYTES = b"synthetic verified asset"


def synthetic_download(stage, artifact):
    mark_started(stage, "synthetic")
    bootstrap_module.urllib.request.urlopen = lambda *a, **kw: io.BytesIO(SYNTHETIC_BYTES)
    ORIGINAL_DOWNLOAD(stage, artifact)


@pytest.mark.parametrize("valid", [True, False])
def test_bootstrap_isolated_worker_adopts_only_verified_bytes(tmp_path, monkeypatch, valid):
    import hashlib

    import embedding_service.model as model_module
    from knowledge.profile import Artifact, EmbeddingProfile

    digest = hashlib.sha256(SYNTHETIC_BYTES).hexdigest() if valid else "0" * 64
    profile = EmbeddingProfile(
        "synthetic", "unused", "unused", (Artifact("asset", len(SYNTHETIC_BYTES), digest),)
    )
    monkeypatch.setattr(bootstrap_module, "PROFILE", profile)
    monkeypatch.setattr(model_module, "PROFILE", profile)
    monkeypatch.setattr(bootstrap_module, "_download_asset", synthetic_download)
    root = tmp_path / "model"
    if valid:
        bootstrap_module.bootstrap(root)
        assert (root / "asset").read_bytes() == SYNTHETIC_BYTES
    else:
        with pytest.raises(EmbeddingUnavailable, match="asset_download_failed"):
            bootstrap_module.bootstrap(root)
        assert not root.exists()
    assert not list(tmp_path.glob("bge-stage-*"))
    assert not psutil.pid_exists(json.loads((tmp_path / "started.json").read_text())["pid"])


def test_bootstrap_interruption_reaps_download_before_staging_cleanup(tmp_path, monkeypatch):
    from types import SimpleNamespace

    real_context = bootstrap_module.multiprocessing.get_context("spawn")
    monkeypatch.setattr(bootstrap_module, "_download_asset", slow_open_download)

    class InterruptOnJoin:
        def __init__(self, **kwargs):
            self.process = real_context.Process(**kwargs)
            self.interrupted = False

        def __getattr__(self, name):
            return getattr(self.process, name)

        def join(self, timeout=None):
            if not self.interrupted:
                self.interrupted = True
                deadline = time.monotonic() + 1
                while not (tmp_path / "started.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert (tmp_path / "started.json").exists()
                raise KeyboardInterrupt()
            return self.process.join(timeout)

    monkeypatch.setattr(
        bootstrap_module.multiprocessing,
        "get_context",
        lambda method: SimpleNamespace(Process=InterruptOnJoin),
    )
    with pytest.raises(KeyboardInterrupt):
        bootstrap_module.bootstrap(tmp_path / "model")
    assert not psutil.pid_exists(json.loads((tmp_path / "started.json").read_text())["pid"])
    assert not (tmp_path / "model").exists()
    assert not list(tmp_path.glob("bge-stage-*"))
