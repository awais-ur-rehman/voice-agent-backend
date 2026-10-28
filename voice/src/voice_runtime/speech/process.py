"""One isolated heavy process, bounded RSS, deadline and generation cancellation."""

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

import psutil


class JobFailure(RuntimeError):
    def __init__(self, reason, *, peak_rss_bytes=0, elapsed_seconds=0):
        super().__init__(reason)
        self.peak_rss_bytes = peak_rss_bytes
        self.elapsed_seconds = elapsed_seconds


@dataclass(frozen=True)
class JobResult:
    stdout: bytes
    elapsed_seconds: float
    peak_rss_bytes: int


class Supervisor:
    _heavy_slot = threading.Lock()

    def __init__(self, memory_bytes: int = 4 * 1024**3):
        if memory_bytes <= 0:
            raise ValueError("memory limit must be positive")
        self.memory_bytes = memory_bytes
        self.generation = 0
        self._lock = threading.Lock()
        self._process = None

    def cancel(self):
        with self._lock:
            self.generation += 1
            if self._process is not None:
                self._kill(self._process)

    @staticmethod
    def _kill(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def run(
        self,
        command: list[str],
        *,
        generation: int,
        timeout: float,
        input_bytes: bytes = b"",
        env: dict[str, str] | None = None,
    ) -> JobResult:
        if timeout <= 0:
            raise ValueError("deadline must be positive")
        started = time.monotonic()
        while not self._heavy_slot.acquire(timeout=0.02):
            if self.generation != generation:
                raise JobFailure("stale generation")
            if time.monotonic() - started >= timeout:
                raise JobFailure("deadline waiting for heavy slot")
        process = None
        try:
            with self._lock:
                if generation != self.generation:
                    raise JobFailure("stale generation")
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
                self._process = process
            # communicate in a helper thread to drain bounded child output without pipe deadlock.
            captured = []
            output_limit = threading.Event()

            def communicate():
                try:
                    process.stdin.write(input_bytes)
                    process.stdin.close()
                    data = process.stdout.read(8 * 1024**2 + 1)
                    if len(data) > 8 * 1024**2:
                        output_limit.set()
                    captured.append(data)
                except (BrokenPipeError, OSError):
                    captured.append(b"")

            io = threading.Thread(target=communicate, daemon=True)
            io.start()
            peak = 0
            reason = None
            while process.poll() is None:
                try:
                    child = psutil.Process(process.pid)
                    rss = child.memory_info().rss + sum(
                        p.memory_info().rss for p in child.children(recursive=True)
                    )
                    peak = max(peak, rss)
                except psutil.Error:
                    pass
                if output_limit.is_set():
                    reason = "output limit exceeded"
                elif generation != self.generation:
                    reason = "stale generation"
                elif time.monotonic() - started > timeout:
                    reason = "deadline exceeded"
                elif psutil.virtual_memory().available < 256 * 1024**2:
                    reason = "memory headroom exhausted"
                elif peak > self.memory_bytes:
                    reason = "memory limit exceeded"
                if reason:
                    self._kill(process)
                    break
                time.sleep(0.02)
            io.join(timeout=2)
            if reason:
                raise JobFailure(
                    reason, peak_rss_bytes=peak, elapsed_seconds=time.monotonic() - started
                )
            if output_limit.is_set():
                raise JobFailure("output limit exceeded", peak_rss_bytes=peak)
            if generation != self.generation:
                raise JobFailure("stale generation")
            if process.returncode:
                raise JobFailure(f"inference child failed ({process.returncode})")
            return JobResult(captured[0], time.monotonic() - started, peak)
        finally:
            if process is not None and process.poll() is None:
                self._kill(process)
            with self._lock:
                self._process = None
            self._heavy_slot.release()
