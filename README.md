# Pi Hub Plugin: Proxmox Update All

Run `apt-get update && apt-get upgrade` on **every running Proxmox
container**, for each configured instance — straight from the Pi Hub
dashboard. One click, live progress, done.

## What it does

- Enumerates all running containers per configured Proxmox instance
  (via the PVE API).
- Runs `apt-get update && apt-get upgrade -y` inside each container
  through `pct exec` over SSH — **non-interactive** (`DEBIAN_FRONTEND`),
  so nothing waits on a dpkg prompt.
- Reports per-instance results: updated VMIDs, errors, timestamps.
- Runs as a background task: you can watch progress in the tab and
  leave the page — a toast announces the outcome.

## Install

1. Open **Settings → Plugins** in Pi Hub (needs an admin account).
2. Add the repository:

   ```
   https://github.com/Valli-2020/pi-hub-plugin-proxmox-update-all
   ```

3. Click **Scan** — the plugin appears in the list.
4. Click **Install**, then **Enable**.

The sidebar now shows a **Container updates** tab with an
**Update all containers** button and live status.

> Requires Pi Hub **7.3.2+** (plugin tabs need the v7.3.2 UI renderer)
> and SSH access from the Pi to the Proxmox host as `root`.

## Usage

| Action | Effect |
|--------|--------|
| **Update all containers** | Starts a background run over all enabled instances |
| **Tab status** | Live: idle / running (`instance: container (i/n)`) |
| **Toasts** | `All containers updated` / `N error(s)` / `Update run failed` |

While a run is in progress, a second trigger is refused (`409`) —
single-flight by design. Disabling the plugin cancels the run.

## Configuration

The plugin uses your existing Pi Hub configuration — nothing to set up:

- **Instances**: every enabled entry in `config.json` → `proxmox` list.
- **Host resolution**: the instance's `host` field is matched against
  the host registry by IP **or** hostname/id (whichever your config
  uses).
- **Containers**: only `running` ones are updated; stopped containers
  are skipped.

## Requirements

- Pi Hub 7.2.0+ (7.3.2+ for the UI tab)
- Passwordless SSH from the Pi to the Proxmox host (`ssh root@<host>`),
  same key the core already uses
- Containers must run a Debian/Ubuntu-based image with `apt-get`

## How it works (for plugin authors)

This repo is a reference Pi Hub plugin: a `Plugin` subclass with two
routes and a `TabUIDef` UI descriptor. It demonstrates:

- `PluginContext.get_proxmox_instances()` — list configured instances
- `PluginContext.get_proxmox_containers()` — enumerate containers
- `PluginContext.ssh_cmd()` — allowlisted `pct exec` SSH
- `PluginContext.run_task()` / `set_task_status()` / `toast()` —
  background work with visible progress
- `thread_cancel()` — cooperative cancellation on disable

## Release assets

Each GitHub release ships two assets:

- `pihub-plugin.json` — the Pi Hub plugin manifest
- `pi-hub-plugin-proxmox-update-all-<version>.tar.gz` — the plugin code
  (top-level dir `pi_hub_plugins/proxmox-update-all/`)

## License

MIT
