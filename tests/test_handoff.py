"""A suspended run moves between queues intact -- and an altered one is refused."""

import json
import zipfile
from pathlib import Path

import pytest
from awrun import handoff
from awrun.store import RunError, RunStore


def _parked_flow(store: RunStore, lines=("one", "two")):
    item = store.submit("flow", {"entry": "pkg.mod:main"}, priority=4, name="mover",
                        lineage={"goal": "G-9"})
    journal = store.path / "journals" / item.id / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text("".join(json.dumps({"type": "AGENT_CALL", "n": n}) + "\n"
                               for n in lines), encoding="utf-8")
    store.claim(item.id, worker_id="w")
    store.start(item.id)
    store.park(item.id, {"kind": "flow-journal", "journal": str(store.path / "journals")})
    return item, journal


def test_a_run_moves_with_its_journal_and_leaves_the_source(tmp_path):
    src, dst = RunStore(tmp_path / "a"), RunStore(tmp_path / "b")
    item, journal = _parked_flow(src)
    out = handoff.export_run(src, item.id, tmp_path / "out", sign=False)
    assert src.get(item.id).status == "cancelled"          # it lives in one place

    moved = handoff.import_run(dst, Path(out["bundle"]), expect_sha256=out["sha256"])
    assert moved.id == item.id and moved.status == "suspended"
    assert moved.priority == 4 and moved.lineage == {"goal": "G-9"}
    landed = dst.path / "journals" / item.id / "journal.jsonl"
    assert landed.read_bytes() == journal.read_bytes()
    assert moved.checkpoint["journal_sha256"] == handoff.sha256_file(landed)
    assert dst.claim_next(worker_id="w") is None           # arriving is not running
    assert dst.resume(item.id).status == "queued"


def test_an_unverified_bundle_is_refused(tmp_path):
    src, dst = RunStore(tmp_path / "a"), RunStore(tmp_path / "b")
    item, _ = _parked_flow(src)
    out = handoff.export_run(src, item.id, tmp_path / "out", sign=False)
    with pytest.raises(RunError, match="unverified"):
        handoff.import_run(dst, Path(out["bundle"]))
    assert dst.list() == []


def test_a_bundle_altered_in_transit_is_refused(tmp_path):
    src, dst = RunStore(tmp_path / "a"), RunStore(tmp_path / "b")
    item, _ = _parked_flow(src)
    out = handoff.export_run(src, item.id, tmp_path / "out", sign=False)
    bundle = Path(out["bundle"])
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(forged, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "journal.jsonl":
                data += b'{"type":"AGENT_CALL","response":"forged"}\n'
            zout.writestr(name, data)
    with pytest.raises(RunError, match="digest mismatch"):
        handoff.import_run(dst, forged, expect_sha256=out["sha256"])
    assert dst.list() == []


def test_a_bundle_cannot_choose_where_it_is_written(tmp_path):
    dst = RunStore(tmp_path / "b")
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("run.json", "{}")
        zf.writestr("../../escaped.txt", "x")
    with pytest.raises(RunError, match="unexpected members"):
        handoff.import_run(dst, evil, expect_sha256=handoff.sha256_file(evil))
    assert not (tmp_path / "escaped.txt").exists()


def test_only_a_suspended_run_can_leave(tmp_path):
    src = RunStore(tmp_path / "a")
    item = src.submit("agent", {"agent": "a", "task": "t"})
    with pytest.raises(RunError, match="only a suspended run"):
        handoff.export_run(src, item.id, tmp_path / "out")


awseal = pytest.importorskip("awseal", reason="signing brick not installed")


def test_a_sealed_bundle_is_trusted_only_against_the_expected_key(tmp_path, monkeypatch):
    from awseal import keys
    monkeypatch.setenv(keys.KEY_PATH_ENV, str(tmp_path / "signing.key"))
    keys.generate(tmp_path / "signing.key")
    src, dst = RunStore(tmp_path / "a"), RunStore(tmp_path / "b")
    item, _ = _parked_flow(src)
    out = handoff.export_run(src, item.id, tmp_path / "out")
    assert out["sealed_by"], "a signing key existed and the bundle was not sealed"

    with pytest.raises(RunError, match="seal verification failed"):
        handoff.import_run(dst, Path(out["bundle"]), expect_key="00" * 32)
    assert dst.list() == []
    moved = handoff.import_run(dst, Path(out["bundle"]), expect_key=out["sealed_by"])
    assert moved.status == "suspended"
