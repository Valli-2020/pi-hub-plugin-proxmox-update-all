"""Proxmox update-all plugin — apt upgrade every running LXC container.

For each configured Proxmox instance the plugin enumerates the instance's
containers, resolves the Proxmox host to a *registry host id* (the only
thing :meth:`PluginContext.ssh_cmd` accepts), and runs

    pct exec <vmid> -- sh -c 'apt-get update && apt-get upgrade -y'

on the Proxmox host for every running container.

The whole ``apt`` chain is wrapped in ``sh -c`` deliberately: ``pct exec
<vmid> -- apt-get update && apt-get upgrade -y`` would run the *upgrade*
half on the Proxmox host itself, because ``&&`` binds in the outer SSH
shell, not inside the container.

Enumeration goes through the Proxmox API (``get_proxmox_containers``)
rather than ``pct list`` because ``ssh_cmd`` only allows ``pct exec``
commands — raw ``pct list`` is rejected by the capability boundary.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from pi_hub.plugins.base import (
    Plugin,
    PluginContext,
    RouteDef,
    thread_cancel,
)

TASK_NAME = "update-all"
# Runs inside the container; DEBIAN_FRONTEND keeps dpkg from prompting.
UPGRADE_CMD = (
    "sh -c 'DEBIAN_FRONTEND=noninteractive apt-get update "
    "&& DEBIAN_FRONTEND=noninteractive apt-get upgrade -y'"
)


class ProxmoxUpdateAllPlugin(Plugin):
    name = "proxmox-update-all"
    version = "1.0.0"
    description = "Run apt-get update && upgrade on all Proxmox containers"
    min_core_version = "7.2.0"
    capabilities: list[str] = ["proxmox.read", "ssh.execute", "hosts.read"]

    def load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._running = False
        # Per-instance result of the last completed (or in-flight) run.
        self._last: list[dict[str, Any]] = []

    def get_routes(self) -> list[RouteDef]:
        return [
            RouteDef("GET", "/status", self.status_handler, caps=["admin"]),
            RouteDef("POST", "/update-all", self.update_all_handler, caps=["admin"]),
        ]

    # ── Route handlers ─────────────────────────────────────────────────────

    def status_handler(self, **kw: Any) -> tuple[Any, int]:
        with self._lock:
            running = self._running
            instances = [dict(r) for r in self._last]
        return {
            "running": running,
            "instances": instances,
            "task": self.ctx.get_task_status(TASK_NAME),
        }, 200

    def update_all_handler(self, **kw: Any) -> tuple[Any, int]:
        with self._lock:
            if self._running:
                return {"error": "Update run already in progress"}, 409
            self._running = True
            self._last = []
        self.ctx.set_task_status(TASK_NAME, "running", "starting")
        self.ctx.run_task(TASK_NAME, self._run)
        return {"success": True, "started": True}, 202

    # ── Background run ─────────────────────────────────────────────────────

    def _run(self) -> None:
        cancel = thread_cancel()
        instances = [i for i in self.ctx.get_proxmox_instances() if i["enabled"]]
        if not instances:
            self._finish("done", "no Proxmox instances configured",
                         "No Proxmox instances configured", "info")
            return

        try:
            host_ids = self._host_id_map()
        except PermissionError as e:
            # get_hosts() is gated on 'hosts.read' — surface the missing
            # capability instead of failing every instance one by one.
            self._finish("error", str(e), "Update run failed", "error")
            return

        total_errors = 0
        for inst in instances:
            if cancel is not None and cancel.is_set():
                break
            result = self._update_instance(inst, host_ids.get(inst["host"], ""), cancel)
            total_errors += len(result["errors"])
            with self._lock:
                self._last.append(result)

        if cancel is not None and cancel.is_set():
            self._finish("cancelled", "run cancelled", "Update run cancelled", "info")
            return
        if total_errors:
            self._finish("done", f"{total_errors} error(s)",
                         f"Container updates finished with {total_errors} error(s)",
                         "warn")
        else:
            self._finish("done", "all containers updated",
                         "All containers updated", "success")

    def _update_instance(
        self,
        inst: dict[str, Any],
        host_id: str,
        cancel: threading.Event | None,
    ) -> dict[str, Any]:
        """Update every running container of one instance."""
        result: dict[str, Any] = {
            "instance": inst["id"],
            "host": inst["host"],
            "ok": False,
            "errors": [],
            "updated": [],
            "updated_at": time.time(),
        }
        if not host_id:
            result["errors"].append(
                f"no registry host matches Proxmox host {inst['host']}"
            )
            return result

        listing = self.ctx.get_proxmox_containers(inst["id"])
        if not listing.get("success"):
            result["errors"].append(
                str(listing.get("error", "container list failed"))
            )
            return result

        running = [
            c for c in listing.get("containers", [])
            if str(c.get("status", "")) == "running" and str(c.get("vmid", "")).isdigit()
        ]
        for idx, c in enumerate(running, 1):
            if cancel is not None and cancel.is_set():
                break
            vmid = str(c["vmid"])
            name = str(c.get("name", vmid))
            self.ctx.set_task_status(
                TASK_NAME, "running",
                f"{inst['id']}: {name} ({idx}/{len(running)})",
            )
            res = self.ctx.ssh_cmd(host_id, f"pct exec {vmid} -- {UPGRADE_CMD}")
            if res.get("ok"):
                result["updated"].append(vmid)
            else:
                result["errors"].append(f"{vmid} ({name}): {res.get('output', '')}")

        result["ok"] = not result["errors"]
        result["updated_at"] = time.time()
        return result

    # ── Helpers ────────────────────────────────────────────────────────────

    def _host_id_map(self) -> dict[str, str]:
        """Map registry host IP → registry host id.

        ``ssh_cmd`` addresses *registry* hosts, but a Proxmox instance only
        carries an IP, so the two are matched on IP.
        """
        return {
            str(h.get("ip", "")): str(h.get("id", ""))
            for h in self.ctx.get_hosts()
            if h.get("ip") and h.get("id")
        }

    def _finish(self, status: str, message: str, toast: str, kind: str) -> None:
        self.ctx.set_task_status(TASK_NAME, status, message)
        self.ctx.toast(toast, kind)
        with self._lock:
            self._running = False
