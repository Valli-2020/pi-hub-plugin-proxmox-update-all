# Pi Hub Plugin: Proxmox Update All

Run `apt-get update && apt-get upgrade` on **every running Proxmox
container**, for each configured instance — straight from the Pi Hub
dashboard. One click, live progress, done. v1.2.0 adds a dry-run check,
reboot detection, and opt-in auto-reboot.

## What it does

- Enumerates all running containers per configured Proxmox instance
  (via the PVE API).
- Runs `apt-get update && apt-get upgrade -y` inside each container
  through `pct exec` over SSH — **non-interactive** (`DEBIAN_FRONTEND`),
  so nothing waits on a dpkg prompt. The SSH timeout is raised to 30 min
  for these commands (core change in 7.4.1+) so a slow upgrade is never
  killed mid-transaction.
- **Dry-run** (`Check for updates`): runs `apt list --upgradable` inside
  each running container and reports which packages would be upgraded —
  no changes applied.
- **Reboot detection**: after an upgrade, each container is probed for
  `/var/run/reboot-required`. Containers that need a reboot are counted
  and surfaced in the result and toast. With auto-reboot enabled they are
  rebooted via the Proxmox API.
- Reports structured per-instance / per-container results (status, errors,
  packages, reboot state) and runs as a background task with a live
  progress tab and a toast on completion.

## Install

1. Open **Settings → Plugins** in Pi Hub (needs an admin account).
2. Add the repository:

   ```
   https://github.com/Valli-2020/pi-hub-plugin-proxmox-update-all
   ```

3. Click **Scan** — the plugin appears in the list.
4. Click **Install**, then **Enable**.

The sidebar now shows a **Container updates** tab with an
**Update all containers** button, a **Check for updates** button, and a
live status table.

> Requires Pi Hub **7.4.2+** for the long SSH timeout used by the upgrade
> command, and **7.3.2+** for the plugin-tab UI renderer. Passwordless SSH
> from the Pi to the Proxmox host as `root` is required.

## Usage

| Action | Effect |
|--------|--------|
| **Update all containers** | Starts a background upgrade run over all enabled instances |
| **Check for updates** | Dry-run: lists upgradable packages per container, no changes |
| **Tab status** | Live: idle / running (`instance: container (i/n)`) |
| **Toasts** | `All containers updated` / `N error(s)` / `N container(s) need reboot` |

While a run is in progress, a second trigger (update *or* check) is
refused (`409`) — single-flight by design. Disabling the plugin cancels
the run.

## Configuration

Options are persisted in the plugin's `config.json` (via `POST /config`):

- `auto_reboot` (default `false`): when `true`, containers flagged
  reboot-required are rebooted through the Proxmox API after upgrade.
- `continue_on_error` (default `true`): keep updating the remaining
  containers if one fails, instead of aborting the whole run.

The plugin uses your existing Pi Hub configuration for the rest:

- **Instances**: every enabled entry in `config.json` → `proxmox` list.
- **Host resolution**: the instance's `host` field is matched against
  the host registry by IP **or** hostname/id (whichever your config uses).
- **Containers**: only `running` ones are processed; stopped containers
  are skipped.

## Reboot detection requirement

`/var/run/reboot-required` is created by `update-notifier-common`, which
is **not** installed in many minimal Debian/Ubuntu LXC templates. If the
tooling is absent, the probe reports the container's reboot state as
`unknown` rather than a silent "no". Install `update-notifier-common`
(or `needrestart`) inside the container for accurate detection.

## ⚠ Capability note (important)

v1.2.0 declares the **`proxmox.control`** capability, which the plugin
needs for the opt-in auto-reboot. Declaring it grants real container
control (start/stop/reboot) to the plugin **at load time** — there is no
runtime capability negotiation. Auto-reboot stays *off* unless you
explicitly enable `auto_reboot`. Re-review the plugin before upgrading.

## Requirements

- Pi Hub 7.4.2+ (long SSH timeout), 7.3.2+ for the UI tab
- Passwordless SSH from the Pi to the Proxmox host (`ssh root@<host>`),
  same key the core already uses
- Containers must run a Debian/Ubuntu-based image with `apt-get`

## How it works (for plugin authors)

This repo is a reference Pi Hub plugin: a `Plugin` subclass with routes
and a `TabUIDef` UI descriptor. It demonstrates:

- `PluginContext.get_proxmox_instances()` — list configured instances
- `PluginContext.get_proxmox_containers()` — enumerate containers
- `PluginContext.ssh_cmd(timeout=)` — allowlisted `pct exec` SSH with a
  long timeout and structured error output
- `PluginContext.proxmox_action()` — reboot a container (needs
  `proxmox.control`)
- `PluginContext.run_task()` / `set_task_status()` / `toast()` —
  background work with visible progress
- `PluginContext.get_config()` / `save_config()` — per-plugin options
- `thread_cancel()` — cooperative cancellation on disable

## Release assets

Each GitHub release ships two assets:

- `pihub-plugin.json` — the Pi Hub plugin manifest
- `pi-hub-plugin-proxmox-update-all-<version>.tar.gz` — the plugin code
  (top-level dir `pi_hub_plugins/proxmox-update-all/`)

## License

MIT
