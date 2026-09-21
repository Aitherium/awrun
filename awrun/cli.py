"""awrun CLI — submit, bump, watch and cancel prioritized runs.

Subcommand shape matches `adk decide` (`awdk/adk/decisions/cli.py`): one
`cmd_<name>(args, store) -> int` per subcommand, `build_parser()` assembles
them, `main()` dispatches. Exit codes: 0 ok, 1 the operation was refused
(e.g. bumping a closed run), 2 could not run at all (bad args, store error).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from awrun.store import CLOSED_STATUSES, KINDS, RunError, RunItem, RunStore, get_store

#: The file whose presence identifies a monorepo checkout. Chosen because it is
#: the very module the capacity provider needs, so the marker cannot drift away
#: from what it is a marker FOR.
_MONOREPO_MARKER = ("AitherOS", "dev", "tools", "awrun_capacity.py")


def _monorepo_root():
    """The checkout root above this file, or None when there is not one."""
    import pathlib as _pl
    here = _pl.Path(__file__).resolve()
    for parent in here.parents:
        if (parent.joinpath(*_MONOREPO_MARKER)).is_file():
            return parent
    return None


def _add_monorepo_root_to_syspath() -> None:
    """Idempotent. Silent when there is no monorepo -- that is the PyPI case."""
    import sys as _sys
    root = _monorepo_root()
    if root is not None and str(root) not in _sys.path:
        _sys.path.insert(0, str(root))


def _register_capacity_provider() -> None:
    """Register the host's capacity provisioner with awrun.plugins if available.

    This allows ``awrun capacity --add --execute`` to launch new CI runners on AWS.
    If the provisioner is not available (e.g., on a stranger's machine), the command
    still works and honestly reports that no provider is registered -- it is a normal
    state, not an error.
    """
    from . import plugins

    if plugins.get(plugins.PROVISION_CAPACITY) is not None:
        # Already registered (e.g., by the wrapper script)
        return

    # Put the monorepo root on sys.path if we are inside one. Without this the
    # import below fails with ModuleNotFoundError on the very host that owns the
    # AWS account -- measured 2026-08-24, `awrun capacity --add` reported "no
    # capacity provider is registered" against a real 40-job backlog, and the
    # same command with PYTHONPATH set provisioned correctly. A capacity plane
    # that only works when the caller knows an incantation is inert.
    #
    # Safe for a stranger: awrun ships to PyPI, the walk finds no marker, and the
    # existing fallback reports "no provider registered" -- a normal state for a
    # machine with no cloud account, not an error. No absolute path is baked in;
    # this package is public and a hardcoded root would be both a disclosure and
    # wrong on every other machine, our own Linux runners included.
    _add_monorepo_root_to_syspath()

    try:
        # Try to import from the monorepo dev tools
        # This succeeds only on the host; it fails gracefully on PyPI/strangers
        from AitherOS.dev.tools.awrun_capacity import (
            provision_capacity,
            reap_capacity,
        )
        plugins.register(plugins.PROVISION_CAPACITY, provision_capacity)
        # Registered in the SAME place as the provisioner on purpose: an
        # autoscaler that can grow and cannot shrink is a spend generator, and
        # wiring the two halves apart is how one of them stays unwired.
        plugins.register(getattr(plugins, "REAP_CAPACITY", "reap_capacity"),
                         reap_capacity)
    except (ImportError, ModuleNotFoundError) as exc:
        # Absent is FINE for a stranger: awrun ships to PyPI and still measures
        # saturation without a provisioner. But on a host that plainly HAS the
        # monorepo, a swallowed ImportError is how a provisioner stays dead for
        # weeks -- `awrun capacity --add --execute` exits 0 with added=0 and
        # reads exactly like "nothing needed provisioning". So keep the graceful
        # path and record WHY, for the one place that can tell the difference.
        plugins.PROVISION_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def _print_item(item: RunItem, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(item.to_dict(), indent=2))
        return
    age = _fmt_age(time.time() - item.created_at)
    label = (item.name or item.spec.get("workflow") or item.spec.get("task")
             or item.spec.get("entry") or item.spec.get("script") or item.kind)
    why = " ".join(f"{k}={v}" for k, v in sorted((item.lineage or {}).items()))
    print(f"{item.id}  [{item.status:>9}]  p={item.priority:<3}  {item.kind:<5}  "
          f"{age:>5} old  {label}" + (f"  <- {why}" if why else ""))


def _submit_comet_deploy_spec(args: argparse.Namespace) -> dict:
    spec: dict = {}
    if args.spec_json:
        try:
            spec.update(json.loads(args.spec_json))
        except json.JSONDecodeError as exc:
            raise RunError(f"--spec-json is not valid JSON: {exc}") from exc
    if args.service_name:
        spec["service_name"] = args.service_name
    if args.target:
        spec["target"] = args.target
    return spec


def _authorize_gated(kind: str, spec: dict) -> Optional[str]:
    """Phase 8 (generalised 2026-09-19): every kind in `authz.KIND_PERMISSIONS`
    is gated -- comet-deploy (money) and tunnel (perimeter). Returns an error
    string on denial, or None on success -- resolving, checking and auditing
    happen together here so a caller cannot accidentally submit without all
    three. Fails CLOSED at every step: no token, no resolution, no permission,
    or a failed audit write are ALL refusals, never a silent allow."""
    from awrun import authz

    permission = authz.KIND_PERMISSIONS[kind]
    token = os.getenv("AITHER_SESSION_BEARER", "").strip()
    subject_id = authz.resolve_session(token)
    if not subject_id:
        authz.audit(f"{kind}-denied", reason="no resolved session", spec=spec)
        return (f"{kind} requires a resolved awiam session -- set "
                "AITHER_SESSION_BEARER to a valid session token")

    decision = authz.check_permission(subject_id, permission)
    if not decision:
        authz.audit(f"{kind}-denied", subject=subject_id,
                     reason=decision.reason, spec=spec)
        return f"{kind} refused for {subject_id!r}: {decision.reason}"

    record = authz.audit(f"{kind}-submitted", subject=subject_id,
                          reason=decision.reason, spec=spec)
    if record is None:
        # Money-spend / perimeter path fails closed if it can't be recorded --
        # an unaudited spend is not something this package will let through
        # even though the authz decision itself was ALLOW.
        return f"{kind} refused: could not write the audit record (spend must be auditable)"
    return None


def _authorize_comet_deploy(spec: dict) -> Optional[str]:
    return _authorize_gated("comet-deploy", spec)


_TUNNEL_ACTIONS = ("expose", "retire")
_TUNNEL_PLANES = ("tunnel", "pages", "worker")


def _submit_tunnel_spec(args: argparse.Namespace) -> dict:
    """The `tunnel` RunItem spec, validated at submit so the queue never holds a
    row the executor has to reject: expose needs an origin, retire forbids one."""
    action = (getattr(args, "action", None) or "").strip()
    hostname = (getattr(args, "hostname", None) or "").strip().lower()
    origin = (getattr(args, "origin", None) or "").strip()
    plane = (getattr(args, "plane", None) or "tunnel").strip()
    if action not in _TUNNEL_ACTIONS:
        raise RunError(f"--action must be one of {_TUNNEL_ACTIONS}, got {action!r}")
    if not hostname or "." not in hostname or "/" in hostname or " " in hostname:
        raise RunError(f"--hostname must be a bare FQDN, got {hostname!r}")
    if plane not in _TUNNEL_PLANES:
        raise RunError(f"--plane must be one of {_TUNNEL_PLANES}, got {plane!r}")
    if action == "expose":
        if "://" not in origin:
            raise RunError("--origin is required for --action expose and must carry a "
                           "scheme (http://host:port or https://host:port)")
        spec = {"action": action, "hostname": hostname, "origin": origin, "plane": plane}
    else:
        if origin:
            raise RunError("--origin is forbidden for --action retire")
        spec = {"action": action, "hostname": hostname, "plane": plane}
    return spec


def _build_spec(args: argparse.Namespace) -> dict:
    """The run's spec from parsed arguments. PURE -- it authorizes nothing and
    writes nothing, so `apply` can compare a manifest to the queue before it
    decides there is anything to authorize. Raises RunError for a bad request."""
    spec: dict = {}
    kind = args.kind
    if kind == "ci":
        if not getattr(args, "workflow", None):
            raise RunError("--workflow is required for --kind ci")
        spec["workflow"] = args.workflow
        spec["ref"] = getattr(args, "ref", None) or "develop"
        inputs = {}
        for kv in getattr(args, "field", None) or []:
            if "=" not in kv:
                raise RunError(f"--field must be key=value, got {kv!r}")
            k, v = kv.split("=", 1)
            inputs[k] = v
        spec["inputs"] = inputs
    elif kind == "comet-deploy":
        spec = _submit_comet_deploy_spec(args)
        if not spec.get("service_name"):
            raise RunError("--service-name is required for --kind comet-deploy "
                           "(or set it in --spec-json)")
    elif kind == "tunnel":
        spec = _submit_tunnel_spec(args)
    elif kind in ("render", "artpack", "solve"):
        raise RunError(f"--kind {kind} is host-registered: its owner submits it with "
                       f"the spec that worker understands (RunStore.submit), not this CLI")
    elif kind == "flow":
        entry = getattr(args, "entry", None) or ""
        script = getattr(args, "script", None) or ""
        if bool(entry) == bool(script):
            raise RunError("--kind flow needs exactly one of --entry module:function "
                           "or --script path.py")
        if entry:
            spec["entry"] = entry
        else:
            spec["script"] = script
            spec["function"] = getattr(args, "function", None) or "main"
        if getattr(args, "budget_tokens", None):
            spec["budget_tokens"] = int(args.budget_tokens)
    else:
        if not getattr(args, "task", None):
            raise RunError("--task is required for --kind agent")
        if not getattr(args, "agent", None):
            raise RunError("--agent is required for --kind agent "
                           "(see `adk agents ls` for valid names)")
        spec["task"] = args.task
        spec["agent"] = args.agent
        if getattr(args, "adk_args", None):
            spec["adk_args"] = list(args.adk_args)
    image = getattr(args, "isolation_image", None)
    if image:
        spec["isolation"] = {"mode": "container", "image": image,
                             "runtime": getattr(args, "isolation_runtime", None) or "podman"}
    return spec


def _authorize(kind: str, spec: dict) -> Optional[str]:
    """None = allowed. Only kinds in `authz.KIND_PERMISSIONS` are gated; this is
    the ONE place a write path asks, so `apply` cannot submit what `submit` refuses."""
    from awrun import authz
    if kind not in authz.KIND_PERMISSIONS:
        return None
    return _authorize_gated(kind, spec)


def _constraints(args: argparse.Namespace) -> dict:
    """name / lineage / limits / egress from flags. Raises RunError."""
    lineage = {}
    for kv in getattr(args, "lineage", None) or []:
        if "=" not in kv:
            raise RunError(f"--lineage must be key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        lineage[k.strip()] = v.strip()
    limits = {}
    for flag, key in (("timeout_s", "timeout_s"), ("cpus", "cpus"), ("memory_mb", "memory_mb")):
        value = getattr(args, flag, None)
        if value is not None:
            limits[key] = value
    egress = None
    hosts = getattr(args, "egress_host", None) or []
    if getattr(args, "no_egress", False) and hosts:
        raise RunError("--no-egress and --egress-host contradict each other")
    if getattr(args, "no_egress", False) or hosts:
        egress = {"hosts": list(hosts)}
        for flag, key in (("egress_proxy", "proxy"), ("egress_network", "network")):
            if getattr(args, flag, None):
                egress[key] = getattr(args, flag)
    return {"name": getattr(args, "name", None) or "", "lineage": lineage or None,
            "limits": limits or None, "egress": egress}


def cmd_submit(args: argparse.Namespace, store: RunStore) -> int:
    try:
        spec = _build_spec(args)
        extra = _constraints(args)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    denial = _authorize(args.kind, spec)
    if denial is not None:
        print(f"ERROR: {denial}", file=sys.stderr)
        return 1
    try:
        item = store.submit(args.kind, spec, priority=args.priority,
                             paths=args.paths or [], **extra)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    _print_item(item, as_json=args.json)
    return 0


def _guarded(action: str, run_id: str = "", **fields) -> bool:
    """True = go ahead. Prints the refusal otherwise (exit 1 is the caller's)."""
    from awrun import trail
    denial = trail.guard(action, run_id, **fields)
    if denial is not None:
        print(f"ERROR: {denial}", file=sys.stderr)
    return denial is None


def cmd_export(args: argparse.Namespace, store: RunStore) -> int:
    from awrun import handoff
    if not _guarded("export", args.id):
        return 1
    try:
        out = handoff.export_run(store, args.id, Path(args.out), sign=not args.no_seal)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"exported {args.id} -> {out['bundle']}")
    print(f"  sha256     {out['sha256']}")
    print(f"  sealed by  {out['sealed_by'] or '(unsealed: import needs --sha256)'}")
    print(f"  journal    {'included' if out['journal'] else 'none'}")
    return 0


def cmd_import(args: argparse.Namespace, store: RunStore) -> int:
    from awrun import handoff
    if not _guarded("import", bundle=str(args.bundle)):
        return 1
    try:
        item = handoff.import_run(store, Path(args.bundle), expect_sha256=args.sha256 or "",
                                  expect_key=args.expect_key or "")
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    return 0


def cmd_suspend(args: argparse.Namespace, store: RunStore) -> int:
    if not _guarded("suspend", args.id):
        return 1
    try:
        item = store.suspend(args.id)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    if item.status == "running" and not args.json:
        print("  (running: asked its runner to stop; it parks at the next poll)")
    return 0


def cmd_resume(args: argparse.Namespace, store: RunStore) -> int:
    if not _guarded("resume", args.id):
        return 1
    try:
        item = store.resume(args.id)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    return 0


def cmd_egress_proxy(args: argparse.Namespace, store: RunStore) -> int:
    from awrun import egress
    host, _, port = args.listen.rpartition(":")
    if not host or not port.isdigit():
        print(f"ERROR: --listen must be host:port, got {args.listen!r}", file=sys.stderr)
        return 2
    return egress.serve(host, int(port), args.allow or [])


# ── apply: a manifest is the desired queue, and applying it twice is a no-op ──

API_VERSION = "awrun/v1"
_RUN_KEYS = ("task", "agent", "adk_args", "workflow", "ref", "inputs", "service_name",
             "target", "action", "hostname", "origin", "plane", "entry", "script",
             "function", "budget_tokens")


def _load_manifest(path: str) -> list:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    if path.endswith(".json") or text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RunError(f"{path}: not valid JSON: {exc}") from exc
        return data if isinstance(data, list) else [data]
    try:
        import yaml
    except ImportError:
        raise RunError(f"{path} is YAML and PyYAML is not installed: "
                       f"pip install 'awrun[yaml]', or write the manifest as JSON") from None
    try:
        return [doc for doc in yaml.safe_load_all(text) if doc is not None]
    except yaml.YAMLError as exc:
        raise RunError(f"{path}: not valid YAML: {exc}") from exc


def _desired(doc: object, index: int) -> dict:
    """One manifest document -> what the queue should hold. Raises RunError."""
    where = f"document {index + 1}"
    if not isinstance(doc, dict):
        raise RunError(f"{where}: must be a mapping")
    if doc.get("apiVersion") != API_VERSION or doc.get("kind") != "Run":
        raise RunError(f"{where}: needs apiVersion: {API_VERSION} and kind: Run")
    meta = doc.get("metadata") or {}
    body = doc.get("spec") or {}
    if not isinstance(meta, dict) or not isinstance(body, dict):
        raise RunError(f"{where}: metadata and spec must be mappings")
    name = meta.get("name") or ""
    if not name:
        raise RunError(f"{where}: metadata.name is required -- it is what apply converges on")
    unknown = sorted(set(body) - {"kind", "priority", "paths", "limits", "egress", "gpu",
                                  "isolation", "run"})
    if unknown:
        raise RunError(f"{where} ({name}): unknown spec keys {unknown}")
    run = body.get("run") or {}
    if not isinstance(run, dict):
        raise RunError(f"{where} ({name}): spec.run must be a mapping")
    stray = sorted(set(run) - set(_RUN_KEYS))
    if stray:
        raise RunError(f"{where} ({name}): unknown spec.run keys {stray}")
    ns = argparse.Namespace(kind=body.get("kind"), spec_json=None, field=None,
                            **{k: run.get(k) for k in _RUN_KEYS if k != "inputs"})
    if ns.kind not in KINDS:
        raise RunError(f"{where} ({name}): spec.kind must be one of {KINDS}")
    if run.get("inputs"):
        ns.field = [f"{k}={v}" for k, v in run["inputs"].items()]
    spec = _build_spec(ns)
    if body.get("isolation"):
        spec["isolation"] = body["isolation"]
    from awrun.store import (
        validate_egress,
        validate_gpu,
        validate_limits,
        validate_lineage,
        validate_name,
    )
    priority = body.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise RunError(f"{where} ({name}): spec.priority must be an integer")
    return {"name": validate_name(name), "kind": ns.kind, "spec": spec,
            "priority": priority, "paths": list(body.get("paths") or []),
            "lineage": validate_lineage(meta.get("lineage")),
            "limits": validate_limits(body.get("limits")),
            "egress": validate_egress(body.get("egress")),
            "gpu": validate_gpu(ns.kind, body.get("gpu"))}


_CONVERGED_FIELDS = ("kind", "spec", "paths", "lineage", "limits", "egress", "gpu")


def apply_one(want: dict, store: RunStore, *, dry_run: bool = False) -> tuple[str, str]:
    """(verdict, detail). Verdicts: created | unchanged | configured | replaced |
    refused. An authorization denial is `refused` and nothing is written."""
    have = store.find_by_name(want["name"])
    if have is not None:
        current = have.to_dict()
        same = all((current.get(f) or None) == (want[f] or None) for f in _CONVERGED_FIELDS)
        if same and have.priority == want["priority"]:
            return "unchanged", have.id
        if same:
            if not dry_run:
                from awrun import trail
                denial = trail.guard("apply", have.id, name=want["name"],
                                     priority=want["priority"])
                if denial is not None:
                    return "refused", denial
                store.bump(have.id, want["priority"])
            return "configured", f"{have.id} priority {have.priority} -> {want['priority']}"
        if have.status in ("claimed", "running"):
            return "refused", (f"{have.id} is {have.status} with a different spec; "
                               f"suspend or cancel it first")
    denial = _authorize(want["kind"], want["spec"])
    if denial is not None:
        return "refused", denial
    if dry_run:
        return ("replaced" if have is not None else "created"), "(dry run)"
    from awrun import trail
    denial = trail.guard("apply", have.id if have is not None else "", name=want["name"],
                         kind=want["kind"], lineage=want["lineage"])
    if denial is not None:
        return "refused", denial
    if have is not None:
        store.cancel(have.id)
    item = store.submit(want["kind"], want["spec"], priority=want["priority"],
                        paths=want["paths"], gpu=want["gpu"], name=want["name"],
                        lineage=want["lineage"], limits=want["limits"], egress=want["egress"])
    return ("replaced" if have is not None else "created"), item.id


def cmd_apply(args: argparse.Namespace, store: RunStore) -> int:
    # Validate EVERY document before writing ANY: half a manifest applied is a
    # state nobody wrote down.
    try:
        wanted = [_desired(doc, i) for i, doc in enumerate(_load_manifest(args.file))]
    except (RunError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    names = [w["name"] for w in wanted]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        print(f"ERROR: manifest names {dupes} more than once", file=sys.stderr)
        return 2
    rows, refused = [], False
    for want in wanted:
        try:
            verdict, detail = apply_one(want, store, dry_run=args.dry_run)
        except RunError as exc:
            verdict, detail = "refused", str(exc)
        refused = refused or verdict == "refused"
        rows.append({"name": want["name"], "verdict": verdict, "detail": detail})
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(f"run/{row['name']} {row['verdict']}  {row['detail']}")
    return 1 if refused else 0


def cmd_bump(args: argparse.Namespace, store: RunStore) -> int:
    try:
        item = store.bump(args.id, args.priority)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    return 0


def cmd_queue(args: argparse.Namespace, store: RunStore) -> int:
    statuses = None
    if not args.all:
        from awrun.store import OPEN_STATUSES
        statuses = list(OPEN_STATUSES)
    items = store.list(statuses=statuses, kind=args.kind)
    for kv in getattr(args, "lineage", None) or []:
        key, _, value = kv.partition("=")
        items = [i for i in items if (i.lineage or {}).get(key.strip()) == value.strip()]
    if args.json:
        print(json.dumps([i.to_dict() for i in items], indent=2))
        return 0
    if not items:
        print("(empty)")
        return 0
    for item in items:
        _print_item(item, as_json=False)
    return 0


def cmd_status(args: argparse.Namespace, store: RunStore) -> int:
    item = store.get(args.id)
    if item is None:
        print(f"ERROR: no such run: {args.id}", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    return 0


def cmd_cancel(args: argparse.Namespace, store: RunStore) -> int:
    if not _guarded("cancel", args.id):
        return 1
    try:
        item = store.cancel(args.id)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if item is not None and item.status not in CLOSED_STATUSES:
        # cancel() on an already-closed run returns it unchanged rather than
        # raising (idempotent cancel of something already done is fine); a
        # still-open status here means the rename genuinely failed.
        print(f"ERROR: could not cancel {args.id} (status: {item.status})", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    return 0


def _self_test() -> int:
    import tempfile

    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(td)
        a = store.submit("agent", {"task": "low"}, priority=1)
        b = store.submit("agent", {"task": "high"}, priority=5)
        queued = store.list(statuses=["queued"])
        check("higher priority sorts first regardless of submit order",
              queued[0].id == b.id and queued[1].id == a.id)

        bumped = store.bump(a.id, 10)
        queued2 = store.list(statuses=["queued"])
        check("bump() changes order", queued2[0].id == a.id and bumped.priority == 10)

        claimed = store.claim_next(worker_id="w1")
        check("claim_next() claims the highest-priority item", claimed is not None
              and claimed.id == a.id and claimed.status == "claimed")

        again = store.claim(a.id, worker_id="w2")
        check("a second claim on an already-claimed item returns None, not an error",
              again is None)

    # ── Phase 8: comet-deploy submit is trust-plane gated end to end ─────
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(td)

        no_token_args = argparse.Namespace(
            kind="comet-deploy", priority=0, paths=[], json=False,
            service_name="my-svc", target="docker", spec_json=None,
        )
        old_env = {k: os.environ.pop(k, None) for k in
                   ("AITHER_SESSION_BEARER", "AWRUN_COMET_DEPLOY_OPERATORS",
                    "AWRUN_TUNNEL_OPERATORS", "AWRUN_IAM_DIRECTORY", "AWRUN_AUDIT_LOG")}
        try:
            # Pointed at the scratch dir BEFORE the first submit: the denial below
            # is audited, and a self-test must not write the operator's real trail.
            iam_path = Path(td) / "iam.json"
            audit_path = Path(td) / "audit.log"
            os.environ["AWRUN_IAM_DIRECTORY"] = str(iam_path)
            os.environ["AWRUN_AUDIT_LOG"] = str(audit_path)

            rc = cmd_submit(no_token_args, store)
            check("comet-deploy submit with NO session token is refused (exit 1)", rc == 1)
            check("...and nothing was queued as a result",
                  len(store.list(statuses=["queued"], kind="comet-deploy")) == 0)
            check("...and the no-token denial went to the SCRATCH trail",
                  audit_path.exists() and "comet-deploy-denied" in audit_path.read_text())

            try:
                from awiam import Directory, Sessions, Subject
                identity_brick = True
            except ImportError:
                identity_brick = False
            if not identity_brick:
                # A stranger with `pip install awrun` has no identity brick. The
                # fail-closed half (no token -> refused) was judged above; the
                # resolved-session half cannot be, and says so rather than dying.
                print("  -- identity brick (awiam) absent: resolved-session cases not judged")
            else:
                directory = Directory(str(iam_path))
                directory.put(Subject(id="ops-dave", display="Dave"))
                token = Sessions(directory).issue("ops-dave")
                os.environ["AITHER_SESSION_BEARER"] = token or ""

                # Resolved session, but NOT in the operator allowlist.
                os.environ.pop("AWRUN_COMET_DEPLOY_OPERATORS", None)
                rc2 = cmd_submit(no_token_args, store)
                check("a resolved session with NO comet-deploy permission is still refused",
                      rc2 == 1)
                check("...and audit recorded the denial",
                      audit_path.exists() and "comet-deploy-denied" in audit_path.read_text())

                # Now grant the permission and retry the SAME submit.
                os.environ["AWRUN_COMET_DEPLOY_OPERATORS"] = "ops-dave"
                rc3 = cmd_submit(no_token_args, store)
                check("a resolved session WITH the operator role succeeds (exit 0)", rc3 == 0)
                queued = store.list(statuses=["queued"], kind="comet-deploy")
                check("the comet-deploy item actually reached the queue",
                      len(queued) == 1 and queued[0].spec.get("service_name") == "my-svc")
                check("audit recorded the ALLOWED submit too",
                      "comet-deploy-submitted" in audit_path.read_text())

                # ── kind=tunnel: gated the same way, by a DIFFERENT operator list ──
                tunnel_args = argparse.Namespace(
                    kind="tunnel", priority=0, paths=[], json=False,
                    action="expose", hostname="demo.example.com",
                    origin="http://aitheros-veil:3000", plane="tunnel",
                )
                os.environ.pop("AWRUN_TUNNEL_OPERATORS", None)
                rc5 = cmd_submit(tunnel_args, store)
                check("a comet-deploy operator is NOT thereby a tunnel operator (exit 1)",
                      rc5 == 1 and "tunnel-denied" in audit_path.read_text())
                os.environ["AWRUN_TUNNEL_OPERATORS"] = "ops-dave"
                rc6 = cmd_submit(tunnel_args, store)
                queued_t = store.list(statuses=["queued"], kind="tunnel")
                check("a tunnel operator's expose reaches the queue with the decided spec shape",
                      rc6 == 0 and len(queued_t) == 1 and queued_t[0].spec == {
                          "action": "expose", "hostname": "demo.example.com",
                          "origin": "http://aitheros-veil:3000", "plane": "tunnel"})
            bad = argparse.Namespace(kind="tunnel", priority=0, paths=[], json=False,
                                     action="retire", hostname="demo.example.com",
                                     origin="http://x:1", plane="tunnel")
            tunnels_before = len(store.list(statuses=["queued"], kind="tunnel"))
            check("retire with an origin is refused at submit (exit 2), not queued",
                  cmd_submit(bad, store) == 2
                  and len(store.list(statuses=["queued"], kind="tunnel")) == tunnels_before)
            bad2 = argparse.Namespace(kind="tunnel", priority=0, paths=[], json=False,
                                      action="expose", hostname="demo.example.com",
                                      origin="aitheros-veil:3000", plane="tunnel")
            check("expose without a scheme on the origin is refused (exit 2)",
                  cmd_submit(bad2, store) == 2)
        finally:
            for k, v in old_env.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

        # kind=agent/ci submits are UNCHANGED -- no token required at all.
        os.environ.pop("AITHER_SESSION_BEARER", None)
        agent_args = argparse.Namespace(
            kind="agent", priority=0, paths=[], json=False,
            task="do a thing", agent="local-5090", adk_args=[],
        )
        rc4 = cmd_submit(agent_args, store)
        check("kind=agent submit needs NO session token at all (light-touch by design)",
              rc4 == 0)

    # The capacity decision runs HERE, not only under its own import. A
    # self-test that nothing invokes is documentation, not enforcement:
    # `awrun self-test` is the command anyone actually types, so a rule not
    # reachable from it is a rule nobody has watched fail.
    from . import capacity as _cap
    if _cap.self_test() != 0:
        ok = False
    from . import surface as _sf
    if _sf.self_test() != 0:
        ok = False
    from . import confine as _cf
    if _cf.self_test() != 0:
        ok = False
    from . import egress as _eg
    if _eg.self_test() != 0:
        ok = False
    from . import trail as _tr
    if _tr.self_test() != 0:
        ok = False

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="awrun", description="A priority-aware queue for agentic runs and ad-hoc CI builds.")
    sub = parser.add_subparsers(dest="command")

    # --json is defined on a shared PARENT rather than the top-level parser:
    # argparse requires a top-level optional to precede the subcommand token
    # (`awrun --json queue`, not `awrun queue --json`), which is not how
    # anyone actually types it. A parent parser makes it legal in either
    # position and, crucially, an end-to-end smoke test caught this — the
    # unit self-tests call cmd_*() directly and never exercise the real
    # argv parser, so this bug was invisible to them.
    json_flag = argparse.ArgumentParser(add_help=False)
    json_flag.add_argument("--json", action="store_true", help="machine-readable output")

    submit = sub.add_parser("submit", help="queue a new run", parents=[json_flag])
    # choices come from store.KINDS, never a hand-kept list: measured 2026-09-19 the
    # CLI could submit 3 of 6 kinds and filter by 2 of 6 (AWK004/AWK005).
    submit.add_argument("--kind", choices=list(KINDS), required=True)
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--paths", nargs="*", default=[],
                         help="paths this run will touch (lease-awareness)")
    submit.add_argument("--task", help="[kind=agent] one-shot message for the ADK agent")
    submit.add_argument("--agent", help="[kind=agent] registered mesh agent name "
                                         "(see `adk agents ls`)")
    submit.add_argument("--adk-args", dest="adk_args", nargs="*", default=[],
                         help="[kind=agent] extra argv passed to `adk chat`")
    submit.add_argument("--workflow", help="[kind=ci] workflow file name")
    submit.add_argument("--ref", help="[kind=ci] git ref (default: develop)")
    submit.add_argument("--field", action="append",
                         help="[kind=ci] workflow_dispatch input as key=value, repeatable")
    submit.add_argument("--service-name", dest="service_name",
                         help="[kind=comet-deploy] AitherComet DeployRequest.service_name")
    submit.add_argument("--target", help="[kind=comet-deploy] deploy target "
                                          "(docker|k8s|systemd|podman|cloud-gpu|"
                                          "sovereign-iso|aitherzero-playbook|omninode)")
    submit.add_argument("--spec-json", dest="spec_json",
                         help="[kind=comet-deploy] full AitherComet DeployRequest body as "
                              "a JSON object; --service-name/--target override matching keys")
    submit.add_argument("--action", choices=list(_TUNNEL_ACTIONS),
                         help="[kind=tunnel] expose a hostname, or retire it")
    submit.add_argument("--hostname", help="[kind=tunnel] the public FQDN")
    submit.add_argument("--origin", help="[kind=tunnel] scheme://host:port (expose only)")
    submit.add_argument("--plane", choices=list(_TUNNEL_PLANES), default="tunnel",
                         help="[kind=tunnel] which plane serves the hostname")

    submit.add_argument("--entry", help="[kind=flow] module:function of the workflow")
    submit.add_argument("--script", help="[kind=flow] path to a workflow script")
    submit.add_argument("--function", help="[kind=flow] function in --script (default: main)")
    submit.add_argument("--budget-tokens", dest="budget_tokens", type=int,
                         help="[kind=flow] token budget for the whole workflow")
    submit.add_argument("--name", help="declared name; `apply` converges on it")
    submit.add_argument("--lineage", action="append",
                         help="why this run exists, key=value, repeatable "
                              "(intent, goal, expedition, flow, plan, notebook, parent_run)")
    submit.add_argument("--timeout-s", dest="timeout_s", type=int)
    submit.add_argument("--cpus", type=float)
    submit.add_argument("--memory-mb", dest="memory_mb", type=int)
    submit.add_argument("--egress-host", dest="egress_host", action="append",
                         help="allow outbound traffic to this host[:port] ONLY; repeatable")
    submit.add_argument("--no-egress", dest="no_egress", action="store_true",
                         help="no outbound traffic at all")
    submit.add_argument("--egress-proxy", dest="egress_proxy",
                         help="URL of an `awrun egress-proxy` the run must leave through")
    submit.add_argument("--egress-network", dest="egress_network",
                         help="INTERNAL container network the run is placed on")
    submit.add_argument("--isolation-image", dest="isolation_image",
                         help="run inside this container image (needed for cpus, "
                              "memory and egress to be enforceable)")
    submit.add_argument("--isolation-runtime", dest="isolation_runtime",
                         choices=["podman", "docker"])

    suspend = sub.add_parser("suspend", parents=[json_flag],
                              help="park a run: it keeps its id, priority, age and checkpoint")
    suspend.add_argument("id")
    resume = sub.add_parser("resume", parents=[json_flag],
                             help="put a suspended run back in the queue")
    resume.add_argument("id")

    apply = sub.add_parser("apply", parents=[json_flag],
                            help="converge the queue on a manifest (idempotent by name)")
    apply.add_argument("-f", "--file", required=True, help="manifest path, or - for stdin")
    apply.add_argument("--dry-run", dest="dry_run", action="store_true")

    export = sub.add_parser("export", parents=[json_flag],
                             help="bundle a SUSPENDED run (and its journal) to move it "
                                  "to another machine; closes the local copy")
    export.add_argument("id")
    export.add_argument("--out", default=".", help="directory for the bundle")
    export.add_argument("--no-seal", dest="no_seal", action="store_true",
                         help="do not sign the bundle even if a signing key exists")
    imp = sub.add_parser("import", parents=[json_flag],
                          help="take in an exported run; it lands suspended")
    imp.add_argument("bundle")
    imp.add_argument("--sha256", help="the digest the exporter printed")
    imp.add_argument("--expect-key", dest="expect_key",
                      help="the exporter's public signing key (hex)")

    proxy = sub.add_parser("egress-proxy",
                            help="allowlisting forward proxy: the only door out of a "
                                 "confined run")
    proxy.add_argument("--listen", default="127.0.0.1:3128")
    proxy.add_argument("--allow", action="append", help="host[:port] or *.suffix; repeatable")

    bump = sub.add_parser("bump", help="change a queued/claimed run's priority",
                           parents=[json_flag])
    bump.add_argument("id")
    bump.add_argument("--priority", type=int, required=True)

    queue = sub.add_parser("queue", help="list runs, highest priority first",
                            parents=[json_flag])
    queue.add_argument("--all", action="store_true", help="include done/failed/cancelled")
    queue.add_argument("--kind", choices=list(KINDS))
    queue.add_argument("--lineage", action="append",
                        help="only runs whose lineage has key=value, repeatable")

    status = sub.add_parser("status", help="one run in full", parents=[json_flag])
    status.add_argument("id")

    cancel = sub.add_parser("cancel", help="withdraw a queued/claimed run", parents=[json_flag])
    cancel.add_argument("id")

    groups = sub.add_parser(
        "groups", parents=[json_flag],
        help="reserved CI capacity: which workflows may never queue behind CI")
    groups.add_argument("action",
                        choices=["list", "show", "audit", "reserve", "release"])
    groups.add_argument("--org", default="Aitherium")
    groups.add_argument("--group", default="deploy")
    groups.add_argument("--workflow", default="",
                        help="Org/Repo/.github/workflows/x.yml@refs/heads/BRANCH "
                             "-- the ref is required and its absence is not "
                             "reported by the API")
    groups.add_argument("--runner", default="", help="ORG runner name")
    groups.add_argument("--label", default="",
                        help="label the workflow asks for (default: group name)")

    cap = sub.add_parser(
        "capacity", parents=[json_flag],
        help="is the runner pool big enough for the queue -- and add to it")
    cap.add_argument("--org", default="Aitherium")
    cap.add_argument("--label", default="aws",
                     help="the pool to judge (default: aws). A runner in "
                          "another pool cannot take this work.")
    cap.add_argument("--add", action="store_true",
                     help="ask the host's registered provider to add runners. "
                          "Without a provider this reports the gap and does "
                          "nothing -- awrun decides, a host provisions.")
    cap.add_argument("--reap", action="store_true",
                     help="give back idle runners once the queue is drained. "
                          "The scale-down half: --add alone only ever grows a "
                          "pool, and every instance it launches bills until "
                          "something hands it back.")
    cap.add_argument("--execute", action="store_true",
                     help="with --add, actually create capacity. Omit for a "
                          "dry run: this spends money and awrun will not do "
                          "that on its own.")

    surf = sub.add_parser(
        "surface", parents=[json_flag],
        help="CI state across every PUBLIC repo at once, and fan a dispatch "
             "across them (free hosted compute -- see surface.py on why this "
             "is dispatch and not hosting)")
    surf.add_argument("--org", default="Aitherium")
    surf.add_argument("--dispatch", default="",
                      help="workflow name, file or stem to run on every repo "
                           "that HAS it; repos that do not are named, never "
                           "silently skipped")
    surf.add_argument("--ref", default="main",
                      help="ref to dispatch against (default: main)")
    surf.add_argument("--execute", action="store_true",
                      help="with --dispatch, actually start the runs")

    sub.add_parser("self-test", help="run the built-in self-test")

    return parser


def cmd_groups(args: argparse.Namespace, store: "RunStore") -> int:
    """Reserved CI capacity, ranked on the same priority scale as runs.

    Takes no store: reservations live in GitHub, not the local queue. It is a
    subcommand of awrun anyway because it answers the same question the queue
    does -- what runs first when there is not enough to go round -- and putting
    it anywhere else guarantees the two rankings drift apart.
    """
    from . import runner_groups as rg

    try:
        if args.action == "list":
            rows = rg.list_groups(args.org)
            for g in rows:
                mark = "restricted" if g.get("restricted_to_workflows") else "OPEN"
                print(f"  {g['name']:20} id={g['id']:<4} {mark}")
            if not rows:
                print("  (no runner groups)")
            return 0

        if args.action == "show":
            d = rg.describe(args.org, args.group)
            print(json.dumps(d, indent=2) if args.json else
                  f"  {d['name']} (id={d['id']}) restricted="
                  f"{d['restricted_to_workflows']}\n"
                  f"  workflows: {d['selected_workflows'] or '(none)'}\n"
                  f"  runners:   {[r['name'] for r in d['runners']] or '(none)'}")
            return 0

        if args.action == "audit":
            # A group with runners and no restriction LOOKS reserved in every
            # listing and admits the whole org. That is the failure this
            # command exists to make visible, so it exits non-zero.
            problems = rg.audit(args.org)
            for line in problems:
                print(f"  NOT RESERVED: {line}")
            if not problems:
                print("  every non-default group with runners is restricted")
            return 1 if problems else 0

        if args.action == "reserve":
            d = rg.reserve(args.org, args.group, args.workflow, args.runner,
                           label=args.label or args.group)
            print(f"  reserved {args.runner} for {args.workflow}")
            print(f"  group {d['name']} now admits: {d['selected_workflows']}")
            return 0

        if args.action == "release":
            rg.release(args.org, args.group, args.runner)
            print(f"  released {args.runner} back to general availability")
            return 0
    except rg.RunnerGroupError as exc:
        print(f"awrun groups: {exc}", file=sys.stderr)
        return 1
    return 2


def cmd_capacity(args: argparse.Namespace, store: "RunStore") -> int:
    """Measure the queue against the pool, and optionally grow it.

    Exit 0 healthy, 1 saturated, 2 could not judge. Saturated is a real non-zero
    because it is an actionable state, and a run that cannot reach the API must
    never report a healthy pool -- that is the one wrong answer here.
    """
    from . import capacity as cap
    from .runner_groups import _api, org_runners

    try:
        runners = org_runners(args.org)
        # Queued WORKFLOW RUNS, not jobs: a run with no free runner never
        # creates its jobs at all (measured -- superseded runs report jobs=0),
        # so counting jobs would systematically under-report the very
        # saturation this command exists to see.
        runs = _api(f"/repos/{args.org}/AitherOS/actions/runs"
                    f"?status=queued&per_page=100")
        queued = int(runs.get("total_count", 0))
    except Exception as e:                      # noqa: BLE001
        print(f"capacity: could not reach the GitHub API ({e}) -- cannot judge",
              file=sys.stderr)
        return 2

    verdict = cap.assess(queued, runners, label=args.label)

    result = dict(verdict)
    if args.add:
        result["provision"] = cap.provision(verdict["want"],
                                            dry_run=not args.execute,
                                            label=args.label)

    if getattr(args, "reap", False):
        # Uses the SAME verdict["queued"] that --add reads, from the same call,
        # so the two halves can never disagree about whether work is waiting.
        from . import plugins as _pl
        reaper = _pl.get(getattr(_pl, "REAP_CAPACITY", "reap_capacity"))
        if reaper is None:
            result["reap"] = {"reaped": 0, "provider": None,
                              "detail": "no reap provider is registered"}
        else:
            result["reap"] = reaper(verdict["queued"], args.label,
                                    not args.execute)

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        v = verdict
        print(f"pool '{v['label']}': {v['idle']} idle of {v['pool']} online, "
              f"{v['queued']} queued (ratio {v['ratio']})")
        print(f"verdict: {'SATURATED' if v['saturated'] else 'healthy'}"
              + (f" -- wants {v['want']} more runner(s)" if v["want"] else ""))
        if args.add:
            p = result["provision"]
            print(f"provision: added={p['added']} -- {p['detail']}")
        if getattr(args, "reap", False):
            r = result["reap"]
            print(f"reap: reaped={r.get('reaped', 0)} -- {r.get('detail', '')}")

    # Asking for capacity and getting none because NOTHING CAN PROVISION is a
    # failure, and it must not exit 0. Measured: `awrun capacity --add --execute`
    # returned 0 with added=0 while the provider had silently failed to import,
    # so the caller could not distinguish "provisioned nothing, none needed" from
    # "cannot provision at all". A scheduler reading that exit code would report
    # healthy capacity forever.
    if args.add and result.get("provision", {}).get("provider", "") is None:
        from . import plugins as _pl
        why = getattr(_pl, "PROVISION_IMPORT_ERROR", None)
        print("ERROR: --add requested but NO capacity provider is registered"
              + (f" (import failed: {why})" if why else "")
              + ". Nothing was provisioned.", file=sys.stderr)
        return 2

    return 1 if verdict["saturated"] else 0


def cmd_surface(args: argparse.Namespace, store: "RunStore") -> int:
    """CI health across the public surface. Exit 0 all green, 1 gaps, 2 DEAD."""
    from . import surface as sf
    from .runner_groups import _api

    try:
        repos = _api(f"/orgs/{args.org}/repos?type=public&per_page=100")
        # Forks are somebody else's project. Reporting "no CI" on a vendored
        # upstream is noise, and noise is what gets a report ignored.
        repos = [r for r in repos if not r.get("fork")]
    except Exception as e:                      # noqa: BLE001
        print(f"surface: cannot reach the GitHub API ({e}) -- cannot judge",
              file=sys.stderr)
        return 2
    if not repos:
        print("surface: the org reported NO public repos -- refusing to call "
              "that a clean surface", file=sys.stderr)
        return 2

    rows, dispatched, missing = [], [], []
    for r in repos:
        name = r["name"]
        try:
            wfs = _api(f"/repos/{args.org}/{name}/actions/workflows").get(
                "workflows", [])
            runs = _api(f"/repos/{args.org}/{name}/actions/runs?per_page=5").get(
                "workflow_runs", [])
        except Exception:                       # noqa: BLE001
            rows.append({"repo": name, "state": "unknown", "workflows": 0,
                         "detail": "could not be read"})
            continue
        rows.append(sf.classify(name, wfs, runs))
        if args.dispatch:
            wid = sf.dispatchable(wfs, args.dispatch)
            if wid is None:
                missing.append(name)
            elif args.execute:
                try:
                    _api(f"/repos/{args.org}/{name}/actions/workflows/{wid}"
                         f"/dispatches", method="POST", body={"ref": args.ref})
                    dispatched.append(name)
                except Exception as e:          # noqa: BLE001
                    missing.append(f"{name} (dispatch failed: "
                                   f"{type(e).__name__})")
            else:
                dispatched.append(name)

    summary = sf.summarise(rows)
    if getattr(args, "json", False):
        print(json.dumps({"rows": rows, "summary": summary,
                          "dispatched": dispatched, "missing": missing},
                         indent=2))
    else:
        for row in sorted(rows, key=lambda x: (x["state"], x["repo"])):
            if row["state"] != "green":
                print(f"  {row['state']:<8} {row['repo']:<22} {row['detail'][:46]}")
        b = summary["by_state"]
        print("")
        print(f"{summary['total']} public repos: "
              + ", ".join(f"{v} {k}" for k, v in sorted(b.items())))
        if summary["no_ci"]:
            print(f"no CI at all: {', '.join(summary['no_ci'])}")
        if args.dispatch:
            verb = "dispatched" if args.execute else "would dispatch"
            print(f"{verb} '{args.dispatch}' to {len(dispatched)}: "
                  f"{', '.join(dispatched) or '-'}")
            if missing:
                print(f"no such workflow in {len(missing)}: "
                      f"{', '.join(missing[:10])}")
    return 0 if not (summary["no_ci"] or summary["red"]) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    # GENERATED repo-state intercept (gen_aw_doctor.py) -- do not edit
    try:
        from awgit import state as _aw_state
    except Exception:
        _aw_state = None
    if _aw_state is not None:
        _sv = locals().get("argv")
        if _aw_state.cli_banner(_sv if _sv is not None else __import__("sys").argv[1:]):
            return 0

    # Register the capacity provisioner if available (from the monorepo host)
    _register_capacity_provider()

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "self-test":
        from awrun import trail
        with trail.scratch_log():
            return _self_test()
    if args.command is None:
        parser.print_help()
        return 2

    store = get_store()
    handlers = {
        "submit": cmd_submit,
        "bump": cmd_bump,
        "queue": cmd_queue,
        "status": cmd_status,
        "cancel": cmd_cancel,
        "suspend": cmd_suspend,
        "resume": cmd_resume,
        "apply": cmd_apply,
        "export": cmd_export,
        "import": cmd_import,
        "egress-proxy": cmd_egress_proxy,
        "groups": cmd_groups,
        "capacity": cmd_capacity,
        "surface": cmd_surface,
    }
    return handlers[args.command](args, store)


if __name__ == "__main__":
    sys.exit(main())
