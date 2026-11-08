"""Per-application bounded routing. No queued HTTP work or SQL on the caller thread."""

import hashlib
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx2

from analytics.telemetry import current_sink
from analytics.tracing import safe_span


@dataclass(frozen=True)
class EndpointPolicy:
    route: str
    base_url: str
    model: str
    key: Any = field(repr=False)
    input_rate: Any = None
    output_rate: Any = None

    @property
    def identity(self):
        return str(httpx2.URL(self.base_url)).rstrip("/"), hashlib.sha256(
            self.key.get_secret_value().encode()
        ).digest()

    @property
    def provider(self):
        return "nim" if self.route == "primary" else "openai_compatible"


@dataclass
class Circuit:
    failures: deque = field(default_factory=deque)
    open_until: float = 0.0
    generation: int = 0
    probing: bool = False
    tokens: float = 0.0
    refilled: float = 0.0


@dataclass
class Outcome:
    value: Any = None
    error: str | None = None
    retry: bool = False
    status: str = "failure"


@dataclass
class Attempt:
    future: Future = field(default_factory=Future)
    invocation_id: UUID | None = None
    generation: int = 0
    terminal: bool = False


class ProviderRouter:
    """One facade per application process; budgets are not distributed account quotas.

    An admitted daemon worker owns its slot through HTTP, decode and persistence.
    Only the in-memory result is signalled early. No ThreadPoolExecutor queue, audit
    queue, timer thread or cancellation-based slot release exists.
    """

    def __init__(self, settings, client, *, clock=time.monotonic):
        self.settings = settings
        self.client = client
        self.clock = clock
        self.condition = threading.Condition()
        self.circuits: dict[tuple, Circuit] = {}
        self.active = 0
        self.requests = 0
        self.closed = False
        self.closing_client = False
        self.last_failure = None

    def endpoint(self, route):
        prefix = "llm_" if route == "primary" else "llm_secondary_"
        key = getattr(self.settings, prefix + "api_key")
        if key is None or not key.get_secret_value():
            return None
        return EndpointPolicy(
            route,
            getattr(self.settings, prefix + "base_url"),
            getattr(self.settings, prefix + "model"),
            key,
            getattr(self.settings, prefix + "input_rate_usd_per_million"),
            getattr(self.settings, prefix + "output_rate_usd_per_million"),
        )

    def _circuit(self, endpoint, now):
        identity = endpoint.identity
        if identity not in self.circuits:
            self.circuits[identity] = Circuit(tokens=self.settings.llm_endpoint_burst, refilled=now)
        circuit = self.circuits[identity]
        while circuit.failures and circuit.failures[0] <= now - 30:
            circuit.failures.popleft()
        return circuit

    def snapshot(self, route):
        with self.condition:
            endpoint = self.endpoint(route)
            circuit = self._circuit(endpoint, self.clock()) if endpoint else Circuit()
            return {
                "failures": len(circuit.failures),
                "open_until": circuit.open_until,
                "probing": circuit.probing,
                "active": self.active,
            }

    def _admit(self, endpoint, now):
        circuit = self._circuit(endpoint, now)
        if now < circuit.open_until or circuit.probing:
            return None
        circuit.tokens = min(
            self.settings.llm_endpoint_burst,
            circuit.tokens + max(0, now - circuit.refilled) * self.settings.llm_endpoint_rpm / 60,
        )
        circuit.refilled = now
        if circuit.tokens < 1:
            return None
        circuit.tokens -= 1
        if circuit.open_until:
            circuit.probing = True
        return circuit.generation

    def _resolve(self, endpoint, attempt, outcome):
        """Called under the condition; every attempt has exactly one authoritative outcome."""
        if attempt.terminal:
            return False
        attempt.terminal = True
        if attempt.invocation_id is not None:
            now = self.clock()
            circuit = self._circuit(endpoint, now)
            if attempt.generation == circuit.generation:
                if outcome.retry:
                    circuit.failures.append(now)
                    if circuit.probing or len(circuit.failures) >= 5:
                        circuit.open_until = now + 60
                        circuit.generation += 1
                        circuit.probing = False
                elif circuit.probing:
                    # A timely non-retryable response proves connectivity, not action validity.
                    circuit.open_until = 0
                    circuit.probing = False
                    circuit.failures.clear()
                    circuit.generation += 1
            if outcome.error:
                self.last_failure = outcome.error
        return True

    def invoke(self, payload, decode, *, deadline=None):
        logical_deadline = (
            min(self.clock() + 6, deadline) if deadline is not None else self.clock() + 6
        )
        sink = current_sink.get()
        fallback_from = None
        requests = 0
        for route in ("primary", "secondary"):
            endpoint = self.endpoint(route)
            if endpoint is None:
                continue
            until = min(
                logical_deadline, self.clock() + self.settings.llm_timeout_seconds, self.clock() + 3
            )
            attempt = Attempt()
            with self.condition:
                if self.closed or self.clock() >= until:
                    break
                if self.active >= self.settings.llm_max_outstanding:
                    break
                self.active += 1
            worker = threading.Thread(
                target=self._run,
                args=(endpoint, attempt, payload, decode, until, sink, fallback_from),
                daemon=True,
                name="provider-attempt",
            )
            try:
                worker.start()
            except RuntimeError:
                self._release()
                break
            try:
                outcome = attempt.future.result(timeout=max(0, until - self.clock()))
            except TimeoutError:
                with self.condition:
                    outcome = Outcome(error="deadline", retry=True, status="timeout")
                    if self._resolve(endpoint, attempt, outcome):
                        attempt.future.set_result(outcome)
                    else:
                        outcome = attempt.future.result()
            if attempt.invocation_id is not None:
                requests += 1
                fallback_from = attempt.invocation_id
            if outcome.error is None:
                return outcome.value
            if not outcome.retry:
                break
        # Import here to preserve the established public exception identity.
        from brain.provider import ProviderError

        raise ProviderError("provider_failed" if requests else "unavailable", requests=requests)

    def _run(self, endpoint, attempt, payload, decode, until, sink, fallback_from):
        try:
            # A slow tracer still owns the bounded slot; admission is rechecked
            # AFTER its entry callback, so it cannot dispatch after close/deadline.
            with safe_span("provider.invoke", sink.trace_id if sink else None):
                observations = self._perform(
                    endpoint, attempt, payload, decode, until, sink, fallback_from
                )
            if observations:
                for observation in observations:
                    sink.emit(observation)
        finally:
            self._release()

    def _perform(self, endpoint, attempt, payload, decode, until, sink, fallback_from):
        with self.condition:
            now = self.clock()
            generation = None
            if (
                not attempt.terminal
                and not self.closed
                and now < until
                and self.requests < self.settings.llm_request_budget
            ):
                generation = self._admit(endpoint, now)
            if generation is None:
                if not attempt.terminal:
                    attempt.terminal = True
                    attempt.future.set_result(Outcome(error="unavailable", retry=True))
                return
            attempt.generation = generation
            attempt.invocation_id = uuid4()
            self.requests += 1
            measurement = (
                sink.reserve(
                    endpoint.model,
                    endpoint.input_rate,
                    endpoint.output_rate,
                    invocation_id=attempt.invocation_id,
                    provider=endpoint.provider,
                    route=endpoint.route,
                    fallback_from=fallback_from,
                )
                if sink
                else None
            )
        # The dispatch above is the last admission boundary: no SQL/tracer callback
        # or other blocking operation can interpose before the HTTP call.
        started = time.monotonic()
        usage = None
        outcome = Outcome(error="provider_failed")
        response = None
        completed_monotonic = None
        completed_at = None
        try:
            response = self.client.post(
                endpoint.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + endpoint.key.get_secret_value()},
                timeout=max(0.001, until - self.clock()),
                json=payload | {"model": endpoint.model},
            )
            completed_monotonic = time.monotonic()
            completed_at = datetime.now(UTC)
            try:
                data = response.json()
                usage = data.get("usage") if isinstance(data, dict) else None
            except (ValueError, TypeError):
                data = None
            if response.is_success:
                outcome.status = "malformed"
                outcome.value = decode(data["choices"][0]["message"]["content"])
                outcome.error = None
                outcome.status = "success"
            else:
                outcome.retry = response.status_code == 429 or 500 <= response.status_code < 600
        except httpx2.TimeoutException:
            outcome = Outcome(error="deadline", retry=True, status="timeout")
        except httpx2.TransportError:
            outcome = Outcome(error="transport", retry=True)
        except Exception:
            # Never retain arbitrary exception text (including decoder/transport bodies).
            outcome.error = "provider_failed"
        with self.condition:
            if self.clock() >= until:
                outcome = Outcome(error="deadline", retry=True, status="timeout")
            authoritative = self._resolve(endpoint, attempt, outcome)
            if not authoritative:
                outcome = attempt.future.result()
            completed = (
                sink.complete(
                    measurement[0],
                    started,
                    outcome.status,
                    usage,
                    completed_monotonic=completed_monotonic,
                    completed_at=completed_at,
                    record_stage=authoritative,
                )
                if measurement
                else None
            )
            if authoritative:
                attempt.future.set_result(outcome)
        return (measurement[0], completed) if measurement else None

    def _release(self):
        close_client = False
        with self.condition:
            if self.closed and self.active == 1 and not self.closing_client:
                self.closing_client = True
                close_client = True
            else:
                self.active -= 1
                self.condition.notify_all()
        if close_client:
            try:
                self.client.close()
            finally:
                with self.condition:
                    self.active -= 1
                    self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            if self.active or self.closing_client:
                return
            self.active = 1
        # Client teardown itself may block in an injected/custom transport. It owns
        # the last slot and is included by drain, never joined by close.
        threading.Thread(target=self._release, daemon=True, name="provider-attempt").start()

    def drain(self, timeout: float) -> bool:
        if not 0 <= timeout <= 60:
            raise ValueError("drain timeout must be between 0 and 60 seconds")
        with self.condition:
            return self.condition.wait_for(lambda: self.active == 0, timeout=timeout)
