import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from core.inventory import InventoryManager
from core.ssh_runner import SSHRunner
from core.state_enforcer import StateEnforcer


class RecordingRunner(SSHRunner):
    """Exercise production routing without executing installation commands."""

    def __init__(self, local_ip):
        super().__init__(local_hosts=[local_ip])
        self.calls = []
        self.restarted = set()

    def run_command(self, host, command, component_name="core"):
        self.calls.append((host, component_name, self._execution_command(host, command)))
        if component_name == "patroni-role":
            return (0 if self.is_local(host) else 1), ""
        if component_name == "pending-restart":
            return 0, "" if host in self.restarted else "shared_buffers"
        if component_name == "rolling-restart":
            self.restarted.add(host)
        if component_name == "db-extensions":
            return 0, "t"
        if component_name in {"db-config-files", "db-config-ack"}:
            return 0, "t"
        if component_name == "db-config-reload":
            return 0, "1.0\nt"
        if component_name == "ssot-check":
            return 0, "{}"
        return 0, ""

    def dispatch_script(self, host, script_path, env_vars=None, component_name="core"):
        self.calls.append((host, component_name, self._execution_command(host)))
        return 0


class LocalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cluster.yml"

    def inventory(self, profile="standalone", hosts=None):
        if hosts is None:
            hosts = [{"ip": "192.0.2.11"}]
        self.path.write_text(yaml.safe_dump({"cluster": {"name": "test", "profile": profile},
                                            "topology": {"hosts": hosts}}))
        inv = InventoryManager(str(self.path))
        return inv, inv.load()

    def ha(self, local_index=1):
        hosts = [{"ip": f"192.0.2.{11 + i}", "local": i == local_index} for i in range(3)]
        return self.inventory("ha", hosts)

    def test_standalone_implicitly_local_at_real_interface_ip(self):
        _, cfg = self.inventory()
        runner = main._runner(SimpleNamespace(), cfg)
        self.assertTrue(cfg["topology"]["hosts"][0]["local"])
        self.assertTrue(runner.is_local("192.0.2.11"))
        self.assertEqual(runner._execution_command("192.0.2.11"), ["/bin/bash"])

    def test_ha_can_run_on_second_node(self):
        _, cfg = self.ha()
        runner = main._runner(SimpleNamespace(), cfg)
        self.assertFalse(runner.is_local("192.0.2.11"))
        self.assertTrue(runner.is_local("192.0.2.12"))
        self.assertFalse(runner.is_local("192.0.2.13"))

    def test_invalid_local_topologies_are_rejected(self):
        cases = [
            ("standalone", [{"ip": "192.0.2.11", "local": False}]),
            ("standalone", [{"ip": "192.0.2.11"}, {"ip": "192.0.2.12"}]),
            ("standalone", [{"ip": "192.0.2.11", "local": "true"}]),
            ("ha", [{"ip": f"192.0.2.{i}"} for i in range(11, 14)]),
            ("ha", [{"ip": f"192.0.2.{i}", "local": True} for i in range(11, 14)]),
            ("ha", [{"ip": "192.0.2.11", "local": True}, {"ip": "192.0.2.11"}, {"ip": "192.0.2.13"}]),
            ("ha", [{"ip": "192.0.2.11", "local": True}, {"ip": "127.0.0.1"}, {"ip": "192.0.2.13"}]),
            ("ha", [{"ip": "127.0.0.1", "local": True}, {"ip": "192.0.2.12"}, {"ip": "192.0.2.13"}]),
            ("ha", [{"ip": "192.0.2.11", "local": True}, {"ip": "192.0.2.12", "ssh_host": "192.0.2.11"}, {"ip": "192.0.2.13"}]),
        ]
        for profile, hosts in cases:
            with self.subTest(profile=profile, hosts=hosts), self.assertRaises(ValueError):
                self.inventory(profile, hosts)

    def test_etcd_quorum_requires_odd_2n_plus_1_and_db_role(self):
        # 4 nodes all running etcd -> even count (not 2n+1), must fail
        even_etcd = [{"ip": f"192.0.2.{11 + i}", "local": i == 0} for i in range(4)]
        with self.assertRaisesRegex(ValueError, "2n\\+1"):
            self.inventory("ha", even_etcd)
        # 3 nodes but only 2 have etcd role -> must fail
        two_etcd = [
            {"ip": "192.0.2.11", "local": True, "roles": ["db", "etcd"]},
            {"ip": "192.0.2.12", "roles": ["db", "etcd"]},
            {"ip": "192.0.2.13", "roles": ["balancer"]},
        ]
        with self.assertRaisesRegex(ValueError, "2n\\+1"):
            self.inventory("ha", two_etcd)
        # 4 nodes with 3 etcd (2n+1) + 1 db/balancer-only -> valid
        three_etcd_four_hosts = [
            {"ip": "192.0.2.11", "local": True, "roles": ["db", "etcd", "balancer"]},
            {"ip": "192.0.2.12", "roles": ["db", "etcd", "balancer"]},
            {"ip": "192.0.2.13", "roles": ["etcd"]},
            {"ip": "192.0.2.14", "roles": ["db", "balancer"]},
        ]
        _, cfg = self.inventory("ha", three_etcd_four_hosts)
        self.assertEqual(len(cfg["topology"]["hosts"]), 4)
        # Standalone missing etcd or invalid role -> must fail
        with self.assertRaisesRegex(ValueError, "db.*etcd"):
            self.inventory("standalone", [{"ip": "192.0.2.11", "roles": ["db"]}])
        with self.assertRaisesRegex(ValueError, "Invalid roles"):
            self.inventory("standalone", [{"ip": "192.0.2.11", "roles": ["db", "etcd", "badrole"]}])

    def test_moving_control_to_another_node_does_not_change_service_hash(self):
        inv, first = self.ha(local_index=0)
        other, second = self.ha(local_index=1)
        self.assertEqual(StateEnforcer(first).get_desired_hash(), StateEnforcer(second).get_desired_hash())
        first_hosts = sorted(first["topology"]["hosts"], key=lambda h: h["ip"])
        second_hosts = sorted(second["topology"]["hosts"], key=lambda h: h["ip"])
        for left, right in zip(first_hosts, second_hosts):
            left_env = main._build_host_env(first, inv, left)
            right_env = main._build_host_env(second, other, right)
            self.assertEqual(StateEnforcer(first).component_hashes(left, left_env),
                             StateEnforcer(second).component_hashes(right, right_env))

    def test_plan_dry_run_does_not_discover_hardware(self):
        self.inventory()
        args = SimpleNamespace(config=str(self.path), offline=False, dry_run=True)
        with patch.object(main, "_discover_hardware") as discover, contextlib.redirect_stdout(io.StringIO()):
            main.cmd_plan(args)
        discover.assert_not_called()

    def test_local_sudo_uses_current_user_not_remote_ssh_user(self):
        runner = SSHRunner(user="root", port=22220, key_file="/missing/key", use_sudo=True,
                           local_hosts=["192.0.2.11"])
        with patch("core.ssh_runner.os.geteuid", return_value=1000):
            self.assertEqual(runner._execution_command("192.0.2.11", "id -u"),
                             ["sudo", "-n", "/bin/bash", "-c", "id -u"])
            self.assertEqual(runner._execution_command("192.0.2.11"), ["sudo", "-n", "/bin/bash"])

    def test_remote_sudo_port_and_key_are_preserved(self):
        key = Path(self.tmp.name) / "key"
        key.touch()
        runner = SSHRunner(user="ops", port=22220, key_file=str(key), use_sudo=True,
                           local_hosts=["192.0.2.11"])
        command = runner._execution_command("192.0.2.12", "printf '%s' '$2'")
        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[command.index("-p") + 1], "22220")
        self.assertEqual(command[command.index("-i") + 1], str(key))
        self.assertEqual(command[-2], "ops@192.0.2.12")
        self.assertTrue(command[-1].startswith("sudo -n /bin/bash -c "))

    def test_explicit_root_and_port_22_override_yaml(self):
        _, cfg = self.ha()
        cfg["ssh"] = {"user": "ops", "port": 22220}
        defaults = main._runner(SimpleNamespace(), cfg)
        overridden = main._runner(SimpleNamespace(user="root", port=22), cfg)
        self.assertEqual((defaults.user, defaults.port), ("ops", 22220))
        self.assertEqual((overridden.user, overridden.port), ("root", 22))

    def test_local_commands_and_scripts_really_execute_without_ssh(self):
        runner = SSHRunner(local_hosts=["192.0.2.11"], key_file="/missing/ssh-key")
        script = Path(self.tmp.name) / "safe.sh"
        output = Path(self.tmp.name) / "result.txt"
        script.write_text('printf "%s" "$VALUE" > "$OUTPUT"\n')
        with contextlib.redirect_stdout(io.StringIO()), patch("core.ssh_runner.subprocess.Popen", wraps=subprocess.Popen) as popen:
            rc, text = runner.run_command("192.0.2.11", "printf local-command")
            self.assertEqual(rc, 0)
            self.assertIn("local-command", text.splitlines())
            rc = runner.dispatch_script("192.0.2.11", str(script),
                                        {"VALUE": "value with 'quotes' and $sign", "OUTPUT": str(output)})
            self.assertEqual(rc, 0)
            self.assertEqual(output.read_text(), "value with 'quotes' and $sign")
            self.assertTrue(all(call.args[0][0] == "/bin/bash" for call in popen.call_args_list))

    def test_dry_run_never_starts_local_or_remote_processes(self):
        script = Path(self.tmp.name) / "never.sh"
        script.write_text("exit 99\n")
        runner = SSHRunner(dry_run=True, local_hosts=["192.0.2.11"])
        with patch("core.ssh_runner.subprocess.Popen") as popen, contextlib.redirect_stdout(io.StringIO()):
            for host in ["192.0.2.11", "192.0.2.12"]:
                self.assertEqual(runner.run_command(host, "exit 99")[0], 0)
                self.assertEqual(runner.dispatch_script(host, str(script)), 0)
        popen.assert_not_called()

    def test_preflight_checks_local_identity_before_remote(self):
        _, cfg = self.ha(local_index=2)
        runner = main._runner(SimpleNamespace(), cfg)
        with patch.object(runner, "run_command", return_value=(0, "4096\n4\n192.0.2.99/24\n192.0.2.99")) as run:
            with self.assertRaisesRegex(RuntimeError, "Run the tool on that node"):
                main._discover_hardware(cfg, runner, strict=True)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], "192.0.2.13")

    def test_preflight_prefers_configured_interface_over_default_route(self):
        _, cfg = self.inventory()
        runner = main._runner(SimpleNamespace(), cfg)
        hardware = "\n".join([
            "8192", "4", "192.0.2.11/24", "198.51.100.9/24",
            "OS_ID=rocky", "OS_VERSION_ID=9.7", "OS_PRETTY_NAME=Rocky Linux 9.7",
            "ARCH=x86_64", "CPU_MODEL=Intel Xeon",
            "CPU_FLAGS=cx16 lahf_lm popcnt sse4_2 ssse3",
            "198.51.100.9",
        ])
        with patch.object(runner, "run_command", return_value=(0, hardware)) as run, contextlib.redirect_stdout(io.StringIO()):
            facts = main._discover_hardware(cfg, runner, strict=True)
        self.assertEqual(facts["192.0.2.11"]["node_ip"], "192.0.2.11")
        self.assertEqual(facts["192.0.2.11"]["os_id"], "rocky")
        self.assertIn("/etc/os-release", run.call_args.args[1])

    def test_pipeline_routes_components_config_baseline_and_restarts(self):
        inv, cfg = self.ha()
        for tags in [None, "guc,hba"]:
            with self.subTest(tags=tags):
                runner = RecordingRunner("192.0.2.12")
                with contextlib.redirect_stdout(io.StringIO()):
                    main._run_pipeline(cfg, inv, runner, cfg["topology"]["hosts"], tags=tags, allow_restart=True)
                self.assertTrue(runner.calls)
                for host, _, command in runner.calls:
                    self.assertEqual(command[0], "/bin/bash" if host == "192.0.2.12" else "ssh")
                names = {name for _, name, _ in runner.calls}
                self.assertTrue({"patroni-config-sync", "rolling-restart"} <= names)
                self.assertIn("ssot-enforce", names)
                if tags is None:
                    self.assertTrue({"os_baseline", "etcd", "patroni", "haproxy"} <= names)

    def test_status_and_enforce_use_same_local_remote_routing(self):
        inv, cfg = self.ha()
        runner = RecordingRunner("192.0.2.12")
        args = SimpleNamespace(config=str(self.path), limit=None, tags="guc", restart=True, force=False)
        with patch.object(main, "_runner", return_value=runner), contextlib.redirect_stdout(io.StringIO()):
            main.cmd_status(args)
        with patch.object(main, "_load_with_preflight", return_value=(inv, cfg, runner)), contextlib.redirect_stdout(io.StringIO()):
            main.cmd_enforce(args)
        self.assertIn("service-status", {name for _, name, _ in runner.calls})
        self.assertIn("ssot-check", {name for _, name, _ in runner.calls})
        for host, _, command in runner.calls:
            self.assertEqual(command[0], "/bin/bash" if host == "192.0.2.12" else "ssh")

    def test_status_counts_members_and_leaders_from_named_table_columns(self):
        self.inventory()
        report = """--- endpoint health (cluster) ---
http://192.0.2.11:2379 is healthy: successfully committed proposal
http://192.0.2.12:2379 is healthy: successfully committed proposal
http://192.0.2.13:2379 is unhealthy: context deadline exceeded
--- members ---
+------------------+---------+------+------------+
| ID | STATUS | NAME | IS LEARNER |
+------------------+---------+------+------------+
| 78e6a19a254ec549 | started | pg1 | false |
| 92a21a031e119431 | started | pg2 | false |
| f25c1f4a65977814 | started | pg3 | true |
+------------------+---------+------+------------+
IS LEARNER: false = voting member; true = non-voting learner (not a health flag).
--- endpoint status ---
{"level":"warn","msg":"endpoint unreachable"}
| ENDPOINT | ID | IS LEADER | IS LEARNER | ERRORS |
| http://192.0.2.11:2379 | 78e6a19a254ec549 |      true |      false | |
| http://192.0.2.12:2379 | 92a21a031e119431 |     false |       true | |
"""
        runner = SSHRunner()

        def respond(host, command, component_name="core"):
            if component_name == "etcd-cluster":
                self.assertIn("member list -w table", command)
                self.assertNotIn("head -20", command)
                return 0, report
            return 0, ""

        args = SimpleNamespace(config=str(self.path), limit=None)
        output = io.StringIO()
        with patch.object(main, "_runner", return_value=runner), \
                patch.object(runner, "run_command", side_effect=respond), \
                contextlib.redirect_stdout(output):
            main.cmd_status(args)
        self.assertIn("DEGRADED/UNHEALTHY - 2 healthy / 1 unhealthy", output.getvalue())
        self.assertIn("3 member(s), leader(s)=1", output.getvalue())

    def test_etcd_table_rows_ignores_failed_commands_without_a_table(self):
        self.assertEqual(main._etcd_table_rows('Error: context deadline exceeded'), [])

    def test_non_root_install_requires_sudo_before_preflight(self):
        self.inventory()
        args = SimpleNamespace(config=str(self.path), command="apply", dry_run=False)
        with patch("main.os.geteuid", return_value=1000), patch.object(main, "_discover_hardware") as discover:
            with self.assertRaisesRegex(RuntimeError, "requires root or --sudo"):
                main._load_with_preflight(args)
        discover.assert_not_called()

    def test_init_marks_first_ip_local_and_cli_dry_run_never_uses_ssh_for_it(self):
        args = SimpleNamespace(output=str(self.path), force=False, ips="192.0.2.11,192.0.2.12,192.0.2.13",
                               profile="ha", name="test-ha")
        with contextlib.redirect_stdout(io.StringIO()):
            main.cmd_init(args)
        cfg = InventoryManager(str(self.path)).load()
        self.assertEqual([h["ip"] for h in cfg["topology"]["hosts"] if h["local"]], ["192.0.2.11"])
        result = subprocess.run([sys.executable, str(Path(main.__file__)), "apply", "-c", str(self.path), "--dry-run"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for line in result.stdout.splitlines():
            if line.startswith("[192.0.2.11]"):
                self.assertIn("[LOCAL]", line)
        self.assertIn("[192.0.2.11] [LOCAL]", result.stdout)
        self.assertIn("[192.0.2.12] [SSH]", result.stdout)


if __name__ == "__main__":
    unittest.main()
