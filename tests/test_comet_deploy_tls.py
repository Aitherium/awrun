"""`comet-deploy` over https: the internal CA is trusted, verification stays on.

The deployment service is a local fake behind a throwaway certificate; nothing
here reaches a real AitherComet, and the real-dispatch gate is only ever opened
against that fake.
"""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from awrun import dispatcher
from awrun.store import RunItem

_OK = {"status": "succeeded", "message": "deployed"}


def _item(spec=None) -> RunItem:
    return RunItem(id="r-tls00001", kind="comet-deploy",
                   spec={"service_name": "lane-probe"} if spec is None else spec)


class _Comet:
    """A fake deployment service. `cert`/`key` make it https."""

    def __init__(self, cert: str = "", key: str = ""):
        self.requests: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
                body = json.dumps(_OK).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        scheme = "http"
        if cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
            scheme = "https"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"{scheme}://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _make_cert(directory, name: str) -> tuple[str, str]:
    """A self-signed certificate for 127.0.0.1, standing in for an internal CA."""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is not on PATH; cannot make a throwaway certificate")
    cert, key = str(directory / f"{name}.pem"), str(directory / f"{name}.key")
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", key, "-out", cert, "-subj", "/CN=awrun-test-internal-ca",
         "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
        check=True, capture_output=True, timeout=60)
    return cert, key


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("AWRUN_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
                "AITHER_COMET_URL", "AWRUN_ALLOW_REAL_COMET_DEPLOY"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def https_comet(tmp_path):
    cert, key = _make_cert(tmp_path, "comet")
    comet = _Comet(cert, key)
    yield comet, cert
    comet.close()


# ── the gate: nothing is sent while real dispatch is off ───────────────────

def test_gate_off_refuses_before_any_network_call(clean_env):
    clean_env.setenv("AITHER_COMET_URL", "https://127.0.0.1:8125/comet")

    def _never(*_a, **_k):
        raise AssertionError("a refused comet-deploy must not open a connection")

    clean_env.setattr(dispatcher.urllib.request, "urlopen", _never)
    code, msg = dispatcher._real_run_comet_deploy(_item())
    assert code == 1 and "AWRUN_ALLOW_REAL_COMET_DEPLOY" in msg


def test_there_is_still_no_default_address(clean_env):
    clean_env.setenv("AWRUN_ALLOW_REAL_COMET_DEPLOY", "1")
    code, msg = dispatcher._real_run_comet_deploy(_item())
    assert code == 1 and "AITHER_COMET_URL is not set" in msg


# ── which bundle ───────────────────────────────────────────────────────────

def test_ca_bundle_convention(clean_env, tmp_path):
    a, b = tmp_path / "a.pem", tmp_path / "b.pem"
    a.write_text("x")
    b.write_text("x")
    assert dispatcher._ca_bundle() is None
    clean_env.setenv("SSL_CERT_FILE", str(b))
    assert dispatcher._ca_bundle() == str(b)
    clean_env.setenv("AWRUN_CA_BUNDLE", str(a))
    assert dispatcher._ca_bundle() == str(a)          # awrun's own name wins
    clean_env.setenv("AWRUN_CA_BUNDLE", str(tmp_path / "missing.pem"))
    assert dispatcher._ca_bundle() is None            # a typo is not a trust decision


def test_context_always_verifies(clean_env):
    assert dispatcher._https_context("http://127.0.0.1:8125") is None
    ctx = dispatcher._https_context("HTTPS://127.0.0.1:8125/comet")
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_the_dispatch_hands_urlopen_that_context(clean_env):
    seen: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(_OK).encode()

    def _urlopen(req, timeout=None, context=None):
        seen.update(url=req.full_url, context=context)
        return _Resp()

    clean_env.setenv("AWRUN_ALLOW_REAL_COMET_DEPLOY", "1")
    clean_env.setenv("AITHER_COMET_URL", "https://comet.internal:8125/comet/")
    clean_env.setattr(dispatcher.urllib.request, "urlopen", _urlopen)
    code, _ = dispatcher._real_run_comet_deploy(_item())
    assert code == 0
    assert seen["url"] == "https://comet.internal:8125/comet/deploy"
    assert isinstance(seen["context"], ssl.SSLContext)
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED


# ── real TLS against the fake ──────────────────────────────────────────────

def test_internal_ca_named_by_awrun_ca_bundle_is_trusted(clean_env, https_comet):
    comet, cert = https_comet
    clean_env.setenv("AWRUN_ALLOW_REAL_COMET_DEPLOY", "1")
    clean_env.setenv("AITHER_COMET_URL", comet.url)
    clean_env.setenv("AWRUN_CA_BUNDLE", cert)
    code, msg = dispatcher._real_run_comet_deploy(_item())
    assert code == 0, msg
    assert json.loads(msg) == _OK
    assert comet.requests == [("/deploy", {"service_name": "lane-probe"})]


def test_ssl_cert_file_is_honoured_too(clean_env, https_comet):
    comet, cert = https_comet
    clean_env.setenv("AWRUN_ALLOW_REAL_COMET_DEPLOY", "1")
    clean_env.setenv("AITHER_COMET_URL", comet.url)
    clean_env.setenv("SSL_CERT_FILE", cert)
    code, msg = dispatcher._real_run_comet_deploy(_item())
    assert code == 0, msg


def test_untrusted_certificate_fails_closed_and_names_the_fix(clean_env, https_comet):
    comet, _cert = https_comet
    clean_env.setenv("AWRUN_ALLOW_REAL_COMET_DEPLOY", "1")
    clean_env.setenv("AITHER_COMET_URL", comet.url)
    code, msg = dispatcher._real_run_comet_deploy(_item())
    assert code == 1
    assert "does not trust" in msg and "AWRUN_CA_BUNDLE" in msg
    assert comet.requests == []          # the request body never left this process


def test_a_different_ca_does_not_unlock_the_service(clean_env, https_comet, tmp_path):
    comet, _cert = https_comet
    other, _ = _make_cert(tmp_path, "other")
    clean_env.setenv("AWRUN_ALLOW_REAL_COMET_DEPLOY", "1")
    clean_env.setenv("AITHER_COMET_URL", comet.url)
    clean_env.setenv("AWRUN_CA_BUNDLE", other)
    code, msg = dispatcher._real_run_comet_deploy(_item())
    assert code == 1 and "does not trust" in msg
    assert comet.requests == []


def test_plain_http_still_dispatches(clean_env):
    comet = _Comet()
    try:
        clean_env.setenv("AWRUN_ALLOW_REAL_COMET_DEPLOY", "1")
        clean_env.setenv("AITHER_COMET_URL", comet.url)
        code, _ = dispatcher._real_run_comet_deploy(_item())
        assert code == 0
        assert comet.requests == [("/deploy", {"service_name": "lane-probe"})]
    finally:
        comet.close()


def test_relay_lane_reads_the_same_bundle(clean_env, tmp_path, monkeypatch):
    """One convention, one function: the relay post and the deploy post agree."""
    httpx = pytest.importorskip("httpx")
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    seen: dict = {}

    class _R:
        status_code = 200
        text = ""

    def _post(url, **kw):
        seen.update(kw)
        return _R()

    monkeypatch.setattr(httpx, "post", _post)
    lane = dispatcher._AgentLane("https://relay.internal", "t", "awrun")
    lane.send_text(channel="#x", text="hi")
    assert seen["verify"] is True
    clean_env.setenv("AWRUN_CA_BUNDLE", str(bundle))
    lane.send_text(channel="#x", text="hi")
    assert seen["verify"] == str(bundle)
