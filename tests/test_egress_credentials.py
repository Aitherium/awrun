"""Credential injection at the egress proxy: the key lives at the boundary only.

Real loopback sockets. The upstream mirrors the request head it received, so a
test can see exactly what crossed the boundary.
"""

import json
import logging
import socket
import socketserver
import threading

import pytest
from awrun import egress

REAL = "realvalue-test-0123456789abcdef"


class _Mirror(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            data += chunk
        self.server.seen.append(data)  # type: ignore[attr-defined]
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n" + data)


def _start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def mirrors():
    made = []

    def make():
        m = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Mirror)
        m.daemon_threads = True
        m.seen = []
        made.append(_start(m))
        return m

    yield make
    for m in made:
        m.shutdown()
        m.server_close()


@pytest.fixture
def proxy_factory():
    made = []

    def make(allow, bindings, resolver=lambda ref: REAL):
        p = egress.EgressProxy(("127.0.0.1", 0), allow, bindings=bindings,
                               resolver=resolver)
        made.append(_start(p))
        return p

    yield make
    for p in made:
        p.shutdown()
        p.server_close()


def _send(proxy, request: str) -> bytes:
    with socket.create_connection(proxy.server_address, timeout=5) as s:
        s.sendall(request.encode("latin-1"))
        s.settimeout(5)
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            return data
        return data


def _get(proxy, url, headers=""):
    return _send(proxy, f"GET {url} HTTP/1.1\r\nHost: x\r\n{headers}\r\n")


def _binding(port, ph=None, **kw):
    return egress.CredentialBinding(placeholder=ph or egress.make_placeholder("k"),
                                    secret_ref="OPENAI_API_KEY",
                                    domains=(f"127.0.0.1:{port}",), **kw)


def test_placeholder_is_swapped_for_the_real_value_on_the_bound_host(
        mirrors, proxy_factory):
    up = mirrors()
    port = up.server_address[1]
    b = _binding(port)
    proxy = proxy_factory([f"127.0.0.1:{port}"], [b])
    got = _get(proxy, f"http://127.0.0.1:{port}/v1?key={b.placeholder}",
               f"Authorization: Bearer {b.placeholder}\r\n")
    assert got.startswith(b"HTTP/1.1 200")
    seen = up.seen[0]
    assert f"Authorization: Bearer {REAL}".encode() in seen
    assert f"key={REAL}".encode() in seen
    assert b.placeholder.encode() not in seen
    assert b"Connection: close" in seen
    assert proxy.injected == [("127.0.0.1", ("OPENAI_API_KEY",))]


def test_placeholder_to_an_unbound_host_is_refused_and_never_dialled(
        mirrors, proxy_factory):
    bound, other = mirrors(), mirrors()
    bp, op = bound.server_address[1], other.server_address[1]
    b = _binding(bp)
    # the other host IS on the allowlist; only the credential is not bound to it
    proxy = proxy_factory([f"127.0.0.1:{bp}", f"127.0.0.1:{op}"], [b])
    got = _get(proxy, f"http://127.0.0.1:{op}/", f"Authorization: Bearer {b.placeholder}\r\n")
    assert got.startswith(b"HTTP/1.1 403")
    assert REAL.encode() not in got
    assert other.seen == []


def test_header_mode_injects_only_for_the_allowed_domain(mirrors, proxy_factory):
    bound, other = mirrors(), mirrors()
    bp, op = bound.server_address[1], other.server_address[1]
    b = _binding(bp, header="X-Api-Key", template="{secret}")
    proxy = proxy_factory([f"127.0.0.1:{bp}", f"127.0.0.1:{op}"], [b])
    _get(proxy, f"http://127.0.0.1:{bp}/", "X-Api-Key: forged\r\n")
    _get(proxy, f"http://127.0.0.1:{op}/")
    assert f"X-Api-Key: {REAL}".encode() in bound.seen[0]
    assert b"forged" not in bound.seen[0]          # the client cannot pre-set it
    assert len(other.seen) == 1
    assert b"X-Api-Key" not in other.seen[0]
    assert REAL.encode() not in other.seen[0]


def test_connect_to_a_bound_host_is_refused_with_the_reason(mirrors, proxy_factory):
    up = mirrors()
    port = up.server_address[1]
    proxy = proxy_factory([f"127.0.0.1:{port}"], [_binding(port)])
    got = _send(proxy, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: x\r\n\r\n")
    assert got.startswith(b"HTTP/1.1 403")
    assert "opaque" in proxy.denied[0]
    assert up.seen == []


def test_bindings_never_widen_the_allowlist(mirrors, proxy_factory):
    up = mirrors()
    port = up.server_address[1]
    b = _binding(port)
    proxy = proxy_factory([], [b])                 # bound, but not allowlisted
    got = _get(proxy, f"http://127.0.0.1:{port}/", f"Authorization: {b.placeholder}\r\n")
    assert got.startswith(b"HTTP/1.1 403")
    assert up.seen == []


def test_vault_miss_is_loud_and_sends_nothing(mirrors, proxy_factory):
    up = mirrors()
    port = up.server_address[1]
    b = _binding(port)

    def miss(ref):
        raise egress.CredentialUnavailableError("vault has no value for this ref")

    proxy = proxy_factory([f"127.0.0.1:{port}"], [b], resolver=miss)
    got = _get(proxy, f"http://127.0.0.1:{port}/", f"Authorization: {b.placeholder}\r\n")
    assert got.startswith(b"HTTP/1.1 502")
    assert up.seen == []


def test_logs_never_contain_the_secret(mirrors, proxy_factory, caplog):
    caplog.set_level(logging.DEBUG, logger="awrun.egress")
    bound, other = mirrors(), mirrors()
    bp, op = bound.server_address[1], other.server_address[1]
    b = _binding(bp, header="X-Api-Key")
    proxy = proxy_factory([f"127.0.0.1:{bp}"], [b])
    _get(proxy, f"http://127.0.0.1:{bp}/", f"Authorization: {b.placeholder}\r\n")
    _get(proxy, f"http://127.0.0.1:{op}/", f"Authorization: {b.placeholder}\r\n")
    assert REAL.encode() in bound.seen[0]          # it really was injected
    text = caplog.text + json.dumps(proxy.denied) + json.dumps(proxy.injected)
    assert "OPENAI_API_KEY" in caplog.text         # the NAME is logged
    assert REAL not in text


def test_no_bindings_leaves_the_proxy_byte_identical(mirrors, proxy_factory):
    up = mirrors()
    port = up.server_address[1]
    proxy = proxy_factory([f"127.0.0.1:{port}"], [])
    req = f"GET http://127.0.0.1:{port}/a HTTP/1.1\r\nHost: x\r\nAuthorization: z\r\n\r\n"
    _send(proxy, req)
    assert up.seen[0] == req.encode()


@pytest.mark.parametrize("bad", [
    {"placeholder": "aw-cred-x-0123456789abcdef", "secret_ref": "K", "domains": []},
    {"placeholder": "short", "secret_ref": "K", "domains": ["a.com"]},
    {"placeholder": "aw-cred-x-0123456789abcdef", "secret_ref": "K",
     "domains": ["a.com"], "value": "a-literal-value"},
    {"placeholder": "aw-cred-x-0123456789abcdef", "secret_ref": "has space",
     "domains": ["a.com"]},
])
def test_bad_bindings_are_refused(tmp_path, bad):
    p = tmp_path / "b.json"
    p.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(ValueError):
        egress.load_bindings(str(p))


def test_vault_resolver_has_no_env_fallback(monkeypatch):
    import sys
    import types

    calls = {}
    fake = types.ModuleType("lib.clients.aither_secrets")

    def get_secret(name, fallback=None, **kw):
        calls.update(kw, name=name)
        return ""

    fake.get_secret = get_secret
    monkeypatch.setitem(sys.modules, "lib.clients.aither_secrets", fake)
    monkeypatch.setenv("OPENAI_API_KEY", REAL)
    with pytest.raises(egress.CredentialUnavailableError):
        egress.vault_resolver("OPENAI_API_KEY")
    assert calls["use_env_fallback"] is False
