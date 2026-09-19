"""dispatcher x GPU lease: the three door outcomes and the one invariant.

* REFUSED      -> the item goes back to queued/ with a backoff. NEVER failed,
                  never run anyway, and no release for a lease never held.
* GRANTED      -> run; release EXACTLY once on done, on fail, and on a
                  handler that raises.
* UNREACHABLE  -> run unleased, loudly, and say so on the result.

A `_Door` here is a lease client with no network that counts every call, so
each assertion below is about what the dispatcher DID, not what it logged.
"""

from __future__ import annotations

import pytest
from awrun import dispatcher, gpu_lease
from awrun.dispatcher import (
    _LEASE_BACKOFF_CAP_S,
    _lease_backoff_s,
    dispatch_once,
    run_forever,
)
from awrun.store import STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING, RunItem, RunStore

GPU = {"class": "interactive_media", "vram_mb": 18000}


class _Door:
    LeaseRefused = gpu_lease.LeaseRefused
    LeaseUnavailable = gpu_lease.LeaseUnavailable

    def __init__(self, mode: str = "grant") -> None:
        self.mode = mode
        self.acquired: list = []
        self.released: list = []
        self.beats = 0

    def acquire(self, cls, vram_mb, **kw):
        self.acquired.append((cls, vram_mb, kw))
        if self.mode == "refuse":
            raise gpu_lease.LeaseRefused({
                "reason": "outranked", "need_mb": vram_mb, "free_mb": 400, "host": "gpu-0",
                "lanes": {"wait": {"eta_s": 42}, "cloud_card": {"card_id": "d-9"}}})
        if self.mode == "down":
            raise gpu_lease.LeaseUnavailable("no GPU lease door answered")
        return gpu_lease.Lease(token="tok", host="gpu-0", backend_url="http://backend",
                               granted_mb=vram_mb, door="http://door")

    def heartbeat(self, lease):
        self.beats += 1
        return True

    def release(self, lease, outcome="done"):
        self.released.append(outcome)
        return True


@pytest.fixture(autouse=True)
def _no_relay(monkeypatch):
    """Broadcast is best-effort over the network; these tests assert dispatch
    behaviour, so the relay lookup answers "none" and no socket is opened."""
    monkeypatch.setattr(dispatcher, "_relay_client", lambda: None)


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path)


def _ok(item: RunItem):
    return 0, "ok"


def _crash(item: RunItem):
    return 1, "render crashed"


def _raise(item: RunItem):
    raise ValueError("handler blew up")


# ── refusal ─────────────────────────────────────────────────────────────────

def test_refused_lease_requeues_never_fails(store):
    item = store.submit("render", {"job": "x"}, gpu=GPU)
    door = _Door("refuse")
    ran: list = []

    def fn(i):
        ran.append(i.id)
        return 0, "ok"

    back = dispatch_once(store, worker_id="w1", run_fns={"render": fn},
                         lease_client=door, now_fn=lambda: 1000.0)
    assert back is not None and back.id == item.id
    assert back.status == STATUS_QUEUED
    assert store.list(statuses=[STATUS_FAILED]) == []
    assert ran == []
    assert door.released == []  # never releases a lease it does not hold
    assert back.claimed_by is None
    assert back.requeues == 1
    assert back.not_before == 1042.0  # the door's own eta (42 s) is the backoff
    assert (back.wait or {}).get("card_id") == "d-9"
    assert (back.wait or {}).get("lease_refused") is True


def test_refused_item_is_not_claimable_until_not_before(store):
    store.submit("render", {"job": "x"}, gpu=GPU)
    dispatch_once(store, worker_id="w1", run_fns={"render": _ok},
                  lease_client=_Door("refuse"), now_fn=lambda: 1000.0)
    assert store.claim_next(worker_id="w1", now=1041.0) is None
    assert store.claim_next(worker_id="w1", now=1042.0) is not None


def test_refusal_backoff_doubles_per_trip_and_caps():
    assert _lease_backoff_s(0, None) == 15.0
    assert _lease_backoff_s(1, 42000) == 84.0
    assert _lease_backoff_s(50, 42000) == _LEASE_BACKOFF_CAP_S
    assert _lease_backoff_s(0, True) == 15.0  # bool is not a retry hint


def test_run_forever_sleeps_on_a_requeued_item_instead_of_spinning(store):
    store.submit("render", {"job": "x"}, gpu=GPU)
    slept: list = []
    run_forever(store, worker_id="w1", run_fns={"render": _ok}, sleep_fn=slept.append,
                max_iterations=3, lease_client=_Door("refuse"), poll_interval=0.5)
    assert slept == [0.5, 0.5, 0.5]


# ── release exactly once ────────────────────────────────────────────────────

def test_success_releases_exactly_once_with_outcome_done(store):
    item = store.submit("render", {"job": "x"}, gpu=GPU)
    door = _Door("grant")

    def fn(i):
        return 0, str(i.spec.get("_gpu_lease", {}).get("backend_url"))

    done = dispatch_once(store, worker_id="w1", run_fns={"render": fn}, lease_client=door)
    assert done is not None and done.id == item.id and done.status == "done"
    assert done.result["message"] == "http://backend"  # the handler saw the placement
    assert done.result["gpu_lease"]["state"] == "granted"
    assert done.result["gpu_lease"]["released"] is True
    assert door.released == ["done"]
    assert len(door.acquired) == 1
    cls, mb, kw = door.acquired[0]
    assert (cls, mb) == ("interactive_media", 18000)
    assert kw["job_ref"] == f"awrun:{item.id}" and kw["consumer_id"] == "awrun:w1"
    assert kw["ttl_s"] == 600 and kw["host_pref"] == "auto"


def test_failure_releases_exactly_once_with_outcome_failed(store):
    store.submit("render", {"job": "y"}, gpu=GPU)
    door = _Door("grant")
    failed = dispatch_once(store, worker_id="w1", run_fns={"render": _crash},
                           lease_client=door)
    assert failed is not None and failed.status == STATUS_FAILED
    assert door.released == ["failed"]


def test_raising_handler_is_failed_with_one_release_not_stranded(store):
    store.submit("render", {"job": "z"}, gpu=GPU)
    door = _Door("grant")
    raised = dispatch_once(store, worker_id="w1", run_fns={"render": _raise},
                           lease_client=door)
    assert raised is not None and raised.status == STATUS_FAILED
    assert "handler blew up" in raised.result["message"]
    assert door.released == ["failed"]
    assert store.list(statuses=[STATUS_RUNNING]) == []


def test_release_raising_is_logged_not_propagated(store):
    class _Sticky(_Door):
        def release(self, lease, outcome="done"):
            super().release(lease, outcome)
            raise OSError("door hung up")

    store.submit("render", {"job": "x"}, gpu=GPU)
    door = _Sticky("grant")
    done = dispatch_once(store, worker_id="w1", run_fns={"render": _ok}, lease_client=door)
    assert done is not None and done.status == "done"
    assert door.released == ["done"]  # attempted once, not retried into a double release
    assert done.result["gpu_lease"]["released"] is False


# ── unreachable ─────────────────────────────────────────────────────────────

def test_unreachable_door_runs_unleased_and_says_so(store, caplog):
    store.submit("render", {"job": "u"}, gpu=GPU)
    door = _Door("down")
    with caplog.at_level("ERROR", logger=dispatcher.__name__):
        out = dispatch_once(store, worker_id="w1", run_fns={"render": _ok}, lease_client=door)
    assert out is not None and out.status == "done"
    assert out.result["gpu_lease"]["state"] == "unleased"
    assert "UNREACHABLE" in out.result["gpu_lease"]["why"]
    assert door.released == []
    assert any("UNLEASED" in rec.getMessage() for rec in caplog.records)


def test_non_gpu_kind_never_asks_the_door(store):
    store.submit("agent", {"task": "x", "agent": "a"})
    door = _Door("refuse")
    out = dispatch_once(store, worker_id="w1", run_fns={"agent": _ok}, lease_client=door)
    assert out is not None and out.status == "done"
    assert door.acquired == []
    assert "gpu_lease" not in out.result


# ── the honest gap ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,gpu", [
    ("render", GPU), ("artpack", GPU), ("solve", {"class": "arc", "vram_mb": 8000}),
])
def test_gpu_kind_without_host_handler_fails_naming_the_gap_and_takes_no_lease(
        store, kind, gpu):
    store.submit(kind, {"spec": {}}, gpu=gpu)
    door = _Door("grant")
    gap = dispatch_once(store, worker_id="w1", lease_client=door)
    assert gap is not None and gap.status == STATUS_FAILED
    assert "NotImplemented" in gap.result["message"]
    assert kind in gap.result["message"]
    assert door.acquired == []  # no lease evicted real work for a stub


# ── a refusal is a refusal whatever the client calls it ─────────────────────

def test_refusal_from_a_client_that_hides_its_exception_class_still_requeues(store):
    """A host client that raises a refusal but does not expose `LeaseRefused`
    on the object. Before the shape check this fell into the "door broke, run
    unleased" arm -- a refusal that PROCEEDED, the one outcome it may never have."""
    class _Bare:
        def acquire(self, cls, vram_mb, **kw):
            raise gpu_lease.LeaseRefused({"reason": "outranked",
                                          "lanes": {"wait": {"eta_s": 7}}})

        def release(self, lease, outcome="done"):
            raise AssertionError("nothing to release")

    ran: list = []
    store.submit("render", {"job": "x"}, gpu=GPU)
    back = dispatch_once(store, worker_id="w1", lease_client=_Bare(),
                         run_fns={"render": lambda i: ran.append(i.id) or (0, "ok")},
                         now_fn=lambda: 1000.0)
    assert back is not None and back.status == STATUS_QUEUED
    assert ran == []
    assert back.not_before == 1015.0  # the floor, not the 7 s hint
    assert (back.wait or {}).get("reason") == "outranked"


def test_refusal_by_shape_when_the_client_raises_its_own_type(store):
    class _OutrankedError(RuntimeError):
        lease_refused = True
        reason = "outranked by arc"

    class _Theirs:
        LeaseRefused = "not-a-class"  # an `except "str"` would TypeError mid-propagation

        def acquire(self, cls, vram_mb, **kw):
            raise _OutrankedError("no")

    store.submit("render", {"job": "x"}, gpu=GPU)
    back = dispatch_once(store, worker_id="w1", lease_client=_Theirs(),
                         run_fns={"render": _ok}, now_fn=lambda: 1000.0)
    assert back is not None and back.status == STATUS_QUEUED
    assert (back.wait or {}).get("reason") == "outranked by arc"
    assert store.list(statuses=[STATUS_RUNNING]) == []


def test_claim_next_honours_the_injected_clock(store):
    """requeue() stamps not_before from now_fn; the next claim must read the
    SAME clock, or a test-injected clock is only half honoured."""
    store.submit("render", {"job": "x"}, gpu=GPU)
    dispatch_once(store, worker_id="w1", run_fns={"render": _ok},
                  lease_client=_Door("refuse"), now_fn=lambda: 10.0**12)
    # Wall clock is far behind 10**12 + 42: with time.time() this would be claimable.
    assert dispatch_once(store, worker_id="w1", run_fns={"render": _ok},
                         lease_client=_Door("grant"), now_fn=lambda: 10.0**12 + 41) is None
    out = dispatch_once(store, worker_id="w1", run_fns={"render": _ok},
                        lease_client=_Door("grant"), now_fn=lambda: 10.0**12 + 42)
    assert out is not None and out.status == "done"
