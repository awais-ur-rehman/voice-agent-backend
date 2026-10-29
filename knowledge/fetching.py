"""Single public HTTPS document, pinned transport, bounded DNS/wire/decoded body."""

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import zlib
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import parse_qsl, urljoin, urlsplit

from .parsing import MAX_BYTES, IngestionError

_DNS_SLOT = threading.BoundedSemaphore(1)
_FETCH_SLOT = threading.BoundedSemaphore(1)


def _resolve(host):
    return list({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def _dns(resolve, host, remaining):
    if not _DNS_SLOT.acquire(blocking=False):
        raise IngestionError("url_dns_busy")
    result: Future[list[str]] = Future()

    def run():
        try:
            result.set_result(resolve(host))
        except Exception:
            result.set_exception(IngestionError("url_destination"))
        finally:
            _DNS_SLOT.release()

    threading.Thread(target=run, daemon=True).start()
    try:
        return result.result(timeout=remaining)
    except FutureTimeout:
        raise IngestionError("url_timeout") from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, ip, *, timeout):
        self._context: ssl.SSLContext = ssl.create_default_context()
        super().__init__(hostname, port=443, timeout=timeout, context=self._context)
        self.ip = ip

    def connect(self):
        # No hostname resolution occurs here; TLS still verifies the original hostname.
        raw = socket.create_connection((self.ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class _Response:
    def __init__(self, connection, response, deadline, *, sock=None):
        self.connection, self.response, self.deadline = connection, response, deadline
        self.sock = sock if sock is not None else connection.sock
        self.status = response.status
        self.headers = dict((k.lower(), v) for k, v in response.getheaders())

    def read(self, size):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise IngestionError("url_timeout")
        # Fixed-length read1 closes fp at exactly zero; a closed response is normal EOF.
        # A nonzero remaining length is instead a truncated document, never usable input.
        if self.response.isclosed():
            if self.response.length not in (None, 0):
                raise IngestionError("url_response")
            return b""
        if self.sock is not None:
            self.sock.settimeout(remaining)
        block = self.response.read1(size)
        if not block and self.response.length not in (None, 0):
            raise IngestionError("url_response")
        return block

    def close(self):
        self.response.close()
        self.connection.close()


def _transport(*, hostname, ip, path, deadline):
    conn = PinnedHTTPSConnection(hostname, ip, timeout=max(0.001, deadline - time.monotonic()))
    try:
        conn.request(
            "GET",
            path,
            headers={
                "Host": f"[{hostname}]" if ":" in hostname else hostname,
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Accept": "text/html,text/plain,application/pdf",
                "User-Agent": "ClinicKnowledge/1",
            },
        )
        # getresponse may detach conn.sock for Connection: close; retain the actual TLS socket.
        sock = conn.sock
        return _Response(conn, conn.getresponse(), deadline, sock=sock)
    except Exception:
        conn.close()
        raise


def _target(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            len(url) > 2048
            or re.search(r"[\x00-\x20\x7f\\]", url)
            or parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            if re.search(
                r"token|key|password|secret|auth|signature|credential|session|^sig$", key, re.I
            ):
                raise ValueError
        host = host.encode("idna").decode("ascii")
        return host, (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    except (ValueError, UnicodeError):
        raise IngestionError("unsafe_url") from None


def _fetch_url(url, *, resolve, transport, clock, timeout):
    deadline = clock() + timeout

    def remaining():
        value = deadline - clock()
        if value <= 0:
            raise IngestionError("url_timeout")
        return value

    try:
        for hop in range(4):
            host, path = _target(url)
            addresses = _dns(resolve, host, remaining())
            remaining()
            if not addresses:
                raise IngestionError("url_destination")
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if (
                    not ip.is_global
                    or ip.is_multicast
                    or ip.is_reserved
                    or getattr(ip, "ipv4_mapped", None)
                    or (
                        ip.version == 6
                        and any(
                            ip in ipaddress.ip_network(prefix)
                            for prefix in (
                                "64:ff9b::/96",
                                "64:ff9b:1::/48",
                                "2002::/16",
                                "2001::/32",
                            )
                        )
                    )
                    or getattr(ip, "is_site_local", False)
                    or "%" in address
                ):
                    raise IngestionError("url_destination")
            response = transport(hostname=host, ip=addresses[0], path=path, deadline=deadline)
            try:
                remaining()
                headers = {k.lower(): v for k, v in response.headers.items()}
                if response.status in {301, 302, 303, 307, 308}:
                    if hop == 3:
                        raise IngestionError("url_redirects")
                    if not headers.get("location"):
                        raise IngestionError("unsafe_url")
                    url = urljoin(url, headers["location"])
                    continue
                if response.status != 200:
                    raise IngestionError("url_response")
                media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                format = {
                    "text/html": "html",
                    "text/plain": "txt",
                    "text/markdown": "md",
                    "application/pdf": "pdf",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                }.get(media)
                if format is None:
                    raise IngestionError("url_format")
                if int(headers.get("content-length", 0)) > MAX_BYTES:
                    raise IngestionError("url_too_large")
                encoding = headers.get("content-encoding", "identity").lower()
                if encoding not in {"identity", "gzip"}:
                    raise IngestionError("url_encoding")
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
                body, wire = bytearray(), 0
                while True:
                    remaining()
                    block = response.read(65536)
                    remaining()
                    if not block:
                        break
                    wire += len(block)
                    if wire > MAX_BYTES:
                        raise IngestionError("url_too_large")
                    decoded = (
                        decoder.decompress(block, MAX_BYTES - len(body) + 1) if decoder else block
                    )
                    body.extend(decoded)
                    if len(body) > MAX_BYTES or (decoder and decoder.unconsumed_tail):
                        raise IngestionError("url_too_large")
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise IngestionError("url_encoding")
                return bytes(body), format
            finally:
                response.close()
    except (TimeoutError, socket.timeout):
        raise IngestionError("url_timeout") from None
    except (OSError, ValueError, http.client.HTTPException, zlib.error):
        raise IngestionError("url_unavailable") from None


def fetch_url(url, *, resolve=_resolve, transport=_transport, clock=time.monotonic, timeout=30.0):
    """Absolute caller bound includes blocked DNS/TLS/headers and slow transports.

    A timed-out operation retains the single slot until its socket timeout exits;
    repeated calls cannot accumulate unbounded background threads or connections.
    """
    timeout = min(30.0, timeout)
    if timeout <= 0:
        raise IngestionError("url_timeout")
    if not _FETCH_SLOT.acquire(blocking=False):
        raise IngestionError("url_busy")
    result: Future[tuple[bytes, str]] = Future()

    def run():
        try:
            result.set_result(
                _fetch_url(url, resolve=resolve, transport=transport, clock=clock, timeout=timeout)
            )
        except Exception as exc:
            result.set_exception(exc)
        finally:
            _FETCH_SLOT.release()

    threading.Thread(target=run, daemon=True).start()
    try:
        return result.result(timeout=timeout)
    except FutureTimeout:
        raise IngestionError("url_timeout") from None
