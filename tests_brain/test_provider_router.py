"""Real facade with fake HTTP and barriers; no provider, dotenv or model assets."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest
from pydantic import ValidationError

from analytics.telemetry import MeasurementSink, measurement_scope
from brain.provider import Nemotron, ProviderError
from scheduler.config import Settings


def settings(**changes):
    return Settings(
        **(
            {
                "database_url": "postgresql://unused",
                "jwt_secret": "x" * 40,
                "llm_api_key": "synthetic-primary",
                "llm_secondary_api_key": "synthetic-secondary",
                "llm_secondary_base_url": "https://secondary.invalid/v1",
                "llm_secondary_model": "synthetic/secondary",
                "_env_file": None,
            }
            | changes
        )
    )


def answer(content='{"name":"ask_caller","arguments":{}}'):
    return httpx2.Response(
        200,
        json={
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            "choices": [{"message": {"content": content}}],
        },
    )


def plan(provider):
    return provider.plan({"task": "book", "fields": {}}, "hello", {}, {})


def test_primary_429_falls_back_once_with_distinct_usage():
    calls, values = [], []

    def serve(request):
        calls.append(request.url.host)
        return httpx2.Response(429) if len(calls) == 1 else answer()

    provider = Nemotron(settings(), transport=httpx2.MockTransport(serve))
    sink = MeasurementSink(writer=values.append)
    with measurement_scope(sink):
        assert plan(provider).name == "ask_caller"
        assert sink.requests == 2 and len(sink.stages) == 1
    assert provider.drain(1)
    done = [v for v in values if v.status != "started"]
    assert calls == ["integrate.api.nvidia.com", "secondary.invalid"]
    assert [v.route for v in done] == ["primary", "secondary"]
    assert [v.provider for v in done] == ["nim", "openai_compatible"]
    assert done[0].invocation_id != done[1].invocation_id
    assert done[1].fallback_from == done[0].invocation_id
    assert done[1].input_tokens == 7 and done[1].cost is None
    assert "synthetic-primary" not in repr(values)
    provider.close()


@pytest.mark.parametrize(
    "status,content",
    [
        (401, None),
        (403, None),
        (400, None),
        (200, "not json"),
        (200, '{"name":"book_appointment","arguments":{}}'),
    ],
)
def test_auth_and_invalid_action_never_fall_back(status, content):
    calls = []

    def serve(request):
        calls.append(request.url.host)
        return answer(content) if content else httpx2.Response(status)

    provider = Nemotron(settings(), transport=httpx2.MockTransport(serve))
    with pytest.raises(ProviderError) as failure:
        plan(provider)
    assert failure.value.requests == 1 and len(calls) == 1
    assert provider.drain(1)
    assert provider.router.snapshot("primary")["failures"] == 0
    provider.close()


def test_shared_endpoint_key_bucket_and_total_budget():
    calls = []
    provider = Nemotron(
        settings(
            llm_secondary_base_url="https://integrate.api.nvidia.com/v1",
            llm_secondary_api_key="synthetic-primary",
        ),
        transport=httpx2.MockTransport(lambda r: (calls.append(r), httpx2.Response(429))[1]),
    )
    with pytest.raises(ProviderError):
        plan(provider)
    assert len(calls) == 1  # different models cannot double the endpoint/key budget
    provider.close()
    provider = Nemotron(
        settings(llm_request_budget=1),
        transport=httpx2.MockTransport(lambda r: (calls.append(r), httpx2.Response(503))[1]),
    )
    with pytest.raises(ProviderError):
        plan(provider)
    assert len(calls) == 2
    provider.close()


def test_rolling_breaker_single_half_open_probe():
    clock = [0.0]
    entered, release = threading.Event(), threading.Event()
    calls = []

    def serve(request):
        calls.append(request)
        if len(calls) == 6:
            entered.set()
            assert release.wait(2)
            return answer()
        return httpx2.Response(503)

    provider = Nemotron(
        settings(
            llm_secondary_api_key=None,
            llm_secondary_base_url=None,
            llm_secondary_model=None,
            llm_endpoint_burst=20,
        ),
        transport=httpx2.MockTransport(serve),
        clock=lambda: clock[0],
    )
    for _ in range(5):
        with pytest.raises(ProviderError):
            plan(provider)
    assert provider.router.snapshot("primary")["open_until"] == 60
    clock[0] = 59.999
    with pytest.raises(ProviderError):
        plan(provider)
    clock[0] = 60
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(plan, provider)
        assert entered.wait(1)
        with pytest.raises(ProviderError):
            plan(provider)
        release.set()
        assert result.result().name == "ask_caller"
    assert len(calls) == 6
    assert provider.router.snapshot("primary")["failures"] == 0
    provider.close()


def test_timeout_late_success_cannot_reset_circuit_or_replace_result():
    entered, release = threading.Event(), threading.Event()
    values, calls = [], []

    def serve(request):
        calls.append(request.url.host)
        if request.url.host == "integrate.api.nvidia.com":
            entered.set()
            assert release.wait(2)
        return answer()

    provider = Nemotron(settings(llm_timeout_seconds=0.03), transport=httpx2.MockTransport(serve))
    sink = MeasurementSink(writer=values.append)
    try:
        with measurement_scope(sink):
            assert plan(provider).name == "ask_caller"
        assert entered.is_set() and calls == ["integrate.api.nvidia.com", "secondary.invalid"]
        assert provider.router.snapshot("primary")["failures"] == 1
    finally:
        release.set()
    assert provider.drain(1)
    assert provider.router.snapshot("primary")["failures"] == 1
    assert len(sink.stages) == 1
    done = [v for v in values if v.status == "timeout"]
    assert len(done) == 1 and done[0].input_tokens == 7
    provider.close()


@pytest.mark.parametrize("blocked", ["transport", "ledger"])
def test_hung_transport_or_ledger_retains_bounded_slot(blocked):
    entered, release = threading.Event(), threading.Event()
    calls, values = [], []

    def serve(request):
        calls.append(request)
        if blocked == "transport":
            entered.set()
            assert release.wait(2)
        return answer()

    def write(value):
        if blocked == "ledger":
            entered.set()
            assert release.wait(2)
        values.append(value)

    provider = Nemotron(
        settings(llm_max_outstanding=1, llm_timeout_seconds=0.03, llm_endpoint_burst=20),
        transport=httpx2.MockTransport(serve),
    )
    sink = MeasurementSink(writer=write)
    started = time.monotonic()
    try:
        with measurement_scope(sink):
            if blocked == "transport":
                with pytest.raises(ProviderError):
                    plan(provider)
            else:
                assert plan(provider).name == "ask_caller"
                assert len(sink.stages) == 1
        assert entered.wait(1)
        assert time.monotonic() - started < 0.5
        assert not provider.drain(0.01)
        for _ in range(20):
            with pytest.raises(ProviderError):
                plan(provider)
        assert len(calls) == 1 and sink.requests == 1
        assert len([t for t in threading.enumerate() if t.name == "provider-attempt"]) == 1
        assert values == []  # bounded outage may have no durable observations at return
    finally:
        release.set()
    assert provider.drain(1)
    provider.close()


def test_ground_deadline_and_close_do_not_queue_new_http():
    calls = []
    provider = Nemotron(
        settings(), transport=httpx2.MockTransport(lambda r: (calls.append(r), answer())[1])
    )
    with pytest.raises(ProviderError):
        provider.ground("hello", "en", [], deadline=time.monotonic() - 1)
    provider.close()
    with pytest.raises(ProviderError):
        plan(provider)
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.invalid/v1",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?secret=x",
        "https://example.com/v1#token",
        "file:///tmp/model",
    ],
)
def test_provider_urls_reject_unsafe_authorities(url):
    with pytest.raises(ValidationError):
        settings(llm_base_url=url)


def test_secondary_requires_its_own_complete_configuration():
    with pytest.raises(ValidationError):
        settings(llm_secondary_api_key=None)


@pytest.mark.parametrize("stop", ["deadline", "close"])
def test_tracing_callback_cannot_dispatch_after_deadline_or_close(monkeypatch, stop):
    from contextlib import contextmanager

    import brain.provider_router as routing

    entered, release = threading.Event(), threading.Event()
    calls, values = [], []

    @contextmanager
    def delayed_span(*args):
        entered.set()
        assert release.wait(2)
        yield

    monkeypatch.setattr(routing, "safe_span", delayed_span)
    provider = Nemotron(
        settings(llm_secondary_api_key=None, llm_secondary_model=None, llm_secondary_base_url=None),
        transport=httpx2.MockTransport(lambda r: (calls.append(r), answer())[1]),
    )
    sink = MeasurementSink(writer=values.append)

    def run():
        with measurement_scope(sink):
            return provider.ground("hello", "en", [], deadline=time.monotonic() + 0.05)

    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(run)
            assert entered.wait(1)
            assert calls == []
            if stop == "close":
                provider.close()
                release.set()
            with pytest.raises(ProviderError):
                future.result(timeout=1)
    finally:
        release.set()
    assert provider.drain(1)
    assert calls == [] and values == [] and sink.requests == 0
    provider.close()


def test_equivalent_endpoint_urls_share_the_operator_bucket():
    calls = []
    provider = Nemotron(
        settings(
            llm_secondary_base_url="https://INTEGRATE.API.NVIDIA.COM:443/v1/",
            llm_secondary_api_key="synthetic-primary",
        ),
        transport=httpx2.MockTransport(lambda r: (calls.append(r), httpx2.Response(429))[1]),
    )
    with pytest.raises(ProviderError):
        plan(provider)
    assert len(calls) == 1
    provider.close()


def test_non_5xx_server_status_does_not_trigger_fallback():
    calls = []
    provider = Nemotron(
        settings(),
        transport=httpx2.MockTransport(lambda r: (calls.append(r), httpx2.Response(600))[1]),
    )
    with pytest.raises(ProviderError):
        plan(provider)
    assert len(calls) == 1
    provider.close()


def test_default_attempt_three_second_and_logical_six_second_wall_bounds():
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    calls = []

    def serve(request):
        index = 0 if request.url.host == "integrate.api.nvidia.com" else 1
        calls.append((index, time.monotonic(), request.extensions["timeout"]["read"]))
        entered[index].set()
        assert release.wait(8)
        return answer()

    provider = Nemotron(settings(), transport=httpx2.MockTransport(serve))
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError) as error:
            plan(provider)
        elapsed = time.monotonic() - started
        assert error.value.requests == 2
        assert entered[0].is_set() and entered[1].is_set()
        assert 2.8 <= calls[1][1] - calls[0][1] <= 3.4
        assert 5.8 <= elapsed <= 6.5
        assert all(0 < timeout <= 3 for _, _, timeout in calls)
        assert not provider.drain(0)
    finally:
        release.set()
    assert provider.drain(1)
    provider.close()


def test_ground_earlier_deadline_limits_both_routes():
    clock = [0.0]
    timeouts = []

    def serve(request):
        timeouts.append(request.extensions["timeout"]["read"])
        clock[0] += 0.75
        return httpx2.Response(503)

    provider = Nemotron(settings(), transport=httpx2.MockTransport(serve), clock=lambda: clock[0])
    with pytest.raises(ProviderError) as error:
        provider.ground("hi", "en", [], deadline=1)
    assert error.value.requests == 2
    assert timeouts == [1, 0.25]
    provider.close()


def test_refill_and_rolling_failure_boundaries_use_monotonic_clock():
    clock = [0.0]
    calls = []
    provider = Nemotron(
        settings(llm_secondary_api_key=None, llm_secondary_model=None, llm_secondary_base_url=None),
        transport=httpx2.MockTransport(lambda r: (calls.append(r), httpx2.Response(503))[1]),
        clock=lambda: clock[0],
    )
    for instant in (0, 5.999, 6, 12, 18, 30):
        clock[0] = instant
        with pytest.raises(ProviderError):
            plan(provider)
    assert len(calls) == 5
    assert provider.router.snapshot("primary")["failures"] == 4  # t=0 expired
    assert provider.router.snapshot("primary")["open_until"] == 0
    clock[0] = 36
    with pytest.raises(ProviderError):
        plan(provider)
    assert len(calls) == 6 and provider.router.snapshot("primary")["failures"] == 4
    provider.close()


def test_success_admitted_before_new_open_cannot_close_new_circuit():
    clock = [0.0]
    entered, release = threading.Event(), threading.Event()
    calls = []

    def serve(request):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
            return answer()
        return httpx2.Response(503)

    provider = Nemotron(
        settings(
            llm_secondary_api_key=None,
            llm_secondary_model=None,
            llm_secondary_base_url=None,
            llm_endpoint_burst=20,
        ),
        transport=httpx2.MockTransport(serve),
        clock=lambda: clock[0],
    )
    with ThreadPoolExecutor(1) as pool:
        original = pool.submit(plan, provider)
        try:
            assert entered.wait(1)
            for _ in range(5):
                with pytest.raises(ProviderError):
                    plan(provider)
            assert provider.router.snapshot("primary")["open_until"] == 60
        finally:
            release.set()
        assert original.result().name == "ask_caller"
    assert provider.router.snapshot("primary")["open_until"] == 60
    assert provider.router.snapshot("primary")["failures"] == 5
    provider.close()


@pytest.mark.parametrize("kind", ["transport", "timeout", "503"])
def test_retryable_failure_uses_exactly_one_secondary(kind):
    calls = []

    def serve(request):
        calls.append(request)
        if kind == "transport":
            raise httpx2.ConnectError("synthetic-private-secret")
        if kind == "timeout":
            raise httpx2.ReadTimeout("synthetic-private-secret")
        return httpx2.Response(503, json={"error": "synthetic-private-secret"})

    provider = Nemotron(settings(), transport=httpx2.MockTransport(serve))
    with pytest.raises(ProviderError) as error:
        plan(provider)
    assert len(calls) == 2 and error.value.requests == 2
    assert "synthetic-private-secret" not in str(error.value)
    assert "synthetic-private-secret" not in str(provider.last_failure)
    provider.close()


def test_primary_unconfigured_or_throttled_has_no_fabricated_invocation():
    clock = [0.0]
    values = []
    provider = Nemotron(
        settings(llm_api_key=None),
        transport=httpx2.MockTransport(lambda r: answer()),
        clock=lambda: clock[0],
    )
    sink = MeasurementSink(writer=values.append)
    with measurement_scope(sink):
        assert plan(provider).name == "ask_caller"
        with pytest.raises(ProviderError) as error:
            plan(provider)
        assert error.value.requests == 0
    assert provider.drain(1)
    assert sink.requests == 1 and len(values) == 2
    assert values[0].route == "secondary" and values[0].fallback_from is None
    provider.close()


@pytest.mark.parametrize("limit", [0, 21])
def test_outstanding_limit_rejects_unbounded_configuration(limit):
    with pytest.raises(ValidationError):
        settings(llm_max_outstanding=limit)


def test_close_returns_without_waiting_for_transport_teardown():
    entered, release = threading.Event(), threading.Event()

    class Transport(httpx2.MockTransport):
        def close(self):
            entered.set()
            assert release.wait(2)

    provider = Nemotron(settings(), transport=Transport(lambda r: answer()))
    started = time.monotonic()
    try:
        provider.close()
        assert entered.wait(1)
        assert time.monotonic() - started < 0.5
        assert not provider.drain(0.01)
        with pytest.raises(ProviderError):
            plan(provider)
    finally:
        release.set()
    assert provider.drain(1)


@pytest.mark.parametrize("field", ["llm_base_url", "llm_secondary_base_url"])
def test_rejected_provider_url_diagnostics_hide_short_credentials(field):
    with pytest.raises(ValidationError) as error:
        settings(**{field: "https://u:LEAK@example.com/v1"})
    assert "LEAK" not in str(error.value)
    assert "https://u:" not in str(error.value)
    assert "Provider base URL must be HTTPS" in str(error.value)
