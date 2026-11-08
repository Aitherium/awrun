# Changelog

All notable changes to `awrun` are recorded here.

## [0.2.8] - 2026-11-08

### Added

- **awrun**: `deploy`, `ps` and `rm` verbs for standing agent deployments. Records live in their own directory beside the run queue; the act is delegated to a host-registered `plugins.AGENT_DEPLOYER` backend. `--dry-run` calls nothing, `rm` needs `--yes`, and a backend with no teardown is reported as `REMOTE NOT TOUCHED`.

## [0.2.7] - 2026-11-07

### Fixed

- **awrun**: the vault client in `egress.vault_resolver` is an optional host integration resolved by name at call time; the installed package no longer carries a monorepo import.

