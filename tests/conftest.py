"""Every test gets its own audit log: lifecycle changes are recorded, and a test
run must never append to the operator's real trail."""

import sys as _sys
from pathlib import Path as _Path

_PKG_ROOT = _Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PKG_ROOT))


import pytest


@pytest.fixture(autouse=True)
def _scratch_audit_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AWRUN_AUDIT_LOG", str(tmp_path / "awrun-audit.log"))
