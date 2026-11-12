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

On a host that has joined a fleet as a placement node (``placement_budget``),
the memory floor is RAISED to: the node's declared headroom + the memory of the
services placed there that are not running yet + what the item itself declares
(``limits.memory_mb``). It is only ever raised: a lower figure is ignored, and
``AWRUN_HOST_BUDGET`` cannot take the floor below it. On such a node a light
kind that declares its memory is judged too. Nothing is asked of a controller,
so a controller outage changes no verdict; local placement state that cannot be
read reserves nothing and the verdict says the reserve is unknown.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from awrun import placement_budget

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


def _declared_gb(memory_mb: object) -> float:
    """The item's declared memory in GB; 0 when it declared none (or nonsense)."""
    if isinstance(memory_mb, bool) or not isinstance(memory_mb, (int, float)):
        return 0.0
    return memory_mb / 1024.0 if memory_mb > 0 else 0.0


def admit(kind: str, proc: Path = Path("/proc"), memory_mb: Optional[float] = None,
          placement_root: Optional[Path] = None) -> Tuple[bool, str, Optional[dict]]:
    """(run?, why, sample-or-None). Light kinds and unjudgeable hosts are admitted.

    ``memory_mb`` is what the item declares it needs. It matters only on a host
    that is a fleet placement node; elsewhere the verdict is what it always was.
    """
    heavy = kind in HEAVY_KINDS
    job_gb = _declared_gb(memory_mb)
    light = (True, f"{kind} is not a heavy kind", None)
    if not heavy and job_gb <= 0:
        return light
    if os.environ.get("AWRUN_HOST_ADMISSION", "").strip().lower() == "off":
        if not heavy:
            return light
        return True, "host admission disabled (AWRUN_HOST_ADMISSION=off)", None
    placed = placement_budget.read(placement_root)
    if not heavy and placed is None:
        return light
    try:
        now = sample(proc)
    except (OSError, ValueError, IndexError) as exc:
        return True, f"host pressure unreadable ({exc}) -- admitted unjudged", None
    limits = budget()
    if placed is None:
        ok, why = decide(now, limits)
        return ok, why, now

    # A placement node: raise the memory floor, never lower it.
    floor = placed["headroom_gb"] + placed["pending_gb"] + job_gb
    raised = floor > limits.get("mem_available_min_gb", 0.0)
    if raised:
        limits["mem_available_min_gb"] = floor
    ok, why = decide(now, limits)
    if raised and not ok and why.startswith("MemAvailable"):
        why += (f" (placement floor: headroom {placed['headroom_gb']:.1f} GB"
                f" + placed units not running {placed['pending_gb']:.1f} GB"
                f" + this job {job_gb:.1f} GB)")
    if placed["note"]:
        why += f" [{placed['note']}]"
    return ok, why, now
