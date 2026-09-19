"""A small client for a GPU lease door -- standard library only.

A lease door is any HTTP service that arbitrates one or more GPUs between
competing jobs. The wire contract is four JSON POSTs under one base URL:

    POST {base}/acquire    {"class", "vram_mb", "backend", "host_pref", "ttl_s",
                            "job_ref", "wait_s", "consumer_id"}
        200 {"token", "host", "backend_url", "granted_mb", "expires_at"}   granted
        409 {"detail": {"reason", "need_mb", "free_mb", "host", "lanes"}}  refused
    POST {base}/heartbeat  {"token"}
    POST {base}/release    {"token", "outcome"}  ->  200 {"released": true}

Three outcomes, and the difference between the last two is the whole point:

* **granted**  -- run the job, heartbeat while it runs, release exactly once.
* **refused**  (`LeaseRefused`)     -- the door said NO. Never run anyway; the
  caller queues the job and retries after `busy_retry_ms`.
* **unreachable** (`LeaseUnavailable`) -- nobody said yes and nobody said no.
  The caller decides; awrun's dispatcher runs the job unleased and says so
  loudly, because an arbiter being down must not take the queue down with it.

THERE IS NO DEFAULT DOOR, deliberately -- the same reason `AITHER_COMET_URL`
has none. Set `AITHER_GPU_LEASE_BASE` to one base URL, or several separated by
commas (tried in order; the first that answers wins).

A host that already has its own lease client does not need this module: any
object exposing `acquire`, `heartbeat`, `release`, `LeaseRefused` and
`LeaseUnavailable` can be handed to `dispatch_once(lease_client=...)` or
registered under `awrun.plugins.GPU_LEASE_CLIENT`.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_BASE_ENV = "AITHER_GPU_LEASE_BASE"

#: "Nothing usable came back": a dead socket, a timeout, a URL error, a body that
#: is not JSON -- and http.client's own failures (BadStatusLine, IncompleteRead,
#: RemoteDisconnected), which are NOT OSErrors and would otherwise escape acquire()
#: as a raw exception instead of the LeaseUnavailable the caller sorts on.
_TRANSPORT_ERRORS = (OSError, urllib.error.URLError, TimeoutError, ValueError,
                     http.client.HTTPException)

#: (url, body, timeout) -> (http_status, parsed_json_or_None). Injectable so a
#: test never opens a socket. Raises OSError/URLError when nothing answered.
PostFn = Callable[[str, "dict[str, Any]", float], "tuple[int, Any]"]


class LeaseRefused(RuntimeError):  # noqa: N818 - the door's own word
    """The door answered, and the answer was no."""

    def __init__(self, detail: Optional[dict[str, Any]] = None) -> None:
        self.detail: dict[str, Any] = detail or {}
        self.reason = str(self.detail.get("reason") or "refused")
        lanes = self.detail.get("lanes")
        self.lanes: dict[str, Any] = lanes if isinstance(lanes, dict) else {}
        card = self.lanes.get("cloud_card")
        self.card_id: Optional[str] = card.get("card_id") if isinstance(card, dict) else None
        wait = self.lanes.get("wait")
        eta = wait.get("eta_s") if isinstance(wait, dict) else None
        ok = isinstance(eta, (int, float)) and not isinstance(eta, bool) and eta > 0
        self.busy_retry_ms = int((eta if ok else 15) * 1000)
        super().__init__(
            f"gpu lease refused: {self.reason} (need {self.detail.get('need_mb')} MB, "
            f"free {self.detail.get('free_mb')} MB on {self.detail.get('host')})")

    def to_json(self) -> dict[str, Any]:
        return {"ok": False, "lease_refused": True, "reason": self.reason,
                "busyRetryMs": self.busy_retry_ms, "lanes": self.lanes,
                "card_id": self.card_id}


class LeaseUnavailable(RuntimeError):  # noqa: N818
    """No door answered. Nobody said no; nobody said yes."""


@dataclass
class Lease:
    token: str = ""
    host: str = ""
    backend_url: str = ""
    granted_mb: int = 0
    expires_at: float = 0.0
    actions_taken: list = field(default_factory=list)
    granted: bool = True
    door: str = ""


def bases() -> list[str]:
    raw = os.getenv(_BASE_ENV, "")
    return [b.strip().rstrip("/") for b in raw.split(",") if b.strip()]


def _ssl_context() -> ssl.SSLContext:
    """System trust by default; a private CA via a bundle PATH. Never weakened."""
    ca = os.environ.get("AWRUN_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca and os.path.isfile(ca):
        return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


def _http_post(url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Caller-Type": "platform"})
    kwargs: dict[str, Any] = {"timeout": timeout}
    if url.lower().startswith("https://"):
        kwargs["context"] = _ssl_context()
    try:
        with urllib.request.urlopen(req, **kwargs) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        # A 409 is an ANSWER, not a transport failure -- keep its body.
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None


def acquire(cls: str, vram_mb: int, *, backend: str = "", host_pref: str = "auto",
            ttl_s: int = 600, job_ref: str = "", wait_s: float = 0.0,
            consumer_id: str = "", door_bases: Optional[list[str]] = None,
            post: Optional[PostFn] = None) -> Lease:
    send = post if post is not None else _http_post
    body = {"class": cls, "vram_mb": int(vram_mb), "backend": backend,
            "host_pref": host_pref, "ttl_s": int(ttl_s), "job_ref": job_ref,
            "wait_s": float(wait_s), "consumer_id": consumer_id}
    errors: list[str] = []
    for base in (door_bases if door_bases is not None else bases()):
        try:
            status, payload = send(f"{base}/acquire", body, wait_s + 120)
        except _TRANSPORT_ERRORS as exc:
            errors.append(f"{base}: {type(exc).__name__}: {exc}")
            continue
        if status == 409:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not isinstance(detail, dict):
                detail = {"reason": str(payload)[:200]}
            raise LeaseRefused(detail)
        if status == 200 and isinstance(payload, dict) and payload.get("token"):
            return Lease(token=str(payload["token"]), host=str(payload.get("host") or ""),
                         backend_url=str(payload.get("backend_url") or ""),
                         granted_mb=int(payload.get("granted_mb") or 0),
                         expires_at=float(payload.get("expires_at") or 0),
                         actions_taken=list(payload.get("actions_taken") or []),
                         door=base)
        errors.append(f"{base}: HTTP {status} {str(payload)[:160]}")
    raise LeaseUnavailable(
        "no GPU lease door answered: "
        + ("; ".join(errors) or f"{_BASE_ENV} is not set, so there is no door to ask"))


def heartbeat(lease: Lease, *, post: Optional[PostFn] = None) -> bool:
    if not lease.token or not lease.door:
        return False
    send = post if post is not None else _http_post
    try:
        status, _ = send(f"{lease.door}/heartbeat", {"token": lease.token}, 15)
    except _TRANSPORT_ERRORS:
        return False  # the door's own TTL is the backstop; one missed beat is not fatal
    return status == 200


def release(lease: Lease, outcome: str = "done", *, post: Optional[PostFn] = None) -> bool:
    if not lease.token or not lease.door:
        return False
    send = post if post is not None else _http_post
    try:
        status, payload = send(f"{lease.door}/release",
                               {"token": lease.token, "outcome": outcome}, 120)
    except _TRANSPORT_ERRORS:
        return False  # say so rather than raise out of a `finally`; the TTL reclaims it
    return status == 200 and isinstance(payload, dict) and bool(payload.get("released"))
