"""Agent deployments: ``awrun deploy``, ``awrun ps``, ``awrun rm``.

A run is one-shot and ends; a deployment is a standing agent somewhere that keeps
existing until somebody takes it down. They are different records with different
lifecycles, so deployments live in their OWN directory (``<awrun dir>/deployments``)
and never touch the run queue's status directories.

awrun does not deploy anything itself. The ACT belongs to a host-registered
backend (``plugins.AGENT_DEPLOYER``), exactly like capacity provisioning: awrun
ships publicly with the standard library only, and creating an agent needs an
account, a credential and a compiler that live in the host. awrun owns the
record, the plan it prints, and the honesty of what it reports.

Backend shape (duck-typed; every method but ``deploy`` is optional):

* ``name: str``
* ``deploy(agent, target, *, company=None) -> dict`` with ``ok: bool`` and, when
  ok, ``handle`` (the remote id) and optional ``endpoint``; when not ok,
  ``error`` and optional ``guidance``. ``detail`` (dict) is stored verbatim.
* ``status(record: dict) -> dict | None`` with ``status``, ``live: bool``
  (True only when the backend asked the remote side just now) and ``source``.
* ``teardown(record: dict) -> dict`` with ``ok``, ``remote_touched: bool`` and
  ``detail``; ``supported: False`` means "I have no teardown for this record",
  which is handled exactly like a backend with no ``teardown`` at all.

Storage follows store.py: one JSON file per record, written by atomic replace.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import plugins
from .store import RunError, runs_dir

TARGETS = ("managed", "local")

STATUS_DEPLOYED = "deployed"
STATUS_FAILED = "failed"
STATUS_REMOVED = "removed"

_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
_ID_RE = re.compile(r"^d-[" + _ID_ALPHABET + r"]{4,12}$")

#: What the record says when the backend cannot be asked. Printed, never hidden.
LAST_KNOWN = "last-known"


def deployments_dir() -> Path:
    """``<awrun dir>/deployments``; honours AITHER_AWRUN_DIR like the run store."""
    return runs_dir() / "deployments"


def _new_id() -> str:
    return "d-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(8))


@dataclass
class Deployment:
    """One standing agent awrun was asked to deploy."""

    id: str
    agent: str
    target: str
    created_at: float
    status: str
    backend: str = ""
    handle: Optional[str] = None
    endpoint: Optional[str] = None
    company: Optional[str] = None
    error: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0
    removed_at: Optional[float] = None
    #: Set by ``rm``: True only when the backend's own teardown ran and said so.
    remote_touched: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Deployment":
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416 - explicit set
        return cls(**{k: v for k, v in data.items() if k in known})


class DeploymentStore:
    """One file per deployment under ``root``; atomic replace on every write."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else deployments_dir()

    def _path(self, dep_id: str) -> Path:
        if not _ID_RE.match(dep_id):
            raise RunError(f"not a deployment id: {dep_id!r} (expected d-xxxxxxxx)")
        return self.root / f"{dep_id}.json"

    def put(self, dep: Deployment) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        dep.updated_at = time.time()
        path = self._path(dep.id)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(dep.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, dep_id: str) -> Deployment:
        path = self._path(dep_id)
        if not path.is_file():
            raise RunError(f"no such deployment: {dep_id}")
        return Deployment.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[Deployment]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.glob("d-*.json")):
            try:
                out.append(Deployment.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue  # a half-written foreign file is not ours to judge
        return sorted(out, key=lambda d: d.created_at)


def get_backend() -> Any:
    """The registered deployer backend, or None. Calls the factory once."""
    factory = plugins.get(plugins.AGENT_DEPLOYER)
    if factory is None:
        return None
    return factory()


def _backend_name(backend: Any) -> str:
    return str(getattr(backend, "name", "") or type(backend).__name__)


def plan(agent: str, target: str, *, company: Optional[str] = None,
         backend_registered: bool) -> dict[str, Any]:
    """What ``deploy`` would create. Pure: reads nothing remote, calls nothing."""
    creates = {
        "managed": "a managed agent compiled from the pack (remote; the backend's "
                   "deploy creates it, or updates the one it already deployed for "
                   "this pack)",
        "local": "nothing remote: the pack is resolved and compiled on this host",
    }[target]
    return {
        "agent": agent,
        "target": target,
        "company": company,
        "creates": creates,
        "record_dir": str(deployments_dir()),
        "backend_registered": backend_registered,
    }


def deploy(agent: str, target: str, *, company: Optional[str] = None,
           dry_run: bool = False, store: Optional[DeploymentStore] = None,
           backend: Any = None) -> tuple[int, dict[str, Any]]:
    """Deploy ``agent`` to ``target`` through the backend and record it.

    Returns ``(exit_code, payload)``: 0 deployed (or dry run), 1 the backend
    refused / failed (a ``failed`` record is still written so ``ps`` shows the
    attempt), 2 could not run (bad target, no backend registered).
    ``dry_run`` never resolves or calls a backend.
    """
    if target not in TARGETS:
        return 2, {"error": f"unknown target {target!r}; one of {list(TARGETS)}"}
    if dry_run:
        registered = plugins.get(plugins.AGENT_DEPLOYER) is not None
        return 0, {"dry_run": True,
                   "plan": plan(agent, target, company=company,
                                backend_registered=registered)}
    store = store or DeploymentStore()
    if backend is None:
        backend = get_backend()
    if backend is None:
        why = plugins.DEPLOYER_IMPORT_ERROR or "no host registered plugins.AGENT_DEPLOYER"
        return 2, {"error": f"no deployer backend: {why}",
                   "plan": plan(agent, target, company=company, backend_registered=False)}
    the_plan = plan(agent, target, company=company, backend_registered=True)
    dep = Deployment(id=_new_id(), agent=agent, target=target, created_at=time.time(),
                     status=STATUS_FAILED, backend=_backend_name(backend), company=company)
    try:
        result = backend.deploy(agent, target, company=company) or {}
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    dep.detail = dict(result.get("detail") or {})
    if result.get("ok"):
        dep.status = STATUS_DEPLOYED
        dep.handle = result.get("handle")
        dep.endpoint = result.get("endpoint")
    else:
        dep.error = str(result.get("error") or "backend returned ok=false")
        if result.get("guidance"):
            dep.detail["guidance"] = result["guidance"]
    store.put(dep)
    return (0 if dep.status == STATUS_DEPLOYED else 1), {"plan": the_plan,
                                                         "deployment": dep.to_dict()}


def ps(*, include_removed: bool = False, store: Optional[DeploymentStore] = None,
       backend: Any = None, probe: bool = True) -> list[dict[str, Any]]:
    """Every deployment record plus where its status came from.

    ``status_source`` is the backend's ``source`` when it answered, else
    ``last-known``; ``live`` is True only when the backend said it asked the
    remote side just now. A backend failure degrades to last-known, never hides.
    """
    store = store or DeploymentStore()
    if backend is None and probe:
        try:
            backend = get_backend()
        except Exception:  # noqa: BLE001 - ps must list even with a broken host
            backend = None
    status_fn = getattr(backend, "status", None) if backend is not None else None
    rows = []
    for dep in store.list():
        if dep.status == STATUS_REMOVED and not include_removed:
            continue
        row = dep.to_dict()
        row.update(current_status=dep.status, status_source=LAST_KNOWN, live=False)
        if callable(status_fn) and dep.status == STATUS_DEPLOYED:
            try:
                answer = status_fn(dep.to_dict())
            except Exception as exc:  # noqa: BLE001 - labelled, not hidden
                answer = None
                row["status_error"] = f"{type(exc).__name__}: {exc}"
            if answer:
                row["current_status"] = str(answer.get("status") or dep.status)
                row["status_source"] = str(answer.get("source") or row["backend"])
                row["live"] = bool(answer.get("live"))
        rows.append(row)
    return rows


def rm(dep_id: str, *, yes: bool = False, store: Optional[DeploymentStore] = None,
       backend: Any = None) -> tuple[int, dict[str, Any]]:
    """Tear a deployment down through the backend's own teardown, if it has one.

    Without ``yes`` nothing happens (exit 1, refused). With no teardown the
    record is marked removed locally and the payload says, in ``warning``, that
    the remote side was NOT touched. Exit 0 removed, 1 refused or teardown
    failed (record left as it was), 2 no such deployment.
    """
    store = store or DeploymentStore()
    try:
        dep = store.get(dep_id)
    except RunError as exc:
        return 2, {"error": str(exc)}
    if dep.status == STATUS_REMOVED:
        return 1, {"error": f"{dep_id} is already removed", "deployment": dep.to_dict()}
    if not yes:
        return 1, {"error": "refused: rm needs --yes",
                   "would": f"tear down {dep.agent} ({dep.target}, handle {dep.handle}) "
                            f"via {dep.backend or 'the registered backend'}",
                   "deployment": dep.to_dict()}
    if backend is None:
        try:
            backend = get_backend()
        except Exception:  # noqa: BLE001 - treated as "no teardown available"
            backend = None
    teardown = getattr(backend, "teardown", None) if backend is not None else None
    result: Optional[dict[str, Any]] = None
    if callable(teardown):
        try:
            result = teardown(dep.to_dict()) or {}
        except Exception as exc:  # noqa: BLE001 - a failed teardown leaves the record
            return 1, {"error": f"teardown failed: {type(exc).__name__}: {exc}",
                       "deployment": dep.to_dict()}
        if result.get("supported") is False:
            result = None
        elif not result.get("ok"):
            return 1, {"error": f"teardown failed: {result.get('error') or result}",
                       "deployment": dep.to_dict()}
    payload: dict[str, Any] = {}
    dep.status = STATUS_REMOVED
    dep.removed_at = time.time()
    if result is None:
        dep.remote_touched = False
        payload["warning"] = (
            f"REMOTE NOT TOUCHED: {dep.backend or 'no backend'} has no teardown for "
            f"target {dep.target!r}; only awrun's record was marked removed. "
            f"{('Handle ' + str(dep.handle) + ' still exists remotely.') if dep.handle else ''}"
        ).strip()
    else:
        dep.remote_touched = bool(result.get("remote_touched"))
        dep.detail["teardown"] = result.get("detail")
        if not dep.remote_touched:
            payload["warning"] = "REMOTE NOT TOUCHED: the backend's teardown reports " \
                                 "it changed nothing remote."
    store.put(dep)
    payload["deployment"] = dep.to_dict()
    return 0, payload
