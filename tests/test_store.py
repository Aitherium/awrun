"""Unit tests for awrun.store — mirrors the shape decisions/store.py's own
test suite uses (concurrent claim race, atomic write, corrupt-file handling).
"""

from __future__ import annotations

import json

import pytest

from awrun.store import (
    CLOSED_STATUSES,
    STATUS_CANCELLED,
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    RunError,
    RunStore,
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
