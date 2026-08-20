# awrun — dynamic priority for the runs you actually control

GitHub Actions' self-hosted runner queue is FIFO-per-label, opaque, and
un-reprioritizable — there is no API to say "run this one next." That is a
hard platform limit, not a bug. What that limit produces in practice is a
"belt-fed machine gun": everything fires in arrival order, and a low-value
build queued a minute earlier blocks a high-value one indefinitely, with no
lever to move it up.

`awrun` does not try to reprioritize GitHub's own queue for other people's
pushes — it can't, and claiming otherwise would be a lie. What it delivers,
completely and correctly, is dynamic priority over the class of run that
actually matters most: work submitted *through it* — agentic/ADK runs
end-to-end (no GitHub Actions involvement at all), and ad-hoc
`workflow_dispatch` CI builds, so the order in which *we* fire them is
finally something a human or an agent can steer while it's in flight.

```bash
pip install -e AitherOS/packages/awrun
```

Python 3.10+. No hard dependencies for the core queue.

## What it is

A durable, cross-process priority queue (`store.py`) plus a CLI (`cli.py`)
and a dispatch loop (`dispatcher.py`). Modeled directly on
`awdk/adk/decisions/store.py` — one JSON file per item, atomic
`os.replace()` writes — with one deliberate improvement: claiming an item is
an `os.rename()` between status directories (`queued/` → `claimed/` → ...),
which is a genuine cross-process mutex with no lock file needed, rather than
a Python `threading.RLock()` that only protects one process.

```bash
awrun submit --kind agent --task "verify the fix" --priority 5
awrun submit --kind ci --workflow product-images.yml --ref develop \
  --field images=gargbot --field push=true --priority 8
awrun queue                       # highest priority first, oldest breaks ties
awrun bump r-7f3a9c2e --priority 10   # the literal missing feature
awrun cancel r-7f3a9c2e
awrun status r-7f3a9c2e
```

`AITHER_AWRUN_DIR` overrides the store location (default
`~/.aither/awrun/`) — set it in tests and for per-tenant isolation.

## What it is not

Not a new daemon. `decisions/store.py` already proves a durable, pollable,
cross-process queue doesn't need one — `awrun dispatch` (via
`awrun.dispatcher`) runs as a scheduled task or a one-shot `--once`
invocation, matching `AitherOS/config/routines/*.yaml`'s existing pattern.

Not a replacement for required PR-gate CI, which stays on GitHub Actions'
native triggers. `awrun` targets ad-hoc dispatches only.

## Phase 1 scope (this package, as shipped)

`kind=agent` dispatch only — the dispatcher claims the highest-priority
queued agent item and invokes `adk` directly. This is the part with no
GitHub platform limit in the way, so it's where correctness is proven first.
`kind=ci` dispatch, `awdk` tool integration, `awgit` lease-awareness and
`awrelay` status broadcast are the next phases — see the design doc this
package was built from for the full plan.

## Verification

```bash
python -m pytest AitherOS/packages/awrun/tests/
python -m awrun.cli self-test
python -m awrun.dispatcher --self-test
```
