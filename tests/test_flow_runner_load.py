"""A --script flow that defines a dataclass must load (2026-11-24, the s2 task miner)."""
from __future__ import annotations

import textwrap

from awrun._flow_runner import _load


def test_script_with_a_dataclass_loads(tmp_path):
    script = tmp_path / "flow_with_dataclass.py"
    script.write_text(textwrap.dedent("""
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Row:
            n: int

        async def main():
            return Row(3).n
    """), encoding="utf-8")
    fn = _load("", str(script), "main")
    assert fn.__name__ == "main"
