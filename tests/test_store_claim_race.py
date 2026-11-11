"""Several workers on one queue: an item is claimed once, and losing a race is quiet.

Measured 2026-11-10 with a second dispatcher beside the solve worker on Windows:
the loser of a claim race put the run back in queued/, died on FileExistsError,
the copy was claimed and run a second time, and that worker died moving it to
done/. With that fixed, a six-claimer hammer on Windows still double-claimed 4
of 120 items, because a Windows rename acts on an open handle and reports
success to both racers; hence the per-item lock.
"""

from __future__ import annotations

import os
import threading

from awrun import store as store_mod
from awrun.store import RunStore


def _files(root, status: str) -> list[str]:
    return sorted(p.name for p in (root / status).iterdir())


def _locks(root) -> list[str]:
    d = root / "locks"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_loser_of_a_claim_race_gets_none_and_leaves_nothing_behind(tmp_path, monkeypatch):
    """Worker A has read the item and is about to move it when worker B claims
    it. A must come back empty-handed and the run must exist exactly once."""
    a, b = RunStore(tmp_path), RunStore(tmp_path)
    item = a.submit("flow", {"script": "x.py"})

    real_read = RunStore._read
    fired = []

    def read_then_lose(self, status, item_id):
        got = real_read(self, status, item_id)
        if self is a and status == "queued" and not fired:
            fired.append(1)
            assert b.claim(item_id, worker_id="worker-b") is not None
        return got

    monkeypatch.setattr(RunStore, "_read", read_then_lose)
    assert a.claim(item.id, worker_id="worker-a") is None

    assert _files(tmp_path, "queued") == []                  # not resurrected
    assert _files(tmp_path, "claimed") == [f"{item.id}.json"]
    assert b.get(item.id).claimed_by == "worker-b"           # the winner keeps it
    assert b.get(item.id).status == "claimed"


def test_the_winner_then_finishes_normally(tmp_path, monkeypatch):
    """The second half of the incident: with no copy left in queued/, the run
    cannot be picked up again, so running -> done has nothing in its way."""
    a, b = RunStore(tmp_path), RunStore(tmp_path)
    item = a.submit("flow", {"script": "x.py"})
    real_read = RunStore._read
    fired = []

    def read_then_lose(self, status, item_id):
        got = real_read(self, status, item_id)
        if self is a and status == "queued" and not fired:
            fired.append(1)
            b.claim(item_id, worker_id="worker-b")
        return got

    monkeypatch.setattr(RunStore, "_read", read_then_lose)
    a.claim(item.id, worker_id="worker-a")
    monkeypatch.setattr(RunStore, "_read", real_read)

    assert b.start(item.id) is not None
    assert b.finish(item.id, status="done", result={"code": 0, "message": ""}) is not None
    assert a.claim_next(worker_id="worker-a") is None        # nothing to run twice
    assert _files(tmp_path, "done") == [f"{item.id}.json"]
    assert _files(tmp_path, "running") == []


def test_a_destination_that_already_exists_is_a_lost_race_not_a_crash(tmp_path, monkeypatch):
    """Windows raises FileExistsError where POSIX renames over the target."""
    s = RunStore(tmp_path)
    item = s.submit("flow", {"script": "x.py"})

    def _exists(_src, _dst):
        raise FileExistsError(183, "Cannot create a file when that file already exists")

    monkeypatch.setattr(store_mod.os, "rename", _exists)
    assert s.claim(item.id, worker_id="w") is None
    assert _files(tmp_path, "queued") == [f"{item.id}.json"]


def test_a_bump_that_lands_before_the_claim_is_kept(tmp_path, monkeypatch):
    """The item is read again after the move, so an in-place edit made between
    this worker's first look and its rename is not written over."""
    a, b = RunStore(tmp_path), RunStore(tmp_path)
    item = a.submit("flow", {"script": "x.py"}, priority=1)
    real_read = RunStore._read
    fired = []

    def read_then_bump(self, status, item_id):
        got = real_read(self, status, item_id)
        if self is a and status == "queued" and not fired:
            fired.append(1)
            b.bump(item_id, 9)
        return got

    monkeypatch.setattr(RunStore, "_read", read_then_bump)
    claimed = a.claim(item.id, worker_id="worker-a")
    assert claimed is not None and claimed.priority == 9
    assert a.get(item.id).priority == 9 and a.get(item.id).claimed_by == "worker-a"


def test_many_workers_claim_every_item_exactly_once(tmp_path):
    n_items, n_workers = 120, 6
    ids = [RunStore(tmp_path).submit("flow", {"n": i}).id for i in range(n_items)]
    claims: dict[str, list[str]] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()
    go = threading.Event()

    def work(name: str) -> None:
        s = RunStore(tmp_path)
        go.wait()
        try:
            while True:
                got = s.claim_next(worker_id=name)
                if got is None:
                    return
                with lock:
                    claims.setdefault(got.id, []).append(name)
        except BaseException as exc:  # noqa: BLE001 - the test reports it
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(n_workers)]
    for t in threads:
        t.start()
    go.set()
    for t in threads:
        t.join(60)

    assert errors == []
    assert sorted(claims) == sorted(ids)
    assert all(len(who) == 1 for who in claims.values())
    assert _files(tmp_path, "queued") == []
    assert len(_files(tmp_path, "claimed")) == n_items       # and no .tmp litter
    for item_id, (who,) in claims.items():
        assert RunStore(tmp_path).get(item_id).claimed_by == who


def test_no_temp_file_is_left_in_the_source_directory(tmp_path):
    s = RunStore(tmp_path)
    item = s.submit("flow", {"script": "x.py"})
    s.claim(item.id, worker_id="w")
    s.start(item.id)
    s.finish(item.id, status="done", result={"code": 0, "message": ""})
    for status in ("queued", "claimed", "running", "done"):
        assert all(name.endswith(".json") for name in _files(tmp_path, status))
    assert os.path.exists(tmp_path / "done" / f"{item.id}.json")


# -- Windows: a file another process has open cannot be renamed for a moment --

def test_a_sharing_violation_is_waited_out(tmp_path, monkeypatch):
    s = RunStore(tmp_path)
    item = s.submit("flow", {"script": "x.py"})
    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)
    real_rename, calls = os.rename, []

    def busy_twice(src, dst):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(13, "The process cannot access the file because it is "
                                      "being used by another process")
        return real_rename(src, dst)

    monkeypatch.setattr(store_mod.os, "rename", busy_twice)
    claimed = s.claim(item.id, worker_id="w")
    assert claimed is not None and claimed.claimed_by == "w"
    assert len(calls) == 3
    assert _files(tmp_path, "claimed") == [f"{item.id}.json"]


def test_a_permission_error_that_does_not_clear_is_still_raised(tmp_path, monkeypatch):
    s = RunStore(tmp_path)
    item = s.submit("flow", {"script": "x.py"})
    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)
    calls = []

    def denied(_src, _dst):
        calls.append(1)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(store_mod.os, "rename", denied)
    try:
        s.claim(item.id, worker_id="w")
    except PermissionError:
        pass
    else:
        raise AssertionError("a lasting permission error must not read as a lost race")
    assert len(calls) == store_mod._SHARING_TRIES            # bounded, not forever
    assert _files(tmp_path, "queued") == [f"{item.id}.json"]


def test_the_content_write_waits_out_a_sharing_violation_too(tmp_path, monkeypatch):
    s = RunStore(tmp_path)
    item = s.submit("flow", {"script": "x.py"})
    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)
    real_replace, calls = os.replace, []

    def busy_first(src, dst):
        calls.append(1)
        if len(calls) in (1, 3):          # the claim's write, then the bump's
            raise PermissionError(13, "being used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", busy_first)
    assert s.claim(item.id, worker_id="w") is not None
    assert s.get(item.id).claimed_by == "w" and s.get(item.id).status == "claimed"
    assert s.bump(item.id, 7).priority == 7 and s.get(item.id).priority == 7
    assert len(calls) == 4


# -- the per-item lock: what actually picks the winner ----------------------

def test_a_move_in_progress_elsewhere_makes_this_one_stand_down(tmp_path, monkeypatch):
    """Another process holds the item's lock (it is mid-move). This worker must
    not rename anything: on Windows its rename would also report success."""
    s = RunStore(tmp_path)
    item = s.submit("flow", {"script": "x.py"})
    (tmp_path / "locks").mkdir(exist_ok=True)
    (tmp_path / "locks" / f"{item.id}.lock").write_text("")
    monkeypatch.setattr(store_mod, "_LOCK_WAIT_S", 0.05)
    renames = []
    real_rename = os.rename
    monkeypatch.setattr(store_mod.os, "rename",
                        lambda a, b: (renames.append((a, b)), real_rename(a, b))[1])
    assert s.claim(item.id, worker_id="w") is None
    assert renames == []
    assert _files(tmp_path, "queued") == [f"{item.id}.json"]
    assert _locks(tmp_path) == [f"{item.id}.lock"]           # not ours to remove


def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path, monkeypatch):
    s = RunStore(tmp_path)
    item = s.submit("flow", {"script": "x.py"})
    (tmp_path / "locks").mkdir(exist_ok=True)
    lock = tmp_path / "locks" / f"{item.id}.lock"
    lock.write_text("")
    old = lock.stat().st_mtime - store_mod._LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    monkeypatch.setattr(store_mod, "_LOCK_WAIT_S", 0.05)
    claimed = s.claim(item.id, worker_id="w")
    assert claimed is not None and claimed.claimed_by == "w"
    assert _locks(tmp_path) == []


def test_the_lock_is_released_whether_the_move_wins_or_loses(tmp_path):
    a, b = RunStore(tmp_path), RunStore(tmp_path)
    item = a.submit("flow", {"script": "x.py"})
    assert a.claim(item.id, worker_id="worker-a") is not None
    assert _locks(tmp_path) == []
    assert b.claim(item.id, worker_id="worker-b") is None    # already claimed
    assert _locks(tmp_path) == []
    assert a.start(item.id) is not None
    assert a.finish(item.id, status="done", result={"code": 0, "message": ""}) is not None
    assert _locks(tmp_path) == []


def test_the_lock_is_released_when_the_move_raises(tmp_path, monkeypatch):
    s = RunStore(tmp_path)
    item = s.submit("flow", {"script": "x.py"})
    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)

    def denied(_src, _dst):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(store_mod.os, "rename", denied)
    try:
        s.claim(item.id, worker_id="w")
    except PermissionError:
        pass
    assert _locks(tmp_path) == []
