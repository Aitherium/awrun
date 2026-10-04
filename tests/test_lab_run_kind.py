"""`lab-run`: queue an experiment run, gated like every other spend.

The lab service is a local fake; nothing here reaches a real lab or rents anything.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from awrun import authz, cli, dispatcher
from awrun.store import KINDS, RunStore


class _Lab:
    def __init__(self, reply: dict, status: int = 200):
        self.requests: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
                body = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path)


def _item(store, spec):
    return store.submit("lab-run", spec)


def test_lab_run_is_a_kind_with_its_own_gate():
    assert "lab-run" in KINDS
    assert authz.KIND_PERMISSIONS["lab-run"] == authz.LAB_RUN_PERMISSION
    assert authz.KIND_OPERATORS_ENV["lab-run"] == "AWRUN_LAB_RUN_OPERATORS"
    assert dispatcher._RUN_FNS["lab-run"] is dispatcher._real_run_lab_run


def test_dispatch_is_off_by_default(store, monkeypatch):
    monkeypatch.delenv("AWRUN_ALLOW_REAL_LAB_RUN", raising=False)
    code, msg = dispatcher._real_run_lab_run(_item(store, {"experiment": "x"}))
    assert code == 1 and "OFF" in msg


def test_no_lab_address_is_refused_not_guessed(store, monkeypatch):
    monkeypatch.setenv("AWRUN_ALLOW_REAL_LAB_RUN", "1")
    monkeypatch.delenv("AITHER_LAB_URL", raising=False)
    code, msg = dispatcher._real_run_lab_run(_item(store, {"experiment": "x"}))
    assert code == 1 and "AITHER_LAB_URL" in msg


def test_an_accepted_run_returns_its_id(store, monkeypatch):
    lab = _Lab({"run_id": "run_abc", "status": "pending"})
    try:
        monkeypatch.setenv("AWRUN_ALLOW_REAL_LAB_RUN", "1")
        monkeypatch.setenv("AITHER_LAB_URL", lab.url + "/lab")
        spec = {"experiment": "engine-tournament", "skip_finetune": True,
                "budget_cap_usd": 10.0}
        code, msg = dispatcher._real_run_lab_run(_item(store, spec))
    finally:
        lab.close()
    assert code == 0 and "run_abc" in msg
    path, body = lab.requests[0]
    assert path == "/lab/experiments/engine-tournament/run"
    assert body == {"skip_finetune": True, "budget_cap_usd": 10.0}


@pytest.mark.parametrize("reply,status", [({"detail": "Spec not found"}, 404),
                                          ({"status": "pending"}, 200)])
def test_a_run_the_lab_did_not_accept_is_a_failure(store, monkeypatch, reply, status):
    lab = _Lab(reply, status)
    try:
        monkeypatch.setenv("AWRUN_ALLOW_REAL_LAB_RUN", "1")
        monkeypatch.setenv("AITHER_LAB_URL", lab.url)
        code, _msg = dispatcher._real_run_lab_run(_item(store, {"experiment": "x"}))
    finally:
        lab.close()
    assert code == 1


def _args(**kw):
    base = {"kind": "lab-run", "experiment": "engine-tournament", "with_finetune": False,
            "budget_cap_usd": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_spec_skips_finetune_by_default_and_refuses_a_bad_name():
    assert cli._build_spec(_args()) == {"experiment": "engine-tournament",
                                        "skip_finetune": True}
    assert cli._build_spec(_args(with_finetune=True, budget_cap_usd=5))["budget_cap_usd"] == 5
    for bad in ("", "a/b"):
        with pytest.raises(cli.RunError):
            cli._build_spec(_args(experiment=bad))


def test_submit_without_a_session_is_refused(monkeypatch):
    monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
    why = cli._authorize_gated("lab-run", {"experiment": "x"})
    assert why and "session" in why
