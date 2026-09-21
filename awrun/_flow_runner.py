"""Child process for kind=flow: run one journaled workflow under a fixed run id.

Started by the dispatcher as ``python -m awrun._flow_runner``. It is a separate
process on purpose: suspending a running flow means stopping it, and the journal
on disk -- appended and fsynced per call -- is what the next start continues from.
The run id is ALWAYS passed as the journal's resume id, so the first start and
the fifth are the same code path.

Exit codes: 0 done, 1 the workflow raised, 3 the workflow engine is not installed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path


def _load(entry: str, script: str, function: str):
    if entry:
        module_name, _, attr = entry.partition(":")
        if not module_name or not attr:
            raise ValueError(f"--entry must be module:function, got {entry!r}")
        return getattr(importlib.import_module(module_name), attr)
    path = Path(script).resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, function)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="awrun._flow_runner")
    ap.add_argument("--entry", default="")
    ap.add_argument("--script", default="")
    ap.add_argument("--function", default="main")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--budget", type=int, default=1000000)
    args = ap.parse_args(argv)

    try:
        import awflow
    except ImportError:
        print("kind=flow needs the awflow package: pip install aitherium-awflow", file=sys.stderr)
        return 3
    if not args.entry and not args.script:
        print("one of --entry or --script is required", file=sys.stderr)
        return 1

    try:
        flow = _load(args.entry, args.script, args.function)
        kwargs = {}
        # A fake model call needs BOTH the script and the environment to ask for it.
        hook = getattr(flow, "awrun_dispatcher", None)
        if hook is not None and os.environ.get("AWRUN_FLOW_FAKE_DISPATCHER") == "1":
            kwargs["dispatcher"] = hook
        result = asyncio.run(awflow.run_workflow(
            flow, journal_path=args.journal, resume_from=args.run_id,
            budget_tokens=args.budget,
            mirror=os.environ.get("AITHER_AWFLOW_MIRROR", "1") not in ("0", "false"),
            **kwargs))
    except Exception as exc:  # noqa: BLE001 - the workflow's failure is this run's result
        print(f"flow raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"run_id": args.run_id, "result": result}, default=str)[-3500:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
