import gzip
import io

import pytest


class Response:
    def __init__(self, body=b"<p>Clinic hours</p>", *, status=200, headers=None):
        self.status = status
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.body = io.BytesIO(body)

    def read(self, size):
        return self.body.read(size)

    def close(self):
        pass


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.targets = []

    def __call__(self, **target):
        self.targets.append(target)
        return self.responses.pop(0)


def test_public_ip_is_pinned_and_original_hostname_preserved():
    from knowledge.fetching import fetch_url

    transport = Transport(Response())
    calls = []

    def resolve(host):
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

    result = fetch_url("https://example.com/hours", resolve=resolve, transport=transport)
    assert result == (b"<p>Clinic hours</p>", "html")
    assert calls == ["example.com"]
    assert transport.targets[0]["ip"] == "93.184.216.34"
    assert transport.targets[0]["hostname"] == "example.com"
    assert transport.targets[0]["path"] == "/hours"


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["169.254.169.254"],
        ["::1"],
        ["fe80::1"],
        ["::ffff:127.0.0.1"],
        ["::ffff:93.184.216.34"],
        ["64:ff9b::7f00:1"],
        ["fec0::1"],
        ["2002:7f00:1::"],
        ["93.184.216.34", "192.168.1.1"],
        ["2001:db8::1"],
        [],
    ],
)
def test_nonpublic_or_mixed_answers_never_connect(addresses):
    from knowledge.fetching import fetch_url
    from knowledge.parsing import IngestionError

    transport = Transport()
    with pytest.raises(IngestionError):
        fetch_url("https://example.com", resolve=lambda _: addresses, transport=transport)
    assert transport.targets == []


def test_public_ipv6_and_redirect_revalidation():
    from knowledge.fetching import fetch_url
    from knowledge.parsing import IngestionError

    transport = Transport(Response(status=302, headers={"location": "https://private.example/x"}))
    with pytest.raises(IngestionError, match="url_destination"):
        fetch_url(
            "https://example.com",
            resolve=lambda h: ["2606:4700:4700::1111"] if h == "example.com" else ["10.0.0.1"],
            transport=transport,
        )
    assert len(transport.targets) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com/?access_token=private",
        "https://example.com/?api%5Fkey=x",
        "https://example.com/#secret",
        "https://example.com:8443",
        "https://example.com/\r\ninject",
    ],
)
def test_secret_and_invalid_urls_fail_without_resolution(url):
    from knowledge.fetching import fetch_url
    from knowledge.parsing import IngestionError

    with pytest.raises(IngestionError, match="unsafe_url"):
        fetch_url(url, resolve=lambda _: pytest.fail("unsafe DNS"), transport=Transport())


def test_redirect_limit_and_rebinding_on_same_host():
    from knowledge.fetching import fetch_url
    from knowledge.parsing import IngestionError

    transport = Transport(*[Response(status=302, headers={"location": "/again"}) for _ in range(4)])
    with pytest.raises(IngestionError, match="url_redirects"):
        fetch_url("https://example.com", resolve=lambda _: ["93.184.216.34"], transport=transport)
    assert len(transport.targets) == 4
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    transport = Transport(Response(status=302, headers={"location": "/again"}))
    with pytest.raises(IngestionError, match="url_destination"):
        fetch_url("https://example.com", resolve=lambda _: next(answers), transport=transport)
    assert len(transport.targets) == 1


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"x" * (10 * 1024 * 1024 + 1), {"content-type": "text/plain"}),
        (
            gzip.compress(b"x" * (10 * 1024 * 1024 + 1)),
            {"content-type": "text/plain", "content-encoding": "gzip"},
        ),
        (b"x", {"content-type": "text/plain", "content-encoding": "br"}),
    ],
)
def test_wire_and_decompression_limits(body, headers):
    from knowledge.fetching import fetch_url
    from knowledge.parsing import IngestionError

    with pytest.raises(IngestionError):
        fetch_url(
            "https://example.com",
            resolve=lambda _: ["93.184.216.34"],
            transport=Transport(Response(body, headers=headers)),
        )


def test_total_timeout_covers_stream_and_dns():
    from knowledge.fetching import fetch_url
    from knowledge.parsing import IngestionError

    now = [0.0]

    class Slow(Response):
        def read(self, size):
            now[0] += 16
            return b"x"

    with pytest.raises(IngestionError, match="url_timeout"):
        fetch_url(
            "https://example.com",
            resolve=lambda _: ["93.184.216.34"],
            transport=Transport(Slow()),
            clock=lambda: now[0],
        )

    def slow_dns(_):
        now[0] += 31
        return ["93.184.216.34"]

    with pytest.raises(IngestionError, match="url_timeout"):
        fetch_url(
            "https://example.com", resolve=slow_dns, transport=Transport(), clock=lambda: now[0]
        )


def test_tls_verification_sni_and_host_use_original_hostname(monkeypatch):
    from knowledge.fetching import PinnedHTTPSConnection

    calls = []

    class Context:
        def wrap_socket(self, sock, *, server_hostname):
            calls.append(("tls", server_hostname))
            return sock

    import socket

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, timeout, source_address=None: calls.append(("tcp", address)) or object(),
    )
    conn = PinnedHTTPSConnection("example.com", "93.184.216.34", timeout=3)
    assert conn._context.check_hostname is True
    assert conn._context.verify_mode == 2
    conn._context = Context()
    conn.connect()
    assert calls == [("tcp", ("93.184.216.34", 443)), ("tls", "example.com")]
    assert conn.host == "example.com"


def test_blocked_transport_cannot_escape_absolute_deadline():
    import threading
    import time

    from knowledge.fetching import fetch_url
    from knowledge.parsing import IngestionError

    release = threading.Event()
    entered = threading.Event()
    workers = []

    def blocked(**_):
        workers.append(threading.current_thread())
        entered.set()
        release.wait(2)
        return Response()

    started = time.monotonic()
    try:
        with pytest.raises(IngestionError, match="url_timeout"):
            fetch_url(
                "https://example.com",
                resolve=lambda _: ["93.184.216.34"],
                transport=blocked,
                timeout=0.03,
            )
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        # A timed-out fetch deliberately owns the shared slot until its worker
        # exits. Do not let the next test race that worker's finally cleanup.
        assert entered.wait(1)
        workers[0].join(timeout=1)
        assert not workers[0].is_alive()


def test_transport_host_header_uses_original_authority(monkeypatch):
    from knowledge import fetching

    calls = {}

    class Connection:
        sock = None

        def __init__(self, hostname, ip, *, timeout):
            calls["host"] = hostname
            calls["ip"] = ip

        def request(self, method, path, headers):
            calls.update(headers=headers, path=path)

        def getresponse(self):
            class Wire:
                status = 200

                def getheaders(self):
                    return [("Content-Type", "text/plain")]

                def close(self):
                    pass

            return Wire()

        def close(self):
            pass

    monkeypatch.setattr(fetching, "PinnedHTTPSConnection", Connection)
    fetching._transport(
        hostname="example.com", ip="93.184.216.34", path="/hours", deadline=10
    ).close()
    assert calls["headers"]["Host"] == "example.com" and calls["ip"] == "93.184.216.34"


@pytest.mark.parametrize(
    "framing,body,expected",
    [
        (b"Content-Length: 5\r\n", b"hello", b"hello"),
        (b"Content-Length: 0\r\n", b"", b""),
        (b"Transfer-Encoding: chunked\r\n", b"2\r\nhe\r\n3\r\nllo\r\n0\r\n\r\n", b"hello"),
    ],
    ids=["fixed-length", "zero-length", "chunked"],
)
def test_real_http_response_completion(framing, body, expected):
    import http.client
    import socket
    import time

    from knowledge.fetching import _Response, fetch_url

    reader, writer = socket.socketpair()

    class Connection:
        sock = reader

        def close(self):
            reader.close()

    try:
        writer.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n" + framing + b"\r\n" + body
        )
        writer.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(reader)
        response.begin()
        result = fetch_url(
            "https://example.com/hours",
            resolve=lambda _: ["93.184.216.34"],
            transport=lambda **_: _Response(Connection(), response, time.monotonic() + 10),
        )
        assert result == (expected, "txt")
        assert response.isclosed()
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize(
    "framing,body",
    [
        (b"Content-Length: 5\r\n", b"hel"),
        (b"Content-Length: 5\r\n", b""),
        (b"Transfer-Encoding: chunked\r\n", b"5\r\nhel"),
        (b"Transfer-Encoding: chunked\r\n", b"5\r\nhello\r\n"),
    ],
    ids=["short-fixed", "empty-fixed", "short-chunk", "missing-final-chunk"],
)
def test_real_http_response_rejects_truncated_document(framing, body):
    import http.client
    import socket
    import time

    from knowledge.fetching import _Response, fetch_url
    from knowledge.parsing import IngestionError

    reader, writer = socket.socketpair()

    class Connection:
        sock = reader

        def close(self):
            reader.close()

    try:
        writer.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n" + framing + b"\r\n" + body
        )
        writer.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(reader)
        response.begin()
        with pytest.raises(IngestionError):
            fetch_url(
                "https://example.com/hours",
                resolve=lambda _: ["93.184.216.34"],
                transport=lambda **_: _Response(Connection(), response, time.monotonic() + 10),
            )
    finally:
        reader.close()
        writer.close()


def test_real_http_connection_close_detaches_socket_before_body_completion(monkeypatch):
    import http.client
    import socket

    from knowledge import fetching

    reader, writer = socket.socketpair()

    class Connection(http.client.HTTPConnection):
        def getresponse(self):
            response = super().getresponse()
            assert self.sock is None, "real getresponse must detach the Connection: close socket"
            return response

    connection = Connection("example.com")
    connection.sock = reader
    monkeypatch.setattr(fetching, "PinnedHTTPSConnection", lambda *a, **k: connection)
    try:
        writer.sendall(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello"
        )
        writer.shutdown(socket.SHUT_WR)
        assert fetching.fetch_url("https://example.com", resolve=lambda _: ["93.184.216.34"]) == (
            b"hello",
            "txt",
        )
    finally:
        reader.close()
        writer.close()
