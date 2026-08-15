# pi-hub-plugin-proxmox-update-all

Pi Hub plugin: run `apt-get update && apt-get upgrade` on every running
Proxmox container, per configured instance, via `pct exec` over SSH.

## Install

Add this repo as a plugin source in Pi Hub Settings → Plugins, scan,
then install.

## Release assets

Each release ships two assets:

- `pihub-plugin.json` — the Pi Hub plugin manifest
- `pi-hub-plugin-proxmox-update-all-<version>.tar.gz` — the plugin code
  (top-level dir `pi_hub_plugins/proxmox-update-all/`)
