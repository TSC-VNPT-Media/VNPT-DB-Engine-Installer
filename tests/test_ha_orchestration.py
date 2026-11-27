import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import main
from core.inventory import InventoryManager
from core.platform_check import X86_64_V2_FLAGS
from core.state_enforcer import StateEnforcer


def probe(ip, epoch="100", firewalld="inactive", ports=""):
    return "\n".join([
        f"EPOCH={epoch}",
        "8192", "4", f"{ip}/24",
        "OS_ID=rocky", "OS_VERSION_ID=9.7", "OS_PRETTY_NAME=Rocky Linux 9.7",
        "ARCH=x86_64", "CPU_MODEL=Intel Xeon",
        "CPU_FLAGS=" + " ".join(X86_64_V2_FLAGS),
        ip,
        f"FIREWALLD={firewalld}",
        f"FW_PORTS={ports}",
        "FW_RICH=",
    ])


class EnvRunner:
    dry_run = False

    def __init__(self):
        self.dispatches = []
        self.commands = []

    def run_command(self, host, command, component_name="core"):
        self.commands.append((host, component_name, command))
        if component_name == "patroni-role":
            return (0 if str(host).endswith(".11") else 1), ""
        if "PG_VERSION" in command:
            return 1, ""
        if component_name == "pgbackrest-order":
            return (0 if str(host).endswith(".12") else 1), ""
        if component_name in {"db-config-files", "db-config-ack", "db-extensions"}:
            return 0, "t"
        if component_name == "db-config-reload":
            return 0, "1.0\nt"
        if component_name == "ssot-check":
            return 0, "{}"
        return 0, ""

    def dispatch_script(self, host, path, env_vars=None, component_name="core"):
        env = env_vars or {}
        self.dispatches.append((component_name, host, env.get("VDE_ETCD_PHASE"), env.get("VDE_PATRONI_EXPECT_PRIMARY")))
        return 0


class HaOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def inventory(self, profile="ha", hosts=None):
        if hosts is None:
            hosts = [{"ip": f"192.0.2.{11 + i}", "name": f"n{i}", "local": i == 0} for i in range(3)]
        path = Path(self.tmp.name) / "cluster.yml"
        path.write_text(yaml.safe_dump({"cluster": {"name": "test", "profile": profile},
                                        "topology": {"hosts": hosts}}))
        inv = InventoryManager(str(path))
        return inv, inv.load()

    def test_ha_etcd_configures_every_member_before_quorum_check(self):
        inv = InventoryManager(str(ROOT / "src/config/vde-cluster.example.yml"))
        cfg = inv.load()
        runner = EnvRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            main._run_pipeline(cfg, inv, runner, cfg["topology"]["hosts"], tags="etcd")
        etcd = [item for item in runner.dispatches if item[0] == "etcd"]
        hosts = [h["ip"] for h in cfg["topology"]["hosts"]]
        self.assertEqual([(phase, host) for _name, host, phase, _expect in etcd],
                         [("configure", host) for host in hosts] + [("verify", host) for host in hosts])

    def test_standalone_etcd_stays_a_single_phase(self):
        inv = InventoryManager(str(ROOT / "src/config/standalone.example.yml"))
        cfg = inv.load()
        runner = EnvRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            main._run_pipeline(cfg, inv, runner, cfg["topology"]["hosts"], tags="etcd")
        etcd = [item for item in runner.dispatches if item[0] == "etcd"]
        self.assertEqual([(item[2], item[1]) for item in etcd], [("all", cfg["topology"]["hosts"][0]["ip"])])

    def test_fresh_ha_patroni_only_bootstraps_the_first_node_as_primary(self):
        _inv, cfg = self.inventory()
        hosts = cfg["topology"]["hosts"]
        expectations = main._patroni_expectations(cfg, hosts, EnvRunner())
        self.assertEqual(expectations, {hosts[0]["ip"]: "1", hosts[1]["ip"]: "0", hosts[2]["ip"]: "0"})

    def test_existing_patroni_nodes_wait_for_health_not_only_the_first(self):
        _inv, cfg = self.inventory()
        runner = EnvRunner()
        runner.run_command = lambda host, command, component_name="core": (0, "")
        expectations = main._patroni_expectations(cfg, cfg["topology"]["hosts"], runner)
        self.assertEqual(set(expectations.values()), {"1"})

    def test_ha_db_ready_patches_dcs_once_then_reloads_every_node(self):
        inv, cfg = self.inventory()
        runner = EnvRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            main._run_pipeline(cfg, inv, runner, cfg["topology"]["hosts"], tags="guc")
        syncs = [item for item in runner.commands if item[1] == "patroni-config-sync"]
        reloads = [item for item in runner.commands if item[1] == "patroni-reload"]
        self.assertEqual(len(syncs), 1)
        self.assertEqual(len(reloads), 3)
        self.assertIn("/config", syncs[0][2])
        self.assertTrue(all("/reload" in item[2] for item in reloads))

    def test_primary_is_ordered_before_replicas_for_pgbackrest(self):
        _inv, cfg = self.inventory()
        ordered = main._order_primary_first(cfg, cfg["topology"]["hosts"], EnvRunner())
        self.assertEqual(ordered[0]["ip"], "192.0.2.12")

    def test_haproxy_backend_follows_pgbouncer_and_uses_postgres_when_pooler_is_off(self):
        inv, cfg = self.inventory()
        host = cfg["topology"]["hosts"][0]
        pooled = main._build_host_env(cfg, inv, host)
        self.assertEqual(pooled["VDE_HAPROXY_BACKEND_PORT"], pooled["VDE_PGBOUNCER_PORT"])
        cfg["settings_normalized"]["pgbouncer"]["enabled"] = False
        direct = main._build_host_env(cfg, inv, host)
        self.assertEqual(direct["VDE_HAPROXY_BACKEND_PORT"], direct["VDE_PG_PORT"])

    def test_component_scripts_encode_the_ha_safety_gates(self):
        etcd = (ROOT / "src/components/etcd/run.sh").read_text()
        patroni = (ROOT / "src/components/patroni/run.sh").read_text()
        backup = (ROOT / "src/components/pgbackrest/run.sh").read_text()
        self.assertIn("start --no-block", etcd)
        self.assertIn("verify_quorum", etcd)
        self.assertNotIn("/tmp/vde-objects.sql", patroni)
        self.assertIn("waldir: ${PGWAL}", patroni)
        self.assertIn("import etcd", patroni)
        self.assertIn("patroni_py", patroni)
        self.assertNotIn("python3 -c 'import etcd'", patroni)
        self.assertNotIn("backup-standby", backup)
        self.assertNotIn("stop-auto", backup)
        self.assertIn("/primary >/dev/null 2>&1 && ", backup)
        self.assertIn("VDE_BACKUP_S3_URI_STYLE", backup)
        self.assertIn("VDE_BACKUP_S3_VERIFY_TLS", backup)
        vip = (ROOT / "src/components/keepalived/run.sh").read_text()
        self.assertIn("unicast_src_ip", vip)
        self.assertIn("/etc/keepalived/vde-check.sh", vip)
        self.assertNotIn("echo", "\n".join(line for line in vip.splitlines() if "AUTH_PASS" in line))

    def test_enforce_adds_the_leader_when_only_a_replica_drifts_db_ready(self):
        inv = InventoryManager(str(ROOT / "src/config/vde-cluster.example.yml"))
        cfg = inv.load()
        enforcer = StateEnforcer(cfg)
        baselines = {}
        for host in cfg["topology"]["hosts"]:
            desired = enforcer.component_hashes(host, main._build_host_env(cfg, inv, host))
            observed = dict(desired)
            if host["ip"] == "10.10.20.12":
                observed["db_ready"] = "c" * 64
            baselines[host["ip"]] = json.dumps({"schema_version": 3, "components_hash": observed})
        runner = EnvRunner()
        original = runner.run_command

        def respond(host, command, component_name="core"):
            if component_name == "ssot-check":
                return 0, baselines[host]
            return original(host, command, component_name)

        runner.run_command = respond
        calls = []

        def capture(cfg, inv, runner, hosts, tags=None, force=False, allow_restart=False):
            calls.append((tags, [h["ip"] for h in hosts]))
            return {"scope": "partial", "completed": {}, "baseline_written": False}

        args = SimpleNamespace(config="ignored", limit=None, tags=None, restart=False, force=False)
        with patch.object(main, "_load_with_preflight", return_value=(inv, cfg, runner)), \
                patch.object(main, "_run_pipeline", side_effect=capture), \
                contextlib.redirect_stdout(io.StringIO()):
            main.cmd_enforce(args)
        self.assertEqual(calls, [("db_ready", ["10.10.20.12", "10.10.20.11"])])

    def test_active_firewall_without_cluster_ports_stops_and_prints_the_command(self):
        _inv, cfg = self.inventory()
        runner = EnvRunner()

        def respond(host, command, component_name="core"):
            return 0, probe(host, firewalld="active", ports="")

        runner.run_command = respond
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                main._discover_hardware(cfg, runner, strict=True)
        text = str(raised.exception)
        self.assertIn("firewall-cmd --permanent", text)
        self.assertIn("--add-port=2379/tcp", text)
        self.assertIn("--add-port=5432/tcp", text)
        self.assertIn("--add-port=5433/tcp", text)
        self.assertIn("firewall-cmd --reload", text)

    def test_clock_skew_above_ten_seconds_stops_and_small_skew_only_warns(self):
        _inv, cfg = self.inventory()
        runner = EnvRunner()

        def respond(host, command, component_name="core"):
            epoch = "100" if host.endswith(".11") else "130"
            return 0, probe(host, epoch=epoch)

        runner.run_command = respond
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Clock skew"):
                main._discover_hardware(cfg, runner, strict=True)

        def close(host, command, component_name="core"):
            epoch = "1000" if host.endswith(".12") else "1003"
            return 0, probe(host, epoch=epoch)

        runner.run_command = close
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            facts = main._discover_hardware(cfg, runner, strict=True)
        self.assertIn("192.0.2.11", facts)
        self.assertIn("[WARN] [preflight] Clock skew", output.getvalue())

    def test_remote_inventory_requires_an_ssh_client_before_discovery(self):
        _inv, cfg = self.inventory()
        runner = EnvRunner()
        called = []
        runner.run_command = lambda *args, **kwargs: called.append(args) or (0, "")
        with patch("main.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "OpenSSH"):
                main._discover_hardware(cfg, runner, strict=True)
        self.assertEqual(called, [])
        _inv, standalone = self.inventory("standalone", [{"ip": "192.0.2.11", "name": "n0"}])
        runner.run_command = lambda host, command, component_name="core": (0, probe(host))
        with patch("main.shutil.which", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertIn("192.0.2.11", main._discover_hardware(standalone, runner, strict=True))

    def test_init_ha_writes_ssh_and_external_pmm_without_a_local_server(self):
        path = Path(self.tmp.name) / "ha.yml"
        args = SimpleNamespace(output=str(path), force=True, ips="192.0.2.11,192.0.2.12,192.0.2.13",
                               profile="ha", name="ha", offline=True)
        with contextlib.redirect_stdout(io.StringIO()):
            main.cmd_init(args)
        text = path.read_text()
        self.assertIn("\nssh:\n", text)
        self.assertIn('key_file:', text)
        self.assertIn('mode: "external"', text)
        self.assertNotIn("server_install", text)
        self.assertIn('destination: "s3"', text)
        cfg = InventoryManager(str(path)).load()
        self.assertEqual(cfg["ssh"]["user"], "root")
        self.assertFalse(cfg["settings_normalized"]["pmm"]["enabled"])

    def test_status_limits_units_to_roles_and_prints_one_cluster_summary(self):
        _inv, cfg = self.inventory()
        balancer = {"ip": "192.0.2.20", "name": "lb", "roles": ["balancer"]}
        self.assertEqual(main._status_units(cfg, balancer), ["haproxy"])
        self.assertIn("etcd", main._status_units(cfg, cfg["topology"]["hosts"][0]))
        path = Path(self.tmp.name) / "status.yml"
        path.write_text(yaml.safe_dump({"cluster": {"name": "test", "profile": "standalone"},
                                        "topology": {"hosts": [{"ip": "192.0.2.11"}]}}))
        payload = json.dumps({"members": [{"name": "n0", "role": "leader", "state": "running", "lag": 0}]})

        class StatusRunner(EnvRunner):
            def run_command(self, host, command, component_name="core"):
                self.commands.append((host, component_name, command))
                if component_name == "cluster-summary":
                    return 0, payload
                if component_name == "pgbackrest-info":
                    return 0, "status: ok"
                return 0, ""

        output = io.StringIO()
        runner = StatusRunner()
        args = SimpleNamespace(config=str(path), limit=None, dry_run=False)
        with patch.object(main, "_runner", return_value=runner), contextlib.redirect_stdout(output):
            main.cmd_status(args)
        text = output.getvalue()
        self.assertIn("=== patroni ===", text)
        self.assertIn("n0", text)
        self.assertIn("leader", text)
        self.assertIn("pgBackRest: stanza=vde_cluster", text)
        self.assertEqual(text.count("=== patroni ==="), 1)
        info = [command for _host, name, command in runner.commands if name == "pgbackrest-info"]
        self.assertTrue(info and info[0].startswith("su - postgres -c "))

    def test_status_random_uses_one_eligible_node_and_node_pins_the_probe(self):
        hosts = [
            {"ip": "192.0.2.11", "name": "n0", "local": True, "roles": ["db", "etcd", "balancer"]},
            {"ip": "192.0.2.12", "name": "n1", "roles": ["db", "etcd"]},
            {"ip": "192.0.2.13", "name": "n2", "roles": ["db", "etcd", "balancer"]},
        ]
        _inv, _cfg = self.inventory("ha", hosts)
        path = Path(self.tmp.name) / "cluster.yml"
        pools = []

        def choose(pool):
            pools.append([host["ip"] for host in pool])
            return pool[-1]

        runner = EnvRunner()
        args = SimpleNamespace(config=str(path), limit=None, dry_run=False, random=False, node=None)
        with patch("main.random.choice", side_effect=choose), \
                patch.object(main, "_runner", return_value=runner), \
                contextlib.redirect_stdout(io.StringIO()):
            main.cmd_status(args)
        self.assertIn(["192.0.2.11", "192.0.2.12", "192.0.2.13"], pools)
        self.assertIn(["192.0.2.11", "192.0.2.13"], pools)
        etcd_hosts = [host for host, name, _command in runner.commands if name == "etcd-cluster"]
        self.assertEqual(etcd_hosts, ["192.0.2.13"])

        pinned = EnvRunner()
        pinned_args = SimpleNamespace(config=str(path), limit=None, dry_run=False, random=False, node=["n1"])
        output = io.StringIO()
        with patch("main.random.choice") as choice, \
                patch.object(main, "_runner", return_value=pinned), \
                contextlib.redirect_stdout(output):
            main.cmd_status(pinned_args)
        choice.assert_not_called()
        text = output.getvalue()
        self.assertIn("Probe mode: dedicated (n1)", text)
        self.assertIn("skip: none of the --node hosts run this component", text)
        etcd_pinned = [host for host, name, _command in pinned.commands if name == "etcd-cluster"]
        self.assertEqual(etcd_pinned, ["192.0.2.12"])
        haproxy_pinned = [host for host, name, command in pinned.commands
                          if name == "service-status" and "haproxy" in command]
        self.assertEqual(haproxy_pinned, [])

        both = SimpleNamespace(config=str(path), limit=None, dry_run=False, random=True, node=["n0"])
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(io.StringIO()):
                main.cmd_status(both)


if __name__ == "__main__":
    unittest.main()
