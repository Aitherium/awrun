"""kind=eval: validated against agent_ruler.ARMS at submit; only a local arm leases."""

from __future__ import annotations

import argparse
import json

import pytest
from awrun import cli
from awrun.dispatcher import _RUN_FNS
from awrun.store import GPU_KINDS, KINDS, RunStore

#: A frozen @dataclass, exactly like agent_ruler.Arm: the dataclass machinery looks the
#: module up in sys.modules, so a path import that does not register it fails.
_RULER = (
    "from dataclasses import dataclass\n"
    "@dataclass(frozen=True)\n"
    "class A:\n"
    "    kind: str\n"
    "    transport: str = 'openai'\n"
    "ARMS = {'L-x': A(kind='local', transport='openai'),\n"
    "        'H-x': A(kind='frontier', transport='harness')}\n"
)


@pytest.fixture()
def ruler(tmp_path, monkeypatch):
    path = tmp_path / "agent_ruler.py"
    path.write_text(_RULER, encoding="utf-8")
    monkeypatch.setenv(cli._RULER_ENV, str(path))
    cli._RULER_CACHE.clear()
    yield path
    cli._RULER_CACHE.clear()


def _ns(**kw) -> argparse.Namespace:
    base = dict(kind="eval", priority=0, paths=[], json=False, spec_json=None,
                arm=None, suite=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_eval_is_a_host_registered_non_gpu_kind():
    assert "eval" in KINDS and "eval" not in GPU_KINDS
    code, msg = _RUN_FNS["eval"](type("I", (), {"id": "r-x"})())
    assert code == 1 and "awrun_eval_worker.py" in msg


def test_local_arm_gets_a_gpu_request_harness_arm_none(ruler, tmp_path):
    store = RunStore(tmp_path / "runs")
    spec = {"arm": "L-x", "suite": "s2", "ids": ["repo__1"], "work_root": "D:/w"}
    assert cli.cmd_submit(_ns(spec_json=json.dumps(spec)), store) == 0
    assert cli.cmd_submit(_ns(arm="H-x", suite="s3"), store) == 0
    got = {i.spec["arm"]: i for i in store.list(statuses=["queued"], kind="eval")}
    assert got["L-x"].gpu["class"] == "training"
    assert got["L-x"].spec["ids"] == ["repo__1"] and got["L-x"].spec["work_root"] == "D:/w"
    assert got["H-x"].gpu is None and got["H-x"].spec["arm_transport"] == "harness"


@pytest.mark.parametrize("spec", [
    {"arm": "nope", "suite": "s2"},
    {"arm": "L-x", "suite": "s5"},
    {"arm": "L-x", "suite": "s2", "ids": "repo__1"},
    {"arm": "L-x", "suite": "s2", "ids": [""]},
    {"arm": "L-x", "suite": "s2", "timeout_s": 0},
    {"arm": "H-x", "suite": "s2", "gpu": {"class": "training", "vram_mb": 1}},
    {"arm": "L-x", "suite": "s2", "extra": True},
])
def test_bad_eval_specs_are_refused_and_queue_nothing(ruler, tmp_path, spec):
    store = RunStore(tmp_path / "runs")
    assert cli.cmd_submit(_ns(spec_json=json.dumps(spec)), store) == 2
    assert not store.list(statuses=["queued"], kind="eval")


def test_no_ruler_means_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(cli._RULER_ENV, str(tmp_path / "missing.py"))
    cli._RULER_CACHE.clear()
    store = RunStore(tmp_path / "runs")
    assert cli.cmd_submit(_ns(arm="L-x", suite="s2"), store) == 2
