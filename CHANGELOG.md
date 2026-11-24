# Changelog

All notable changes to `awrun` are recorded here.

## [Unreleased]

### Added

- **awrun**: `awrun swarm submit <tasks.json> --size 2B|4B|9B` sends a list of single, narrowly scoped tasks to hosted CI runners, one job and one small Bonsai 2 model per task (`.github/workflows/bonsai-swarm.yml`, also callable with `workflow_call`; `awrun/swarm.py` is the loop: read-only file tools over a clone of one public repository, a step budget, an optional `check` regex that grades the answer). `awrun swarm result <id>` prints the collected report: verdicts, answers, tokens per second per runner, and any task that reported nothing. The jobs hold no secret and never call back. A task list that carries a secret pattern, a local path, a private address or one of the host's own patterns (`AWRUN_SWARM_BOUNDARY`) is refused before anything is queued, never redacted. An answer given without one successful tool call is `UNGROUNDED`, an empty or non-string answer is not an answer, and a `check` that also accepts a placeholder such as `1.2.3` is refused: a verdict of `PASS` means the answer was read from the repository and matched a pinned value.
- **awrun**: `submit --kind ci --collect <artifact>` waits for the workflow run it started and downloads that artifact beside the queue (`artifacts/<run id>/`); the run is `done` when the artifact is on local disk, and its result names the run and the directory. The run is found by the queue id the workflow shows in its `run-name` (input `awrun_id`), never by "the newest run". Without `--collect` a `ci` run is unchanged: done once GitHub accepts the dispatch.
- **awrun**: an approval-backed submit grants only on a consume receipt Genesis SIGNED (Ed25519, the platform attestation key) for exactly this approval, action, payload and decider, verified against the key pinned by `AWSTORAGE_ATTEST_PUBKEY_FILE`, fresh (5 min) and not already spent on this host. The plane is reached through the platform's MCP gateway as the service registry resolves it (tools `approval_request`, `approval_status`, `approval_consume`, `approval_revoke`), which works from a host where Genesis is not published; no environment variable can name another URL (`AWRUN_APPROVAL_URL` is refused). Closes the forged-consume path (a caller-chosen URL answering `status: consumed`). Extra: `awrun[approval]`.
- **awrun**: gated kinds accept an owner's platform approval (`submit --approval <id>` or `AWRUN_APPROVAL_ID`), spent once through Genesis `/api/v1/agent-approvals` on exactly the runs being queued (action `awrun:<kind>`, digest of a text built from the runs). `submit --ask-owner <summary>` requests it when no awiam session authorizes the kind, reports where the owner was told, waits for the decision and then queues. A JSON list in `--spec-json` is one `node-op` batch: one approval, one consume. The queue options (priority, paths, limits, egress, lineage, name) are part of what is approved; the decider must be the asking account or be named in `AWRUN_APPROVERS` (checked before the spend); redirects are never followed with the bearer. The approver is the audited subject; every failure is a refusal.
- **awrun**: `node-op` takes `--action secret-status` and `secret-sync` (spec keys `slots`, `roll`, `dry_run`). They read which fleet secret pair a mesh node holds, or have the host worker write the store's pair into the node's declared env files. A spec never carries a value, and a spec that tries to is refused at submit.

### Fixed

- **awrun**: several workers on one queue no longer crash, or run an item twice, when they race for it. The store wrote an item's new content at its old path before renaming it, which put the file back in the old directory for the loser of the race; on Windows the loser then died on `FileExistsError` (and the worker that picked up the copy died finishing it), on POSIX it took over the winner's claim. Nothing is written at the old path now, and each move takes a per-item lock (`locks/<id>.lock`, created with `O_EXCL`), because a Windows rename reports success to both racers. A rename refused on Windows while another process has the file open is retried for under a second instead of killing the worker.
- **awrun**: a `comet-deploy` dispatch to an `https://` deployment service now verifies against the system store plus the CA bundle named by `AWRUN_CA_BUNDLE` (else `SSL_CERT_FILE`), the convention the relay lane already used. A fleet service signed by an internal CA was unreachable from a dispatcher whose environment did not happen to set `SSL_CERT_FILE`, and the failure read as "could not reach". A certificate the host does not trust is now reported as that, naming the variable to set. Verification is never turned off.
- **awrun**: `python -m awrun.dispatcher --self-test` judges the code, not the machine. The self-test left host admission ON, so on a runner whose `MemAvailable` sits under the default 12 GB floor every heavy kind was deferred and eight assertions went red -- dispatch order across kinds and the whole lease/backoff block -- with nothing wrong in the code they name (measured 2026-11-22 on `ubuntu-latest`: `MemAvailable 6.9 GB < 12 GB`). Admission is now forced off for the duration, exactly as `tests/conftest.py` already does for pytest, and the deferral path is asserted explicitly with the verdict forced, so it is covered on Windows and macOS too, where `/proc/pressure` does not exist.

## [0.2.10] - 2026-11-24

### Fixed

- **awrun**: a `--kind flow --script` module is registered in `sys.modules` before it is executed. A flow script that defines a `@dataclass` under `from __future__ import annotations` looks itself up there while it loads, so every such script died with `'NoneType' object has no attribute '__dict__'` and the run reported a failure that named neither the script nor the line.

## [0.2.9] - 2026-11-19

### Added

- **awrun**: kind `screen`, one awscreen crop-inspection batch. It is not a GPU kind (the vision model behind it is arbitrated by its scheduler, and the item caps its own concurrency at 4), so it submits without a `gpu` request. The spec carries file paths under the local queue root, never pixels. awrun has no built-in executor for it: a dispatcher started without the awscreen handler fails the item with the name of the worker to start (`python -m awscreen.awrun_kind`), and `awrun submit --kind screen` is refused because its owner submits it.

## [0.2.8] - 2026-11-08

### Added

- **awrun**: `deploy`, `ps` and `rm` verbs for standing agent deployments. Records live in their own directory beside the run queue; the act is delegated to a host-registered `plugins.AGENT_DEPLOYER` backend. `--dry-run` calls nothing, `rm` needs `--yes`, and a backend with no teardown is reported as `REMOTE NOT TOUCHED`.

## [0.2.7] - 2026-11-07

### Fixed

- **awrun**: the vault client in `egress.vault_resolver` is an optional host integration resolved by name at call time; the installed package no longer carries a monorepo import.

