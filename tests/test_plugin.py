import importlib.util
import json
import os
import sys
import unittest

try:
    import pytest
except ImportError:  # run without pytest: see __main__ below
    pytest = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV = os.environ.get("PIHUB_DEV_TREE") or os.path.join(ROOT, "..", "..", "pi-hub-dev")
sys.path.insert(0, DEV)
PKG = os.path.join(ROOT, "pi_hub_plugins", "proxmox-update-all")

from pi_hub.plugins import contrib  # noqa: E402

spec = importlib.util.spec_from_file_location("pua", os.path.join(PKG, "__init__.py"))
pua = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pua)


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.saved = 0
        self.cmds = []
        self.statuses = []

    def get_config(self):
        return self.cfg

    def save_config(self):
        self.saved += 1

    def get_proxmox_instances(self):
        return [{"id": "pve1", "host": "h1", "enabled": True},
                {"id": "pve2", "host": "h2", "enabled": True},
                {"id": "off", "host": "h3", "enabled": False}]

    def get_hosts(self):
        return [{"id": "h1", "ip": "192.0.2.1"}, {"id": "h2", "ip": "192.0.2.2"}]

    def get_proxmox_containers(self, iid):
        return {"success": True, "containers": [
            {"vmid": "100", "name": "a", "status": "running"},
            {"vmid": "101", "name": "b", "status": "running"}]}

    def ssh_cmd(self, host, cmd, timeout=0):
        self.cmds.append((host, cmd, timeout))
        return {"ok": True, "output": "no" if "reboot-required" in cmd else ""}

    def set_task_status(self, *a):
        self.statuses.append(a)

    def toast(self, *a):
        pass

    def get_task_status(self, t):
        return {}

    def run_task(self, name, fn):
        fn()


def _skip(reason):
    if pytest is not None:
        pytest.skip(reason)
    raise unittest.SkipTest(reason)


def make(cfg=None):
    p = pua.ProxmoxUpdateAllPlugin()
    p.load(FakeCtx(cfg))
    return p


def test_manifest_matches_class():
    for path in (os.path.join(ROOT, "pihub-plugin.json"),
                 os.path.join(PKG, "pihub-plugin.json")):
        m = json.load(open(path))
        c = pua.ProxmoxUpdateAllPlugin
        assert m["version"] == c.version == "1.3.0"
        assert m["min_core_version"] == c.min_core_version == "8.3.0"
        assert sorted(m["capabilities"]) == sorted(c.capabilities)


def test_schema_core_validator_and_defaults():
    schema = make().get_config_schema()
    assert any(f["type"] == "textarea" for f in schema)
    if "list" not in getattr(contrib, "CONFIG_FIELD_TYPES", getattr(contrib, "FIELD_TYPES", ())) or \
            "textarea" not in contrib.CONFIG_FIELD_TYPES:
        _skip("core contrib lacks list/textarea (8.3.0 not built yet)")
    valid = contrib.validate_config_schema(schema)
    assert [f["name"] for f in valid] == [f["name"] for f in schema]
    assert {f["name"] for f in schema} == set(pua.DEFAULT_CONFIG)
    values = {f["name"]: f["default"] for f in schema}
    out, err = contrib.coerce_config_values(valid, values, {})
    assert err is None and set(out) == set(values)


def test_schema_covers_every_default_key():
    names = {f["name"] for f in make().get_config_schema()}
    assert names == set(pua.DEFAULT_CONFIG)


def test_load_seeds_defaults():
    p = make({})
    assert p.ctx.cfg == pua.DEFAULT_CONFIG and p.ctx.saved == 1


def test_migrate_old_live_shape():
    p = make()
    out = p.migrate_config("1.2.0", {"auto_reboot": False, "continue_on_error": True})
    assert out == pua.DEFAULT_CONFIG


def test_migrate_normalises_types():
    p = make()
    out = p.migrate_config("1.1.0", {"auto_reboot": "true", "exclude_vmids": [100, "101"],
                                     "update_timeout": "99999", "instances": ["pve1"]})
    assert out["auto_reboot"] is True
    assert out["exclude_vmids"] == "100\n101"
    assert out["instances"] == "pve1"
    assert out["update_timeout"] == 14400


def test_exclusions_instances_timeouts():
    p = make({"exclude_vmids": "100, 999", "instances": "pve1", "update_timeout": 77})
    p.update_all_handler()
    assert p.ctx.cmds and all(c[0] == "h1" for c in p.ctx.cmds)
    assert all(" 100 " not in c[1] for c in p.ctx.cmds)
    upg = [c for c in p.ctx.cmds if "apt-get upgrade" in c[1]]
    assert len(upg) == 1 and upg[0][2] == 77


def test_dry_run_only_never_upgrades():
    p = make({"dry_run": True})
    p.update_all_handler()
    assert p.ctx.cmds
    assert not any("apt-get upgrade" in c[1] for c in p.ctx.cmds)


def test_on_config_change_clears_stale_results():
    p = make()
    p._last = [{"x": 1}]
    p._last_check = [{"x": 1}]
    p.on_config_change({"instances": ""}, {"instances": "pve1"})
    assert p._last == [] and p._last_check == []
    p._last = [{"x": 1}]
    p.on_config_change({"auto_reboot": False}, {"auto_reboot": True})
    assert p._last == [{"x": 1}]


if __name__ == "__main__":  # fallback runner when pytest is not installed
    ran = skipped = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                ran += 1
            except unittest.SkipTest as e:
                skipped += 1
                print("SKIP", n, e)
    print("passed", ran, "skipped", skipped)
