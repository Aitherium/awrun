"""Host admission: do not start heavy work on a host that is already starving.

A queue that only asks "is a runner free?" will happily start three builds on a
box whose memory is already stalled, and the services sharing that box pay for
it. Before a heavy item runs, awrun reads Linux pressure-stall information
(``/proc/pressure``), the load average and ``MemAvailable``. When any is over
budget, the item goes back to ``queued/`` with a backoff -- "not now", never
"failed".

Budget: the defaults below, overridden by ``AWRUN_HOST_BUDGET`` (a JSON object
with any of the same keys); ``AWRUN_HOST_ADMISSION=off`` disables the check.
A host without ``/proc/pressure`` (Windows, macOS, an old kernel) cannot be
judged and is ADMITTED, and the verdict says so.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

#: Kinds that build, render, train or deploy: the ones that can starve a host.
HEAVY_KINDS = frozenset({"ci", "comet-deploy", "render", "artpack", "solve"})

DEFAULT_BUDGET: Dict[str, float] = {
    "memory_full_avg60_pct": 10.0,
    "cpu_some_avg60_pct": 60.0,
    "load_per_cpu": 3.0,
    "mem_available_min_gb": 12.0,
}


def budget() -> Dict[str, float]:
    out = dict(DEFAULT_BUDGET)
    raw = os.environ.get("AWRUN_HOST_BUDGET", "").strip()
    if raw:
        try:
            out.update({k: float(v) for k, v in json.loads(raw).items()})
        except (ValueError, TypeError, AttributeError):
            out = dict(DEFAULT_BUDGET)  # a malformed override keeps the defaults
    return out


def _avg60(path: Path, head: str) -> float:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(head + " "):
            for field in line.split()[1:]:
                k, _, v = field.partition("=")
                if k == "avg60":
                    return float(v)
    return 0.0


def sample(proc: Path = Path("/proc")) -> Dict[str, float]:
    mem_kb = 0
    for line in (proc / "meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            mem_kb = int(line.split()[1])
    load1 = float((proc / "loadavg").read_text(encoding="utf-8").split()[0])
    return {
        "memory_full_avg60_pct": _avg60(proc / "pressure" / "memory", "full"),
        "cpu_some_avg60_pct": _avg60(proc / "pressure" / "cpu", "some"),
        "load_per_cpu": load1 / max(os.cpu_count() or 1, 1),
        "mem_available_gb": mem_kb / (1024 * 1024),
    }


def decide(now: Dict[str, float], limits: Dict[str, float]) -> Tuple[bool, str]:
    for key, limit in limits.items():
        if key == "mem_available_min_gb":
            if now.get("mem_available_gb", limit) < limit:
                return False, f"MemAvailable {now['mem_available_gb']:.1f} GB < {limit:g} GB"
        elif key in now and now[key] > limit:
            return False, f"{key} {now[key]:.1f} > {limit:g}"
    return True, "within host budget"


def admit(kind: str, proc: Path = Path("/proc")) -> Tuple[bool, str, Optional[dict]]:
    """(run?, why, sample-or-None). Light kinds and unjudgeable hosts are admitted."""
    if kind not in HEAVY_KINDS:
        return True, f"{kind} is not a heavy kind", None
    if os.environ.get("AWRUN_HOST_ADMISSION", "").strip().lower() == "off":
        return True, "host admission disabled (AWRUN_HOST_ADMISSION=off)", None
    try:
        now = sample(proc)
    except (OSError, ValueError, IndexError) as exc:
        return True, f"host pressure unreadable ({exc}) -- admitted unjudged", None
    ok, why = decide(now, budget())
    return ok, why, now
