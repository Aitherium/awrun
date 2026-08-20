"""awrun dispatcher — claims the highest-priority queued run and executes it.

Deliberately NOT a new always-on daemon process. `decisions/store.py` already
proves a live daemon isn't required for a durable, pollable, cross-process
queue to work correctly — this is a single loop meant to run as a scheduled
task (matching `AitherOS/config/routines/*.yaml`'s existing pattern) or a
one-shot `--once` invocation. One fewer always-on service is one fewer thing
that can silently die without anyone noticing.

Phase 1 scope: `kind=agent` runs only, dispatched directly (no GitHub Actions
involvement at all) by invoking the `adk` console script. This is the part
that delivers COMPLETE, correct dynamic priority, because it never touches
GitHub's own opaque scheduler. `kind=ci` dispatch (governing the order in
which awrun-submitted `workflow_dispatch` calls actually fire) is a separate,
later addition — see the plan this package was built from.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import Callable, Optional

from awrun.store import RunItem, RunStore, get_store

#: Injectable so tests never spawn a real `adk` process. Returns
#: (returncode, message) — message is stored in the run's `result` field.
RunAgentFn = Callable[[RunItem], "tuple[int, str]"]


def _build_argv(item: RunItem) -> Optional[list[str]]:
    """Pure, so --self-test can pin the exact real invocation without
    spawning a process. `adk start` is an interactive chat launcher with no
    one-shot task flag -- confirmed live via `adk start --help`, which has no
    --task option at all. The real one-shot invocation is
    `adk chat <agent> "<message>"` (message given = one turn and exit;
    omitted = interactive loop), aimed at a REGISTERED mesh agent identity
    (`adk agents ls`), not a free-form description. Caught by actually
    dispatching a real item, not by the unit self-test, which only ever
    exercised the injectable RunAgentFn and never the real argv shape."""
    agent = item.spec.get("agent", "")
    if not agent:
        return None
    task = item.spec.get("task", "")
    extra = item.spec.get("adk_args") or []
    return ["adk", "chat", agent, task, *extra]


def _real_run_agent(item: RunItem) -> tuple[int, str]:
    argv = _build_argv(item)
    if argv is None:
        return 1, "spec.agent is required (see `adk agents ls` for valid names)"
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=3600, check=False,
                               encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"could not run adk: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-4000:]  # tail only — a run's full log is not the queue's job


def dispatch_once(store: RunStore, *, worker_id: str,
                   run_agent_fn: RunAgentFn = _real_run_agent) -> Optional[RunItem]:
    """Claim and run the single highest-priority queued agent item. Returns
    the finished item, or None if there was nothing claimable — that is the
    ordinary "queue is empty" outcome, not an error."""
    claimed = store.claim_next(worker_id=worker_id, kind="agent")
    if claimed is None:
        return None
    running = store.start(claimed.id)
    if running is None:
        # Lost to a cancel between claim and start -- nothing to run.
        return None
    code, message = run_agent_fn(running)
    status = "done" if code == 0 else "failed"
    return store.finish(running.id, status=status, result={"code": code, "message": message})


def run_forever(store: RunStore, *, worker_id: str, poll_interval: float = 5.0,
                 run_agent_fn: RunAgentFn = _real_run_agent,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 max_iterations: Optional[int] = None) -> int:
    """Loop dispatching one item at a time. `max_iterations` exists only for
    tests -- production callers leave it None and rely on the process being
    stopped externally (a scheduled task's own lifecycle, or a signal)."""
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        result = dispatch_once(store, worker_id=worker_id, run_agent_fn=run_agent_fn)
        if result is None:
            sleep_fn(poll_interval)
    return 0


def _self_test() -> int:
    import tempfile

    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    # Pins the real bug found live: `adk start` has no --task flag at all
    # (confirmed via `adk start --help`). The real shape is `adk chat
    # <agent> <message>`.
    real_item = RunItem(id="r-test0000", kind="agent",
                         spec={"agent": "local-5090", "task": "verify the deploy"})
    argv = _build_argv(real_item)
    check("real invocation uses `adk chat`, never `adk start --task`",
          argv is not None and argv[:2] == ["adk", "chat"] and "--task" not in argv)
    check("the agent name and task land in the right argv positions",
          argv == ["adk", "chat", "local-5090", "verify the deploy"])

    no_agent_item = RunItem(id="r-test0001", kind="agent", spec={"task": "x"})
    check("a spec with no agent produces no argv (caught before spawning anything)",
          _build_argv(no_agent_item) is None)

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(td)
        low = store.submit("agent", {"task": "low", "agent": "local-5090"}, priority=1)
        high = store.submit("agent", {"task": "high", "agent": "local-5090"}, priority=9)
        order: list[str] = []

        def fake_run(item: RunItem) -> tuple[int, str]:
            order.append(item.id)
            return 0, "ok"

        result1 = dispatch_once(store, worker_id="w1", run_agent_fn=fake_run)
        check("dispatch_once() picks the HIGHER priority item first",
              result1 is not None and result1.id == high.id)
        check("the finished item is marked done", result1.status == "done")

        result2 = dispatch_once(store, worker_id="w1", run_agent_fn=fake_run)
        check("the second dispatch picks the remaining (lower priority) item",
              result2 is not None and result2.id == low.id)
        check("dispatch order matches priority, not submit order",
              order == [high.id, low.id])

        empty = dispatch_once(store, worker_id="w1", run_agent_fn=fake_run)
        check("dispatch_once() returns None (not an error) on an empty queue",
              empty is None)

        # A failing agent run is recorded as failed, not silently dropped.
        boom = store.submit("agent", {"task": "boom"}, priority=0)
        result3 = dispatch_once(
            store, worker_id="w1", run_agent_fn=lambda item: (1, "it exploded"))
        check("a nonzero exit is recorded as failed, with the message kept",
              result3 is not None and result3.id == boom.id
              and result3.status == "failed" and result3.result["message"] == "it exploded")

        # ci-kind items must never be picked up by the phase-1 agent dispatcher.
        store.submit("ci", {"workflow": "product-images.yml"}, priority=99)
        result4 = dispatch_once(store, worker_id="w1", run_agent_fn=fake_run)
        check("a kind=ci item is never claimed by the agent dispatcher (phase 1 scope)",
              result4 is None)

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worker-id", default="dispatcher")
    ap.add_argument("--once", action="store_true", help="dispatch a single item and exit")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    store = get_store()
    if args.once:
        result = dispatch_once(store, worker_id=args.worker_id)
        if result is None:
            print("nothing to dispatch")
            return 0
        print(f"{result.id}: {result.status}")
        return 0 if result.status == "done" else 1

    return run_forever(store, worker_id=args.worker_id, poll_interval=args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
