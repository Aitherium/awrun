"""What a run may use -- and the refusal to run it when that cannot be enforced.

A limit that is declared and not enforced is worse than no limit: the manifest
reads as confined and the process is not. So the rule here is one line --
**a run asking for something its runner cannot enforce FAILS, it never runs
unconfined.**

What can be enforced, and by what:

* ``limits.timeout_s`` -- by any built-in runner: it owns the child process.
* ``limits.cpus`` / ``limits.memory_mb`` -- only by a container runtime.
* ``egress.hosts == []`` -- a container with no network at all.
* ``egress.hosts == [...]`` -- a container on an INTERNAL network (no route out)
  whose only exit is an allowlisting proxy (`awrun egress-proxy`). The proxy
  environment variables are advice to the process; the internal network is the
  confinement. Both are required, and the network is checked to BE internal.

A run opts in with ``spec.isolation = {"mode": "container", "image": "...",
"runtime": "podman"}``. A host-registered runner declares what it enforces with
``run_fn.awrun_enforces = {"timeout_s", "cpus", ...}``.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional

from awrun.store import RunItem

#: Kinds whose built-in runner owns a local child process.
_LOCAL_PROCESS_KINDS = frozenset({"agent", "flow"})
_RUNTIMES = ("podman", "docker")

NetworkProbe = Callable[[str, str], Optional[bool]]


def demands(item: RunItem) -> set[str]:
    """The things this run asks a runner to enforce."""
    wanted = set((item.limits or {}).keys())
    if item.egress is not None:
        wanted.add("egress")
    return wanted


def isolation(item: RunItem) -> Optional[dict]:
    iso = item.spec.get("isolation") if isinstance(item.spec, dict) else None
    if not isinstance(iso, dict) or iso.get("mode") != "container":
        return None
    return iso


def network_is_internal(runtime: str, network: str) -> Optional[bool]:
    """True/False from the runtime itself; None when it could not be asked."""
    try:
        proc = subprocess.run([runtime, "network", "inspect", network,
                               "--format", "{{.Internal}}"],
                              capture_output=True, timeout=20, check=False,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    answer = (proc.stdout or "").strip().lower()
    if answer not in ("true", "false"):
        return None
    return answer == "true"


def enforcement_gap(item: RunItem, run_fn=None, *,
                    probe: NetworkProbe = network_is_internal) -> Optional[str]:
    """None when everything this run demands will be enforced; otherwise the
    sentence that goes on the FAILED run."""
    wanted = demands(item)
    if not wanted:
        return None

    declared = getattr(run_fn, "awrun_enforces", None)
    if declared is not None:
        missing = sorted(wanted - set(declared))
        return None if not missing else (
            f"the {item.kind!r} runner does not enforce {missing}; refusing to run unconfined")

    if item.kind not in _LOCAL_PROCESS_KINDS:
        return (f"kind={item.kind!r} does not run as a local process, so {sorted(wanted)} "
                f"cannot be enforced here; refusing to run unconfined")

    heavy = wanted - {"timeout_s"}
    if not heavy:
        return None
    iso = isolation(item)
    if iso is None:
        return (f"{sorted(heavy)} need a container: set spec.isolation = "
                f"{{'mode': 'container', 'image': ...}}; refusing to run unconfined")
    if not isinstance(iso.get("image"), str) or not iso["image"].strip():
        return "spec.isolation.image is required; refusing to run unconfined"
    runtime = iso.get("runtime", "podman")
    if runtime not in _RUNTIMES:
        return f"spec.isolation.runtime must be one of {_RUNTIMES}, got {runtime!r}"

    hosts = (item.egress or {}).get("hosts")
    if item.egress is not None and hosts:
        proxy = item.egress.get("proxy", "")
        network = item.egress.get("network", "")
        if not proxy or not network:
            return ("an egress allowlist needs egress.proxy (an `awrun egress-proxy` URL) and "
                    "egress.network (an internal network); refusing to run unconfined")
        internal = probe(runtime, network)
        if internal is None:
            return (f"could not confirm network {network!r} is internal "
                    f"({runtime} network inspect failed); refusing to run unconfined")
        if not internal:
            return (f"network {network!r} is NOT internal, so the proxy would be optional; "
                    f"refusing to run unconfined")
    return None


def wrap_argv(item: RunItem, argv: list[str]) -> list[str]:
    """`argv` as it must actually be started. Unchanged when the run asked for
    no container; call enforcement_gap() first -- this does not re-validate."""
    iso = isolation(item)
    if iso is None:
        return list(argv)
    limits = item.limits or {}
    out = [iso.get("runtime", "podman"), "run", "--rm",
           "--cap-drop=ALL", "--security-opt=no-new-privileges"]
    if "cpus" in limits:
        out += ["--cpus", str(limits["cpus"])]
    if "memory_mb" in limits:
        out += ["--memory", f"{int(limits['memory_mb'])}m"]
    if item.egress is not None:
        hosts = item.egress.get("hosts") or []
        if not hosts:
            out += ["--network", "none"]
        else:
            proxy = item.egress["proxy"]
            out += ["--network", item.egress["network"]]
            for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                out += ["-e", f"{name}={proxy}"]
            out += ["-e", "NO_PROXY=", "-e", "no_proxy="]
    for key, value in sorted((iso.get("env") or {}).items()):
        out += ["-e", f"{key}={value}"]
    return out + [iso["image"].strip(), *argv]


def timeout_for(item: RunItem, default: float) -> float:
    return float((item.limits or {}).get("timeout_s") or default)


def self_test() -> int:
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    def item(**kw) -> RunItem:
        kw.setdefault("kind", "agent")
        kw.setdefault("spec", {})
        return RunItem(id="r-2222", **kw)

    iso = {"isolation": {"mode": "container", "image": "img:1"}}
    check("no demands -> no gap", enforcement_gap(item()) is None)
    check("timeout alone needs no container",
          enforcement_gap(item(limits={"timeout_s": 5})) is None)
    check("cpus without a container is REFUSED",
          "refusing" in (enforcement_gap(item(limits={"cpus": 1})) or ""))
    check("egress on a kind with no local process is REFUSED",
          "refusing" in (enforcement_gap(item(kind="ci", egress={"hosts": []})) or ""))
    check("deny-all egress in a container is enforceable",
          enforcement_gap(item(spec=iso, egress={"hosts": []})) is None)
    allow = {"hosts": ["pypi.org"], "proxy": "http://10.9.0.1:3128", "network": "jail"}
    check("an allowlist with no proxy/network is REFUSED",
          "refusing" in (enforcement_gap(item(spec=iso, egress={"hosts": ["pypi.org"]})) or ""))
    check("an allowlist on a NON-internal network is REFUSED", "NOT internal" in (
        enforcement_gap(item(spec=iso, egress=allow), probe=lambda r, n: False) or ""))
    check("an allowlist on an unverifiable network is REFUSED", "could not confirm" in (
        enforcement_gap(item(spec=iso, egress=allow), probe=lambda r, n: None) or ""))
    check("an allowlist on an internal network passes",
          enforcement_gap(item(spec=iso, egress=allow), probe=lambda r, n: True) is None)

    def host_fn(_item):
        return 0, ""
    check("a host runner declaring nothing is REFUSED for a demanding run",
          enforcement_gap(item(kind="render", limits={"cpus": 2}),
                          _with(host_fn, set())) is not None)
    check("a host runner declaring cpus passes",
          enforcement_gap(item(kind="render", limits={"cpus": 2}),
                          _with(host_fn, {"cpus"})) is None)

    argv = wrap_argv(item(spec=iso, limits={"cpus": 1.5, "memory_mb": 512},
                          egress={"hosts": []}), ["adk", "chat", "a", "t"])
    check("deny-all wraps with --network none, caps dropped, limits set",
          argv[:5] == ["podman", "run", "--rm", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges"]
          and argv[argv.index("--cpus") + 1] == "1.5"
          and argv[argv.index("--memory") + 1] == "512m"
          and argv[argv.index("--network") + 1] == "none"
          and argv[-5:] == ["img:1", "adk", "chat", "a", "t"])
    argv2 = wrap_argv(item(spec=iso, egress=allow), ["x"])
    check("an allowlist wraps onto the internal network with the proxy set",
          argv2[argv2.index("--network") + 1] == "jail"
          and "HTTPS_PROXY=http://10.9.0.1:3128" in argv2)
    check("no isolation -> argv untouched", wrap_argv(item(), ["x", "y"]) == ["x", "y"])
    print("CONFINE SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _with(fn, enforces):
    def wrapped(item):
        return fn(item)
    wrapped.awrun_enforces = frozenset(enforces)
    return wrapped


if __name__ == "__main__":
    raise SystemExit(self_test())
