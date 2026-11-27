import tempfile
from pathlib import Path
import unittest

import yaml

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from core.inventory import InventoryManager
from core.platform_check import firewall_open_command, vrrp_allowed


def write_config(path, settings):
    path.write_text(yaml.safe_dump({
        "cluster": {"name": "test", "profile": "ha"},
        "topology": {"hosts": [
            {"ip": "192.0.2.11", "name": "n0", "local": True, "roles": ["db", "etcd", "balancer"]},
            {"ip": "192.0.2.12", "name": "n1", "roles": ["db", "etcd", "balancer"]},
            {"ip": "192.0.2.13", "name": "n2", "roles": ["db", "etcd"]},
        ]},
        "settings": settings,
    }))


class KeepalivedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cluster.yml"

    def load(self, settings):
        write_config(self.path, settings)
        inv = InventoryManager(str(self.path))
        return inv, inv.load()

    def test_disabled_by_default_and_absent_from_the_dag(self):
        inv, cfg = self.load({})
        self.assertFalse(cfg["settings_normalized"]["keepalived"]["enabled"])
        self.assertNotIn("keepalived", {item["name"] for item in cfg["components"]})
        env = main._build_host_env(cfg, inv, cfg["topology"]["hosts"][0])
        self.assertEqual(env["VDE_KEEPALIVED_VIP"], "")

    def test_virtual_ip_is_placed_on_balancers_with_one_preferred_node(self):
        inv, cfg = self.load({"keepalived": {
            "enabled": True, "virtual_ip": "192.0.2.20/24", "auth_pass": "vdeVip01",
            "priorities": {"n1": 200},
        }})
        component = next(item for item in cfg["components"] if item["name"] == "keepalived")
        self.assertEqual(component["dependencies"], ["haproxy"])
        self.assertEqual(component["target_roles"], ["balancer"])
        first = cfg["topology"]["hosts"][0]
        second = cfg["topology"]["hosts"][1]
        db_only = cfg["topology"]["hosts"][2]
        preferred = main._build_host_env(cfg, inv, second)
        backup = main._build_host_env(cfg, inv, first)
        untouched = main._build_host_env(cfg, inv, db_only)
        self.assertEqual(preferred["VDE_KEEPALIVED_STATE"], "MASTER")
        self.assertEqual(preferred["VDE_KEEPALIVED_PRIORITY"], "200")
        self.assertEqual(preferred["VDE_KEEPALIVED_PEERS"], "192.0.2.11")
        self.assertEqual(preferred["VDE_KEEPALIVED_PREFIX"], "24")
        self.assertEqual(backup["VDE_KEEPALIVED_STATE"], "BACKUP")
        self.assertEqual(backup["VDE_KEEPALIVED_PRIORITY"], "150")
        self.assertEqual(untouched["VDE_KEEPALIVED_VIP"], "")
        self.assertEqual(main._status_units(cfg, second), ["etcd", "patroni", "pgbouncer", "haproxy", "keepalived"])
        self.assertNotIn("keepalived", main._status_units(cfg, db_only))

    def test_rejects_a_node_address_a_long_password_and_haproxy_disabled(self):
        with self.assertRaisesRegex(ValueError, "already a node address"):
            self.load({"keepalived": {"enabled": True, "virtual_ip": "192.0.2.11", "auth_pass": "vdeVip01"}})
        with self.assertRaisesRegex(ValueError, "1-8"):
            self.load({"keepalived": {"enabled": True, "virtual_ip": "192.0.2.20", "auth_pass": "too-long"}})
        with self.assertRaisesRegex(ValueError, "haproxy.enabled"):
            self.load({"haproxy": {"enabled": False}, "keepalived": {
                "enabled": True, "virtual_ip": "192.0.2.20", "auth_pass": "vdeVip01"}})

    def test_firewall_hint_adds_vrrp_without_rewriting_tcp_ports(self):
        self.assertTrue(vrrp_allowed("inactive", "", ""))
        self.assertFalse(vrrp_allowed("active", "ssh", ""))
        self.assertTrue(vrrp_allowed("active", "ssh vrrp", ""))
        self.assertEqual(
            firewall_open_command([5433, "vrrp"]),
            "firewall-cmd --permanent --add-port=5433/tcp --add-service=vrrp && firewall-cmd --reload")


if __name__ == "__main__":
    unittest.main()
