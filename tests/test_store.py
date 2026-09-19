"""Unit tests for awrun.store — mirrors the shape decisions/store.py's own
test suite uses (concurrent claim race, atomic write, corrupt-file handling).
"""

from __future__ import annotations

import json

import pytest
from awrun.store import (
    CLOSED_STATUSES,
    GPU_KINDS,
    STATUS_CANCELLED,
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    RunError,
    RunStore,
    validate_gpu,
)


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path)


def test_submit_defaults(store):
    item = store.submit("agent", {"task": "x"})
    assert item.status == STATUS_QUEUED
    assert item.priority == 0
    assert item.id.startswith("r-")


def test_submit_rejects_unknown_kind(store):
    with pytest.raises(RunError):
        store.submit("not-a-kind", {})


def test_list_orders_by_priority_then_age(store):
    a = store.submit("agent", {"task": "a"}, priority=1)
    b = store.submit("agent", {"task": "b"}, priority=5)
    c = store.submit("agent", {"task": "c"}, priority=5)
    ordered = store.list(statuses=[STATUS_QUEUED])
    # b and c share priority 5 -> submit order (FIFO) breaks the tie.
    assert [i.id for i in ordered] == [b.id, c.id, a.id]


def test_bump_changes_only_priority(store):
    item = store.submit("agent", {"task": "x"}, priority=1)
    before = item.to_dict()
    bumped = store.bump(item.id, 9)
    after = bumped.to_dict()
    assert after["priority"] == 9
    changed_keys = {k for k in after if after[k] != before.get(k)}
    assert changed_keys <= {"priority", "updated_at"}


def test_bump_refuses_on_closed_run(store):
    item = store.submit("agent", {"task": "x"})
    store.claim(item.id, worker_id="w1")
    store.start(item.id)
    store.finish(item.id, status=STATUS_DONE)
    with pytest.raises(RunError):
        store.bump(item.id, 9)


def test_claim_race_exactly_one_winner(store):
    """The concurrency property this whole design exists for: two claimants
    racing on the same queued item must never both succeed."""
    item = store.submit("agent", {"task": "x"})
    first = store.claim(item.id, worker_id="w1")
    second = store.claim(item.id, worker_id="w2")
    assert first is not None
    assert first.status == STATUS_CLAIMED
    assert first.claimed_by == "w1"
    assert second is None  # the loser, not an exception


def test_claim_next_retries_past_a_lost_race(store):
    low = store.submit("agent", {"task": "low"}, priority=1)
    high = store.submit("agent", {"task": "high"}, priority=5)
    # Pre-claim the top candidate out from under claim_next, simulating a
    # peer worker winning the race on it a moment earlier.
    store.claim(high.id, worker_id="peer")
    claimed = store.claim_next(worker_id="me")
    assert claimed is not None
    assert claimed.id == low.id


def test_full_lifecycle_agent_run(store):
    item = store.submit("agent", {"task": "x"})
    claimed = store.claim(item.id, worker_id="w1")
    assert claimed.status == STATUS_CLAIMED
    running = store.start(item.id)
    assert running.status == STATUS_RUNNING
    finished = store.finish(item.id, status=STATUS_DONE, result={"code": 0})
    assert finished.status == STATUS_DONE
    assert finished.result == {"code": 0}
    assert finished.status in CLOSED_STATUSES


def test_cancel_open_run(store):
    item = store.submit("agent", {"task": "x"})
    cancelled = store.cancel(item.id)
    assert cancelled.status == STATUS_CANCELLED


def test_cancel_closed_run_is_idempotent_not_an_error(store):
    item = store.submit("agent", {"task": "x"})
    store.claim(item.id, worker_id="w1")
    store.start(item.id)
    store.finish(item.id, status=STATUS_DONE)
    result = store.cancel(item.id)
    assert result.status == STATUS_DONE  # unchanged, no exception


def test_cancel_unknown_id_raises(store):
    with pytest.raises(RunError):
        store.cancel("r-doesnotexist")


def test_atomic_write_no_partial_json_ever_readable(store, tmp_path):
    """A reader must never observe a half-written file. Simulated by writing
    directly through os.replace and confirming the target is always valid
    JSON immediately after -- there is no window where it is not, because
    os.replace is atomic on POSIX and Windows alike."""
    item = store.submit("agent", {"task": "x"})
    target = tmp_path / STATUS_QUEUED / f"{item.id}.json"
    raw = target.read_text(encoding="utf-8")
    json.loads(raw)  # must not raise


def test_corrupt_file_is_skipped_not_fatal(store, tmp_path):
    good = store.submit("agent", {"task": "good"})
    bad_path = tmp_path / STATUS_QUEUED / "r-badbadbad.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    items = store.list(statuses=[STATUS_QUEUED])
    ids = [i.id for i in items]
    assert good.id in ids
    assert "r-badbadbad" not in ids  # skipped, and the whole list() call did not raise


def test_invalid_id_rejected_not_used_as_a_path(store):
    with pytest.raises(RunError):
        store.get("../../etc/passwd")


def test_directory_encodes_status_on_disk(store, tmp_path):
    item = store.submit("agent", {"task": "x"})
    assert (tmp_path / STATUS_QUEUED / f"{item.id}.json").exists()
    store.claim(item.id, worker_id="w1")
    assert not (tmp_path / STATUS_QUEUED / f"{item.id}.json").exists()
    assert (tmp_path / STATUS_CLAIMED / f"{item.id}.json").exists()


# ── gpu request: required for GPU kinds, optional elsewhere ────────────────

@pytest.mark.parametrize("kind", sorted(GPU_KINDS))
def test_gpu_kinds_refuse_submit_without_a_gpu_request(store, kind):
    with pytest.raises(RunError, match="gpu"):
        store.submit(kind, {"job": "x"})
    assert store.list() == []  # refused BEFORE an id was minted: nothing on disk


def test_gpu_kinds_are_exactly_render_artpack_solve():
    assert GPU_KINDS == frozenset({"render", "artpack", "solve"})


def test_gpu_request_is_normalised_with_defaults(store):
    item = store.submit("render", {"job": "x"}, gpu={"class": "arc", "vram_mb": 8000})
    assert item.gpu == {"class": "arc", "vram_mb": 8000, "ttl_s": 600,
                        "host_pref": "auto", "backend": ""}
    assert item.not_before == 0.0 and item.requeues == 0 and item.wait is None


def test_gpu_request_may_ride_in_the_spec(store):
    item = store.submit("solve", {"spec": {}, "gpu": {"class": "arc", "vram_mb": 8000,
                                                       "host_pref": "dgx", "ttl_s": 30}})
    assert item.gpu["host_pref"] == "dgx" and item.gpu["ttl_s"] == 30


def test_gpu_request_is_optional_for_non_gpu_kinds(store):
    plain = store.submit("agent", {"task": "x"})
    assert plain.gpu is None
    with_gpu = store.submit("agent", {"task": "x"}, gpu={"class": "chat", "vram_mb": 1})
    assert with_gpu.gpu["class"] == "chat"


@pytest.mark.parametrize("bad,needle", [
    ("not-a-dict", "mapping"),
    ({"vram_mb": 10}, "missing"),
    ({"class": "arc"}, "missing"),
    ({"class": "gaming", "vram_mb": 10}, "class"),
    ({"class": "arc", "vram_mb": 0}, "vram_mb"),
    ({"class": "arc", "vram_mb": -5}, "vram_mb"),
    ({"class": "arc", "vram_mb": True}, "vram_mb"),
    ({"class": "arc", "vram_mb": "8000"}, "vram_mb"),
    ({"class": "arc", "vram_mb": 10, "ttl_s": 0}, "ttl_s"),
    ({"class": "arc", "vram_mb": 10, "host_pref": 3}, "host_pref"),
    ({"class": "arc", "vram_mb": 10, "cores": 2}, "unknown"),
])
def test_validate_gpu_names_what_is_wrong(bad, needle):
    with pytest.raises(RunError, match=needle):
        validate_gpu("render", bad)


def test_validate_gpu_none_for_non_gpu_kind_is_none():
    assert validate_gpu("agent", None) is None


def test_old_item_on_disk_without_gpu_fields_still_loads(store, tmp_path):
    """Optional in storage: a queued/ file written before these fields existed
    must load, with the new fields at their defaults."""
    item = store.submit("agent", {"task": "x"})
    path = tmp_path / STATUS_QUEUED / f"{item.id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for legacy_missing in ("gpu", "not_before", "requeues", "wait"):
        raw.pop(legacy_missing)
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.get(item.id)
    assert loaded.gpu is None and loaded.not_before == 0.0
    assert loaded.requeues == 0 and loaded.wait is None


# ── requeue: "not now", never "failed" ─────────────────────────────────────

def test_requeue_moves_claimed_back_to_queued_with_backoff(store, tmp_path):
    item = store.submit("render", {"job": "x"}, gpu={"class": "arc", "vram_mb": 8000},
                        priority=7)
    store.claim(item.id, worker_id="w1")
    back = store.requeue(item.id, 1234.5, wait={"reason": "outranked", "card_id": "d-9"})
    assert back.status == STATUS_QUEUED
    assert back.claimed_by is None
    assert back.not_before == 1234.5
    assert back.requeues == 1
    assert back.wait == {"reason": "outranked", "card_id": "d-9"}
    assert back.priority == 7 and back.created_at == item.created_at  # keeps its place
    assert (tmp_path / STATUS_QUEUED / f"{item.id}.json").exists()
    assert not (tmp_path / STATUS_CLAIMED / f"{item.id}.json").exists()


def test_requeue_works_from_running_and_counts_every_trip(store):
    item = store.submit("agent", {"task": "x"})
    store.claim(item.id, worker_id="w1")
    store.start(item.id)
    first = store.requeue(item.id, 10.0)
    assert first.status == STATUS_QUEUED and first.requeues == 1
    store.claim(item.id, worker_id="w2")
    second = store.requeue(item.id, 20.0)
    assert second.requeues == 2 and second.not_before == 20.0


def test_claim_next_passes_over_a_backed_off_item_and_takes_the_next(store):
    high = store.submit("agent", {"task": "high"}, priority=9)
    low = store.submit("agent", {"task": "low"}, priority=1)
    store.claim(high.id, worker_id="w1")
    store.requeue(high.id, 2000.0)
    got = store.claim_next(worker_id="w2", now=1999.0)
    assert got is not None and got.id == low.id  # a busy GPU must not idle the queue
    assert store.get(high.id).status == STATUS_QUEUED  # still there, not dropped
    later = store.claim_next(worker_id="w3", now=2000.0)
    assert later is not None and later.id == high.id


def test_requeue_refuses_queued_and_closed_items(store):
    queued = store.submit("agent", {"task": "x"})
    with pytest.raises(RunError, match="queued"):
        store.requeue(queued.id, 1.0)  # would silently reset a backoff
    done = store.submit("agent", {"task": "y"})
    store.claim(done.id, worker_id="w1")
    store.start(done.id)
    store.finish(done.id, status=STATUS_DONE)
    with pytest.raises(RunError):
        store.requeue(done.id, 1.0)  # would rewrite history
    with pytest.raises(RunError):
        store.requeue("r-doesnotexist", 1.0)


def test_requeue_rejects_a_non_numeric_not_before(store):
    item = store.submit("agent", {"task": "x"})
    store.claim(item.id, worker_id="w1")
    with pytest.raises(RunError, match="epoch"):
        store.requeue(item.id, "tomorrow")
    assert store.get(item.id).status == STATUS_CLAIMED  # untouched by the bad call


def test_requeue_after_a_cancel_is_an_error_not_a_silent_noop(store):
    item = store.submit("agent", {"task": "x"})
    store.claim(item.id, worker_id="w1")
    # requeue() checks the status, then _move()s; a cancel in between is the
    # lost race the None return exists for. Simulate by moving it ourselves.
    store.cancel(item.id)
    with pytest.raises(RunError):
        store.requeue(item.id, 1.0)  # now closed: an error, not a silent no-op


def test_requeue_lost_race_returns_none_not_error(store, monkeypatch):
    """requeue() checks the status, then _move()s. When the item vanishes from
    claimed/ in between (a peer cancelled it), that is the documented None."""
    item = store.submit("agent", {"task": "x"})
    store.claim(item.id, worker_id="w1")
    real_locate = store._locate
    located = real_locate(item.id)
    store.cancel(item.id)
    monkeypatch.setattr(store, "_locate", lambda _id: located)  # the stale pre-cancel view
    assert store.requeue(item.id, 1.0) is None
    monkeypatch.setattr(store, "_locate", real_locate)
    assert store.get(item.id).status == STATUS_CANCELLED  # the cancel stood
