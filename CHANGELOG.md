# Changelog

All notable changes to this plugin are documented here. Releases follow
the repo-tag = version convention; each GitHub release ships the two
assets `pihub-plugin.json` and the versioned tarball.

## [1.2.0] - 2026-08-16

### Added
- **Dry-run / check mode** (`Check for updates` button, `POST /check`):
  runs `apt list --upgradable` inside every running container and reports
  which packages would be upgraded — no changes applied. Results shown in a
  separate "Checks" table in the tab.
- **Reboot-required detection**: after an upgrade, each container is probed
  for `/var/run/reboot-required`; containers needing a reboot are counted
  and surfaced in the toast (`N container(s) need reboot`).
- **Opt-in auto-reboot**: when `config.auto_reboot` is enabled, containers
  flagged reboot-required are rebooted via `proxmox_action(... "reboot")`.
  Off by default.
- **Options endpoint** (`POST /config`): persist `auto_reboot` and
  `continue_on_error`. Seeded with safe defaults on load.
- **Structured per-container results**: each container now reports
  `vmid`, `name`, `status` (`ok`/`error`/`skipped`), `packages` (dry-run),
  `reboot_required`, `reboot_status`, and `detail`.

### Changed
- Manifest now declares the `proxmox.control` capability (required for the
  opt-in auto-reboot). **This grants real container control
  (start/stop/reboot) to the plugin at load time.** Re-review before
  upgrading. Auto-reboot itself remains off unless explicitly enabled.
- `continue_on_error` defaults to `true` (a single failing container no
  longer aborts the whole run).
- Update and dry-run results are kept in separate tables so a stale
  dry-run can never be mistaken for a completed update.

### Compatibility
- Requires Pi Hub **7.3.2+** (plugin tabs / `proxmox_action` support).

## [1.1.2] - 2026-08-15
- Documentation: full README, clearer description.

## [1.1.1] - 2026-08-15
- Fix: match registry host by id/name/ip (config-style agnostic).

## [1.1.0] - 2026-08-15
- Host resolution hardened; single-flight + cancellation.

## [1.0.0] - 2026-08-15
- Initial release: apt upgrade all running containers per instance.
