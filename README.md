# awrun — dynamic priority for the runs you actually control

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awrun/)**  ·  [Source](https://github.com/Aitherium/awrun)  ·  `pip install awrun`  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. [awnix](https://github.com/Aitherium/awnix) is the Linux underneath it; **awrun** is one of its 33 bricks — each installs on its own, runs offline, and needs no account.
>
> **Start here:** Queue two runs at different priorities and watch the urgent one overtake.

<!-- aither-header:end -->

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

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| [awrepl](https://github.com/Aitherium/awrepl) | what the agent believes the value is | the value, printed from the live session |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| **awrun** _(you are here)_ | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| [awrepl](https://github.com/Aitherium/awrepl) | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |

<div id="aither-constellation" data-self="awrun"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
