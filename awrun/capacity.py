"""Is the runner pool big enough, and can anything make it bigger?

WHY THIS EXISTS
---------------
awrun could already REDISTRIBUTE capacity -- ``runner_groups`` reserves a runner
for a critical workflow, and its own header says so: "Reserve CI capacity for a
workflow, because labels cannot." Reserving is zero-sum. When every runner is
busy it does not create a slot, it takes one from somebody else.

Measured 2026-08-24 on this org: 47 runs queued, 6 of 8 self-hosted runners busy,
and runs on the default branch being SUPERSEDED with ``jobs=0`` before they could
start -- CI was not slow, it was not completing. No amount of reserving fixes
that. The one thing that would (launch more runners) lived in a monorepo dev
tool that awrun cannot import and nothing in the queue could reach.

So: awrun owns the DECISION that more capacity is needed -- it is the only
component that can see the queue and the pool together -- and a host owns the
ACT of creating it, through :data:`awrun.plugins.PROVISION_CAPACITY`. That split
is deliberate. Creating capacity needs a cloud account, a credential, a quota and
a spend gate; a published queue must contain none of those, and CLAUDE.md's B2
rule makes "spends money" something a library must never do on its own.

A missing provider is a NORMAL, reportable state, not an error: ``awrun
capacity`` still measures and still tells you the pool is saturated. Knowing
that with no ability to act on it is strictly better than not knowing.
"""

from __future__ import annotations

from typing import Any, Dict, List

#: Queued runs per idle runner above which the pool counts as saturated.
#: 1.0 would call a single queued job an emergency; the queue is meant to have
#: depth. This fires when there is more than a full extra round of work waiting
#: for every free slot.
SATURATION_RATIO = 2.0


def assess(queued: int, runners: List[Dict[str, Any]],
           label: str = "aws") -> Dict[str, Any]:
    """Decide whether the pool needs to grow. PURE -- no API calls, no clock.

    ``runners`` is the org runner list as GitHub returns it. Only ONLINE runners
    carrying ``label`` are counted: an offline runner is not capacity, and a
    runner in another pool cannot take this work. Getting that wrong in the
    generous direction is what makes a saturation check quietly useless -- it
    reports headroom that does not exist.
    """
    pool = [r for r in runners
            if str(r.get("status", "")).lower() == "online"
            and any(str(lb.get("name", "")).lower() == label.lower()
                    for lb in (r.get("labels") or []))]
    idle = [r for r in pool if not r.get("busy")]
    total, free = len(pool), len(idle)
    # No pool at all is saturated by definition whenever there is work: every
    # queued run is waiting on a slot that does not exist. Treating 0/0 as
    # "healthy" is the vacuous pass this whole family exists to avoid.
    ratio = (queued / free) if free else (float(queued) if queued else 0.0)
    saturated = bool(queued) and (free == 0 or ratio > SATURATION_RATIO)
    want = 0
    if saturated:
        # Enough slots to bring the ratio back to target, never more than the
        # work that actually exists -- provisioning past the queue depth buys
        # idle instances that bill.
        need = int(queued / SATURATION_RATIO) - free
        want = max(1, min(need, queued))
    return {"queued": queued, "pool": total, "idle": free,
            "ratio": round(ratio, 2), "saturated": saturated,
            "want": want, "label": label}


def provision(want: int, dry_run: bool = True) -> Dict[str, Any]:
    """Ask the host to add ``want`` runners.

    Returns a dict always, so a caller never has to distinguish "no provider"
    from a crash -- both are reported, neither raises.
    """
    from . import plugins
    if want <= 0:
        return {"added": 0, "detail": "nothing to do"}
    if plugins.get(plugins.PROVISION_CAPACITY) is None:
        return {"added": 0, "provider": None,
                "detail": ("no capacity provider is registered. awrun decides "
                           "that more runners are needed; a host registers "
                           "plugins.PROVISION_CAPACITY to create them, because "
                           "that needs a cloud account, a quota and a spend "
                           "gate this package must not carry.")}
    res = plugins.call(plugins.PROVISION_CAPACITY, want, dry_run)
    if not isinstance(res, dict):
        return {"added": 0, "provider": "registered",
                "detail": "the provider returned no usable result"}
    res.setdefault("added", 0)
    res.setdefault("provider", "registered")
    return res


def self_test() -> int:
    """Prove the decision can still be wrong in both directions."""
    bad = 0

    def check(cond: bool, what: str) -> None:
        nonlocal bad
        print(f"  {'ok  ' if cond else 'FAIL'} {what}")
        if not cond:
            bad += 1

    def runner(name, busy=False, status="online", label="aws"):
        return {"name": name, "busy": busy, "status": status,
                "labels": [{"name": "self-hosted"}, {"name": label}]}

    a = assess(47, [runner(f"r{i}", busy=True) for i in range(6)])
    check(a["saturated"] and a["want"] > 0,
          "47 queued against 6 busy runners is saturated and wants more")

    a = assess(0, [runner("r1")])
    check(not a["saturated"] and a["want"] == 0,
          "an empty queue is never saturated, however small the pool")

    a = assess(1, [runner(f"r{i}") for i in range(4)])
    check(not a["saturated"],
          "one queued run against four idle runners is a working queue, not an "
          "emergency -- the ratio exists so depth is normal")

    a = assess(5, [runner("off", status="offline")])
    check(a["pool"] == 0 and a["saturated"],
          "an OFFLINE runner is not capacity; counting it would report headroom "
          "that does not exist")

    a = assess(5, [runner("other", label="awnix")])
    check(a["pool"] == 0 and a["saturated"],
          "a runner in another POOL cannot take this work and is not counted")

    a = assess(3, [runner(f"r{i}") for i in range(3)])
    check(a["want"] <= 3,
          "never wants more runners than there is work -- provisioning past the "
          "queue depth buys idle instances that bill")

    from . import plugins
    plugins.clear()
    r = provision(2)
    check(r["added"] == 0 and r.get("provider") is None,
          "no provider registered is a REPORTED state, not a crash")

    plugins.register(plugins.PROVISION_CAPACITY,
                     lambda want, dry: {"added": want, "detail": "fake"})
    check(provision(2)["added"] == 2, "a registered provider is used")

    # Silence the hook's own warning for this arm only. It is correct that a
    # broken provider logs a traceback -- but printing one inside a PASSING
    # self-test reads as a failure, and a self-test whose output looks like a
    # failure is one people stop reading.
    import logging
    _lg = logging.getLogger("awrun.plugins")
    _prev = _lg.disabled
    _lg.disabled = True
    plugins.register(plugins.PROVISION_CAPACITY,
                     lambda want, dry: (_ for _ in ()).throw(RuntimeError("boom")))
    r = provision(2)
    _lg.disabled = _prev
    check(r["added"] == 0,
          "a BROKEN provider degrades to 'added 0' rather than taking the "
          "caller down with it")
    plugins.clear()

    print()
    if bad:
        print(f"capacity self-test: {bad} case(s) FAILED")
        return 1
    print("capacity self-test: all cases behaved correctly")
    return 0
