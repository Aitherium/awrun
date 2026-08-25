"""Extension seam: behaviour a HOST application supplies that awrun must not contain.

awrun ships publicly and has to run on a stranger's machine with nothing but the
standard library. Some behaviour it can usefully USE exists only inside a larger
system -- above all, the ability to CREATE CI capacity, which means an account,
a credential, a spend gate and a cloud SDK. None of that belongs in a queue.

Registration is explicit: awrun does NOT scan entry points at import, because a
dispatcher must not execute third-party code as a side effect of ``import
awrun``. The host calls :func:`register` when it is ready.

A hook never raises into its caller, and a missing hook is a NORMAL state -- the
default behaviour is always correct, merely less capable. That matters here more
than usual: the fallback for "cannot add capacity" is to say so plainly, and a
seam that instead raised would turn a saturated queue into a crashed one.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

#: (want: int, dry_run: bool) -> dict with at least {"added": int, "detail": str}
#: Create additional CI capacity. The host owns the cloud account, the spend gate
#: and the quota; awrun owns only the decision that more is needed.
PROVISION_CAPACITY = "provision_capacity"

#: Scale-DOWN hook. Declared beside PROVISION_CAPACITY because an
#: autoscaler with only the growth half is a spend generator: every instance
#: it launches bills until something hands it back.
REAP_CAPACITY = "reap_capacity"

#: Why the host provisioner failed to import, when it did. None means it was
#: never attempted or it succeeded. Recorded rather than swallowed: a silent
#: ImportError leaves `awrun capacity --add` exiting 0 having done nothing,
#: which is indistinguishable from "no capacity was needed".
PROVISION_IMPORT_ERROR = None

_HOOKS: Dict[str, Callable[..., Any]] = {}


def register(name: str, fn: Callable[..., Any]) -> None:
    """Register a host implementation. Last registration wins."""
    _HOOKS[name] = fn


def get(name: str) -> Callable[..., Any] | None:
    return _HOOKS.get(name)


def clear() -> None:
    """Drop every hook. For tests, so one case cannot leak into the next."""
    _HOOKS.clear()


def call(name: str, *a: Any, **kw: Any):
    """Invoke a hook, or return None if absent OR if it fails.

    Never raises: a host that registers a broken provisioner degrades to "no
    capacity was added", which is exactly the state awrun already knows how to
    report.
    """
    fn = _HOOKS.get(name)
    if fn is None:
        return None
    try:
        return fn(*a, **kw)
    except Exception:                     # noqa: BLE001 - see docstring
        logger.warning("awrun plugin %r failed; continuing without it", name,
                       exc_info=True)
        return None
