"""A filtered dispatcher claims only its own items (`--kind`, `--run-id`).

Measured 2026-10-08: an unfiltered `python -m awrun.dispatcher --once`, run by a
CI lane to drain its two smoke runs, claimed the top item of ANY kind and failed
four queued `solve` runs it had no handler for. These pin that a foreign item is
never claimed, never failed and keeps its place, while the lane's own item runs.
"""
from __future__ import annotations

import pytest
from awrun import dispatcher
from awrun.dispatcher import dispatch_once, run_forever
from awrun.store import STATUS_QUEUED, RunItem, RunStore


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(dispatcher, "_relay_client", lambda: None)
    monkeypatch.setattr(dispatcher.host_admission, "admit", lambda kind: (True, "", {}))


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path)


#: a real `solve` item carries a GPU request; a filtered dispatcher never reaches the lease
GPU = {"class": "arc", "vram_mb": 1024}


def _ran(log):
    def fn(item: RunItem):
        log.append(item.id)
        return 0, "ok"
    return fn


def _status(store, item_id):
    return store.get(item_id).status


def test_kind_filter_never_claims_a_foreign_kind_even_at_higher_priority(store):
    foreign = store.submit("solve", {"domain": "arc"}, priority=9, gpu=GPU)
    mine = store.submit("ci", {"workflow": "node-buildx-smoke.yml"})
    log = []
    out = dispatch_once(store, worker_id="lane", run_fns={"ci": _ran(log), "solve": _ran(log)},
                        kinds=["ci"])
    assert out.id == mine.id and log == [mine.id]
    after = store.get(foreign.id)
    assert after.status == STATUS_QUEUED and after.claimed_by in (None, "")
    assert after.priority == 9 and after.created_at == foreign.created_at


def test_kind_filter_with_nothing_of_its_kind_claims_nothing(store):
    foreign = store.submit("solve", {"domain": "arc"}, gpu=GPU)
    assert dispatch_once(store, worker_id="lane", run_fns={"solve": _ran([])},
                         kinds=["ci"]) is None
    assert _status(store, foreign.id) == STATUS_QUEUED


def test_run_id_filter_claims_exactly_the_named_runs(store):
    a = store.submit("ci", {"workflow": "a.yml"}, priority=5)
    b = store.submit("ci", {"workflow": "b.yml"})
    log = []
    fns = {"ci": _ran(log)}
    assert dispatch_once(store, worker_id="lane", run_fns=fns, run_ids=[b.id]).id == b.id
    assert dispatch_once(store, worker_id="lane", run_fns=fns, run_ids=[b.id]) is None
    assert log == [b.id] and _status(store, a.id) == STATUS_QUEUED


def test_kind_and_run_id_together_must_both_match(store):
    s = store.submit("solve", {"domain": "arc"}, gpu=GPU)
    assert dispatch_once(store, worker_id="lane", run_fns={"solve": _ran([])},
                         kinds=["ci"], run_ids=[s.id]) is None
    assert _status(store, s.id) == STATUS_QUEUED


def test_unfiltered_dispatch_is_unchanged(store):
    t = store.submit("tunnel", {"hostname": "x"}, priority=9)
    store.submit("ci", {"workflow": "x.yml"})
    assert dispatch_once(store, worker_id="any",
                         run_fns={"tunnel": _ran([]), "ci": _ran([])}).id == t.id


def test_run_forever_passes_the_filter_through(store):
    foreign = store.submit("solve", {"domain": "arc"}, priority=9, gpu=GPU)
    mine = store.submit("ci", {"workflow": "x.yml"})
    log = []
    run_forever(store, worker_id="lane", run_fns={"ci": _ran(log), "solve": _ran(log)},
                sleep_fn=lambda s: None, max_iterations=3, kinds=["ci"])
    assert log == [mine.id] and _status(store, foreign.id) == STATUS_QUEUED


def test_cli_exposes_repeatable_kind_and_run_id(monkeypatch, store):
    seen = {}
    monkeypatch.setattr(dispatcher, "get_store", lambda: store)
    monkeypatch.setattr("awrun.cli._register_capacity_provider", lambda: None)
    monkeypatch.setattr(dispatcher, "dispatch_once",
                        lambda st, **kw: seen.update(kw) or None)
    monkeypatch.setattr("sys.argv", ["awrun.dispatcher", "--once", "--kind", "ci",
                                     "--kind", "flow", "--run-id", "r-abc"])
    assert dispatcher.main() == 0
    assert seen["kinds"] == ["ci", "flow"] and seen["run_ids"] == ["r-abc"]


def test_ci_item_with_a_repo_pins_gh_to_it():
    from awrun.dispatcher import _build_ci_argv
    item = RunItem(id="r-repo0001", kind="ci",
                   spec={"workflow": "w.yml", "ref": "develop", "repo": "Org/Repo",
                         "inputs": {"a": "1"}})
    assert _build_ci_argv(item) == ["gh", "workflow", "run", "w.yml", "--ref", "develop",
                                    "--repo", "Org/Repo", "-f", "a=1"]
    bare = RunItem(id="r-repo0002", kind="ci", spec={"workflow": "w.yml"})
    assert "--repo" not in _build_ci_argv(bare)
