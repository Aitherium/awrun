"""An allowlisting forward proxy: the only door out of a confined run.

`awrun egress-proxy --allow pypi.org --allow '*.githubusercontent.com'` listens
for HTTP ``CONNECT`` (every HTTPS client) and plain absolute-URI requests, and
opens an upstream connection ONLY when the target host is on the list. Anything
else gets ``403`` and is logged -- a denied destination is the interesting
event, so it is never silent.

Matching is on the NAME the client asked for, before any DNS lookup: an entry
is ``host``, ``*.suffix`` (subdomains only, not the bare suffix) or either with
``:port``. With no port an entry allows 80 and 443 and nothing else, so listing
a host does not open its admin port. An IP literal matches only if that literal
is listed.

This is a door, not a wall. It confines nothing by itself -- a process that
ignores ``HTTPS_PROXY`` walks past it. Put the run on a network with no other
route out (see `awrun.confine`) and this becomes the only way to reach anything.
"""

from __future__ import annotations

import logging
import select
import socket
import socketserver
import threading
from typing import Iterable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("awrun.egress")

_DEFAULT_PORTS = frozenset({80, 443})
_BUFFER = 65536
_IDLE_S = 300.0


def parse_target(target: str, default_port: int) -> Optional[tuple[str, int]]:
    """`host[:port]` -> (host, port); None when it is not one."""
    target = (target or "").strip().lower()
    if not target or "/" in target or "@" in target:
        return None
    host, sep, port = target.rpartition(":")
    if not sep:
        return target.rstrip("."), default_port
    if not port.isdigit() or not 0 < int(port) < 65536 or not host:
        return None
    return host.rstrip("."), int(port)


def allowed(host: str, port: int, allow: Iterable[str]) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for entry in allow:
        entry = entry.strip().lower()
        name, sep, want_port = entry.rpartition(":")
        if sep and want_port.isdigit():
            if int(want_port) != port:
                continue
        else:
            name = entry
            if port not in _DEFAULT_PORTS:
                continue
        if name.startswith("*."):
            if host.endswith(name[1:]) and host != name[2:]:
                return True
        elif host == name:
            return True
    return False


def _relay(a: socket.socket, b: socket.socket) -> None:
    pair = {a: b, b: a}
    while True:
        ready, _w, _x = select.select([a, b], [], [], _IDLE_S)
        if not ready:
            return
        for sock in ready:
            try:
                chunk = sock.recv(_BUFFER)
            except OSError:
                return
            if not chunk:
                return
            try:
                pair[sock].sendall(chunk)
            except OSError:
                return


class _Handler(socketserver.BaseRequestHandler):
    def _refuse(self, code: int, reason: str, target: str) -> None:
        logger.warning("egress DENIED %s -> %s (%s)", self.client_address[0], target, reason)
        self.server.denied.append(target)          # type: ignore[attr-defined]
        body = f"awrun egress: {reason}\n".encode()
        head = (f"HTTP/1.1 {code} {reason}\r\nContent-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n").encode()
        try:
            self.request.sendall(head + body)
        except OSError:
            return

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(30.0)
        raw = b""
        try:
            while b"\r\n\r\n" not in raw and len(raw) < _BUFFER:
                chunk = client.recv(_BUFFER)
                if not chunk:
                    return
                raw += chunk
        except OSError:
            return
        line = raw.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) != 3:
            return self._refuse(400, "Bad Request", line[:80])
        method, target, _version = parts

        if method.upper() == "CONNECT":
            where = parse_target(target, 443)
        else:
            url = urlsplit(target)
            if url.scheme != "http" or not url.hostname:
                return self._refuse(400, "Bad Request", target[:80])
            where = (url.hostname.lower(), url.port or 80)
        if where is None:
            return self._refuse(400, "Bad Request", target[:80])
        host, port = where
        if not allowed(host, port, self.server.allow):   # type: ignore[attr-defined]
            return self._refuse(403, "Forbidden", f"{host}:{port}")

        try:
            upstream = socket.create_connection((host, port), timeout=20.0)
        except OSError as exc:
            return self._refuse(502, "Bad Gateway", f"{host}:{port} ({exc})")
        logger.info("egress allowed %s -> %s:%s", self.client_address[0], host, port)
        with upstream:
            try:
                if method.upper() == "CONNECT":
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    rest = raw.split(b"\r\n\r\n", 1)[1]
                    if rest:
                        upstream.sendall(rest)
                else:
                    upstream.sendall(raw)
            except OSError:
                return
            client.settimeout(None)
            upstream.settimeout(None)
            _relay(client, upstream)


class EgressProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], allow: Iterable[str]) -> None:
        self.allow = [a for a in (x.strip().lower() for x in allow) if a]
        self.denied: list[str] = []
        super().__init__(address, _Handler)


def serve(host: str, port: int, allow: Iterable[str]) -> int:
    proxy = EgressProxy((host, port), allow)
    bound = proxy.server_address
    print(f"awrun egress-proxy on {bound[0]}:{bound[1]} allowing "
          f"{proxy.allow or '(nothing)'}", flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        proxy.server_close()
    return 0


def self_test() -> int:
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    check("exact host on a default port", allowed("pypi.org", 443, ["pypi.org"]))
    check("a listed host does not open its other ports",
          not allowed("pypi.org", 8443, ["pypi.org"]))
    check("an explicit port allows exactly that port",
          allowed("db.internal", 5432, ["db.internal:5432"])
          and not allowed("db.internal", 443, ["db.internal:5432"]))
    check("a wildcard matches subdomains", allowed("a.b.example.com", 443, ["*.example.com"]))
    check("a wildcard does not match the bare suffix",
          not allowed("example.com", 443, ["*.example.com"]))
    check("a suffix look-alike is not a subdomain",
          not allowed("evilexample.com", 443, ["*.example.com"]))
    check("an empty allowlist allows nothing", not allowed("pypi.org", 443, []))
    check("an unlisted IP literal is refused", not allowed("169.254.169.254", 80, ["pypi.org"]))
    check("a target carrying a path or userinfo is not a host",
          parse_target("a@b:443", 443) is None and parse_target("a/b", 443) is None)

    # Real sockets on loopback: one upstream the proxy may reach, one it may not.
    class _Echo(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.sendall(b"HELLO")

    up = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
    up.daemon_threads = True
    up_port = up.server_address[1]
    proxy = EgressProxy(("127.0.0.1", 0), [f"127.0.0.1:{up_port}"])
    for server in (up, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def connect(target: str) -> bytes:
        with socket.create_connection(proxy.server_address, timeout=5) as s:
            s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            s.settimeout(5)
            data = b""
            try:
                while len(data) < 200:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if data.endswith(b"HELLO"):
                        break
            except OSError:
                return data
            return data

    try:
        good = connect(f"127.0.0.1:{up_port}")
        check("an allowed CONNECT is tunnelled end to end",
              b"200 Connection Established" in good and good.endswith(b"HELLO"))
        bad = connect(f"127.0.0.1:{up_port + 1}")
        check("a CONNECT to an unlisted port gets 403 and no tunnel",
              bad.startswith(b"HTTP/1.1 403") and b"HELLO" not in bad)
        check("the denial was recorded", proxy.denied == [f"127.0.0.1:{up_port + 1}"])
    finally:
        for server in (up, proxy):
            server.shutdown()
            server.server_close()
    print("EGRESS SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
