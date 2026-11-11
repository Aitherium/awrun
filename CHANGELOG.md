# Changelog

All notable changes to `awrun` are recorded here.

## [Unreleased]

### Fixed

- **awrun**: several workers on one queue no longer crash, or run an item twice, when they race for it. The store wrote an item's new content at its old path before renaming it, which put the file back in the old directory for the loser of the race; on Windows the loser then died on `FileExistsError` (and the worker that picked up the copy died finishing it), on POSIX it took over the winner's claim. Nothing is written at the old path now, and each move takes a per-item lock (`locks/<id>.lock`, created with `O_EXCL`), because a Windows rename reports success to both racers. A rename refused on Windows while another process has the file open is retried for under a second instead of killing the worker.
- **awrun**: a `comet-deploy` dispatch to an `https://` deployment service now verifies against the system store plus the CA bundle named by `AWRUN_CA_BUNDLE` (else `SSL_CERT_FILE`), the convention the relay lane already used. A fleet service signed by an internal CA was unreachable from a dispatcher whose environment did not happen to set `SSL_CERT_FILE`, and the failure read as "could not reach". A certificate the host does not trust is now reported as that, naming the variable to set. Verification is never turned off.

## [0.2.8] - 2026-11-08

### Added

- **awrun**: `deploy`, `ps` and `rm` verbs for standing agent deployments. Records live in their own directory beside the run queue; the act is delegated to a host-registered `plugins.AGENT_DEPLOYER` backend. `--dry-run` calls nothing, `rm` needs `--yes`, and a backend with no teardown is reported as `REMOTE NOT TOUCHED`.

## [0.2.7] - 2026-11-07

### Fixed

- **awrun**: the vault client in `egress.vault_resolver` is an optional host integration resolved by name at call time; the installed package no longer carries a monorepo import.

