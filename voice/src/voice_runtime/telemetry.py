"""Bounded, best-effort native diagnostics. No audio/text or publication claims."""

import asyncio
import math
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import UTC, datetime

output_measurement = ContextVar("native_output_measurement", default=None)


class MetricQueue:
    def __init__(self, call):
        self.call = call
        self.pending = deque(maxlen=32)
        self.task = None
        self.sealed = False
        self._final_batch = None

    def offer(self, stage, duration, language, epoch, *, metadata=None, context=None):
        if (
            self.sealed
            or epoch != self.call.epoch
            or not math.isfinite(duration)
            or not 0 <= duration <= 600000
        ):
            return
        context = context or {}
        revision = context.get("revision", self.call.api.revision)
        if revision != self.call.api.revision:
            return
        value = {
            "event_id": str(uuid.uuid4()),
            "command_id": context.get("command_id"),
            "stage": stage,
            "language": language if language in {"en", "ur", "mixed", "unknown"} else "unknown",
            "duration_ms": duration,
            "observed_at": datetime.now(UTC).isoformat(),
            "output_kind": context.get("output_kind", "input"),
            "metadata": dict(metadata or {}),
        }
        self.pending.append((epoch, revision, value))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain())

    def _batch(self, records):
        epoch = self.call.epoch
        revision = self.call.api.revision
        observations = [value for e, r, value in records if e == epoch and r == revision]
        if not observations:
            return None
        return {
            "generation": self.call.generation,
            "revision": revision,
            "observations": observations,
        }

    def seal(self):
        """Freeze already accepted work before local epoch/revision fencing; admit no more."""
        if self.sealed:
            return
        self.sealed = True
        self._final_batch = self._batch(self.pending)
        self.pending.clear()
        if self._final_batch is not None and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._drain())

    async def _drain(self):
        while self.pending or self._final_batch is not None:
            if self._final_batch is not None:
                body = self._final_batch
                self._final_batch = None
            else:
                body = self._batch(self.pending)
                self.pending.clear()
            if body is None:
                continue
            try:
                await self.call.api.metrics(body)
            except Exception:
                # Stable IDs/payload are retried by WorkerAPI, then missing coverage.
                pass

    async def flush(self):
        if self.task is None or self.task.done() or self.sealed and self.task.cancelling():
            return
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 1.2)
        except TimeoutError:
            if self.sealed:
                # Shutdown never leaves a sender waiting indefinitely or sends
                # the remaining frozen work after server authority is revoked.
                self._final_batch = None
                self.pending.clear()
                self.task.cancel()
        except Exception:
            pass
