"""awrun/approval.py -- an owner's approval, decided on the platform, as the
authorization for one gated submit.

WHY. The gated kinds (``authz.KIND_PERMISSIONS``) accepted one credential: an awiam
session resolved against a LOCAL file (``~/.aither/awrun-iam.json``) plus an operator
list in env. An owner away from that machine's terminal could not say yes, so an
owner-gated step meant pasting PowerShell into a console. The platform already has the
plane that fits: ``lib/agent_approval`` behind Genesis ``/api/v1/agent-approvals``. A
named human decides from a fresh passkey / authenticator-app browser session, the
decision is bound to the sha256 of the exact payload text the human read, and it is
spent once. This module is awrun's client for it; ``cli`` calls it.

THE BINDING
    action   ``awrun:<kind>``, so an approval for a node-op opens no tunnel.
    payload  :func:`payload`: deterministic, fully visible text built ONLY from the kind
             and the exact specs the submit queues -- one line a person can read per
             run, then the canonical JSON of all of them. The approver reads this text
             (the plane refuses a preview that does not hash to the digest), and the
             submit rebuilds it from what it is about to queue, so a spec that differs
             by one key is another digest and is refused.
    once     Genesis consumes it exactly once. A batch (one approval for every node of
             a rotation phase) is ONE payload and ONE consume: every run in it is
             queued by that one submit, never spread over several.

TRANSPORT. Through the platform's MCP gateway, as its service registry names it (the
approval tools ``approval_request`` / ``approval_status`` / ``approval_consume`` /
``approval_revoke``, which forward the caller's own bearer to Genesis): a host outside
the fleet network, where Genesis is not published, reaches the plane that way. Never a
caller-chosen URL outside tests, and the gateway is only a relay: what grants is the
consume receipt Genesis signed.

IDENTITY. Request and consume are the SAME account (the plane binds an approval to the
user who asked and spends it only for that user). Both carry the operator's own platform
bearer (``AWRUN_APPROVAL_BEARER``, else ``~/.aither/session-bearer``), never the fleet
internal key: with an opaque session bearer AND ``X-Internal-Key`` on one request,
Genesis' JWT middleware accepts the key and skips introspecting the bearer, so the caller
has no user and the plane answers 401. A dev-box bearer is a device-code session, which
the plane never lets DECIDE: the owner approves in a browser with a passkey or an
authenticator app, from the approvals page, the mail or the decision card.

FAIL-CLOSED. No bearer, no reachable Genesis, plain http to anything but loopback, a
non-2xx, or a 2xx whose body does not say ``consumed`` for this id, action and digest
with a verified preview and a named decider: every one is a refusal, never an allow.

stdlib only: this package ships without the monorepo, so nothing here imports ``lib``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

#: An approval's action is this prefix + the kind.
ACTION_PREFIX = "awrun:"
#: Env alternative to ``awrun submit --approval``.
APPROVAL_ID_ENV = "AWRUN_APPROVAL_ID"
#: Genesis base URL. Else AITHER_GENESIS_URL, else the in-network / host default.
URL_ENV = "AWRUN_APPROVAL_URL"
#: The operator's platform bearer. Else ~/.aither/session-bearer.
BEARER_ENV = "AWRUN_APPROVAL_BEARER"
#: The workspace to ask in ("" = the account's default workspace).
WORKSPACE_ENV = "AWRUN_APPROVAL_WORKSPACE"
#: Platform user ids whose approval opens a gated kind (comma-separated). Unset: only
#: the account that ASKED -- the owner approving his own ask from a fresh passkey
#: session. Any other owner/admin of that workspace is refused before the spend.
APPROVERS_ENV = "AWRUN_APPROVERS"
#: Where the approver decides (display only). Never the demo host.
PAGE_BASE_ENV = "AWRUN_APPROVAL_PAGE_BASE"
DEFAULT_PAGE_BASE = "https://app.aitherium.com"
#: The Discord fanout to kick after a request (path), when not at its repo place.
FANOUT_ENV = "AWRUN_DECISION_FANOUT"

ID_RE = re.compile(r"^apr-[0-9a-f]{16}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
#: The plane's caps (lib/agent_approval/service.py PREVIEW_MAX / SUMMARY_MAX).
PREVIEW_MAX = 4000
SUMMARY_MAX = 1000
#: States after which nothing can change.
FINAL = ("denied", "expired", "revoked", "consumed", "undelivered")
#: Consecutive unreachable polls tolerated while waiting before giving up (closed).
MAX_POLL_FAILURES = 6

#: Test seam for the Discord fanout kick: () -> (rc, output). None = run the tool.
FANOUT_RUNNER: Optional[Callable[[], "tuple[int, str]"]] = None


class ApprovalRefusedError(Exception):
    """The approval does not authorize this submit (exit 1)."""


class ApprovalUnavailableError(ApprovalRefusedError):
    """The plane could not be asked or did not answer usably (exit 2). Still a refusal."""


# ── the binding ──────────────────────────────────────────────────────────────


def action_for(kind: str) -> str:
    kind = str(kind or "")
    if not _KIND_RE.match(kind):
        raise ApprovalRefusedError(f"not a run kind: {kind!r}")
    return ACTION_PREFIX + kind


#: A top-level key printed bare on a readable line. Anything else (a newline, a space,
#: a homoglyph) could forge a line of its own above the canonical JSON: refused.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


def _checked_keys(obj: dict, what: str) -> dict:
    bad = [k for k in obj if not isinstance(k, str) or not _KEY_RE.match(k)]
    if bad:
        raise ApprovalRefusedError(f"{what} has a key an approver could not read as one: "
                                   f"{bad[0]!r}")
    return obj


def _checked_specs(specs: Any) -> list:
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, (list, tuple)) or not specs:
        raise ApprovalRefusedError("an approval covers one or more run specs; none were given")
    out = []
    for spec in specs:
        if not isinstance(spec, dict) or not spec:
            raise ApprovalRefusedError(f"every run spec must be a non-empty object, got {spec!r}")
        out.append(_checked_keys(spec, "a run spec"))
    return out


def _checked_options(options: Optional[dict]) -> dict:
    """The queue options that change what runs or where (priority, paths, limits,
    egress, lineage, name), without the empty ones: they are approved with the runs."""
    kept = {k: v for k, v in (options or {}).items() if v not in (None, "", [], {}, 0)}
    return _checked_keys(kept, "the queue options")


def _value(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_json(kind: str, specs: Any, options: Optional[dict] = None) -> str:
    """The exact runs, as one byte string: sorted keys, no spaces, ASCII only."""
    body: dict = {"kind": kind, "runs": _checked_specs(specs)}
    queue = _checked_options(options)
    if queue:
        body["queue"] = queue
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload(kind: str, specs: Any, options: Optional[dict] = None) -> str:
    """The text the owner reads and approves. Built from (kind, specs, queue options)
    alone, so the submit side rebuilds it byte for byte. ASCII only (``ensure_ascii``)
    and every bare key checked, so it carries nothing a person cannot see."""
    action_for(kind)
    runs = _checked_specs(specs)
    queue = _checked_options(options)
    lines = [f"awrun submit --kind {kind}: {len(runs)} run(s), queued together by ONE "
             f"submit under this approval, which is then spent."]
    for n, spec in enumerate(runs, 1):
        lines.append(f"{n}. " + " ".join(f"{k}={_value(spec[k])}" for k in sorted(spec)))
    if queue:
        lines.append("queue: " + " ".join(f"{k}={_value(queue[k])}" for k in sorted(queue)))
    lines += ["", "Exact runs (canonical JSON):", canonical_json(kind, runs, queue)]
    return "\n".join(lines)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── the transport ────────────────────────────────────────────────────────────


#: The platform service the approval tools are reached through: the MCP gateway. It
#: forwards the operator's own bearer to Genesis, so a host outside the fleet network
#: (where Genesis is not published) reaches the approval plane at all.
GATEWAY_SERVICE = "MCPGateway"
GATEWAY_PATH = "/mcp"


def _registry_url(service: str = GATEWAY_SERVICE) -> str:
    """``service`` as the platform's service registry (``lib.core.AitherPorts``) names it.

    The registry ships with the monorepo, not with this package: it is imported from the
    tree this package sits in (``packages/awrun`` -> ``AitherOS``) or from the path. A
    host the registry says cannot reach the service gets that reason, never a guess."""
    here = Path(__file__).resolve()
    root = here.parents[3] if len(here.parents) > 3 else None
    registry = root / "lib" / "core" / "AitherPorts.py" if root is not None else None
    if registry is not None and registry.is_file() and str(root) not in sys.path:
        sys.path.append(str(root))
    try:
        import importlib  # noqa: PLC0415 - optional, resolves only in the monorepo
        get_service_url = importlib.import_module("lib.core.AitherPorts").get_service_url
    except Exception as exc:  # noqa: BLE001 -- no registry = cannot judge
        raise ApprovalUnavailableError(
            f"the platform service registry (lib.core.AitherPorts) is not importable "
            f"({type(exc).__name__}); awrun resolves the approval plane only through it"
        ) from exc
    url = get_service_url(service)
    why = getattr(url, "error", "")
    if why:
        raise ApprovalUnavailableError(f"the service registry cannot reach {service} from "
                                       f"here: {why}")
    return str(url)


def gateway_url() -> str:
    """The MCP gateway's base URL, from the platform's SERVICE REGISTRY only.

    No environment variable names it: a URL a caller controls was the way to forge a
    consume, so ``AWRUN_APPROVAL_URL`` (the old override) is refused outright, tests
    included (a test double is injected in-process by patching :func:`_registry_url`).
    The registry's answer may be plain http: the gateway serves plain HTTP on the host
    loopback and on the fleet network."""
    if os.getenv(URL_ENV, "").strip():
        raise ApprovalUnavailableError(
            f"{URL_ENV} is not honoured: the approval plane is resolved from the service "
            f"registry only (a caller-chosen URL could answer a forged consume). Unset it.")
    url = _registry_url().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme in ("https", "http") and parts.hostname:
        return url
    raise ApprovalUnavailableError(f"the service registry named no usable approval plane: "
                                   f"{url!r}")


def bearer() -> str:
    """The operator's own platform bearer, or raise. Never logged, never printed."""
    token = os.getenv(BEARER_ENV, "").strip()
    if not token:
        try:
            token = (Path.home() / ".aither" / "session-bearer").read_text(
                encoding="utf-8").strip()
        except OSError:
            token = ""
    if not token:
        raise ApprovalUnavailableError(
            f"no platform bearer: set {BEARER_ENV} or sign in once with "
            "`python AitherOS/dev/tools/mint_session_bearer.py` (~/.aither/session-bearer)")
    return token


def _context(url: str):
    """The fleet's own CA when the platform's TLS config is importable (Genesis presents
    an internal certificate), else the system store + AWRUN_CA_BUNDLE. Verification is
    never turned off."""
    if url.lower().startswith("https://"):
        try:
            import importlib  # noqa: PLC0415 - optional, resolves only in the monorepo
            get_internal_ssl_context = importlib.import_module(
                "lib.security.TLSConfig").get_internal_ssl_context

            ctx = get_internal_ssl_context()
            if ctx is not None:
                return ctx
        except Exception as exc:  # noqa: BLE001 -- fall back to the explicit bundle below
            print(f"awrun: fleet TLS context unavailable ({type(exc).__name__}); using the "
                  "system store + AWRUN_CA_BUNDLE", file=sys.stderr)
    from awrun.dispatcher import _https_context  # system store + AWRUN_CA_BUNDLE

    return _https_context(url)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as the 30x it is, never followed: urllib would carry the
    Authorization header to wherever it points, plain http included."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _post(opener, url: str, body: dict, sid: str, token: str,
          timeout: float) -> "tuple[str, str]":
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **({"mcp-session-id": sid} if sid else {})})
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    with opener.open(req, timeout=timeout) as resp:
        return (resp.headers.get("mcp-session-id") or sid,
                resp.read().decode("utf-8", errors="replace"))


def _jsonrpc(text: str) -> dict:
    """An MCP response body: plain JSON or SSE-framed (``data:`` lines)."""
    try:
        got = json.loads(text)
        return got if isinstance(got, dict) else {}
    except ValueError:
        out: dict = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    out = json.loads(line)
                except ValueError:
                    continue
        return out


def _tool(name: str, arguments: dict, timeout: float = 60.0) -> "tuple[int, Any]":
    """(status, json) from one approval tool on the MCP gateway, called as the operator
    (the gateway forwards this bearer to Genesis). A tool's refusal comes back as its
    ``{"error", "status"}``; no answer at all is ApprovalUnavailableError."""
    import http.client

    base = gateway_url()
    token = bearer()
    url = f"{base}{GATEWAY_PATH}"
    # No proxy (it would hold the bearer) and no redirect (urllib would carry the bearer
    # to wherever a 30x points).
    handlers: list = [urllib.request.ProxyHandler({}), _NoRedirect()]
    if base.startswith("https://"):
        handlers.append(urllib.request.HTTPSHandler(context=_context(base)))
    opener = urllib.request.build_opener(*handlers)
    try:
        sid, _ = _post(opener, url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2024-11-05",
                                                "capabilities": {},
                                                "clientInfo": {"name": "awrun",
                                                               "version": "1"}}},
                       "", token, timeout)
        _post(opener, url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
              sid, token, timeout)
        _sid, text = _post(opener, url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": name, "arguments": arguments}},
                           sid, token, timeout)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        return code, {"error": f"the gateway answered HTTP {code}"}
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ApprovalUnavailableError(f"the approval plane did not answer at {url}: "
                                       f"{reason}") from exc
    reply = _jsonrpc(text)
    if reply.get("error"):
        err = reply["error"] if isinstance(reply["error"], dict) else {}
        raise ApprovalUnavailableError(f"the gateway could not run {name}: "
                                       f"{err.get('message') or reply['error']}")
    result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    chunks = [c.get("text", "") for c in result.get("content") or []
              if isinstance(c, dict) and c.get("type") == "text"]
    try:
        data = json.loads("".join(chunks) or "{}")
    except ValueError:
        return 502, {"error": f"{name} answered something that is not JSON"}
    if result.get("isError") or (isinstance(data, dict) and "error" in data
                                 and "approval_id" not in data):
        code = data.get("status") if isinstance(data, dict) else None
        return (int(code) if isinstance(code, int) and code >= 400 else 503), data
    return 200, data


def _detail(data: Any, status: int) -> str:
    if isinstance(data, dict):
        d = data.get("detail") or data.get("error")
        if d:
            return " ".join(str(d).split())[:300]
    return f"HTTP {status}"


def _raise_for(status: int, data: Any, what: str) -> None:
    if 200 <= status < 300:
        return
    msg = f"{what}: the approval plane answered {status}: {_detail(data, status)}"
    if status >= 500 or status in (401, 429):
        raise ApprovalUnavailableError(msg)
    raise ApprovalRefusedError(msg)


def page_url(approval_id: str) -> str:
    base = (os.getenv(PAGE_BASE_ENV) or DEFAULT_PAGE_BASE).strip().rstrip("/")
    return f"{base}/approvals?id={approval_id}"


# ── the requester's side ─────────────────────────────────────────────────────


def request(kind: str, specs: Any, summary: str, *, ttl_minutes: int = 60,
            agent: str = "awrun", options: Optional[dict] = None) -> dict:
    """Ask the workspace's owners/admins to approve exactly these runs (and queue
    options). Returns the plane's view (``approval_id``, ``status`` pending,
    ``card_id`` ...). Genesis picks the approvers from the caller's workspace; nothing
    here names one, and :func:`check_approver` decides whose decision awrun accepts."""
    action = action_for(kind)
    text = payload(kind, specs, options)
    if len(text) > PREVIEW_MAX:
        raise ApprovalRefusedError(f"these runs render to {len(text)} chars; an approval "
                                   f"covers at most {PREVIEW_MAX} (what a person can read). "
                                   f"Split them.")
    words = " ".join(str(summary or "").split())
    if not words or len(words) > SUMMARY_MAX or not words.isprintable():
        raise ApprovalRefusedError(f"summary is required: plain printable text, at most "
                                   f"{SUMMARY_MAX} chars")
    want = digest(text)
    # The gateway tool hashes the text itself and labels the call with its caller's
    # agent; Genesis picks the workspace from the bearer.
    del agent
    status, view = _tool("approval_request", {
        "action": action, "summary": words, "payload": text, "payload_digest": want,
        "ttl_minutes": int(ttl_minutes)})
    _raise_for(status, view, "request")
    if (not isinstance(view, dict) or not ID_RE.match(str(view.get("approval_id") or ""))
            or view.get("action") != action or view.get("payload_digest") != want):
        raise ApprovalUnavailableError("request: the approval plane answered without an approval "
                                  "for this action and payload")
    return view


def status(approval_id: str) -> dict:
    aid = str(approval_id or "").strip()
    if not ID_RE.match(aid):
        raise ApprovalRefusedError("not an approval id (apr-<16 hex>)")
    code, view = _tool("approval_status", {"approval_id": aid})
    _raise_for(code, view, "status")
    if not isinstance(view, dict) or view.get("approval_id") != aid:
        raise ApprovalUnavailableError("status: the approval plane answered about "
                                       "another approval")
    return view


def withdraw(approval_id: str, reason: str) -> bool:
    """Best effort: withdraw an ask nobody is waiting on any more, so the owner is not
    left approving something that will never run. True when the plane took it."""
    try:
        code, _view = _tool("approval_revoke", {
            "approval_id": approval_id, "reason": " ".join(str(reason).split())[:500]})
    except ApprovalRefusedError:
        return False
    return 200 <= code < 300


def wait(approval_id: str, *, poll_s: float = 10.0, timeout_s: Optional[float] = None,
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic,
         on_poll: Optional[Callable[[dict], None]] = None) -> dict:
    """Poll until the approval is decided (``approved``) or final, and return the view.

    A pending view comes back only when ``timeout_s`` ran out; the caller treats it as
    not approved. A few unreachable polls in a row are tolerated (a Genesis restart);
    more than MAX_POLL_FAILURES is ApprovalUnavailableError, never a pass."""
    deadline = None if timeout_s is None else clock() + float(timeout_s)
    failures, last = 0, None
    while True:
        try:
            last = status(approval_id)
            failures = 0
        except ApprovalUnavailableError:
            failures += 1
            if failures > MAX_POLL_FAILURES:
                raise
        if last is not None:
            if on_poll is not None:
                on_poll(last)
            if last.get("status") == "approved" or last.get("status") in FINAL:
                return last
        if deadline is not None and clock() >= deadline:
            if last is None:
                raise ApprovalUnavailableError("the approval plane never answered while waiting")
            return last
        sleep(max(1.0, float(poll_s)))


def _fanout_path() -> Optional[Path]:
    env = os.getenv(FANOUT_ENV, "").strip()
    if env:
        return Path(env)
    # packages/awrun/awrun/approval.py -> AitherOS/dev/tools/decision_fanout.py
    here = Path(__file__).resolve()
    if len(here.parents) > 3:
        cand = here.parents[3] / "dev" / "tools" / "decision_fanout.py"
        if cand.is_file():
            return cand
    return None


def _kick_discord() -> str:
    """Run the Discord fanout once, so the card's DM goes out now instead of on the
    lane's next tick (or never, when that tick is not scheduled on this host). The
    fanout owns the policy -- owner binding, digest rule, sent-state -- this only
    asks it to run."""
    if FANOUT_RUNNER is not None:
        rc, out = FANOUT_RUNNER()
    else:
        path = _fanout_path()
        if path is None:
            return "NOT SENT: no decision fanout on this host (set AWRUN_DECISION_FANOUT)"
        try:
            p = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            return f"NOT SENT: decision fanout did not run ({type(exc).__name__})"
        rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    tail = " ".join((out or "").split())[-200:]
    if rc == 0 and "delivered" in tail:
        return f"sent by the decision fanout: {tail}"
    if rc == 0:
        return f"not sent now (fanout: {tail})"
    return f"NOT SENT: decision fanout rc={rc}: {tail}"


def notify_surfaces(view: dict) -> dict:
    """Where the owner was told, as far as this host can see. Genesis mails each
    approver's own link and, for the platform operator's own ask, raises a decision
    card (no options: answering a card approves nothing). The harness daemon files
    that card in the awdesk inbox and posts it to the relay ``#decisions`` channel the
    Aither app reads; the Discord DM is the decision fanout's, kicked here."""
    aid = str(view.get("approval_id") or "")
    card = str(view.get("card_id") or "")
    out = {"page": page_url(aid),
           "email": "Genesis mails each approver's own link (Identity-verified address)"}
    if not card:
        missing = ("NOT REACHED: Genesis raised no decision card (it does only when the "
                   "requester is the platform operator AND an approver of the workspace)")
        out.update(app=missing, awdesk=missing, discord=missing)
        return out
    out["app"] = f"decision card {card}: relay #decisions (Aither app), page {out['page']}"
    out["awdesk"] = f"decision card {card}: awdesk inbox (fairy when it holds attention)"
    out["discord"] = _kick_discord()
    return out


# ── the submit side ──────────────────────────────────────────────────────────


def _people(view: dict) -> "tuple[str, str]":
    """(who decided, who asked) as the plane reports them, "" when absent."""
    decided = view.get("decided_by") if isinstance(view.get("decided_by"), dict) else {}
    asked = view.get("requested_by") if isinstance(view.get("requested_by"), dict) else {}
    return (str(decided.get("user_id") or "").strip(), str(asked.get("user_id") or "").strip())


def check_approver(view: dict) -> str:
    """The approver, when awrun accepts their decision; else ApprovalRefusedError.

    Genesis lets ANY trusted owner/admin of the asking workspace decide. A gated kind
    takes a host down, opens the perimeter or spends money, so awrun narrows that: the
    approver must be named in AWRUN_APPROVERS, or -- unset -- be the very account that
    asked (the owner approving his own ask from a fresh passkey session)."""
    approver, asked = _people(view)
    if not approver:
        raise ApprovalRefusedError("the approval names no human who decided it")
    named = [s.strip() for s in os.getenv(APPROVERS_ENV, "").split(",") if s.strip()]
    if named:
        if approver not in named:
            raise ApprovalRefusedError(f"{approver} decided it, and is not in {APPROVERS_ENV}")
    elif not asked or approver != asked:
        raise ApprovalRefusedError(f"{approver} decided it, not the account that asked "
                                   f"({asked or '?'}); name other approvers in {APPROVERS_ENV}")
    return approver


def consume(approval_id: str, kind: str, specs: Any, options: Optional[dict] = None) -> dict:
    """Spend ``approval_id`` on exactly these runs, once. Returns who decided it.

    Raises ApprovalRefusedError (or ApprovalUnavailableError) on anything else: a
    different kind, a spec or queue option that differs by one key, an approval that
    is pending, denied, expired, revoked or already consumed (replay), a decider awrun
    does not accept (checked BEFORE the spend), or an unreachable plane."""
    aid = str(approval_id or "").strip()
    if not ID_RE.match(aid):
        raise ApprovalRefusedError("not an approval id (apr-<16 hex>)")
    action = action_for(kind)
    text = payload(kind, specs, options)
    if len(text) > PREVIEW_MAX:
        raise ApprovalRefusedError("these runs exceed what any approval can cover")
    want = digest(text)
    before = status(aid)
    if before.get("status") != "approved":
        raise ApprovalRefusedError(f"approval {aid} is {before.get('status')}, not approved")
    check_approver(before)
    code, view = _tool("approval_consume", {
        "approval_id": aid, "action": action, "payload_digest": want,
        "workspace_id": os.getenv(WORKSPACE_ENV, "").strip()})
    _raise_for(code, view, f"approval {aid}")
    if not isinstance(view, dict):
        raise ApprovalUnavailableError(f"approval {aid}: unreadable answer")
    got_digest = str(view.get("payload_digest") or "")
    if (view.get("approval_id") != aid or view.get("status") != "consumed"
            or view.get("action") != action or not _DIGEST_RE.match(got_digest)
            or got_digest != want or view.get("preview_verified") is not True):
        raise ApprovalRefusedError(f"approval {aid}: the plane did not confirm it consumed "
                                   f"for {action} and this payload")
    try:
        approver = check_approver(view)
    except ApprovalRefusedError as exc:
        raise ApprovalRefusedError(f"approval {aid}: consumed, but {exc}") from exc
    if approver != _people(before)[0]:
        raise ApprovalRefusedError(f"approval {aid}: the decider changed while it was spent")
    # The JSON above is only as good as the URL that answered it. What grants is the
    # receipt Genesis SIGNED for this spend, checked with the pinned attestation key.
    receipt = verify_receipt(view.get("consume_receipt"), approval_id=aid, action=action,
                             payload_digest=want, approver=approver)
    decided = view.get("decided_by") if isinstance(view.get("decided_by"), dict) else {}
    return {"approval_id": aid, "action": action, "payload_digest": want,
            "approver": approver, "approver_name": str(decided.get("name") or ""),
            "requested_by": str(receipt.get("requested_by") or ""),
            "consumed_at": int(receipt["consumed_at"]),
            "receipt_kid": str(view["consume_receipt"].get("kid") or "")}


# ── the signed receipt ───────────────────────────────────────────────────────

#: lib/agent_approval/consume_receipt.py RECEIPT_TYPE / RECEIPT_VERSION.
RECEIPT_TYPE = "agent-approval-consume"
RECEIPT_VERSION = 1
#: A receipt older than this is not the spend that just happened (a replayed answer).
RECEIPT_MAX_AGE_S = 300
#: Clock skew tolerated between Genesis and this host.
RECEIPT_SKEW_S = 60
_SIG_RE = re.compile(r"^[0-9a-f]{128}$")


def _pinned_pubkey() -> str:
    """The platform attestation PUBLIC key, from where the platform pins it for every
    verifier: the file named by ``AWSTORAGE_ATTEST_PUBKEY_FILE``, judged by awstorage's
    own rule (never a raw key in env, never under ~/.aither). Never fetched from the
    Genesis being verified."""
    try:
        from awstorage.attest import _pubkey_and_why  # noqa: PLC0415
    except ImportError as exc:
        raise ApprovalUnavailableError(
            "awstorage is not installed: the attestation public key cannot be read, so "
            "no consume receipt can be verified (pip install awstorage)") from exc
    key, why = _pubkey_and_why()
    if not key:
        raise ApprovalUnavailableError(f"no pinned attestation key: {why}")
    return key


def verify_receipt(envelope: Any, *, approval_id: str, action: str, payload_digest: str,
                   approver: str) -> dict:
    """The verified receipt body, or ApprovalRefusedError. Ed25519 over the canonical
    JSON (decision_attest.canonical), by the PINNED key, for THIS approval, action,
    payload and decider, signed within RECEIPT_MAX_AGE_S."""
    if not isinstance(envelope, dict):
        raise ApprovalRefusedError(f"approval {approval_id}: the consume carries no signed "
                                   "receipt (only a spend Genesis signed counts)")
    receipt, sig = envelope.get("receipt"), str(envelope.get("sig") or "").lower()
    if (envelope.get("alg") != "ed25519" or not isinstance(receipt, dict)
            or not _SIG_RE.match(sig)):
        raise ApprovalRefusedError(f"approval {approval_id}: malformed consume receipt")
    pub = _pinned_pubkey()
    if str(envelope.get("kid") or "").lower() != pub[:16]:
        raise ApprovalRefusedError(f"approval {approval_id}: the receipt is signed by "
                                   "another key than the pinned attestation key")
    try:
        from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
            Ed25519PublicKey,
        )
    except ImportError as exc:
        raise ApprovalUnavailableError("`cryptography` is not installed: the consume "
                                       "receipt cannot be verified") from exc
    body = json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(bytes.fromhex(sig), body)
    except (InvalidSignature, ValueError) as exc:
        raise ApprovalRefusedError(f"approval {approval_id}: the consume receipt's "
                                   "signature does not verify") from exc
    want = {"v": RECEIPT_VERSION, "type": RECEIPT_TYPE, "approval_id": approval_id,
            "action": action, "payload_digest": payload_digest, "approver": approver}
    wrong = sorted(k for k, v in want.items() if receipt.get(k) != v)
    if wrong:
        raise ApprovalRefusedError(f"approval {approval_id}: the signed receipt is for "
                                   f"something else ({', '.join(wrong)} differ)")
    at = receipt.get("consumed_at")
    t = time.time()                    # never injectable: a clock seam is a bypass
    if (isinstance(at, bool) or not isinstance(at, int)
            or at > t + RECEIPT_SKEW_S or t - at > RECEIPT_MAX_AGE_S):
        raise ApprovalRefusedError(f"approval {approval_id}: the signed receipt is not "
                                   "for a spend that just happened (stale or replayed)")
    return receipt
