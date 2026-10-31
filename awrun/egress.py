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

Credential injection (``--bindings FILE``)
------------------------------------------
The sandbox holds a per-run PLACEHOLDER, never the key. A binding maps that
placeholder to a vault secret NAME and the domains the key may reach. On a
request to a bound domain the proxy resolves the name from the vault at request
time, server-side, and substitutes it -- in a header value, the request target,
or (``header`` mode) as a header the proxy sets itself. The value is never
logged, never written to disk, and never sent to any other host: a request that
carries a placeholder to a host the binding does not name is refused.

TLS limitation, stated plainly: a ``CONNECT`` tunnel is opaque bytes, so the
proxy cannot see or rewrite anything inside it. Injection therefore works only
when the client sends the request to the proxy in ABSOLUTE FORM
(``GET http://api.example.com/v1/... HTTP/1.1``). A binding with
``upstream_tls: true`` makes the proxy originate TLS to the real host (port 443,
certificate verified), so the sandbox speaks plain HTTP on its internal network
and the key travels encrypted on the wire outside it. A ``CONNECT`` to a bound
host is REFUSED with a message saying so, rather than silently forwarding an
un-credentialed (or placeholder-carrying) TLS stream.

Not covered: placeholders inside a request BODY are not substituted (bodies are
streamed and length-framed); a keep-alive client's second request on the same
connection is not re-inspected, so injected requests are sent with
``Connection: close``; a host that echoes the key back in its response is
outside the proxy's control.
"""

from __future__ import annotations

import json
import logging
import re
import secrets as _secrets
import select
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence
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


# ── credential injection ─────────────────────────────────────────────────────

_REF_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_MIN_PLACEHOLDER = 16
_HOP_HEADERS = frozenset({"proxy-authorization", "proxy-connection", "connection",
                          "keep-alive"})


class CredentialUnavailableError(Exception):
    """The vault could not produce a value for a bound secret ref."""


Resolver = Callable[[str], str]


@dataclass(frozen=True)
class CredentialBinding:
    """placeholder (what the sandbox holds) -> vault NAME, for these domains only."""

    placeholder: str
    secret_ref: str
    domains: tuple
    header: str = ""             # explicit mode: the proxy sets this header itself
    template: str = "{secret}"   # e.g. "Bearer {secret}"; literal replace, not format()
    upstream_tls: bool = False   # proxy originates TLS to the real host

    def validate(self) -> None:
        if len(self.placeholder) < _MIN_PLACEHOLDER or any(
                c in self.placeholder for c in "\r\n :"):
            raise ValueError(f"binding for {self.secret_ref!r}: placeholder must be "
                             f">= {_MIN_PLACEHOLDER} chars with no space, colon or newline")
        if not _REF_RE.match(self.secret_ref or ""):
            raise ValueError("binding secret_ref must be a vault NAME "
                             "([A-Za-z0-9_.:/-], <=128 chars)")
        if not self.domains:
            raise ValueError(f"binding for {self.secret_ref!r} names no domain: "
                             f"a credential with nowhere to go is refused")
        if self.header and not _HEADER_RE.match(self.header):
            raise ValueError(f"binding for {self.secret_ref!r}: bad header name")
        if "{secret}" not in self.template:
            raise ValueError(f"binding for {self.secret_ref!r}: template lacks {{secret}}")

    def matches(self, host: str, port: int) -> bool:
        return allowed(host, port, self.domains)

    def render(self, value: str) -> str:
        return self.template.replace("{secret}", value)


def make_placeholder(name: str = "cred") -> str:
    """A per-run placeholder: unguessable, so it never collides with real text."""
    slug = re.sub(r"[^A-Za-z0-9]", "", name)[:24] or "cred"
    return f"aw-cred-{slug}-{_secrets.token_hex(16)}"


def binding_from_dict(d: dict) -> CredentialBinding:
    if not isinstance(d, dict):
        raise ValueError("each binding must be an object")
    for forbidden in ("value", "secret", "secret_value"):
        if forbidden in d:
            raise ValueError(f"binding carries {forbidden!r}: bindings hold vault "
                             f"NAMES only, never values")
    domains = d.get("domains") or []
    if isinstance(domains, str):
        domains = [domains]
    b = CredentialBinding(
        placeholder=str(d.get("placeholder") or ""),
        secret_ref=str(d.get("secret_ref") or d.get("ref") or ""),
        domains=tuple(str(x).strip().lower() for x in domains if str(x).strip()),
        header=str(d.get("header") or ""),
        template=str(d.get("template") or "{secret}"),
        upstream_tls=bool(d.get("upstream_tls", False)),
    )
    b.validate()
    return b


def load_bindings(path: str) -> list:
    """Read a per-run bindings JSON file: a list of binding objects (names, not values)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("bindings", [])
    if not isinstance(data, list):
        raise ValueError('bindings file must hold a list (or {"bindings": [...]})')
    out = [binding_from_dict(d) for d in data]
    seen: set = set()
    for b in out:
        if b.placeholder in seen:
            raise ValueError("two bindings share one placeholder")
        seen.add(b.placeholder)
    return out


def vault_resolver(ref: str) -> str:
    """Resolve a vault NAME through the existing AitherSecrets client, server-side.

    No environment fallback: a vault miss is LOUD (the request gets 502), never a
    silent substitute value.
    """
    try:
        from lib.clients.aither_secrets import get_secret  # type: ignore[import-not-found]
    except Exception as exc:   # noqa: BLE001 -- any import failure means no vault here
        raise CredentialUnavailableError(
            f"no vault client importable ({type(exc).__name__})") from None
    try:
        value = get_secret(ref, None, use_env_fallback=False)
    except Exception as exc:   # noqa: BLE001
        raise CredentialUnavailableError(f"vault lookup failed ({type(exc).__name__})") from None
    if not value:
        raise CredentialUnavailableError("vault has no value for this ref")
    return str(value)


def _rewrite_request(method: str, url, header_lines: Sequence[str],
                     subs: dict, set_headers: dict, host_header: str) -> bytes:
    """Origin-form request line + substituted headers; hop-by-hop headers dropped."""
    target = url.path or "/"
    if url.query:
        target += "?" + url.query
    for ph, val in subs.items():
        target = target.replace(ph, val)
    out = [f"{method} {target} HTTP/1.1"]
    lowered_set = {k.lower() for k in set_headers}
    saw_host = False
    for line in header_lines:
        name, sep, value = line.partition(":")
        if not sep:
            continue
        lname = name.strip().lower()
        if lname in _HOP_HEADERS or lname in lowered_set:
            continue
        if lname == "host":
            saw_host = True
        for ph, val in subs.items():
            value = value.replace(ph, val)
        out.append(f"{name.strip()}:{value}")
    if not saw_host:
        out.append(f"Host: {host_header}")
    for name, val in set_headers.items():
        out.append(f"{name}: {val}")
    out.append("Connection: close")
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")


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
        bindings = self.server.bindings                  # type: ignore[attr-defined]
        if bindings:
            if method.upper() != "CONNECT":
                return self._inject_and_forward(method, raw, target, host, port)
            if any(b.matches(host, port) for b in bindings):
                return self._refuse(
                    403, "Forbidden", f"{host}:{port} (a credential is bound to this "
                    f"host and a CONNECT tunnel is opaque: send the request in absolute "
                    f"form, http://{host}/..., on a binding with upstream_tls)")
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


    def _inject_and_forward(self, method: str, raw: bytes, target: str,
                            host: str, port: int) -> None:
        """Absolute-form request on a proxy that holds credential bindings."""
        server = self.server
        bindings = server.bindings                       # type: ignore[attr-defined]
        url = urlsplit(target)
        tls = url.port is None and any(b.upstream_tls and b.matches(host, 443)
                                       for b in bindings)
        if tls:
            port = 443
        # A placeholder may only ever travel to a host its binding names. Checked on
        # the whole buffered request (head and any body bytes already read).
        present = [b for b in bindings if b.placeholder.encode("latin-1") in raw]
        for b in present:
            if not b.matches(host, port):
                return self._refuse(403, "Forbidden",
                                    f"{host}:{port} (carries the placeholder for "
                                    f"{b.secret_ref}, which is not bound to this host)")
        if not allowed(host, port, server.allow):        # type: ignore[attr-defined]
            return self._refuse(403, "Forbidden", f"{host}:{port}")

        use = [b for b in bindings if b.matches(host, port) and (b in present or b.header)]
        subs: dict = {}
        set_headers: dict = {}
        try:
            for b in use:
                value = server.resolver(b.secret_ref)    # type: ignore[attr-defined]
                if not value or any(c in value for c in "\r\n\x00"):
                    raise CredentialUnavailableError("value unusable in a header")
                if b in present:
                    subs[b.placeholder] = value
                if b.header:
                    set_headers[b.header] = b.render(value)
        except CredentialUnavailableError as exc:
            return self._refuse(502, "Bad Gateway",
                                f"{host}:{port} (credential unavailable: {exc})")
        head_part, _sep, body = raw.partition(b"\r\n\r\n")
        header_lines = head_part.decode("latin-1").split("\r\n")[1:]
        host_header = host if port in (80, 443) else f"{host}:{port}"
        head = _rewrite_request(method, url, header_lines, subs, set_headers, host_header)
        try:
            upstream = socket.create_connection((host, port), timeout=20.0)
            if tls:
                upstream = server.upstream_ssl.wrap_socket(  # type: ignore[attr-defined]
                    upstream, server_hostname=host)
        except (OSError, ssl.SSLError) as exc:
            return self._refuse(502, "Bad Gateway",
                                f"{host}:{port} ({type(exc).__name__})")
        refs = tuple(sorted(b.secret_ref for b in use))
        logger.info("egress allowed %s -> %s:%s%s; credential refs injected: %s",
                    self.client_address[0], host, port, " (tls)" if tls else "",
                    ", ".join(refs) or "none")
        server.injected.append((host, refs))             # type: ignore[attr-defined]
        client: socket.socket = self.request
        with upstream:
            try:
                upstream.sendall(head + body)
            except OSError:
                return
            client.settimeout(None)
            upstream.settimeout(None)
            _relay(client, upstream)


class EgressProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], allow: Iterable[str],
                 bindings: Optional[Sequence[CredentialBinding]] = None,
                 resolver: Optional[Resolver] = None,
                 upstream_ca: Optional[str] = None) -> None:
        self.allow = [a for a in (x.strip().lower() for x in allow) if a]
        self.denied: list[str] = []
        self.bindings = list(bindings or [])
        for b in self.bindings:
            b.validate()
        self.resolver: Resolver = resolver or vault_resolver
        self.injected: list = []
        self.upstream_ssl = ssl.create_default_context(cafile=upstream_ca)
        super().__init__(address, _Handler)


def serve(host: str, port: int, allow: Iterable[str],
          bindings_path: Optional[str] = None, upstream_ca: Optional[str] = None) -> int:
    bindings = load_bindings(bindings_path) if bindings_path else []
    proxy = EgressProxy((host, port), allow, bindings=bindings, upstream_ca=upstream_ca)
    bound = proxy.server_address
    refs = sorted(b.secret_ref for b in bindings)
    print(f"awrun egress-proxy on {bound[0]}:{bound[1]} allowing "
          f"{proxy.allow or '(nothing)'}"
          + (f"; credential refs {refs}" if refs else ""), flush=True)
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

    # Credential injection: an upstream that echoes the request head it received.
    class _Mirror(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.request.sendall(data)

    real = "s3lf-test-" + _secrets.token_hex(8)
    ph = make_placeholder("selftest")
    mirror = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Mirror)
    mirror.daemon_threads = True
    m_port = mirror.server_address[1]
    binding = CredentialBinding(placeholder=ph, secret_ref="SELFTEST_KEY",
                                domains=(f"127.0.0.1:{m_port}",))
    cproxy = EgressProxy(("127.0.0.1", 0), [f"127.0.0.1:{m_port}"], bindings=[binding],
                         resolver=lambda ref: real)
    for server in (mirror, cproxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def ask(url: str, auth: str) -> bytes:
        with socket.create_connection(cproxy.server_address, timeout=5) as s:
            s.sendall(f"GET {url} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {auth}"
                      f"\r\n\r\n".encode())
            s.settimeout(5)
            data = b""
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                return data
            return data

    try:
        got = ask(f"http://127.0.0.1:{m_port}/v1", ph)
        check("a placeholder to its bound host arrives as the real key",
              f"Bearer {real}".encode() in got and ph.encode() not in got)
        bad = ask(f"http://127.0.0.1:{m_port + 1}/v1", ph)
        check("a placeholder to an unbound host is refused, key never sent",
              bad.startswith(b"HTTP/1.1 403") and real.encode() not in bad)
    finally:
        for server in (mirror, cproxy):
            server.shutdown()
            server.server_close()
    print("EGRESS SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
