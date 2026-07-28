"""Robust HTTP GET: Happy Eyeballs dual-stack racing + keep-alive reuse.

Why this exists: some users have broken or slow IPv6. Python's stdlib connects
sequentially in getaddrinfo order (IPv6 first on a dual-stack host), so a dead
IPv6 address is *waited out* — often hundreds of milliseconds — before IPv4 is
tried. The panel opens a fresh connection per API call, so that penalty multiplies
across a search and makes the whole app feel slow on such networks.

This module (RFC 8305, "Happy Eyeballs") races the address families with a short
stagger and uses whichever connects first, so:

  * broken / slow IPv6  -> IPv4 wins the race at once (no long wait);
  * IPv6-only network   -> IPv4 has no address, IPv6 is used (nothing breaks);
  * IPv4 degraded        -> IPv6 wins the race (the fallback is preserved).

Connections are pooled per host and reused (keep-alive), so a multi-call search
pays the TCP + TLS handshake once instead of on every request. TLS still uses the
standard library's verifying default context, so certificate checking is intact.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import selectors
import socket
import threading
import time
from urllib.parse import urlsplit

# RFC 8305 "Connection Attempt Delay": how long one attempt gets a head start
# before the next address is raced. 150 ms sits inside the RFC's 100-250 ms
# guidance — long enough that a healthy first attempt usually wins on its own,
# short enough that a dead IPv6 address never stalls the connection.
_ATTEMPT_DELAY = 0.15
_DEFAULT_PORT = {"https": 443, "http": 80}
_MAX_IDLE_PER_HOST = 4
_DEFAULT_UA = "aoe4-replay-launcher"


def _ordered_addresses(host: str, port: int) -> list[tuple]:
    """``getaddrinfo`` results interleaved IPv4-first.

    IPv4-first means a dual-stack host whose IPv6 is broken connects on the very
    first attempt. Interleaving still queues an IPv6 attempt one stagger later, so
    an IPv6-only result set (no IPv4) or an IPv4-degraded host connects over IPv6.
    """
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    v4 = [info for info in infos if info[0] == socket.AF_INET]
    v6 = [info for info in infos if info[0] == socket.AF_INET6]
    ordered: list[tuple] = []
    while v4 or v6:
        if v4:
            ordered.append(v4.pop(0))
        if v6:
            ordered.append(v6.pop(0))
    return ordered


def happy_eyeballs_connect(host: str, port: int, timeout: float) -> socket.socket:
    """Connect to ``host:port`` racing address families; return the first socket
    that completes. Raises :class:`TimeoutError` if the overall timeout elapses,
    or :class:`OSError` if every candidate address fails. The returned socket is
    blocking with no per-operation timeout set (the caller sets one).
    """
    addresses = _ordered_addresses(host, port)
    if not addresses:
        raise OSError(f"could not resolve {host!r}")
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    pending: dict[socket.socket, tuple] = {}
    errors: list[OSError] = []
    index = 0
    next_start = 0.0  # monotonic time the next attempt may begin (0 => immediately)

    def _begin_next() -> None:
        nonlocal index
        family, socktype, proto, _canon, sockaddr = addresses[index]
        index += 1
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setblocking(False)
            sock.connect(sockaddr)
        except BlockingIOError:
            pass  # expected: the non-blocking connect is now in flight
        except OSError as exc:  # e.g. an immediately-unreachable address
            errors.append(exc)
            sock.close()
            return
        selector.register(sock, selectors.EVENT_WRITE)
        pending[sock] = sockaddr

    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(f"connecting to {host!r} timed out")
            # Start the next candidate as soon as its stagger slot arrives; the
            # first starts immediately. Attempts run concurrently and race.
            if index < len(addresses) and now >= next_start:
                _begin_next()
                next_start = now + _ATTEMPT_DELAY
            if not pending and index >= len(addresses):
                break  # every address has been tried and none is still in flight
            # Wake for whichever comes first: the next stagger slot or the deadline.
            wait = deadline - now
            if index < len(addresses):
                wait = min(wait, max(0.0, next_start - now))
            # On Windows a *failed* non-blocking connect is reported through the
            # exception set; selectors.SelectSelector folds that into EVENT_WRITE,
            # so both success and failure surface here and SO_ERROR tells them apart.
            for key, _mask in selector.select(max(0.0, wait)):
                sock = key.fileobj
                selector.unregister(sock)
                del pending[sock]
                err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err == 0:
                    sock.setblocking(True)
                    return sock  # winner; the finally clause closes the losers
                errors.append(OSError(err, os.strerror(err)))
                sock.close()
        raise errors[-1] if errors else OSError(f"could not connect to {host!r}")
    finally:
        for sock in pending:
            with contextlib.suppress(Exception):
                selector.unregister(sock)
            sock.close()
        selector.close()


class _HappyEyeballsHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection whose TCP connect uses Happy Eyeballs racing.

    Only ``connect`` is overridden; TLS (the stdlib's verifying default context),
    HTTP framing and keep-alive are inherited unchanged, so certificate
    verification and hostname checking stay in force.
    """

    def connect(self) -> None:
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 15.0
        sock = happy_eyeballs_connect(self.host, self.port, timeout)
        sock.settimeout(timeout)  # applies to the TLS handshake and later I/O
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _close(conn) -> None:
    with contextlib.suppress(Exception):
        conn.close()


class _Pool:
    """A small thread-safe pool of idle keep-alive connections, keyed by host.

    Each worker thread checks a connection out for its exclusive use and checks it
    back in when the response is fully read, so no connection is ever shared by two
    in-flight requests (``http.client`` connections are not re-entrant).
    """

    def __init__(self, max_idle: int = _MAX_IDLE_PER_HOST) -> None:
        self._idle: dict[tuple[str, int], list[http.client.HTTPSConnection]] = {}
        self._lock = threading.Lock()
        self._max_idle = max_idle

    def acquire(self, host: str, port: int, timeout: float) -> http.client.HTTPSConnection:
        with self._lock:
            bucket = self._idle.get((host, port))
            if bucket:
                return bucket.pop()
        return _HappyEyeballsHTTPSConnection(host, port, timeout=timeout)

    def release(self, host: str, port: int, conn: http.client.HTTPSConnection) -> None:
        with self._lock:
            bucket = self._idle.setdefault((host, port), [])
            if len(bucket) < self._max_idle:
                bucket.append(conn)
                return
        _close(conn)  # pool full: drop the surplus connection


_pool = _Pool()


def get(url: str, *, timeout: float = 15.0, headers: dict | None = None) -> tuple[int, bytes]:
    """GET ``url`` and return ``(status, body)``.

    Reuses a pooled keep-alive connection when one is available, and transparently
    retries once on a fresh connection if a pooled one had been closed by the
    server while idle. Raises on a genuine connect / transport failure.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    port = parts.port or _DEFAULT_PORT.get(parts.scheme, 443)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    request_headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept-Encoding": "identity",  # no gzip: the body is parsed as-is
        "Connection": "keep-alive",
    }
    if headers:
        request_headers.update(headers)

    last_exc: Exception | None = None
    for _attempt in range(2):
        conn = _pool.acquire(host, port, timeout)
        try:
            conn.request("GET", path, headers=request_headers)
            resp = conn.getresponse()
            body = resp.read()  # drain fully so the socket can be reused
            status = resp.status
            if resp.will_close:
                _close(conn)
            else:
                _pool.release(host, port, conn)
            return status, body
        except (OSError, http.client.HTTPException) as exc:
            # A connection taken from the pool may have been closed server-side
            # while idle, so the first write fails; retry once with a fresh one.
            _close(conn)
            last_exc = exc
    assert last_exc is not None  # the loop either returns or records an exception
    raise last_exc
