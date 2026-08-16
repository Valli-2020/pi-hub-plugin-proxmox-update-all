"""Proxmox update-all plugin — apt upgrade every running LXC container.

For each configured Proxmox instance the plugin enumerates the instance's
running containers, resolves the Proxmox host to a *registry host id* (the
only thing :meth:`PluginContext.ssh_cmd` accepts), and runs

    pct exec <vmid> -- sh -c 'apt-get update && apt-get upgrade -y'

on the Proxmox host for every running container.

The ``apt`` chain is wrapped in ``sh -c`` deliberately: ``pct exec <vmid>
-- apt-get update && apt-get upgrade -y`` would run the *upgrade* half on
the Proxmox host itself, because ``&&`` binds in the outer SSH shell, not
inside the container.  The same reasoning applies to the reboot-required
probe and the dry-run check below — every command is a ``pct exec <vmid>
-- sh -c '...'`` so the whole command runs *inside* the container.

Enumeration goes through the Proxmox API (``get_proxmox_containers``)
rather than ``pct list`` because ``ssh_cmd`` only allows ``pct exec``
commands — raw ``pct list`` is rejected by the capability boundary.

v1.2.0 adds (see CHANGELOG.md):
  * dry-run mode (``POST /check``) — ``apt list --upgradable`` per container
  * reboot-required detection (``/var/run/reboot-required``) and an
    opt-in auto-reboot (needs ``proxmox.control``)
  * structured per-container results and a separate dry-run result table
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from pi_hub.plugins.base import (
    Plugin,
    PluginContext,
    RouteDef,
    TabUIDef,
    ActionDef,
    thread_cancel,
)

TASK_UPDATE = "update-all"
TASK_CHECK = "check"

# Force C locale so apt's "[upgradable]" tag (and any parseable output) is
# stable regardless of the container's language.
UPGRADE_CMD = (
    "sh -c 'LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get update "
    "&& LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get upgrade -y'"
)
# Probe for a pending reboot. /var/run/reboot-required is created by
# update-notifier-common; if that package is absent we cannot tell (Debian
# 12 ships needrestart but not update-notifier-common) — so we echo
# "unknown" instead of a silent no in that case.
REBOOT_CHECK_CMD = (
    "sh -c 'if dpkg -l update-notifier-common 2>/dev/null | grep -q ^ii; then "
    "test -f /var/run/reboot-required && echo yes || echo no; "
    "else echo unknown; fi'"
)
# Dry-run: refresh apt lists (C locale), then list upgradable packages.
DRYRUN_CMD = (
    "sh -c 'LC_ALL=C apt-get update >/dev/null 2>&1; "
    "LC_ALL=C apt list --upgradable 2>/dev/null'"
)

_VMID_RE = re.compile(r"^\d+$")

DEFAULT_CONFIG: dict[str, Any] = {
    "auto_reboot": False,        # opt-in; needs proxmox.control
    "continue_on_error": True,   # keep going past a failed container
}


class ProxmoxUpdateAllPlugin(Plugin):
    name = "proxmox-update-all"
    version = "1.2.0"
    description = (
        "apt update + upgrade on all running Proxmox containers, per "
        "instance, with dry-run and reboot detection (pct exec over SSH)"
    )
    min_core_version = "7.3.2"
    # proxmox.control is required for the opt-in auto-reboot.  NOTE: adding
    # it grants real container control (start/stop/reboot) unconditionally
    # at load time — re-review before upgrading.  Auto-reboot itself stays
    # off unless config.auto_reboot is explicitly enabled.
    capabilities: list[str] = [
        "proxmox.read", "ssh.execute", "hosts.read", "proxmox.control"
    ]

    def load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._running = False
        self._running_task: str | None = None   # "update-all" | "check" | None
        self._last: list[dict[str, Any]] = []        # completed update runs
        self._last_check: list[dict[str, Any]] = []   # completed dry runs
        # Seed config defaults only when actually missing (avoid SD churn).
        cfg = self.ctx.get_config()
        changed = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
                changed = True
        if changed:
            self.ctx.save_config()

    def get_routes(self) -> list[RouteDef]:
        return [
            RouteDef("GET", "/status", self.status_handler, caps=["admin"]),
            RouteDef("POST", "/update-all", self.update_all_handler, caps=["admin"]),
            RouteDef("POST", "/check", self.check_handler, caps=["admin"]),
            RouteDef("POST", "/config", self.config_handler, caps=["admin"]),
        ]

    def get_ui(self) -> list[Any]:
        return [
            TabUIDef(
                id="proxmox-update-all",
                label="Container updates",
                icon_svg=(
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
                    '<path d="M4 17 10 11 14 15 20 9"/>'
                    '<path d="M4 21h16"/></svg>'
                ),
                position=10,
                poll_endpoint="/api/plugin/proxmox-update-all/status",
                actions=[
                    ActionDef("update-all", "Update all containers",
                              style="primary", caps=["admin"]),
                    ActionDef("check", "Check for updates (dry-run)",
                              style="secondary", caps=["admin"]),
                ],
            ),
        ]

    # ── Route handlers ─────────────────────────────────────────────────────

    def status_handler(self, **kw: Any) -> tuple[Any, int]:
        with self._lock:
            running = self._running
            task = self._running_task
            last = [dict(r) for r in self._last]
            last_check = [dict(r) for r in self._last_check]
            cfg = dict(self.ctx.get_config())
        return {
            "running": running,
            "task": task,
            "config": cfg,
            "instances": last,
            "checks": last_check,
            "update_task": self.ctx.get_task_status(TASK_UPDATE),
            "check_task": self.ctx.get_task_status(TASK_CHECK),
        }, 200

    def config_handler(self, **kw: Any) -> tuple[Any, int]:
        body = kw.get("body") or {}
        with self._lock:
            cfg = self.ctx.get_config()
            changed = False
            for key in ("auto_reboot", "continue_on_error"):
                if key in body:
                    cfg[key] = bool(body[key])
                    changed = True
            if changed:
                self.ctx.save_config()
            out = dict(cfg)
        return {"config": out}, 200

    def update_all_handler(self, **kw: Any) -> tuple[Any, int]:
        with self._lock:
            if self._running:
                who = self._running_task or "a run"
                return {"error": f"{who} already in progress"}, 409
            self._running = True
            self._running_task = TASK_UPDATE
            self._last = []
        self.ctx.set_task_status(TASK_UPDATE, "running", "starting")
        self.ctx.run_task(TASK_UPDATE, self._run_update)
        return {"success": True, "started": True}, 202

    def check_handler(self, **kw: Any) -> tuple[Any, int]:
        with self._lock:
            if self._running:
                who = self._running_task or "a run"
                return {"error": f"{who} already in progress"}, 409
            self._running = True
            self._running_task = TASK_CHECK
            self._last_check = []
        self.ctx.set_task_status(TASK_CHECK, "running", "starting")
        self.ctx.run_task(TASK_CHECK, self._run_check)
        return {"success": True, "started": True}, 202

    # ── Background runs ─────────────────────────────────────────────────────

    def _instances(self) -> list[dict[str, Any]]:
        return [i for i in self.ctx.get_proxmox_instances() if i["enabled"]]

    def _host_id_map(self) -> dict[str, str]:
        """Map registry host (ip OR id/name) -> registry host id.

        ``ssh_cmd`` addresses *registry* hosts, but a Proxmox instance only
        carries a ``host`` field — which may be the IP (as the plugin
        originally assumed) OR the registry host id/hostname (how the Pi's
        config.json stores it).  Match on all three so either config style
        resolves.
        """
        out: dict[str, str] = {}
        for h in self.ctx.get_hosts():
            hid = str(h.get("id", ""))
            if not hid:
                continue
            for key in (h.get("ip"), h.get("id"), h.get("name")):
                if key:
                    out[str(key)] = hid
        return out

    def _run_update(self) -> None:
        try:
            cancel = thread_cancel()
            # Snapshot config ONCE under the lock so a settings change
            # mid-run cannot flip behaviour halfway through.
            with self._lock:
                cfg = dict(self.ctx.get_config())
            instances = self._instances()
            if not instances:
                self._finish(TASK_UPDATE, "done", "no Proxmox instances configured",
                             "No Proxmox instances configured", "info")
                return
            try:
                host_ids = self._host_id_map()
            except PermissionError as e:
                self._finish(TASK_UPDATE, "error", str(e), "Update run failed", "error")
                return

            total_errors = 0
            reboot_count = 0
            aborted = False
            for inst in instances:
                if cancel is not None and cancel.is_set():
                    break
                result, abort = self._update_instance(
                    inst, host_ids.get(inst["host"], ""), cancel, cfg)
                total_errors += len(result["errors"])
                reboot_count += result.get("reboot_count", 0)
                if abort:
                    aborted = True
                with self._lock:
                    self._last.append(result)
                if aborted:
                    break

            if cancel is not None and cancel.is_set():
                self._finish(TASK_UPDATE, "cancelled", "run cancelled",
                             "Update run cancelled", "info")
                return
            msg = "All containers updated"
            kind = "success"
            if reboot_count:
                msg += f"; {reboot_count} container(s) required a reboot"
            if total_errors:
                msg = f"Container updates finished with {total_errors} error(s)"
                if reboot_count:
                    msg += f"; {reboot_count} required a reboot"
                if aborted:
                    msg += " (stopped early)"
                kind = "warn"
            self._finish(TASK_UPDATE, "done", msg, msg, kind)
        except Exception as e:  # never leave single-flight wedged
            self._finish(TASK_UPDATE, "error", f"run crashed: {e}",
                         "Update run failed", "error")

    def _update_instance(
        self,
        inst: dict[str, Any],
        host_id: str,
        cancel: threading.Event | None,
        cfg: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Update every running container of one instance.

        Returns (result, abort): ``abort`` is True when a container failed
        and ``continue_on_error`` is off, signalling the caller to stop.
        """
        result: dict[str, Any] = {
            "instance": inst["id"],
            "host": inst["host"],
            "ok": False,
            "errors": [],
            "containers": [],
            "reboot_count": 0,
            "aborted": False,
            "detail": "",
            "updated_at": time.time(),
        }
        if not host_id:
            result["errors"].append(
                f"no registry host matches Proxmox host {inst['host']}")
            result["detail"] = "host resolution failed"
            return result, False

        listing = self.ctx.get_proxmox_containers(inst["id"])
        if not listing.get("success"):
            result["errors"].append(
                str(listing.get("error", "container list failed")))
            result["detail"] = "container list failed"
            return result, False

        running = [
            c for c in listing.get("containers", [])
            if str(c.get("status", "")) == "running"
            and _VMID_RE.match(str(c.get("vmid", "")))
        ]
        if not running:
            result["detail"] = "no running containers"
            result["ok"] = True
            return result, False

        continue_on_error = bool(cfg.get("continue_on_error", True))
        auto_reboot = bool(cfg.get("auto_reboot", False))
        abort = False
        for idx, c in enumerate(running, 1):
            if cancel is not None and cancel.is_set():
                break
            vmid = str(c["vmid"])
            name = str(c.get("name", vmid))
            self.ctx.set_task_status(
                TASK_UPDATE, "running",
                f"{inst['id']}: {name} ({idx}/{len(running)})")
            res = self.ctx.ssh_cmd(host_id, f"pct exec {vmid} -- {UPGRADE_CMD}")
            cont: dict[str, Any] = {
                "vmid": vmid, "name": name, "status": "ok",
                "packages_updated": None, "reboot_required": False,
                "reboot_status": None, "detail": "",
            }
            if not res.get("ok"):
                cont["status"] = "error"
                cont["detail"] = (res.get("output") or "ssh failed").strip()[:500]
                result["errors"].append(f"{vmid} ({name}): {cont['detail']}")
                if not continue_on_error:
                    abort = True
            else:
                rb = self.ctx.ssh_cmd(
                    host_id, f"pct exec {vmid} -- {REBOOT_CHECK_CMD}")
                if not rb.get("ok"):
                    cont["detail"] = (
                        "reboot check failed: " + (rb.get("output") or "")[
                            :200]).strip()
                else:
                    need = (rb.get("output") or "").strip()
                    if need == "yes":
                        cont["reboot_required"] = True
                    elif need == "unknown":
                        cont["detail"] = "reboot state unknown (no notifier installed)"
                if cont["reboot_required"]:
                    result["reboot_count"] += 1
                    if auto_reboot:
                        try:
                            ra = self.ctx.proxmox_action(
                                inst["id"], vmid, "reboot")
                            cont["reboot_status"] = (
                                "done" if ra.get("success") else "failed")
                            if not ra.get("success"):
                                cont["detail"] = ra.get("error", "reboot failed")
                        except Exception as e:  # runtime failure, not a cap gate
                            cont["reboot_status"] = "failed"
                            cont["detail"] = f"reboot failed: {e}"
            result["containers"].append(cont)
            if abort:
                break

        result["ok"] = not result["errors"]
        result["aborted"] = abort
        result["updated_at"] = time.time()
        return result, abort

    def _run_check(self) -> None:
        try:
            cancel = thread_cancel()
            with self._lock:
                cfg = dict(self.ctx.get_config())
            instances = self._instances()
            if not instances:
                self._finish(TASK_CHECK, "done", "no Proxmox instances configured",
                             "No Proxmox instances configured", "info")
                return
            try:
                host_ids = self._host_id_map()
            except PermissionError as e:
                self._finish(TASK_CHECK, "error", str(e), "Check failed", "error")
                return

            total_pkgs = 0
            containers_with_updates = 0
            aborted = False
            for inst in instances:
                if cancel is not None and cancel.is_set():
                    break
                result, abort = self._check_instance(
                    inst, host_ids.get(inst["host"], ""), cancel, cfg)
                total_pkgs += result.get("upgradable", 0)
                containers_with_updates += sum(
                    1 for c in result.get("containers", [])
                    if c.get("packages"))
                if abort:
                    aborted = True
                with self._lock:
                    self._last_check.append(result)
                if aborted:
                    break

            if cancel is not None and cancel.is_set():
                self._finish(TASK_CHECK, "cancelled", "check cancelled",
                             "Check cancelled", "info")
                return
            tail = " (stopped early)" if aborted else ""
            self._finish(
                TASK_CHECK, "done",
                f"{total_pkgs} upgradable package(s) across "
                f"{containers_with_updates} container(s){tail}",
                f"Dry-run complete: {containers_with_updates} container(s) "
                f"have updates ({total_pkgs} package(s))",
                "info")
        except Exception as e:  # never leave single-flight wedged
            self._finish(TASK_CHECK, "error", f"check crashed: {e}",
                         "Check failed", "error")

    def _check_instance(
        self,
        inst: dict[str, Any],
        host_id: str,
        cancel: threading.Event | None,
        cfg: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        result: dict[str, Any] = {
            "instance": inst["id"],
            "host": inst["host"],
            "ok": False,
            "errors": [],
            "containers": [],
            "upgradable": 0,
            "aborted": False,
            "detail": "",
            "updated_at": time.time(),
        }
        if not host_id:
            result["errors"].append(
                f"no registry host matches Proxmox host {inst['host']}")
            result["detail"] = "host resolution failed"
            return result, False

        listing = self.ctx.get_proxmox_containers(inst["id"])
        if not listing.get("success"):
            result["errors"].append(
                str(listing.get("error", "container list failed")))
            result["detail"] = "container list failed"
            return result, False

        running = [
            c for c in listing.get("containers", [])
            if str(c.get("status", "")) == "running"
            and _VMID_RE.match(str(c.get("vmid", "")))
        ]
        if not running:
            result["detail"] = "no running containers"
            result["ok"] = True
            return result, False

        continue_on_error = bool(cfg.get("continue_on_error", True))
        abort = False
        for idx, c in enumerate(running, 1):
            if cancel is not None and cancel.is_set():
                break
            vmid = str(c["vmid"])
            name = str(c.get("name", vmid))
            self.ctx.set_task_status(
                TASK_CHECK, "running",
                f"{inst['id']}: {name} ({idx}/{len(running)})")
            res = self.ctx.ssh_cmd(host_id, f"pct exec {vmid} -- {DRYRUN_CMD}")
            cont: dict[str, Any] = {
                "vmid": vmid, "name": name, "status": "ok",
                "packages": [], "detail": "",
            }
            if not res.get("ok"):
                cont["status"] = "error"
                cont["detail"] = (res.get("output") or "ssh failed").strip()[:500]
                result["errors"].append(f"{vmid} ({name}): {cont['detail']}")
                if not continue_on_error:
                    abort = True
            else:
                pkgs = [
                    ln.split("/")[0].strip()
                    for ln in res.get("output", "").splitlines()
                    if "upgradable" in ln
                ]
                cont["packages"] = pkgs
                result["upgradable"] += len(pkgs)
                if not pkgs:
                    cont["status"] = "skipped"
                    cont["detail"] = "up to date"
            result["containers"].append(cont)
            if abort:
                break

        result["ok"] = not result["errors"]
        result["aborted"] = abort
        result["updated_at"] = time.time()
        return result, abort

    # ── Helpers ────────────────────────────────────────────────────────────

    def _finish(self, task: str, status: str,
                message: str, toast: str, kind: str) -> None:
        # Clear single-flight state FIRST so a failure in set_task_status /
        # toast can never wedge the plugin (409 until reload).
        with self._lock:
            self._running = False
            self._running_task = None
        # The status/toast calls must not re-raise into the run's own
        # except handler (which would call _finish again -> duplicate toast).
        try:
            self.ctx.set_task_status(task, status, message)
            self.ctx.toast(toast, kind)
        except Exception:
            pass
