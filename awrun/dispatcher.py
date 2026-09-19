"""awrun dispatcher — claims the highest-priority queued run and executes it.

Deliberately NOT a new always-on daemon process. `decisions/store.py` already
proves a live daemon isn't required for a durable, pollable, cross-process
queue to work correctly — this is a single loop meant to run as a scheduled
task (matching `AitherOS/config/routines/*.yaml`'s existing pattern) or a
one-shot `--once` invocation. One fewer always-on service is one fewer thing
that can silently die without anyone noticing.

Three kinds, three handlers, one dispatch loop:

* `agent`        — Phase 1 (done). `adk chat <agent> "<task>"`.
* `ci`            — Phase 2 (done). `gh workflow run <workflow> --ref <ref> -f k=v...`.
* `comet-deploy`  — Phase 7. A thin POST to AitherComet's own `/deploy`, which
  already tenant-scopes and cost-gates on the cloud-gpu path. Trust-plane
  gating (Phase 8) happens at SUBMIT time in `cli.py`, not here — an item
  already sitting in the queue was already authorized.

Dispatch itself is priority-first across ALL kinds, not per-kind: the whole
point of this package was one queue an urgent item can jump, and a dispatcher
that only ever looked at one kind at a time would silently defeat that for
mixed workloads.

Phase 4 (awgit lease-awareness): before claiming a `kind=agent` item whose
`paths` collide with another actor's live awgit lease, the dispatcher skips
it for this cycle rather than claiming it and colliding. This is a SKIP, not
a refusal — the item stays queued and is retried next cycle, once the lease
clears.

Phase 5 (awrelay broadcast): every claim/start/finish is posted to a relay
channel via the same `RelayClient` `builtin_tools.py`'s own relay integration
uses, so queue state is visible without polling `awrun queue`. Best-effort —
a relay outage must never block a real run from dispatching.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from awrun.store import GPU_KINDS, RunItem, RunStore, get_store

logger = logging.getLogger(__name__)

#: Injectable so tests never spawn a real process or make a real HTTP call.
#: Returns (returncode, message) — message is stored in the run's `result`.
RunFn = Callable[[RunItem], "tuple[int, str]"]


# ─────────────────────────────────────────────────────────────────────────
# kind=agent (Phase 1)
# ─────────────────────────────────────────────────────────────────────────

def _build_agent_argv(item: RunItem) -> Optional[list[str]]:
    """Pure, so --self-test can pin the exact real invocation without
    spawning a process. `adk start` is an interactive chat launcher with no
    one-shot task flag -- confirmed live via `adk start --help`, which has no
    --task option at all. The real one-shot invocation is
    `adk chat <agent> "<message>"` (message given = one turn and exit;
    omitted = interactive loop), aimed at a REGISTERED mesh agent identity
    (`adk agents ls`), not a free-form description."""
    agent = item.spec.get("agent", "")
    if not agent:
        return None
    task = item.spec.get("task", "")
    extra = item.spec.get("adk_args") or []
    return ["adk", "chat", agent, task, *extra]


def _real_run_agent(item: RunItem) -> tuple[int, str]:
    argv = _build_agent_argv(item)
    if argv is None:
        return 1, "spec.agent is required (see `adk agents ls` for valid names)"
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=3600, check=False,
                               encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"could not run adk: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-4000:]  # tail only — a run's full log is not the queue's job


# ─────────────────────────────────────────────────────────────────────────
# kind=ci (Phase 2)
# ─────────────────────────────────────────────────────────────────────────

def _build_ci_argv(item: RunItem) -> Optional[list[str]]:
    """`gh workflow run <workflow> --ref <ref> -f key=value ...` — the exact
    hand-typed sequence this package's own README origin story was built to
    replace. Pure, same reason as _build_agent_argv."""
    workflow = item.spec.get("workflow", "")
    if not workflow:
        return None
    ref = item.spec.get("ref") or "develop"
    argv = ["gh", "workflow", "run", workflow, "--ref", ref]
    for key, value in (item.spec.get("inputs") or {}).items():
        argv += ["-f", f"{key}={value}"]
    return argv


def _real_run_ci(item: RunItem) -> tuple[int, str]:
    argv = _build_ci_argv(item)
    if argv is None:
        return 1, "spec.workflow is required"
    try:
        # gh workflow run only ENQUEUES the run and returns immediately — it
        # does not wait for the job to finish, so a generous timeout here is
        # about a hung `gh` CLI, not a slow workflow.
        proc = subprocess.run(argv, capture_output=True, timeout=120, check=False,
                               encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"could not run gh: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-4000:]


# ─────────────────────────────────────────────────────────────────────────
# kind=comet-deploy (Phase 7)
# ─────────────────────────────────────────────────────────────────────────

#: THERE IS NO DEFAULT, deliberately. This package is published to PyPI, and a
#: baked-in address is wrong there twice over: it names private infrastructure
#: to everyone who installs it, and it silently points a stranger's dispatch at
#: a host they do not own.
#:
#: The lesson the old default carried is kept and strengthened. It existed
#: because a container cannot reach a sibling service on `localhost` — it needs
#: the service's own address — so defaulting to loopback would "work" in a
#: developer shell and fail everywhere real. Refusing to guess enforces that
#: better than any default could: an unset value is a clear, immediate error
#: naming the variable, instead of a connection attempt against the wrong host.
_COMET_URL_ENV = "AITHER_COMET_URL"

#: Phase 10 — "execution gated, not design gated". A comet-deploy CAN provision
#: real, billed infrastructure (Hetzner/AWS/Vast.ai) via AitherComet's own
#: gpu_rental_gate/vps_provisioning_gate — CLAUDE.md's B2 rule (irreversible
#: AND destructive, including "spends money") means this package must not
#: make that call on its own. Default OFF; the owner flips this on when ready,
#: same shape as the dry-run guard `deploy_aws_runner.py` already uses.
_ALLOW_REAL_ENV = "AWRUN_ALLOW_REAL_COMET_DEPLOY"


def comet_deploy_enabled() -> bool:
    return os.getenv(_ALLOW_REAL_ENV, "").strip().lower() in ("1", "true", "yes")


def _comet_url() -> str:
    """The dispatcher base URL, or "" when unset. Never a guess."""
    return os.getenv(_COMET_URL_ENV, "").strip().rstrip("/")


def _real_run_comet_deploy(item: RunItem) -> tuple[int, str]:
    if not comet_deploy_enabled():
        return 1, (f"real comet-deploy dispatch is OFF (set {_ALLOW_REAL_ENV}=1 to enable) -- "
                    f"Phase 10 of the awrun plan requires an explicit owner go-ahead before this "
                    f"kind can provision real, billed infrastructure")
    if not item.spec.get("service_name"):
        return 1, "spec.service_name is required (AitherComet DeployRequest)"
    url = _comet_url()
    if not url:
        return 1, (f"{_COMET_URL_ENV} is not set. awrun ships no default dispatcher "
                   f"address on purpose — set it to the base URL of your deployment "
                   f"service, reachable from wherever awrun runs. Inside a container "
                   f"that is the service's own address, not loopback.")
    body = json.dumps(item.spec).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/deploy", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return 1, f"AitherComet /deploy returned {exc.code}: {exc.read()[:2000]!r}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 1, f"could not reach the deployment service at {url}: {exc}"
    except json.JSONDecodeError as exc:
        return 1, f"AitherComet /deploy returned non-JSON: {exc}"
    status = str(payload.get("status", "")).lower()
    # Judged against AitherComet's OWN DeploymentStatus enum, not a guess at it.
    # This list read ("success", "deployed", "running", "completed") and the enum
    # is {pending, in_progress, succeeded, failed, rolled_back, cancelled} -- so
    # NOT ONE of the four accepted strings could ever be returned, and every
    # comet-deploy was reported failed no matter what happened. Measured
    # 2026-09-05 on the first deploy that ever worked:
    #     awrun:  r-tjeb5s6p: failed
    #     Comet:  {"status": "succeeded", "message": "Successfully deployed ..."}
    #     podman: comet-lane-probe | Up 30 seconds
    # A wrapper that can only ever say "failed" is worse than no wrapper: it
    # would have kept saying failed after the deploy started working, which is
    # exactly what it did tonight for three fixes in a row.
    #
    # `in_progress`/`pending` are NOT success -- /deploy is answering that the
    # work is still going, and reporting that as done would be the same lie
    # pointed the other way. They stay a non-zero code so the run is not closed
    # out early.
    code = 0 if status == "succeeded" else 1
    return code, json.dumps(payload)[:4000]


# ─────────────────────────────────────────────────────────────────────────
# Phase 4 — awgit lease-awareness
# ─────────────────────────────────────────────────────────────────────────

def _lease_blocked(item: RunItem, *, actor: str) -> bool:
    """True when this item's `paths` collide with a PEER's live awgit lease.
    A peek, never an acquire -- claiming the lease is the dispatcher's job
    only once it actually starts the run, not while deciding what to try
    next. Degrades to "never blocked" if awgit is not importable, matching
    tool_guards.py's own "degrade, don't refuse" philosophy -- an inert
    guard is a known, documented gap, not a queue that silently jams."""
    if not item.paths:
        return False
    try:
        from awgit.leases import LeaseRegistry, is_guarded
    except Exception:
        return False
    guarded = [p for p in item.paths if is_guarded(p)]
    if not guarded:
        return False
    held = {lease.target: lease.actor for lease in LeaseRegistry().active_leases()}
    return any(held.get(p) not in (None, actor) for p in guarded)


def _acquire_paths(item: RunItem, *, actor: str) -> Optional[str]:
    """Best-effort acquire right before running, so the lease is actually
    HELD for the run's duration (the peek above only proved it was free a
    moment earlier). Returns an error message on genuine conflict — a race
    lost between the peek and here — or None on success/no-op."""
    if not item.paths:
        return None
    try:
        from awgit.leases import LeaseConflictError, LeaseRegistry, is_guarded
    except Exception:
        return None
    guarded = [p for p in item.paths if is_guarded(p)]
    if not guarded:
        return None
    try:
        LeaseRegistry().acquire(actor, guarded, reason=f"awrun:{item.id}")
    except LeaseConflictError as exc:
        return f"lease conflict acquiring {guarded}: {exc}"
    return None


# ─────────────────────────────────────────────────────────────────────────
# Phase 5 — awrelay broadcast (best-effort, never blocks a real run)
# ─────────────────────────────────────────────────────────────────────────

_RELAY_CHANNEL_ENV = "AWRUN_RELAY_CHANNEL"
_DEFAULT_RELAY_CHANNEL = "#awrun"



class _AgentLane:
    """The AGENT route. An agent may not use the human one.

    `/v1/channels/{c}/messages` authenticates a NICK; an agent posting there
    gets 403 "Requested nick does not match authenticated identity" -- for
    every nick, including none. Measured 2026-08-22: that is what awrun had
    been attempting, and `awrelay`'s CLI cannot reach an agent-only room at all.

    `/v1/agent/message` is agent-native and works. Proven the same day by round
    trip: POST -> 200 {"ok":true,...} and the marker read back out of #agents.

    It keeps `send_text` so the injected-client seam (and the Phase 5 self-test
    that feeds it a raising client) is unchanged.
    """

    def __init__(self, base: str, token: str, nick: str) -> None:
        self._base, self._token, self._nick = base, token, nick

    def send_text(self, *, channel: str, text: str, **_ignored) -> None:
        import httpx

        # Trust the internal CA; the fleet runs its own PKI so this never has
        # to be weakened.
        # 🚨 NO MONOREPO IMPORT. This package ships to PyPI, where
        # `lib.security.TLSConfig` does not exist -- aw-family.md's "client, not
        # lift": a package that imports the monorepo is a broken package that
        # reads as authoritative, which is worse than an absent one. The publish
        # publish-time boundary guard refuses the build on it, so awrun could
        # not be published at all while this import was here.
        #
        # It bought nothing a standard env var does not. The helper only returned
        # a CA-BUNDLE PATH, and httpx accepts exactly that in `verify=`; the fleet
        # sets the path, and off-fleet `True` uses the system store, which is the
        # correct default for a stranger. Same trust on-fleet, no coupling.
        verify: object = True
        _ca = os.environ.get("AWRUN_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
        if _ca and os.path.isfile(_ca):
            verify = _ca
        r = httpx.post(
            f"{self._base}/v1/agent/message",
            headers={"Authorization": f"Bearer {self._token}"},
            json={"channel": channel, "agent_nick": self._nick, "content": text},
            verify=verify, timeout=15,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"{r.status_code} {r.text[:160]}")


def _relay_client():
    """Mirrors builtin_tools.py's own `_relay_client()` exactly -- lazy
    import, same env vars, same "None means not configured" contract, so a
    broken/absent awrelay install degrades this to a no-op rather than
    taking dispatch down with it."""
    base = (os.getenv("AITHERRELAY_URL") or "https://127.0.0.1:8205").rstrip("/")
    # The live service is the BARE origin; the `/api/relay` suffix 404s. Tolerate
    # it rather than making every caller remember which spelling works.
    if base.endswith("/api/relay"):
        base = base[: -len("/api/relay")]
    token = os.getenv("AITHER_RELAY_TOKEN") or os.getenv("AITHER_SESSION_BEARER")
    if not token:
        bearer = Path.home() / ".aither" / "session-bearer"
        if bearer.exists():
            token = bearer.read_text(encoding="utf-8").strip()
    if not token:
        return None
    return _AgentLane(base, token, os.getenv("AITHER_AGENT_NAME", "awrun"))


def _broadcast(item: RunItem, event: str, *, client=None) -> None:
    """Best-effort. A relay outage must never fail a real run — this is
    visibility, not correctness."""
    client = client if client is not None else _relay_client()
    if client is None:
        return
    try:
        from awrelay.envelope import Kind
        label = item.spec.get("workflow") or item.spec.get("task") \
            or item.spec.get("service_name") or item.kind
        client.send_text(
            channel=os.getenv(_RELAY_CHANNEL_ENV, _DEFAULT_RELAY_CHANNEL),
            text=f"{item.id} [{item.kind}] {label}: {event}",
            kind=Kind.MESSAGE,
            payload={"id": item.id, "kind": item.kind, "event": event,
                     "priority": item.priority},
        )
    except Exception as exc:
        # Best-effort really means it — a malformed payload or a transient
        # relay error must not be allowed to fail the run it is describing.
        # Logged rather than swallowed outright, so a persistently broken
        # relay is discoverable without ever blocking a dispatch.
        # WARNING, not debug. `logger.debug` is exactly how this lane stayed
        # silent: measured 2026-08-22 EVERY broadcast since this code was
        # written had 404'd (its channel did not exist) and nobody saw one
        # line at default level. Best-effort must still be AUDIBLE when it is
        # PERMANENTLY broken, or "best effort" and "no effort" look identical.
        logger.warning("awrun broadcast for %s (%s) did not go out: %s",
                       item.id, event, exc)


# ─────────────────────────────────────────────────────────────────────────
# kind=render | artpack | solve — host-registered; awrun ships NO executor
# ─────────────────────────────────────────────────────────────────────────

def _host_registered_gap(kind: str, what: str) -> RunFn:
    """These kinds carry work awrun cannot do itself (a renderer, an art node, a
    solver -- each a large system with its own dependencies). The honest built-in
    is therefore a FAILURE that names the gap, never a pass: a queue that reported
    `done` for a render nobody rendered would be worse than no queue.

    A host supplies the real one: `dispatch_once(..., run_fns={**_RUN_FNS, kind: fn})`.
    """
    def _not_implemented(item: RunItem) -> tuple[int, str]:
        return 1, (f"NotImplemented: awrun has no built-in executor for kind={kind!r} "
                   f"({what}). This dispatcher was started without a host-registered "
                   f"handler for it -- start the worker that owns this kind, or pass "
                   f"run_fns={{{kind!r}: <fn>}} to dispatch_once()/run_forever(). "
                   f"Nothing was executed for {item.id}.")
    _not_implemented.awrun_not_implemented = True  # type: ignore[attr-defined]
    return _not_implemented


_real_run_render = _host_registered_gap("render", "a media render")
_real_run_artpack = _host_registered_gap("artpack", "a character art pack bake")
_real_run_solve = _host_registered_gap("solve", "a problem-solving session")


# ─────────────────────────────────────────────────────────────────────────
# GPU lease — ask the door before a gpu item touches the card
# ─────────────────────────────────────────────────────────────────────────

_LEASE_HEARTBEAT_S = 20.0
_LEASE_BACKOFF_FLOOR_S = 15.0
_LEASE_BACKOFF_CAP_S = 900.0


def _lease_client(explicit=None):
    """explicit argument > host-registered plugin > the built-in stdlib client."""
    if explicit is not None:
        return explicit
    from awrun import plugins
    hook = plugins.get(plugins.GPU_LEASE_CLIENT)
    if hook is not None:
        try:
            client = hook()
        except Exception as exc:  # noqa: BLE001 - a broken host hook must not stop dispatch
            logger.warning("awrun: the registered GPU lease client hook raised (%s); "
                           "using the built-in client", exc)
            client = None
        if client is not None:
            return client
    from awrun import gpu_lease
    return gpu_lease


def _lease_backoff_s(requeues: int, busy_retry_ms: object) -> float:
    """The door's own estimate when it gave one, doubled per trip already made,
    capped -- a run refused ten times must still be looked at again within
    fifteen minutes, because the card may have drained without telling anyone."""
    retry_ms = busy_retry_ms if isinstance(busy_retry_ms, (int, float)) \
        and not isinstance(busy_retry_ms, bool) else 0
    base = max(_LEASE_BACKOFF_FLOOR_S, retry_ms / 1000.0)
    return min(_LEASE_BACKOFF_CAP_S, base * (2 ** max(0, min(int(requeues or 0), 10))))


def _refusal_record(exc: Exception) -> dict:
    to_json = getattr(exc, "to_json", None)
    try:
        record = dict(to_json()) if callable(to_json) else {}
    except Exception:  # noqa: BLE001 - a refusal with a broken serializer is still a refusal
        record = {}
    record.setdefault("lease_refused", True)
    record.setdefault("reason", str(getattr(exc, "reason", "") or exc))
    return record


def _declared_exc(client, name: str) -> tuple:
    """The exception class a lease client declares under `name`, as a tuple an
    `except` can take -- empty when it declares none, or declares something
    that is not an exception class (an `except <non-class>` raises TypeError
    at the moment a real exception is propagating, which would strand the
    claimed item)."""
    declared = getattr(client, name, None)
    if isinstance(declared, type) and issubclass(declared, BaseException):
        return (declared,)
    return ()


def _is_refusal(exc: BaseException, client) -> bool:
    """Was this exception the door saying NO? By the client's declared class
    when it declares one, else by shape: a host client that raises its own
    `LeaseRefused` without exposing the class on the object must still be read
    as a refusal -- the alternative is running the job anyway, which is the
    one outcome a refusal may never have."""
    if isinstance(exc, _declared_exc(client, "LeaseRefused")):
        return True
    if type(exc).__name__ == "LeaseRefused":
        return True
    return bool(getattr(exc, "lease_refused", False))


def _is_unavailable(exc: BaseException, client) -> bool:
    return isinstance(exc, _declared_exc(client, "LeaseUnavailable")) \
        or type(exc).__name__ == "LeaseUnavailable"


class _Heartbeat:
    """Keeps a granted lease alive while the run function blocks."""

    def __init__(self, client, lease) -> None:
        self._client, self._lease = client, lease
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._beat, name="awrun-gpu-lease-beat",
                                        daemon=True)
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(_LEASE_HEARTBEAT_S):
            try:
                self._client.heartbeat(self._lease)
            except Exception as exc:  # noqa: BLE001 - the door's TTL is the backstop
                logger.warning("awrun: gpu lease heartbeat failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────
# dispatch
# ─────────────────────────────────────────────────────────────────────────

_RUN_FNS: dict[str, RunFn] = {
    "agent": _real_run_agent,
    "ci": _real_run_ci,
    "comet-deploy": _real_run_comet_deploy,
    "render": _real_run_render,
    "artpack": _real_run_artpack,
    "solve": _real_run_solve,
}


def dispatch_once(store: RunStore, *, worker_id: str,
                   run_fns: Optional[dict[str, RunFn]] = None,
                   relay_client=None, lease_client=None,
                   now_fn: Callable[[], float] = time.time) -> Optional[RunItem]:
    """Claim and run the single highest-priority queued item, of ANY kind.
    Returns the finished item, or None if there was nothing claimable —
    that is the ordinary "queue is empty" (or "everything claimable is
    lease-blocked") outcome, not an error.

    An item carrying a `gpu` request asks a GPU lease door first
    (`lease_client`, else the registered plugin, else `awrun.gpu_lease`):
    granted -> run, heartbeat, release exactly once; REFUSED -> the item goes
    back to queued/ with a backoff and is RETURNED with status "queued" (never
    failed, never run anyway); door unreachable -> run unleased, loudly."""
    fns = run_fns if run_fns is not None else _RUN_FNS
    actor = os.environ.get("AITHER_ACTOR") or f"awrun:{worker_id}"

    def _skip(item: RunItem) -> bool:
        return item.kind == "agent" and _lease_blocked(item, actor=actor)

    claimed = store.claim_next(worker_id=worker_id, skip=_skip, now=now_fn())
    if claimed is None:
        return None
    _broadcast(claimed, "claimed", client=relay_client)

    if claimed.kind == "agent":
        lease_error = _acquire_paths(claimed, actor=actor)
        if lease_error is not None:
            # Lost the race between the peek and the acquire -- put it back
            # rather than fail it; this is routine contention, not a defect.
            # (This used to call finish(), which only moves a RUNNING item, so
            # the claimed item was neither failed nor put back -- it was stranded.)
            return store.requeue(claimed.id, now_fn() + _LEASE_BACKOFF_FLOOR_S,
                                 wait={"reason": lease_error})

    run_fn = fns.get(claimed.kind)
    runnable = run_fn is not None and not getattr(run_fn, "awrun_not_implemented", False)

    # ── GPU lease: asked BEFORE start, so a refusal is claimed -> queued ────
    # Only when there is something to run: a lease taken for a handler that
    # can only say "not implemented" would evict real work for nothing.
    lease = None
    lease_state: Optional[dict] = None
    client = None
    if runnable and claimed.kind in GPU_KINDS and not claimed.gpu:
        # An item queued before `gpu` was required. It cannot be admitted (it
        # never said what it needs) and must not be dropped; it runs, and says so.
        logger.error("awrun: %s [%s] carries NO gpu request -- running it UNLEASED; "
                     "resubmit such work with gpu={class, vram_mb}", claimed.id, claimed.kind)
        lease_state = {"state": "unleased", "why": "item carries no gpu request"}
    elif runnable and claimed.gpu:
        gpu = claimed.gpu
        client = _lease_client(lease_client)
        try:
            lease = client.acquire(
                gpu.get("class"), int(gpu.get("vram_mb") or 0),
                backend=str(gpu.get("backend") or ""),
                host_pref=str(gpu.get("host_pref") or "auto"),
                ttl_s=int(gpu.get("ttl_s") or 600),
                job_ref=f"awrun:{claimed.id}", consumer_id=f"awrun:{worker_id}")
        # One arm, sorted by SHAPE rather than `except <the client's declared
        # class>`: a client that raises a refusal without exposing its class must
        # still be read as a refusal (_is_refusal), never as "door broke, run anyway".
        except Exception as exc:  # noqa: BLE001 - refused, unreachable, or a client that broke
            if _is_refusal(exc, client):
                # The door said NO. That is "not now" -- NEVER `failed`, and never
                # "run anyway". Back to queued/ with a backoff; the refusal (lanes,
                # decision-card id) rides along so the queue can show what it waits on.
                record = _refusal_record(exc)
                delay = _lease_backoff_s(claimed.requeues, record.get("busyRetryMs"))
                back = store.requeue(claimed.id, now_fn() + delay, wait=record)
                logger.warning("awrun: %s [%s] gpu lease REFUSED (%s) -- requeued, "
                               "retry in %.0fs", claimed.id, claimed.kind,
                               record.get("reason"), delay)
                if back is not None:
                    _broadcast(back, f"requeued: gpu lease refused ({record.get('reason')}), "
                                     f"retry in {delay:.0f}s", client=relay_client)
                return back
            # Nobody said no and nobody said yes. The arbiter being down must not
            # take the queue down -- run, but LOUDLY, and record it on the result.
            kind_of = "UNREACHABLE" if _is_unavailable(exc, client) \
                else f"client error {type(exc).__name__}"
            logger.error("awrun: %s [%s] gpu lease door %s -- running UNLEASED "
                         "(%s MB of class %s is NOT reserved): %s", claimed.id, claimed.kind,
                         kind_of, gpu.get("vram_mb"), gpu.get("class"), exc)
            lease_state = {"state": "unleased", "why": f"{kind_of}: {exc}"[:500]}
        else:
            lease_state = {"state": "granted", "host": getattr(lease, "host", ""),
                           "granted_mb": getattr(lease, "granted_mb", 0)}

    beat: Optional[_Heartbeat] = None
    outcome = "failed"
    code, message = 1, "the run did not complete"
    try:
        running = store.start(claimed.id)
        if running is None:
            # Lost to a cancel between claim and start -- nothing to run.
            outcome = "cancelled"
            return None
        _broadcast(running, "running" if lease_state is None
                   else f"running (gpu lease: {lease_state['state']})", client=relay_client)

        if run_fn is None:
            code, message = 1, f"no run handler registered for kind={running.kind!r}"
        else:
            if lease is not None:
                beat = _Heartbeat(client, lease)
                beat.start()
                # In memory only -- finish() re-reads the item from disk, so the
                # handler sees where the door placed it and nothing is persisted.
                running.spec["_gpu_lease"] = {
                    "granted": True, "host": getattr(lease, "host", ""),
                    "backend_url": getattr(lease, "backend_url", ""),
                    "granted_mb": getattr(lease, "granted_mb", 0)}
            try:
                code, message = run_fn(running)
            except Exception as exc:  # noqa: BLE001 - a raising handler is a FAILED run,
                # not an item stranded in running/ forever with its lease held.
                logger.exception("awrun: run handler for %s raised", running.id)
                code, message = 1, f"run handler raised {type(exc).__name__}: {exc}"[:4000]
        outcome = "done" if code == 0 else "failed"
    finally:
        if beat is not None:
            beat.stop()
        if lease is not None:
            # Exactly once, on done AND on fail AND on an interrupt.
            released = False
            try:
                released = bool(client.release(lease, outcome))
            except Exception as exc:  # noqa: BLE001 - never raise out of finally
                logger.error("awrun: gpu lease release for %s raised: %s", claimed.id, exc)
            if not released:
                logger.error("awrun: gpu lease for %s was NOT confirmed released "
                             "(the door's TTL will reclaim it)", claimed.id)
            if lease_state is not None:
                lease_state["released"] = released

    status = "done" if code == 0 else "failed"
    result = {"code": code, "message": message}
    if lease_state is not None:
        result["gpu_lease"] = lease_state
    finished = store.finish(running.id, status=status, result=result)
    if finished is not None:
        _broadcast(finished, status, client=relay_client)
    return finished


def run_forever(store: RunStore, *, worker_id: str, poll_interval: float = 5.0,
                 run_fns: Optional[dict[str, RunFn]] = None,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 max_iterations: Optional[int] = None, lease_client=None) -> int:
    """Loop dispatching one item at a time. `max_iterations` exists only for
    tests -- production callers leave it None and rely on the process being
    stopped externally (a scheduled task's own lifecycle, or a signal)."""
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        result = dispatch_once(store, worker_id=worker_id, run_fns=run_fns,
                               lease_client=lease_client)
        # A requeued item (gpu lease refused) is "nothing ran": sleep, or a queue
        # holding only refused work would spin on the door.
        if result is None or result.status == "queued":
            sleep_fn(poll_interval)
    return 0


def _self_test() -> int:
    import tempfile

    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    # ── argv builders (pure, no process spawned) ────────────────────────
    real_item = RunItem(id="r-test0000", kind="agent",
                         spec={"agent": "local-5090", "task": "verify the deploy"})
    argv = _build_agent_argv(real_item)
    check("agent invocation uses `adk chat`, never `adk start --task`",
          argv is not None and argv[:2] == ["adk", "chat"] and "--task" not in argv)
    check("the agent name and task land in the right argv positions",
          argv == ["adk", "chat", "local-5090", "verify the deploy"])
    check("a spec with no agent produces no argv (caught before spawning anything)",
          _build_agent_argv(RunItem(id="r-test0001", kind="agent", spec={"task": "x"})) is None)

    ci_item = RunItem(id="r-test0002", kind="ci",
                       spec={"workflow": "product-images.yml", "ref": "develop",
                             "inputs": {"tag": "nightly"}})
    ci_argv = _build_ci_argv(ci_item)
    check("ci invocation is `gh workflow run <file> --ref <ref>`",
          ci_argv is not None
          and ci_argv[:4] == ["gh", "workflow", "run", "product-images.yml"]
          and "--ref" in ci_argv and "develop" in ci_argv)
    check("ci inputs become repeated -f key=value flags",
          "-f" in ci_argv and "tag=nightly" in ci_argv)
    check("a ci spec with no workflow produces no argv",
          _build_ci_argv(RunItem(id="r-test0003", kind="ci", spec={})) is None)
    check("ci ref defaults to develop when omitted",
          "develop" in _build_ci_argv(
              RunItem(id="r-test0004", kind="ci", spec={"workflow": "x.yml"})))

    # ── Phase 10 dry-run default ─────────────────────────────────────────
    os.environ.pop(_ALLOW_REAL_ENV, None)
    check("comet-deploy is OFF by default (Phase 10: execution gated, not design gated)",
          comet_deploy_enabled() is False)
    code, msg = _real_run_comet_deploy(RunItem(id="r-test0005", kind="comet-deploy",
                                                spec={"service_name": "x"}))
    check("a comet-deploy dispatch refuses (not silently drops) while the gate is off",
          code == 1 and "AWRUN_ALLOW_REAL_COMET_DEPLOY" in msg)
    os.environ[_ALLOW_REAL_ENV] = "1"
    code2, msg2 = _real_run_comet_deploy(RunItem(id="r-test0006", kind="comet-deploy", spec={}))
    check("even with the gate on, a spec with no service_name refuses before any HTTP call",
          code2 == 1 and "service_name" in msg2)
    os.environ.pop(_ALLOW_REAL_ENV, None)

    # The old form of this asserted that a HARDCODED default was in-network. The
    # property that actually matters is stronger and survives publication: with
    # nothing configured, refuse and say so — never guess a host, and above all
    # never quietly guess loopback, which is the failure the original guarded.
    _saved_comet = os.environ.pop(_COMET_URL_ENV, None)
    os.environ[_ALLOW_REAL_ENV] = "1"
    code3, msg3 = _real_run_comet_deploy(RunItem(
        id="r-test0100", kind="comet-deploy", spec={"service_name": "x"}))
    check("with no dispatcher URL configured, it refuses and names the variable",
          code3 == 1 and _COMET_URL_ENV in msg3)
    check("the refusal never invents a host", "localhost" not in msg3 and "://" not in msg3)
    os.environ["AITHER_COMET_URL"] = "https://example.invalid:8125/"
    check("a configured URL is used verbatim, trailing slash trimmed",
          _comet_url() == "https://example.invalid:8125")
    os.environ.pop(_ALLOW_REAL_ENV, None)
    os.environ.pop(_COMET_URL_ENV, None)
    if _saved_comet is not None:
        os.environ[_COMET_URL_ENV] = _saved_comet

    # ── dispatch_once: priority across MIXED kinds ──────────────────────
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(td)
        low = store.submit("agent", {"task": "low", "agent": "local-5090"}, priority=1)
        high = store.submit("ci", {"workflow": "x.yml"}, priority=9)
        order: list[tuple[str, str]] = []

        def fake_agent(item: RunItem) -> tuple[int, str]:
            order.append(("agent", item.id))
            return 0, "ok"

        def fake_ci(item: RunItem) -> tuple[int, str]:
            order.append(("ci", item.id))
            return 0, "ok"

        fns = {"agent": fake_agent, "ci": fake_ci, "comet-deploy": lambda i: (1, "unused")}

        result1 = dispatch_once(store, worker_id="w1", run_fns=fns)
        check("dispatch_once() picks the higher-priority item regardless of KIND",
              result1 is not None and result1.id == high.id and result1.kind == "ci")

        result2 = dispatch_once(store, worker_id="w1", run_fns=fns)
        check("the second dispatch picks the remaining lower-priority item",
              result2 is not None and result2.id == low.id)
        check("dispatch order matches priority across kinds, not kind or submit order",
              order == [("ci", high.id), ("agent", low.id)])

        empty = dispatch_once(store, worker_id="w1", run_fns=fns)
        check("dispatch_once() returns None (not an error) on an empty queue", empty is None)

        # A failing run is recorded as failed, not silently dropped.
        boom = store.submit("agent", {"task": "boom", "agent": "x"}, priority=0)
        result3 = dispatch_once(store, worker_id="w1",
                                 run_fns={**fns, "agent": lambda i: (1, "it exploded")})
        check("a nonzero exit is recorded as failed, with the message kept",
              result3 is not None and result3.id == boom.id
              and result3.status == "failed" and result3.result["message"] == "it exploded")

        # No handler registered for a kind -> failed, never a silent no-op.
        orphan = store.submit("comet-deploy", {"service_name": "x"}, priority=0)
        result4 = dispatch_once(store, worker_id="w1", run_fns={"agent": fake_agent, "ci": fake_ci})
        check("a kind with no registered run_fn fails loudly rather than vanishing",
              result4 is not None and result4.id == orphan.id and result4.status == "failed")

    # ── Phase 4: lease-blocked items are SKIPPED, not claimed, not failed ─
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(td)
        blocked = store.submit("agent", {"task": "t", "agent": "x"}, priority=9,
                                paths=["some/leased/file.py"])
        free = store.submit("agent", {"task": "t2", "agent": "x"}, priority=1)

        # Patch via globals(), never `import awrun.dispatcher as dmod` --
        # when this file is run as `python -m awrun.dispatcher`, IT is
        # __main__, and a self-import loads a SECOND, separate module
        # object under the name `awrun.dispatcher`. Patching that copy's
        # attribute leaves __main__'s own globals (what dispatch_once's
        # `_skip` closure actually reads) untouched -- the patch silently
        # does nothing and the real bug (if any) hides behind it. Caught
        # live: this self-test passed under plain `import` and failed
        # under `-m`, for the harness, not the production code.
        orig_lease_blocked = globals()["_lease_blocked"]
        globals()["_lease_blocked"] = lambda item, actor: (
            item.id == blocked.id
        )
        try:
            fns2 = {"agent": lambda i: (0, "ok"), "ci": lambda i: (0, "ok"),
                    "comet-deploy": lambda i: (0, "ok")}
            result = dispatch_once(store, worker_id="w1", run_fns=fns2)
            check("a lease-blocked HIGH-priority item is skipped in favour of a free LOW one",
                  result is not None and result.id == free.id)
            still_queued = store.get(blocked.id)
            check("the skipped item stays QUEUED (retried next cycle), never failed or dropped",
                  still_queued is not None and still_queued.status == "queued")
        finally:
            globals()["_lease_blocked"] = orig_lease_blocked

    # ── GPU lease: refusal -> requeued (never failed); release exactly once ─
    from awrun import gpu_lease as _gl
    from awrun.store import RunError

    class _Door:
        """A lease client with no network: counts every call it receives."""
        LeaseRefused = _gl.LeaseRefused
        LeaseUnavailable = _gl.LeaseUnavailable

        def __init__(self, mode: str) -> None:
            self.mode, self.acquired, self.released = mode, [], []

        def acquire(self, cls, vram_mb, **kw):
            self.acquired.append((cls, vram_mb, kw))
            if self.mode == "refuse":
                raise _gl.LeaseRefused({"reason": "outranked", "need_mb": vram_mb,
                                        "free_mb": 400, "host": "gpu-0",
                                        "lanes": {"wait": {"eta_s": 42},
                                                  "cloud_card": {"card_id": "d-9"}}})
            if self.mode == "down":
                raise _gl.LeaseUnavailable("no GPU lease door answered")
            return _gl.Lease(token="tok", host="gpu-0", backend_url="http://backend",
                             granted_mb=vram_mb, door="http://door")

        def heartbeat(self, lease):
            return True

        def release(self, lease, outcome="done"):
            self.released.append(outcome)
            return True

    gpu_req = {"class": "interactive_media", "vram_mb": 18000}
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(td)
        try:
            store.submit("render", {"job": "x"})
            check("a render with NO gpu request is refused at submit", False)
        except RunError as exc:
            check("a render with NO gpu request is refused at submit, naming the fix",
                  "gpu" in str(exc) and not store.list())
        ran: list[str] = []

        def fake_render(item: RunItem) -> tuple[int, str]:
            ran.append(item.id)
            return 0, str(item.spec.get("_gpu_lease", {}).get("backend_url"))

        clock = [1000.0]
        item = store.submit("render", {"job": "x"}, gpu=gpu_req, priority=5)
        door = _Door("refuse")
        back = dispatch_once(store, worker_id="w1", run_fns={"render": fake_render},
                             lease_client=door, now_fn=lambda: clock[0])
        check("a REFUSED lease requeues the run -- status queued, NEVER failed",
              back is not None and back.id == item.id and back.status == "queued"
              and not store.list(statuses=["failed"]))
        check("a refused run never executes and never releases a lease it does not hold",
              ran == [] and door.released == [])
        check("the requeue carries the door's eta as backoff, the lanes and the card id",
              back is not None and back.not_before == 1042.0 and back.requeues == 1
              and (back.wait or {}).get("card_id") == "d-9" and back.claimed_by is None)
        check("a backed-off run is not claimable before not_before ...",
              store.claim_next(worker_id="w1", now=1041.0) is None)
        door_ok = _Door("grant")
        clock[0] = 1043.0
        # claim_next reads the wall clock; this item's not_before (1042) is long past.
        done = dispatch_once(store, worker_id="w1", run_fns={"render": fake_render},
                             lease_client=door_ok, now_fn=lambda: clock[0])
        check("... and runs once the door grants, on the backend the lease named",
              done is not None and done.status == "done" and ran == [item.id]
              and done.result["message"] == "http://backend"
              and done.result["gpu_lease"]["state"] == "granted")
        check("a successful run releases EXACTLY once, outcome=done",
              door_ok.released == ["done"])

        store.submit("render", {"job": "y"}, gpu=gpu_req)
        door_fail = _Door("grant")
        failed = dispatch_once(store, worker_id="w1", lease_client=door_fail,
                               run_fns={"render": lambda i: (1, "render crashed")})
        check("a failing run releases EXACTLY once, outcome=failed",
              failed is not None and failed.status == "failed"
              and door_fail.released == ["failed"])

        def raising(item: RunItem) -> tuple[int, str]:
            raise ValueError("handler blew up")

        store.submit("render", {"job": "z"}, gpu=gpu_req)
        door_raise = _Door("grant")
        raised = dispatch_once(store, worker_id="w1", lease_client=door_raise,
                               run_fns={"render": raising})
        check("a RAISING handler is a failed run with one release, not a stranded one",
              raised is not None and raised.status == "failed"
              and door_raise.released == ["failed"] and not store.list(statuses=["running"]))

        store.submit("render", {"job": "u"}, gpu=gpu_req)
        door_down = _Door("down")
        unleased = dispatch_once(store, worker_id="w1", lease_client=door_down,
                                 run_fns={"render": lambda i: (0, "ok")})
        check("an UNREACHABLE door runs the item unleased and records that on the result",
              unleased is not None and unleased.status == "done"
              and unleased.result["gpu_lease"]["state"] == "unleased"
              and door_down.released == [])

        store.submit("solve", {"spec": {}}, gpu={"class": "arc", "vram_mb": 8000})
        door_gap = _Door("grant")
        gap = dispatch_once(store, worker_id="w1", lease_client=door_gap)
        check("a gpu kind with no host handler FAILS naming the gap -- and takes no lease",
              gap is not None and gap.status == "failed"
              and "NotImplemented" in gap.result["message"] and door_gap.acquired == [])

    check("refusal backoff doubles per trip and is capped",
          _lease_backoff_s(0, None) == 15.0 and _lease_backoff_s(1, 42000) == 84.0
          and _lease_backoff_s(50, 42000) == _LEASE_BACKOFF_CAP_S)

    # ── Phase 5: broadcast is best-effort and never raises ───────────────
    class _BoomClient:
        def channels(self):
            raise RuntimeError("relay is down")

        def send_text(self, **kwargs):
            raise RuntimeError("relay is down")

    try:
        _broadcast(RunItem(id="r-test0009", kind="agent", spec={}), "claimed",
                   client=_BoomClient())
        check("a broken relay client during broadcast never raises out of dispatch", True)
    except Exception:
        check("a broken relay client during broadcast never raises out of dispatch", False)

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worker-id", default="dispatcher")
    ap.add_argument("--once", action="store_true", help="dispatch a single item and exit")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    store = get_store()
    if args.once:
        result = dispatch_once(store, worker_id=args.worker_id)
        if result is None:
            print("nothing to dispatch")
            return 0
        print(f"{result.id}: {result.status}")
        # "queued" = a gpu lease was refused and the run went back with a
        # backoff. That is the queue working, not a failed dispatch.
        return 0 if result.status in ("done", "queued") else 1

    return run_forever(store, worker_id=args.worker_id, poll_interval=args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
