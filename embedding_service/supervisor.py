"""Bounded child lifecycle. Watchdog runs independently of request handling."""

import json
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from embedding_service.settings import (
    AVAILABLE_FLOOR,
    INFERENCE_SECONDS,
    RSS_LIMIT,
    STARTUP_SECONDS,
)
from knowledge.embedding import EmbeddingUnavailable, validate_texts, validate_vectors


def available_memory():
    available = psutil.virtual_memory().available
    # Host memory alone is misleading inside a constrained Linux container.
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        used = int(Path("/sys/fs/cgroup/memory.current").read_text())
        if limit != "max":
            available = min(available, int(limit) - used)
    except (OSError, ValueError):
        pass
    return available


class Supervisor:
    def __init__(self, assets: Path):
        self.assets = assets
        self.process: subprocess.Popen[bytes] | None = None
        self.ready = False
        self.peak_rss = 0
        self.reason = "not_started"
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(5)  # one active + four queued
        self._stop = threading.Event()
        self._deadline = float("inf")
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        self._watchdog.start()

    def _kill(self, reason):
        self.ready = False
        self.reason = reason
        process = self.process
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    def _watch(self):
        while not self._stop.wait(0.025):
            process = self.process
            if process is None:
                continue
            try:
                if process.poll() is not None:
                    self.ready = False
                    continue
                rss = psutil.Process(process.pid).memory_info().rss
                self.peak_rss = max(self.peak_rss, rss)
                if rss > RSS_LIMIT:
                    self._kill("rss_limit")
                elif time.monotonic() >= self._deadline:
                    self._kill("deadline")
            except (psutil.Error, ProcessLookupError):
                self.ready = False

    def _receive(self, deadline):
        process = self.process
        if process is None or process.stdout is None:
            raise EmbeddingUnavailable("not_ready")
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise EmbeddingUnavailable(
                        self.reason if self.reason != "ready" else "child_exit"
                    )
                if selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                    line = process.stdout.readline(65537)
                    if len(line) > 65536 or not line:
                        raise EmbeddingUnavailable("child_protocol")
                    try:
                        return json.loads(line)
                    except ValueError:
                        raise EmbeddingUnavailable("child_protocol") from None
        self._kill("deadline")
        raise EmbeddingUnavailable("deadline")

    def _start(self, deadline):
        if available_memory() < AVAILABLE_FLOOR:
            self.reason = "memory_floor"
            raise EmbeddingUnavailable("memory_floor")
        if self.process is not None:
            self._kill("restart")
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
        self._deadline = min(deadline, time.monotonic() + STARTUP_SECONDS)
        # Never forward database, provider, raw-document or owner dotenv credentials.
        env = {
            "PATH": os.defpath,
            "PYTHONUNBUFFERED": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "OMP_NUM_THREADS": "2",
            "OPENBLAS_NUM_THREADS": "1",
        }
        self.process = subprocess.Popen(
            [sys.executable, "-m", "embedding_service.child", str(self.assets)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        result = self._receive(self._deadline)
        if result != {"ready": True}:
            self._kill("startup_failed")
            raise EmbeddingUnavailable("startup_failed")
        self.ready = True
        self.reason = "ready"
        self._deadline = float("inf")

    def start(self):
        with self._lock:
            self._start(time.monotonic() + STARTUP_SECONDS)

    def embed(self, texts, *, deadline):
        validate_texts(texts)
        deadline = min(deadline, time.monotonic() + INFERENCE_SECONDS)
        if not self._slots.acquire(blocking=False):
            raise EmbeddingUnavailable("queue_full")
        acquired = False
        try:
            acquired = self._lock.acquire(timeout=max(0, deadline - time.monotonic()))
            if not acquired or time.monotonic() >= deadline:
                raise EmbeddingUnavailable("deadline")
            if not self.ready or self.process is None or self.process.poll() is not None:
                raise EmbeddingUnavailable("not_ready")
            self._deadline = deadline
            assert self.process.stdin is not None
            self.process.stdin.write(json.dumps(texts).encode() + b"\n")
            self.process.stdin.flush()
            result = self._receive(deadline)
            if "error" in result:
                raise EmbeddingUnavailable("model_rejected")
            return validate_vectors(result["vectors"])
        except (BrokenPipeError, OSError, KeyError, TypeError):
            self._kill("child_failure")
            raise EmbeddingUnavailable("child_failure") from None
        finally:
            if acquired:
                self._deadline = float("inf")
                self._lock.release()
            self._slots.release()

    def close(self):
        self._stop.set()
        self._kill("closed")
        self._watchdog.join(timeout=2)
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
