"""Placement budget: on a fleet node, leave room for the services placed there.

A host that has joined a fleet as a placement node has been told which service
units belong on it. Some of them may not be running yet (the node agent starts
them one at a time, and only while enough memory stays free). A queue that looks
only at ``MemAvailable`` would hand that memory to a job, and the placed service
would then never start.

This module reads what the node agent keeps on disk and reports how much memory
is already spoken for:

    <dir>/agent.env      AITHER_PLACEMENT_HEADROOM_MB=<memory to keep free>
    <dir>/desired.json   {"advisory": bool, "present": [{"unit", "mem_mb"}, ...]}

``<dir>`` is ``AITHER_PLACEMENT_DIR`` or ``/etc/aither/placement``.

Decisions, and why:

* NODE-LOCAL. Nothing is asked of a controller, so a controller outage changes
  nothing here, and a job host needs no fleet credential.
* TIGHTEN-ONLY. The caller may raise its memory floor with this answer and must
  ignore a lower one. For that reason the manifest's signature is not checked:
  the file is root-written, and a forged value could only make a job wait.
* FAIL-OPEN on unreadable local state. A manifest that cannot be read reserves
  nothing and the answer SAYS the reserve is unknown; it never blocks every job.
* A host with no ``agent.env`` is not a joined node: ``read()`` returns None and
  the caller behaves exactly as it did before this module existed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Set

DEFAULT_ROOT = "/etc/aither/placement"

#: A systemd unit name that is safe to hand to ``systemctl`` as an argument.
_UNIT_RE = re.compile(r"^[A-Za-z0-9_@:.][A-Za-z0-9_@:.\-]{0,254}$")

NOTE_UNREADABLE = "placement manifest unreadable: placed-unit reserve unknown"
NOTE_UNASKED = "unit states could not be asked: every placed unit is counted as not running"
NOTE_NO_HEADROOM = "no placement headroom declared"

ActiveFn = Callable[[List[str]], Optional[Set[str]]]


def root_dir(root: Optional[os.PathLike] = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(os.environ.get("AITHER_PLACEMENT_DIR", "").strip() or DEFAULT_ROOT)


def systemctl_active(units: Iterable[str]) -> Optional[Set[str]]:
    """The subset of ``units`` that is active, from ONE ``systemctl is-active``.

    None when the question could not be asked or the answer does not line up
    one state per unit (the caller then counts every unit as not running).
    """
    names = list(units)
    if not names:
        return set()
    try:
        done = subprocess.run(
            ["systemctl", "is-active", *names],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    states = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
    if len(states) != len(names):
        return None
    return {name for name, state in zip(names, states) if state == "active"}


def _headroom_mb(agent_env: Path) -> Optional[int]:
    try:
        text = agent_env.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    value = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        if key.strip() == "AITHER_PLACEMENT_HEADROOM_MB":
            value = raw.strip().strip('"').strip("'")
    try:
        mb = int(value) if value else None
    except ValueError:
        return None
    return mb if mb is not None and mb >= 0 else None


def _positive_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def read(root: Optional[os.PathLike] = None,
         is_active: Optional[ActiveFn] = None) -> Optional[dict]:
    """What placement already claims on this host, or None when it is not a node.

    ``{"headroom_gb", "pending_gb", "pending_units", "note"}``: ``pending_gb`` is
    the measured memory of placed units that are not running; ``note`` is empty
    unless something could not be read (and then says what).
    """
    base = root_dir(root)
    agent_env = base / "agent.env"
    if not agent_env.is_file():
        return None
    notes: List[str] = []
    headroom = _headroom_mb(agent_env)
    if headroom is None:
        headroom = 0
        notes.append(NOTE_NO_HEADROOM)
    out = {"headroom_gb": headroom / 1024.0, "pending_gb": 0.0, "pending_units": [],
           "note": ""}

    desired = base / "desired.json"
    if not desired.exists():
        # Joined, nothing assigned yet (a pending node): only the headroom applies.
        out["note"] = "; ".join(notes)
        return out
    try:
        manifest = json.loads(desired.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("not an object")
        present = manifest.get("present", [])
        if not isinstance(present, list):
            raise ValueError("present is not a list")
    except (OSError, ValueError):
        notes.append(NOTE_UNREADABLE)
        out["note"] = "; ".join(notes)
        return out

    # An advisory manifest is a declaration of what already runs here; those
    # services are already inside MemAvailable and reserve nothing more.
    if manifest.get("advisory") is True:
        out["note"] = "; ".join(notes)
        return out

    wanted = {}
    for entry in present:
        if not isinstance(entry, dict):
            continue
        unit, mem_mb = entry.get("unit"), _positive_int(entry.get("mem_mb"))
        # A unit with no measured memory is never started by the node agent.
        if isinstance(unit, str) and _UNIT_RE.match(unit) and mem_mb is not None:
            wanted[unit] = mem_mb
    if wanted:
        active = (is_active or systemctl_active)(sorted(wanted))
        if active is None:
            active = set()
            notes.append(NOTE_UNASKED)
        pending = sorted(unit for unit in wanted if unit not in active)
        out["pending_units"] = pending
        out["pending_gb"] = sum(wanted[unit] for unit in pending) / 1024.0
    out["note"] = "; ".join(notes)
    return out
