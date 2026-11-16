"""An owner's platform approval as the authorization for a gated awrun kind.

A fake Genesis (stdlib HTTP, loopback) answers the three routes awrun uses with the
approval plane's own rules: a request's preview must hash to its digest; consume needs
the SAME bearer that asked, an ``approved`` status, the same action and digest, and
spends the approval exactly once. Nothing here reaches a real Genesis, a real owner or
Discord.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# THIS package, never an installed awrun: the local-verify lane runs pytest with
# --noconftest, and an older awrun on the path has no `approval` module.
_PKG = Path(__file__).resolve().parents[1]
for _p in [_PKG] + [_PKG.parent / n for n in ("awbac", "awiam", "awdit")]:
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_loaded = sys.modules.get("awrun")
if _loaded is not None and not os.path.normcase(str(getattr(_loaded, "__file__", "") or "")
                                               ).startswith(os.path.normcase(str(_PKG))):
    for _name in [m for m in sys.modules if m == "awrun" or m.startswith("awrun.")]:
        del sys.modules[_name]

from awrun import approval, cli  # noqa: E402
from awrun.store import RunStore  # noqa: E402

OWNER_BEARER = "tok-owner-device-session"
_REAL_REGISTRY_URL = approval._registry_url
ATTEST_KEY = Ed25519PrivateKey.generate()          # the platform's attestation key
OTHER_KEY = Ed25519PrivateKey.generate()           # a key that is not pinned


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _pub_hex(key) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw).hex()
SPECS = [{"node": "dgx", "action": "secret-sync", "slots": "previous", "roll": False,
          "dry_run": False},
         {"node": "optiplex-wsl", "action": "secret-sync", "slots": "previous",
          "roll": False, "dry_run": False}]


class FakeGenesis:
    """The approval plane's request / status / consume, as the service enforces them."""

    def __init__(self) -> None:
        self.approvals: dict = {}
        self.calls: list = []
        self.down = False
        #: How the consume receipt is signed: good | none | bad-sig | other-key | stale |
        #: other-digest. The platform key is ATTEST_KEY; OTHER_KEY is anyone else's.
        self.sign_mode = "good"
        outer = self

        class H(BaseHTTPRequestHandler):
            def _reply(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_POST(self):  # noqa: N802 - http.server API
                # The MCP gateway: initialize -> notifications/initialized -> tools/call.
                # Each approval tool maps onto the plane route it forwards to, with the
                # caller's bearer -- as apps/awnode/tools/mcp/mcp_approvals.py does.
                if self.path != "/mcp":
                    self._reply(404, {"detail": "not here"})
                    return
                rpc = self._body()
                method = rpc.get("method")
                if method == "initialize":
                    raw = json.dumps({"jsonrpc": "2.0", "id": rpc.get("id"),
                                      "result": {"protocolVersion": "2024-11-05"}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("mcp-session-id", "sid-1")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if method != "tools/call":
                    self._reply(202, {})
                    return
                if self.headers.get("mcp-session-id") != "sid-1":
                    self._reply(400, {"detail": "no session"})
                    return
                params = rpc.get("params") or {}
                status, out = outer.tool(params.get("name"), params.get("arguments") or {},
                                         self.headers.get("Authorization", ""))
                ok = 200 <= status < 300
                text = json.dumps(out if ok else {"error": out.get("detail"),
                                                  "status": status})
                self._reply(200, {"jsonrpc": "2.0", "id": rpc.get("id"),
                                  "result": {"content": [{"type": "text", "text": text}],
                                             "isError": not ok}})

            def log_message(self, *_a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self) -> None:
        self.srv.shutdown()

    def tool(self, name: str, args: dict, auth: str):
        """One approval tool, onto the plane route it forwards to."""
        base = "/api/v1/agent-approvals"
        aid = str(args.get("approval_id") or "")
        if name == "approval_request":
            body = {"action": args.get("action"), "summary": args.get("summary"),
                    "payload_digest": args.get("payload_digest"),
                    "payload_preview": args.get("payload"),
                    "ttl_minutes": args.get("ttl_minutes")}
            route = ("POST", base, body)
        elif name == "approval_status":
            route = ("GET", f"{base}/{aid}", None)
        elif name == "approval_consume":
            route = ("POST", f"{base}/{aid}/consume",
                     {k: args.get(k) for k in ("action", "payload_digest", "workspace_id")})
        elif name == "approval_revoke":
            route = ("POST", f"{base}/{aid}/revoke", {"reason": args.get("reason")})
        else:
            return 404, {"detail": f"no tool {name}"}
        self.calls.append(route)
        method, path, body = route
        return self.get(path, auth) if method == "GET" else self.post(path, auth, body)

    # -- the plane ---------------------------------------------------------------

    def view(self, rec: dict) -> dict:
        return {k: rec[k] for k in ("approval_id", "action", "summary", "payload_digest",
                                    "payload_preview", "preview_verified", "status",
                                    "decided_by", "requested_by", "card_id")}

    def post(self, path: str, auth: str, body: dict):
        if self.down:
            return 503, {"detail": "approval gate is not configured"}
        base = "/api/v1/agent-approvals"
        if path == base:
            preview = body.get("payload_preview") or ""
            if hashlib.sha256(preview.encode()).hexdigest() != body.get("payload_digest"):
                return 422, {"detail": "payload_preview does not hash to payload_digest"}
            aid = f"apr-{len(self.approvals):016x}"
            self.approvals[aid] = {
                "approval_id": aid, "action": body["action"], "summary": body["summary"],
                "payload_digest": body["payload_digest"], "payload_preview": preview,
                "preview_verified": True, "status": "pending", "decided_by": None,
                "requested_by": {"user_id": "u-david"}, "card_id": "d-card1",
                "_bearer": auth}
            return 200, self.view(self.approvals[aid])
        aid = path[len(base) + 1:].split("/")[0]
        rec = self.approvals.get(aid)
        if rec is None or rec["_bearer"] != auth:
            return 404, {"detail": "no such approval"}
        if path.endswith("/revoke") and rec["status"] in ("pending", "approved"):
            rec["status"] = "revoked"
            return 200, self.view(rec)
        if not path.endswith("/consume"):
            return 404, {"detail": "not here"}
        if rec["status"] == "expired":
            return 410, {"detail": "this approval has expired"}
        if rec["status"] != "approved":
            return 409, {"detail": f"this approval is {rec['status']}, not approved"}
        if rec["action"] != body.get("action"):
            return 403, {"detail": "this approval is for a different action"}
        if rec["payload_digest"] != body.get("payload_digest"):
            return 403, {"detail": "this approval is for a different payload"}
        rec["status"] = "consumed"
        out = self.view(rec)
        if self.sign_mode != "none":
            out["consume_receipt"] = self.receipt(rec)
        return 200, out

    def receipt(self, rec: dict) -> dict:
        """What Genesis signs (lib/agent_approval/consume_receipt.py), per sign_mode."""
        mode = self.sign_mode
        body = {"v": 1, "type": "agent-approval-consume", "approval_id": rec["approval_id"],
                "action": rec["action"], "payload_digest": rec["payload_digest"],
                "approver": (rec["decided_by"] or {}).get("user_id", ""),
                "requested_by": rec["requested_by"]["user_id"], "workspace_id": "ws-1",
                "consumed_at": int(time.time()) - (3600 if mode == "stale" else 0)}
        if mode == "other-digest":
            body["payload_digest"] = "f" * 64
        key = OTHER_KEY if mode == "other-key" else ATTEST_KEY
        sig = key.sign(_canonical(body)).hex()
        if mode == "bad-sig":
            body = dict(body, approver="u-someone-else")      # signed one thing, sent another
        return {"alg": "ed25519", "kid": _pub_hex(key)[:16], "receipt": body, "sig": sig}

    def get(self, path: str, auth: str):
        if self.down:
            return 503, {"detail": "down"}
        aid = path.rsplit("/", 1)[-1]
        rec = self.approvals.get(aid)
        if rec is None or rec["_bearer"] != auth:
            return 404, {"detail": "no such approval"}
        return 200, self.view(rec)

    def decide(self, aid: str, verdict: str, by: str = "u-david") -> None:
        rec = self.approvals[aid]
        rec["status"] = verdict
        rec["decided_by"] = {"user_id": by, "name": by}


@pytest.fixture
def genesis(monkeypatch, tmp_path):
    # Our own scratch trail, conftest or not: a test never appends to the operator's.
    monkeypatch.setenv("AWRUN_AUDIT_LOG", str(tmp_path / "awrun-audit.log"))
    # The verifier key, pinned the platform's way: a FILE named by
    # AWSTORAGE_ATTEST_PUBKEY_FILE, outside ~/.aither.
    pub = tmp_path / "attest.pub"
    pub.write_text(_pub_hex(ATTEST_KEY), encoding="utf-8")
    monkeypatch.setenv("AWSTORAGE_ATTEST_PUBKEY_FILE", str(pub))
    monkeypatch.delenv("AWSTORAGE_ATTEST_PUBKEY", raising=False)
    monkeypatch.setenv("AITHER_TESTING", "1")      # the only way a URL env is honoured
    g = FakeGenesis()
    monkeypatch.delenv(approval.URL_ENV, raising=False)
    monkeypatch.setattr(approval, "_registry_url", lambda: g.url)   # the registry's answer
    monkeypatch.setenv(approval.BEARER_ENV, OWNER_BEARER)
    monkeypatch.delenv(approval.APPROVAL_ID_ENV, raising=False)
    monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
    monkeypatch.setattr(approval, "FANOUT_RUNNER",
                        lambda: (0, "discord: delivered 1 message(s) covering 1 card(s)"))
    yield g
    g.close()


def _approved(genesis: FakeGenesis, specs=SPECS, kind: str = "node-op") -> str:
    view = approval.request(kind, specs, "rotate the fleet secret: accept phase")
    genesis.decide(view["approval_id"], "approved")
    return view["approval_id"]


def _audit_lines(tmp_path) -> list:
    path = tmp_path / "awrun-audit.log"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── the binding ──────────────────────────────────────────────────────────────


def test_payload_is_visible_deterministic_and_carries_every_key():
    text = approval.payload("node-op", SPECS)
    assert text == approval.payload("node-op", [dict(reversed(list(s.items())))
                                                for s in SPECS])
    assert text.isascii() and all(ch.isprintable() or ch == "\n" for ch in text)
    assert "1. action=\"secret-sync\" dry_run=false node=\"dgx\"" in text
    assert json.loads(text.splitlines()[-1]) == {"kind": "node-op", "runs": SPECS}
    assert approval.action_for("node-op") == "awrun:node-op"
    with pytest.raises(approval.ApprovalRefusedError):
        approval.payload("node-op", [])


# ── the four outcomes, through the real submit gate ─────────────────────────


def test_approved_with_matching_spec_is_granted_and_audits_the_approver(genesis, tmp_path):
    aid = _approved(genesis)
    assert cli._authorize_gated("node-op", SPECS, aid) is None
    consumes = [c for c in genesis.calls if c[1].endswith("/consume")]
    assert len(consumes) == 1                                   # one batch, one spend
    assert consumes[0][2]["action"] == "awrun:node-op"
    events = _audit_lines(tmp_path)
    submitted = [e for e in events if e.get("event") == "node-op-submitted"]
    assert len(submitted) == 1
    rec = submitted[0]["data"]
    assert rec["subject"] == "u-david" and rec["via"] == "agent-approval"
    assert rec["approval_id"] == aid
    assert events[0]["event"] == "node-op-approval-presented"   # written BEFORE the spend


def test_a_spec_that_differs_by_one_key_is_refused(genesis):
    aid = _approved(genesis)
    other = [dict(SPECS[0], roll=True), SPECS[1]]
    why = cli._authorize_gated("node-op", other, aid)
    assert why and "different payload" in why
    assert genesis.approvals[aid]["status"] == "approved"       # not spent on the wrong spec
    assert cli._authorize_gated("node-op", SPECS, aid) is None  # the approved spec still works


def test_an_approval_for_another_kind_opens_nothing(genesis):
    aid = _approved(genesis, specs=[{"action": "expose", "hostname": "x.example.com",
                                     "origin": "http://127.0.0.1:1", "plane": "tunnel"}],
                    kind="tunnel")
    why = cli._authorize_gated("node-op", SPECS, aid)
    assert why and "refused" in why


@pytest.mark.parametrize("verdict", ["denied", "expired", "pending", "revoked"])
def test_an_approval_that_is_not_approved_is_refused(genesis, verdict):
    view = approval.request("node-op", SPECS, "rotate")
    if verdict != "pending":
        genesis.decide(view["approval_id"], verdict)
    why = cli._authorize_gated("node-op", SPECS, view["approval_id"])
    assert why and "refused" in why


def test_an_unreachable_plane_is_refused_never_allowed(genesis, monkeypatch):
    aid = _approved(genesis)
    genesis.down = True
    assert "refused" in cli._authorize_gated("node-op", SPECS, aid)
    monkeypatch.setattr(approval, "_registry_url", lambda: "http://127.0.0.1:9")
    assert "refused" in cli._authorize_gated("node-op", SPECS, aid)   # nothing listens


def test_no_bearer_is_refused(genesis, monkeypatch, tmp_path):
    aid = _approved(genesis)
    monkeypatch.delenv(approval.BEARER_ENV)
    monkeypatch.setattr(approval.Path, "home", staticmethod(lambda: tmp_path))
    why = cli._authorize_gated("node-op", SPECS, aid)
    assert why and "no platform bearer" in why


def test_replaying_a_consumed_approval_is_refused(genesis, tmp_path):
    aid = _approved(genesis)
    assert cli._authorize_gated("node-op", SPECS, aid) is None
    why = cli._authorize_gated("node-op", SPECS, aid)
    assert why and "already spent on this host" in why        # this host's trail
    (tmp_path / "awrun-audit.log").unlink()                     # ...and without it:
    why = cli._authorize_gated("node-op", SPECS, aid)
    assert why and "consumed" in why                            # the plane's once-only
    # and per node, after the batch: a single spec is a different payload too
    assert cli._authorize_gated("node-op", SPECS[0], aid)


def test_a_malformed_id_never_reaches_the_plane(genesis):
    assert "not an approval id" in cli._authorize_gated("node-op", SPECS, "apr-nope")
    assert genesis.calls == []


def test_the_env_carries_an_approval_to_submit_only(genesis, monkeypatch, tmp_path, capsys):
    aid = _approved(genesis)
    monkeypatch.setenv(approval.APPROVAL_ID_ENV, aid)
    # apply / a dry run / any other path reaches the gate WITHOUT it: never spent there
    assert "session" in cli._authorize("node-op", SPECS)
    assert genesis.approvals[aid]["status"] == "approved"
    store = RunStore(tmp_path / "runs")
    assert cli.cmd_submit(_submit_args(ask_owner=None), store) == 0, capsys.readouterr().err
    assert len(store.list()) == 2 and genesis.approvals[aid]["status"] == "consumed"


def test_another_admin_of_the_workspace_cannot_open_it(genesis, monkeypatch):
    view = approval.request("node-op", SPECS, "rotate")
    aid = view["approval_id"]
    genesis.decide(aid, "approved", by="u-other-admin")
    why = cli._authorize_gated("node-op", SPECS, aid)
    assert why and "not the account that asked" in why
    assert genesis.approvals[aid]["status"] == "approved"      # refused BEFORE the spend
    monkeypatch.setenv(approval.APPROVERS_ENV, "u-ops,u-other-admin")
    assert cli._authorize_gated("node-op", SPECS, aid) is None
    monkeypatch.setenv(approval.APPROVERS_ENV, "u-ops")
    aid2 = _approved(genesis)                                   # u-david is not named
    assert "not in AWRUN_APPROVERS" in cli._authorize_gated("node-op", SPECS, aid2)


def test_queue_options_are_part_of_what_was_approved(genesis):
    view = approval.request("node-op", SPECS, "rotate", options={"priority": 5})
    genesis.decide(view["approval_id"], "approved")
    assert "priority=5" in view["payload_preview"]
    why = cli._authorize_gated("node-op", SPECS, view["approval_id"], {"priority": 9})
    assert why and "different payload" in why
    assert cli._authorize_gated("node-op", SPECS, view["approval_id"],
                                {"priority": 5, "paths": []}) is None


def test_a_key_that_could_forge_a_line_is_refused():
    with pytest.raises(approval.ApprovalRefusedError):
        approval.payload("node-op", [{"node": "a", "x\n2. node": "b"}])
    homoglyph = chr(0x440) + chr(0x43E)   # Cyrillic letters that look like "po"
    with pytest.raises(approval.ApprovalRefusedError):
        approval.payload("node-op", [{"node": "a"}], options={homoglyph: 1})


def test_a_redirect_is_never_followed_with_the_bearer(genesis, monkeypatch):
    seen = []

    class Hop(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            seen.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", genesis.url + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Hop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        hop = f"http://127.0.0.1:{srv.server_address[1]}"
        monkeypatch.setattr(approval, "_registry_url", lambda: hop)
        with pytest.raises(approval.ApprovalRefusedError):
            approval.status("apr-0000000000000000")
    finally:
        srv.shutdown()
    assert seen and not [c for c in genesis.calls if c[0] == "GET"]   # not followed


def test_a_consume_answer_that_does_not_confirm_is_refused(genesis, monkeypatch):
    aid = _approved(genesis)
    real = genesis.post

    def sloppy(path, auth, body):
        status, out = real(path, auth, body)
        return status, dict(out, decided_by=None)              # no named human

    monkeypatch.setattr(genesis, "post", sloppy)
    why = cli._authorize_gated("node-op", SPECS, aid)
    assert why and "names no human" in why


# ── the helper: ask, tell, wait, then ONE submit for the batch ───────────────


def _submit_args(**kw) -> argparse.Namespace:
    base = {"kind": "node-op", "spec_json": json.dumps(SPECS), "json": True, "priority": 0,
            "paths": [], "name": None, "lineage": None, "timeout_s": None, "cpus": None,
            "memory_mb": None, "egress_host": None, "no_egress": False,
            "egress_proxy": None, "egress_network": None, "isolation_image": None,
            "approval_id": None, "ask_owner": "rotate the fleet secret: accept phase",
            "approval_ttl_minutes": 60, "approval_timeout_s": 30, "approval_poll_s": 1.0}
    base.update(kw)
    return argparse.Namespace(**base)


def test_ask_owner_requests_once_waits_and_queues_the_whole_batch(genesis, tmp_path,
                                                                  monkeypatch, capsys):
    def approve_on_first_poll(view):
        if view["status"] == "pending":
            genesis.decide(view["approval_id"], "approved")

    real_wait = approval.wait
    monkeypatch.setattr(approval, "wait", lambda aid, **kw: real_wait(
        aid, **dict(kw, sleep=lambda _s: approve_on_first_poll(genesis.view(
            genesis.approvals[aid])))))
    store = RunStore(tmp_path / "runs")
    rc = cli.cmd_submit(_submit_args(), store)
    out, err = capsys.readouterr()
    assert rc == 0, err
    items = json.loads(out)
    assert [i["spec"]["node"] for i in items] == ["dgx", "optiplex-wsl"]
    requests = [c for c in genesis.calls if c[0] == "POST" and c[1].endswith("approvals")]
    consumes = [c for c in genesis.calls if c[1].endswith("/consume")]
    assert len(requests) == 1 and len(consumes) == 1           # ONE approval, not per node
    assert requests[0][2]["payload_preview"] == approval.payload("node-op", SPECS)
    assert "decide at" in err and "/approvals?id=apr-" in err
    for surface in ("app", "awdesk", "discord", "email"):
        assert f"  {surface}" in err
    assert "sent by the decision fanout" in err


def test_ask_owner_that_times_out_withdraws_the_ask(genesis, tmp_path, monkeypatch, capsys):
    real_wait = approval.wait
    monkeypatch.setattr(approval, "wait", lambda aid, **kw: real_wait(
        aid, **dict(kw, timeout_s=0, sleep=lambda _s: None)))
    store = RunStore(tmp_path / "runs")
    rc = cli.cmd_submit(_submit_args(), store)
    assert rc == 2 and "withdrawn" in capsys.readouterr().err
    assert store.list() == []
    assert [r["status"] for r in genesis.approvals.values()] == ["revoked"]


def test_ask_owner_denied_queues_nothing(genesis, tmp_path, monkeypatch, capsys):
    real_wait = approval.wait

    def deny(aid, **kw):
        genesis.decide(aid, "denied")
        return real_wait(aid, **kw)

    monkeypatch.setattr(approval, "wait", deny)
    store = RunStore(tmp_path / "runs")
    rc = cli.cmd_submit(_submit_args(), store)
    assert rc == 1 and "denied" in capsys.readouterr().err
    assert store.list() == []
    assert not [c for c in genesis.calls if c[1].endswith("/consume")]


def test_ask_owner_uses_a_session_that_already_authorizes(genesis, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_session_authorizes", lambda kind: True)
    seen = []
    monkeypatch.setattr(cli, "_authorize_gated",
                        lambda kind, spec, approval_id="": seen.append(approval_id))
    rc = cli.cmd_submit(_submit_args(), RunStore(tmp_path / "runs"))
    assert rc == 0 and seen == [""] and genesis.calls == []   # no approval asked


def test_a_batch_is_node_op_only_and_takes_no_per_run_flags(tmp_path):
    with pytest.raises(cli.RunError):
        cli._build_specs(_submit_args(kind="tunnel"))
    with pytest.raises(cli.RunError):
        cli._build_specs(_submit_args(node="dgx"))
    with pytest.raises(cli.RunError):
        cli._build_specs(_submit_args(spec_json="[]"))
    with pytest.raises(cli.RunError):
        cli._build_specs(_submit_args(spec_json=json.dumps(
            [{"node": "dgx", "action": "secret-sync", "value": "x"}])))


def test_no_card_means_the_surfaces_say_not_reached(genesis):
    out = approval.notify_surfaces({"approval_id": "apr-0000000000000001", "card_id": None})
    assert out["discord"].startswith("NOT REACHED") and out["awdesk"].startswith("NOT REACHED")
    assert out["page"].endswith("/approvals?id=apr-0000000000000001")


# ── the signed consume receipt and the registry-only URL ─────────────────────


def test_a_caller_chosen_url_is_never_honoured(genesis, monkeypatch):
    aid = _approved(genesis)
    monkeypatch.setenv(approval.URL_ENV, genesis.url)       # even naming a live answerer
    why = cli._authorize_gated("node-op", SPECS, aid)
    assert why and "is not honoured" in why
    assert genesis.calls == [c for c in genesis.calls if c[1].endswith("approvals")]
    assert genesis.approvals[aid]["status"] == "approved"


def test_outside_tests_genesis_comes_from_the_registry(genesis, monkeypatch):
    aid = _approved(genesis)
    monkeypatch.delenv("AITHER_TESTING")
    monkeypatch.setattr(approval, "_registry_url", lambda: genesis.url)
    assert cli._authorize_gated("node-op", SPECS, aid) is None


def test_a_registry_that_cannot_reach_genesis_is_refused(genesis, monkeypatch):
    aid = _approved(genesis)
    monkeypatch.delenv("AITHER_TESTING")
    monkeypatch.setattr(approval, "_registry_url", _REAL_REGISTRY_URL)

    class Unreachable(str):
        error = "Genesis is not published to the host"

    monkeypatch.setitem(sys.modules, "lib.core.AitherPorts", type(sys)("fake_ports"))
    sys.modules["lib.core.AitherPorts"].get_service_url = (
        lambda name: Unreachable("https://aitheros-genesis:8001"))
    why = cli._authorize_gated("node-op", SPECS, aid)
    assert why and "not published to the host" in why


@pytest.mark.parametrize("mode,why", [
    ("none", "no signed receipt"),
    ("bad-sig", "does not verify"),
    ("other-key", "another key"),
    ("stale", "stale or replayed"),
    ("other-digest", "payload_digest differ"),
])
def test_an_unsigned_or_badly_signed_consume_is_refused(genesis, mode, why):
    aid = _approved(genesis)
    genesis.sign_mode = mode
    got = cli._authorize_gated("node-op", SPECS, aid)
    assert got and why in got, got


def test_no_pinned_key_is_refused(genesis, monkeypatch, tmp_path):
    aid = _approved(genesis)
    monkeypatch.delenv("AWSTORAGE_ATTEST_PUBKEY_FILE")
    got = cli._authorize_gated("node-op", SPECS, aid)
    assert got and "no pinned attestation key" in got


def test_a_replayed_signed_consume_opens_nothing_twice(genesis):
    """A recorder that answers the same (fresh, validly signed) consume again: Genesis
    would say "consumed"; the host's own trail refuses the second spend too."""
    aid = _approved(genesis)
    assert cli._authorize_gated("node-op", SPECS, aid) is None
    genesis.approvals[aid]["status"] = "approved"              # the replaying answerer
    got = cli._authorize_gated("node-op", SPECS, aid)
    assert got and "already spent on this host" in got
