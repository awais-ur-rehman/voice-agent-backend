"""Small private RPC client; safe to import into API processes."""

import json
import math
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

import httpx2

from knowledge.profile import PROFILE


class EmbeddingUnavailable(RuntimeError):
    """Bounded safe reason; never include provider response bodies."""


def validate_texts(texts):
    if (
        not isinstance(texts, list)
        or len(texts) != 1
        or not isinstance(texts[0], str)
        or not texts[0].strip()
        or len(texts[0]) > 32768
    ):
        raise EmbeddingUnavailable("invalid_input")


def validate_vectors(vectors, count=1):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise EmbeddingUnavailable("invalid_vectors")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != 1024:
            raise EmbeddingUnavailable("invalid_vectors")
        if any(
            type(x) not in (float, int) or abs(x) > 1.0001 or not math.isfinite(x) for x in vector
        ):
            raise EmbeddingUnavailable("invalid_vectors")
        if abs(math.sqrt(sum(x * x for x in vector)) - 1.0) > 1e-4:
            raise EmbeddingUnavailable("invalid_vectors")
    return vectors


class EmbeddingClient:
    def __init__(self, url: str, secret: str, *, transport=None):
        if len(secret) < 32:
            raise ValueError("embedding secret required")
        self.url = url.rstrip("/") + "/internal/embed"
        self.secret = secret
        self.transport = transport
        self._slots = threading.BoundedSemaphore(5)

    def embed(self, texts: list[str], *, deadline: float) -> list[list[float]]:
        validate_texts(texts)
        deadline = min(deadline, time.monotonic() + 10.0)
        remaining = min(10.0, deadline - time.monotonic())
        if remaining <= 0:
            raise EmbeddingUnavailable("deadline")
        if not self._slots.acquire(blocking=False):
            raise EmbeddingUnavailable("queue_full")
        result: Future[list[list[float]]] = Future()

        def request():
            try:
                result.set_result(self._request(texts, deadline, remaining))
            except Exception as exc:
                result.set_exception(exc)
            finally:
                self._slots.release()

        threading.Thread(target=request, daemon=True).start()
        try:
            return result.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeout:
            raise EmbeddingUnavailable("deadline") from None

    def _request(self, texts, deadline, remaining):
        try:
            with httpx2.Client(
                transport=self.transport, trust_env=False, timeout=remaining
            ) as client:
                with client.stream(
                    "POST",
                    self.url,
                    headers={"Authorization": f"Bearer {self.secret}"},
                    json={"texts": texts, "profile": PROFILE.fingerprint},
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        if time.monotonic() >= deadline:
                            raise EmbeddingUnavailable("deadline")
                        if len(body) + len(chunk) > 65536:
                            raise EmbeddingUnavailable("invalid_response")
                        body.extend(chunk)
                    data = json.loads(body)
            if time.monotonic() >= deadline:
                raise EmbeddingUnavailable("deadline")
            if set(data) != {"profile", "vectors"} or data["profile"] != PROFILE.fingerprint:
                raise EmbeddingUnavailable("profile_mismatch")
            return validate_vectors(data["vectors"], len(texts))
        except (httpx2.HTTPError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            raise EmbeddingUnavailable("rpc_unavailable") from None
