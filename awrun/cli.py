"""awrun CLI — submit, bump, watch and cancel prioritized runs.

Subcommand shape matches `adk decide` (`awdk/adk/decisions/cli.py`): one
`cmd_<name>(args, store) -> int` per subcommand, `build_parser()` assembles
them, `main()` dispatches. Exit codes: 0 ok, 1 the operation was refused
(e.g. bumping a closed run), 2 could not run at all (bad args, store error).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional, Sequence

from awrun.store import CLOSED_STATUSES, RunError, RunItem, RunStore, get_store


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def _print_item(item: RunItem, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(item.to_dict(), indent=2))
        return
    age = _fmt_age(time.time() - item.created_at)
    label = item.spec.get("workflow") or item.spec.get("task") or item.kind
    print(f"{item.id}  [{item.status:>9}]  p={item.priority:<3}  {item.kind:<5}  "
          f"{age:>5} old  {label}")


def cmd_submit(args: argparse.Namespace, store: RunStore) -> int:
    spec: dict = {}
    if args.kind == "ci":
        spec["workflow"] = args.workflow
        spec["ref"] = args.ref or "develop"
        inputs = {}
        for kv in args.field or []:
            if "=" not in kv:
                print(f"ERROR: --field must be key=value, got {kv!r}", file=sys.stderr)
                return 2
            k, v = kv.split("=", 1)
            inputs[k] = v
        spec["inputs"] = inputs
    else:
        if not args.task:
            print("ERROR: --task is required for --kind agent", file=sys.stderr)
            return 2
        if not args.agent:
            print("ERROR: --agent is required for --kind agent "
                  "(see `adk agents ls` for valid names)", file=sys.stderr)
            return 2
        spec["task"] = args.task
        spec["agent"] = args.agent
        if args.adk_args:
            spec["adk_args"] = args.adk_args

    try:
        item = store.submit(args.kind, spec, priority=args.priority,
                             paths=args.paths or [])
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    _print_item(item, as_json=args.json)
    return 0


def cmd_bump(args: argparse.Namespace, store: RunStore) -> int:
    try:
        item = store.bump(args.id, args.priority)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    return 0


def cmd_queue(args: argparse.Namespace, store: RunStore) -> int:
    statuses = None
    if not args.all:
        from awrun.store import OPEN_STATUSES
        statuses = list(OPEN_STATUSES)
    items = store.list(statuses=statuses, kind=args.kind)
    if args.json:
        print(json.dumps([i.to_dict() for i in items], indent=2))
        return 0
    if not items:
        print("(empty)")
        return 0
    for item in items:
        _print_item(item, as_json=False)
    return 0


def cmd_status(args: argparse.Namespace, store: RunStore) -> int:
    item = store.get(args.id)
    if item is None:
        print(f"ERROR: no such run: {args.id}", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    return 0


def cmd_cancel(args: argparse.Namespace, store: RunStore) -> int:
    try:
        item = store.cancel(args.id)
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if item is not None and item.status not in CLOSED_STATUSES:
        # cancel() on an already-closed run returns it unchanged rather than
        # raising (idempotent cancel of something already done is fine); a
        # still-open status here means the rename genuinely failed.
        print(f"ERROR: could not cancel {args.id} (status: {item.status})", file=sys.stderr)
        return 1
    _print_item(item, as_json=args.json)
    return 0


def _self_test() -> int:
    import tempfile

    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(td)
        a = store.submit("agent", {"task": "low"}, priority=1)
        b = store.submit("agent", {"task": "high"}, priority=5)
        queued = store.list(statuses=["queued"])
        check("higher priority sorts first regardless of submit order",
              queued[0].id == b.id and queued[1].id == a.id)

        bumped = store.bump(a.id, 10)
        queued2 = store.list(statuses=["queued"])
        check("bump() changes order", queued2[0].id == a.id and bumped.priority == 10)

        claimed = store.claim_next(worker_id="w1")
        check("claim_next() claims the highest-priority item", claimed is not None
              and claimed.id == a.id and claimed.status == "claimed")

        again = store.claim(a.id, worker_id="w2")
        check("a second claim on an already-claimed item returns None, not an error",
              again is None)

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="awrun", description="A priority-aware queue for agentic runs and ad-hoc CI builds.")
    sub = parser.add_subparsers(dest="command")

    # --json is defined on a shared PARENT rather than the top-level parser:
    # argparse requires a top-level optional to precede the subcommand token
    # (`awrun --json queue`, not `awrun queue --json`), which is not how
    # anyone actually types it. A parent parser makes it legal in either
    # position and, crucially, an end-to-end smoke test caught this — the
    # unit self-tests call cmd_*() directly and never exercise the real
    # argv parser, so this bug was invisible to them.
    json_flag = argparse.ArgumentParser(add_help=False)
    json_flag.add_argument("--json", action="store_true", help="machine-readable output")

    submit = sub.add_parser("submit", help="queue a new run", parents=[json_flag])
    submit.add_argument("--kind", choices=["agent", "ci"], required=True)
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--paths", nargs="*", default=[],
                         help="paths this run will touch (lease-awareness)")
    submit.add_argument("--task", help="[kind=agent] one-shot message for the ADK agent")
    submit.add_argument("--agent", help="[kind=agent] registered mesh agent name "
                                         "(see `adk agents ls`)")
    submit.add_argument("--adk-args", dest="adk_args", nargs="*", default=[],
                         help="[kind=agent] extra argv passed to `adk chat`")
    submit.add_argument("--workflow", help="[kind=ci] workflow file name")
    submit.add_argument("--ref", help="[kind=ci] git ref (default: develop)")
    submit.add_argument("--field", action="append",
                         help="[kind=ci] workflow_dispatch input as key=value, repeatable")

    bump = sub.add_parser("bump", help="change a queued/claimed run's priority",
                           parents=[json_flag])
    bump.add_argument("id")
    bump.add_argument("--priority", type=int, required=True)

    queue = sub.add_parser("queue", help="list runs, highest priority first",
                            parents=[json_flag])
    queue.add_argument("--all", action="store_true", help="include done/failed/cancelled")
    queue.add_argument("--kind", choices=["agent", "ci"])

    status = sub.add_parser("status", help="one run in full", parents=[json_flag])
    status.add_argument("id")

    cancel = sub.add_parser("cancel", help="withdraw a queued/claimed run", parents=[json_flag])
    cancel.add_argument("id")

    sub.add_parser("self-test", help="run the built-in self-test")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "self-test":
        return _self_test()
    if args.command is None:
        parser.print_help()
        return 2

    store = get_store()
    handlers = {
        "submit": cmd_submit,
        "bump": cmd_bump,
        "queue": cmd_queue,
        "status": cmd_status,
        "cancel": cmd_cancel,
    }
    return handlers[args.command](args, store)


if __name__ == "__main__":
    sys.exit(main())
