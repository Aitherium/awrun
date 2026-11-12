"""placement_budget: what a fleet placement node has already promised its placed
services, read from the node's own files. Node-local, tighten-only, and never a
reason to block every job when the local state cannot be read."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from awrun import placement_budget as pb


def _node(tmp_path, env="AITHER_PLACEMENT_HEADROOM_MB=6144\n", manifest=None, raw=None,
          name="placement"):
    root = tmp_path / name
    root.mkdir()
    if env is not None:
        (root / "agent.env").write_text(env, encoding="utf-8")
    if raw is not None:
        (root / "desired.json").write_text(raw, encoding="utf-8")
    elif manifest is not None:
        (root / "desired.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _never(units):
    raise AssertionError(f"systemd must not be asked here (asked about {units})")


def test_a_host_without_agent_env_is_not_a_node(tmp_path):
    assert pb.read(tmp_path / "missing", is_active=_never) is None
    # A manifest alone does not make a node: agent.env is what a join writes.
    root = _node(tmp_path, env=None, manifest={"present": [{"unit": "a.service",
                                                             "mem_mb": 4096}]})
    assert pb.read(root, is_active=_never) is None


def test_root_comes_from_the_environment_then_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PLACEMENT_DIR", raising=False)
    assert pb.root_dir() == Path("/etc/aither/placement")
    root = _node(tmp_path)
    monkeypatch.setenv("AITHER_PLACEMENT_DIR", str(root))
    assert pb.root_dir() == root
    assert pb.read(is_active=_never) == {
        "headroom_gb": 6.0, "pending_gb": 0.0, "pending_units": [], "note": ""}


def test_joined_node_with_no_manifest_reserves_only_headroom(tmp_path):
    out = pb.read(_node(tmp_path), is_active=_never)
    assert out == {"headroom_gb": 6.0, "pending_gb": 0.0, "pending_units": [], "note": ""}


@pytest.mark.parametrize("env", [
    "AITHER_NODE_ID=n\n",
    "AITHER_PLACEMENT_HEADROOM_MB=\n",
    "AITHER_PLACEMENT_HEADROOM_MB=lots\n",
    "AITHER_PLACEMENT_HEADROOM_MB=-5\n",
    "# AITHER_PLACEMENT_HEADROOM_MB=6144\n",
])
def test_missing_or_malformed_headroom_is_zero_and_says_so(tmp_path, env):
    out = pb.read(_node(tmp_path, env=env), is_active=_never)
    assert out["headroom_gb"] == 0.0 and out["note"] == pb.NOTE_NO_HEADROOM


def test_headroom_is_parsed_from_a_real_shaped_env_file(tmp_path):
    env = ("# written by the join\nAITHER_NODE_ID=n1\n"
           "AITHER_PLACEMENT_HEADROOM_MB = \"8192\"\nAITHER_PLACEMENT_MODE=observe\n")
    assert pb.read(_node(tmp_path, env=env), is_active=_never)["headroom_gb"] == 8.0


def test_pending_is_the_sum_of_placed_units_that_are_not_active(tmp_path):
    manifest = {"present": [{"unit": "a.service", "mem_mb": 4096},
                            {"unit": "b.service", "mem_mb": 2048},
                            {"unit": "c.service", "mem_mb": 1024}]}
    asked = []

    def is_active(units):
        asked.append(list(units))
        return {"b.service"}

    out = pb.read(_node(tmp_path, manifest=manifest), is_active=is_active)
    assert asked == [["a.service", "b.service", "c.service"]], "one question, all units"
    assert out["pending_units"] == ["a.service", "c.service"]
    assert out["pending_gb"] == 5.0 and out["note"] == ""


def test_all_units_active_reserves_nothing_more(tmp_path):
    manifest = {"present": [{"unit": "a.service", "mem_mb": 4096}]}
    out = pb.read(_node(tmp_path, manifest=manifest), is_active=lambda u: set(u))
    assert out["pending_gb"] == 0.0 and out["pending_units"] == []


def test_advisory_manifest_adds_no_pending_and_asks_nothing(tmp_path):
    manifest = {"advisory": True, "present": [{"unit": "a.service", "mem_mb": 40960}]}
    out = pb.read(_node(tmp_path, manifest=manifest), is_active=_never)
    assert out["pending_gb"] == 0.0 and out["note"] == ""
    # Only the literal true is advisory: a truthy string is not.
    manifest["advisory"] = "true"
    out = pb.read(_node(tmp_path, manifest=manifest, name="second"),
                  is_active=lambda u: set())
    assert out["pending_gb"] == 40.0


def test_unasked_unit_states_count_every_present_unit_and_say_so(tmp_path):
    manifest = {"present": [{"unit": "a.service", "mem_mb": 4096},
                            {"unit": "b.service", "mem_mb": 2048}]}
    out = pb.read(_node(tmp_path, manifest=manifest), is_active=lambda u: None)
    assert out["pending_gb"] == 6.0 and out["note"] == pb.NOTE_UNASKED
    assert out["pending_units"] == ["a.service", "b.service"]


@pytest.mark.parametrize("raw", ["", "{not json", "[]", "null", '{"present": {"a": 1}}'])
def test_unreadable_manifest_reserves_nothing_and_says_the_reserve_is_unknown(tmp_path, raw):
    out = pb.read(_node(tmp_path, raw=raw), is_active=_never)
    assert out is not None, "still a node: the headroom applies"
    assert out["headroom_gb"] == 6.0 and out["pending_gb"] == 0.0
    assert out["note"] == pb.NOTE_UNREADABLE


def test_entries_without_measured_memory_or_a_safe_name_are_not_counted(tmp_path):
    manifest = {"present": [
        {"unit": "ok.service", "mem_mb": 1024},
        {"unit": "unmeasured.service", "mem_mb": None},
        {"unit": "boolean.service", "mem_mb": True},
        {"unit": "negative.service", "mem_mb": -4096},
        {"unit": "--now", "mem_mb": 4096},
        {"unit": "a b; reboot", "mem_mb": 4096},
        {"unit": 7, "mem_mb": 4096},
        "not-a-mapping",
    ]}
    asked = []

    def is_active(units):
        asked.append(list(units))
        return set()

    out = pb.read(_node(tmp_path, manifest=manifest), is_active=is_active)
    assert asked == [["ok.service"]], "nothing but a clean unit name reaches systemctl"
    assert out["pending_units"] == ["ok.service"] and out["pending_gb"] == 1.0


# ── the one systemctl question ───────────────────────────────────────────────

def _fake_run(monkeypatch, stdout="", raises=None):
    calls = []

    def run(argv, **kw):
        calls.append((list(argv), kw))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, 3, stdout=stdout, stderr="")

    monkeypatch.setattr(pb.subprocess, "run", run)
    return calls


def test_systemctl_active_asks_once_and_maps_states_in_order(monkeypatch):
    calls = _fake_run(monkeypatch, "active\ninactive\nfailed\nactive\n")
    units = ["a.service", "b.service", "c.service", "d.service"]
    assert pb.systemctl_active(units) == {"a.service", "d.service"}
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == ["systemctl", "is-active", *units]
    assert kw["encoding"] == "utf-8" and kw["errors"] == "replace" and kw["timeout"] == 10
    assert kw.get("shell") in (None, False)


def test_systemctl_active_with_no_units_asks_nothing(monkeypatch):
    calls = _fake_run(monkeypatch, "")
    assert pb.systemctl_active([]) == set() and calls == []


@pytest.mark.parametrize("raises", [
    FileNotFoundError("systemctl"), PermissionError("denied"),
    subprocess.TimeoutExpired("systemctl", 10),
])
def test_systemctl_active_that_cannot_ask_returns_none(monkeypatch, raises):
    _fake_run(monkeypatch, raises=raises)
    assert pb.systemctl_active(["a.service"]) is None


def test_systemctl_answer_that_does_not_line_up_is_not_trusted(monkeypatch):
    _fake_run(monkeypatch, "active\n")
    assert pb.systemctl_active(["a.service", "b.service"]) is None


def test_read_uses_the_real_question_by_default(tmp_path, monkeypatch):
    calls = _fake_run(monkeypatch, "inactive\n")
    manifest = {"present": [{"unit": "a.service", "mem_mb": 2048}]}
    out = pb.read(_node(tmp_path, manifest=manifest))
    assert len(calls) == 1 and out["pending_gb"] == 2.0


# ── this module ships in a public package ────────────────────────────────────

def test_module_is_standalone_and_names_nothing_internal():
    source = Path(pb.__file__).read_bytes()
    assert not re.search(rb"^\s*(?:from|import)\s+(?:lib|services)(?:\.|\s|$)",
                         source, re.MULTILINE)
    assert not re.search(rb"^\s*from\s+AitherOS(?:\.|\s|$)", source, re.MULTILINE)
    for pattern in (rb"\bD-\d{3,4}\b", rb"aitheros-|aither-vllm|aither-worker",
                    rb"[A-Za-z]:[\\/]AitherOS", rb"\b100\.64\.\d+\.\d+\b"):
        assert not re.search(pattern, source), pattern
