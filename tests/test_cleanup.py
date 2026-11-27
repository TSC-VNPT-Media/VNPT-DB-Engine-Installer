import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from core import cleanup
import main


def args(**overrides):
    values = dict(config=None, check=False, dry_run=False, sudo=False, decommissioned=False,
                  data_dir=None, wal_dir=None, etcd_data_dir=None, backup_dir=None)
    values.update(overrides)
    return SimpleNamespace(**values)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scan = cleanup.LocalCleanup(args())
        self.scan.findings["postgresql"].packages = ["percona-postgresql16-server-16.4-1.x86_64"]
        self.scan.findings["postgresql"].units = {"postgresql-16.service": "inactive"}
        self.data = self.root / "pgdata"
        self.data.mkdir()
        (self.data / "valuable").write_text("keep")
        self.scan.findings["postgresql"].data = [str(self.data)]

    def interact(self, answers, options=None):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(cleanup.os, "geteuid", return_value=0))
        stack.enter_context(patch.object(cleanup.sys.stdin, "isatty", return_value=True))
        stack.enter_context(patch.object(cleanup.LocalCleanup, "scan", return_value=self.scan))
        stack.enter_context(patch.object(self.scan, "inspect_cluster"))
        stack.enter_context(patch("builtins.input", side_effect=answers))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(cleanup, "execute_plan"))
        stack.enter_context(patch.object(cleanup, "verify_plan"))
        return stack

    def test_classifies_packages_without_removing_shared_os_libraries(self):
        cases = {"percona-postgresql16-server": "postgresql", "postgresql16-libs": "postgresql",
                 "percona-pgaudit16": "postgresql", "pgaudit16": "postgresql", "pg_repack_16": "postgresql", "percona-patroni": "patroni", "patroni": "patroni",
                 "etcd": "etcd", "etcdctl": "etcd", "pmm2-client": "pmm", "percona-haproxy": "haproxy", "haproxy": "haproxy", "keepalived": "keepalived", "pgbouncer": "pgbouncer", "pgbackrest": "pgbackrest",
                 "python3-etcd3": None, "python3-psycopg2": None, "percona-release": None, "vnpt-db-engine-release": None,
                 "libpq": None, "postgresqlish": None, "etcdish": None}
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(cleanup.package_component(name), expected)

    def test_default_plan_keeps_data_backup_and_configs(self):
        cfg = self.root / "config.yml"
        cfg.touch()
        self.scan.findings["postgresql"].configs = [str(cfg)]
        self.scan.backups = [str(self.root / "backup")]
        plan = cleanup.make_plan(self.scan, ["postgresql"])
        self.assertEqual(plan.paths, [])
        self.assertTrue((self.data / "valuable").exists())

    def test_scan_reads_rpm_units_and_binary_ownership_without_mutating(self):
        scan = cleanup.LocalCleanup(args())
        calls = []

        def respond(argv, timeout=30):
            calls.append(argv)
            if argv[:2] == ["rpm", "-qa"]:
                return 0, "etcd\tetcd-3.5.12-1.x86_64\nlibpq\tlibpq-16.4-1.x86_64\n", ""
            if argv[:2] == ["systemctl", "list-unit-files"]:
                return 0, "etcd.service enabled\nsshd.service enabled\n", ""
            if argv[:2] == ["systemctl", "show"]:
                return 0, "active\n", ""
            if argv[:2] == ["rpm", "-qf"]:
                return 0, "etcd-3.5.12-1.x86_64\n", ""
            self.fail(f"Unexpected command: {argv}")

        with patch.object(cleanup, "command", side_effect=respond), \
                patch.object(cleanup, "active_processes", return_value=["123:etcd"]), \
                patch.object(scan, "discover_paths"), patch.object(Path, "glob", return_value=[]), \
                patch.object(cleanup.shutil, "which", side_effect=lambda name: "/usr/bin/etcd" if name == "etcd" else None):
            scan.scan()
        self.assertEqual(scan.findings["etcd"].packages, ["etcd-3.5.12-1.x86_64"])
        self.assertEqual(scan.findings["etcd"].units, {"etcd.service": "active"})
        self.assertEqual(scan.findings["etcd"].unmanaged, [])
        self.assertEqual(scan.findings["etcd"].processes, ["123:etcd"])
        self.assertFalse(scan.issues)
        self.assertTrue(all(argv[0] in {"rpm", "systemctl"} for argv in calls))

    def test_membership_probe_failure_is_unknown_and_static_dcs_risk_survives_recheck(self):
        self.scan.config_cluster_issues = ["Patroni config declares multiple DCS endpoints"]
        self.scan.findings["etcd"].packages = ["etcd-3.5.12-1.x86_64"]
        with patch.object(cleanup, "command", return_value=(1, "", "timeout")):
            for _ in range(2):
                self.scan.inspect_cluster()
                self.assertIn("Patroni config declares multiple DCS endpoints", self.scan.cluster_issues)
                self.assertTrue(any("Cannot verify etcd" in issue for issue in self.scan.cluster_issues))

    def test_purge_only_removes_data_with_separate_opt_in(self):
        plan = cleanup.make_plan(self.scan, ["postgresql"], purge=True)
        self.assertNotIn(str(self.data), plan.paths)
        plan = cleanup.make_plan(self.scan, ["postgresql"], purge=True, delete_data=True)
        self.assertIn(str(self.data), plan.paths)

    def test_refuses_parent_that_contains_retained_backup(self):
        self.scan.backups = [str(self.data / "backup")]
        with self.assertRaisesRegex(ValueError, "retained data"):
            cleanup.make_plan(self.scan, ["postgresql"], delete_data=True)

    def test_refuses_config_path_inside_retained_data(self):
        self.scan.findings["postgresql"].configs = [str(self.data / "valuable")]
        with self.assertRaisesRegex(ValueError, "retained data"):
            cleanup.make_plan(self.scan, ["postgresql"], purge=True)

    def test_refuses_unsafe_parent_and_traversal_paths(self):
        for path in ("/", "/etc", "/data", "/var/lib", "/data/postgres", "/data/backup",
                     "relative/path", "/tmp/../etc/patroni", "/proc/1", "/sys/devices", "/dev/shm"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                cleanup.validate_path(path)

    def test_refuses_symlink_in_parent_but_allows_unlinking_final_symlink(self):
        link = self.root / "alias"
        link.symlink_to(self.data, target_is_directory=True)
        self.assertEqual(cleanup.validate_path(str(link)), str(link))
        with self.assertRaisesRegex(ValueError, "Symlink"):
            cleanup.validate_path(str(link / "valuable"))

    def test_refuses_mount_root_and_nested_mounts(self):
        mountinfo = f"1 0 0:1 / {self.data}/mounted rw - tmpfs tmpfs rw\n"
        with patch.object(Path, "read_text", return_value=mountinfo):
            with self.assertRaisesRegex(ValueError, "Unmount"):
                cleanup.validate_path(str(self.data))
            with self.assertRaisesRegex(ValueError, "Unmount"):
                cleanup.validate_path(str(self.data / "mounted"))

    def test_tablespace_discovery_only_targets_current_version_subdirectory(self):
        (self.data / "PG_VERSION").write_text("16\n")
        table_root = self.root / "tablespace"
        (table_root / "PG_16_202307071").mkdir(parents=True)
        (table_root / "PG_15_202209061").mkdir()
        links = self.data / "pg_tblspc"
        links.mkdir()
        (links / "12345").symlink_to(table_root, target_is_directory=True)
        self.scan.args.data_dir = str(self.data)
        with patch.object(self.scan, "read_yaml", return_value={}):
            self.scan.discover_paths()
        paths = self.scan.findings["postgresql"].data
        self.assertIn(str(table_root / "PG_16_202307071"), paths)
        self.assertNotIn(str(table_root), paths)
        self.assertNotIn(str(table_root / "PG_15_202209061"), paths)

    def test_backup_discovery_excludes_s3_and_remote_posix_repositories(self):
        local = self.root / "local-repo"
        s3 = self.root / "s3-path"
        remote = self.root / "remote-path"
        for path in (local, s3, remote):
            path.mkdir()
        config_path = "/etc/pgbackrest/pgbackrest.conf"
        config_text = (f"[global]\nrepo1-type=posix\nrepo1-path={local}\n"
                       f"repo2-type=s3\nrepo2-path={s3}\n"
                       f"repo3-host=backup.example\nrepo3-path={remote}\n")
        original_read = Path.read_text
        original_present = cleanup.present

        def read(path, *a, **kw):
            return config_text if str(path) == config_path else original_read(path, *a, **kw)

        with patch.object(self.scan, "read_yaml", return_value={}), \
                patch.object(cleanup, "present", side_effect=lambda p: str(p) == config_path or original_present(p)), \
                patch.object(Path, "read_text", read):
            self.scan.discover_paths()
        self.assertIn(str(local), self.scan.backups)
        self.assertNotIn(str(s3), self.scan.backups)
        self.assertNotIn(str(remote), self.scan.backups)

    def test_requires_patroni_selection_before_postgresql_or_etcd(self):
        self.scan.findings["patroni"].packages = ["percona-patroni-4.0-1.noarch"]
        for selected in (["postgresql"], ["etcd"]):
            with self.assertRaisesRegex(ValueError, "Patroni"):
                cleanup.make_plan(self.scan, selected)

    def test_incomplete_inventory_or_unmanaged_binary_blocks_removal(self):
        self.scan.issues = ["RPM inventory unavailable"]
        with self.assertRaisesRegex(ValueError, "UNVERIFIED"):
            cleanup.make_plan(self.scan, ["postgresql"])
        self.scan.issues.clear()
        self.scan.findings["postgresql"].unmanaged = ["/usr/local/bin/postgres"]
        with self.assertRaisesRegex(ValueError, "non-RPM"):
            cleanup.make_plan(self.scan, ["postgresql"])

    def test_failed_dependency_check_happens_before_any_stop_or_delete(self):
        plan = cleanup.make_plan(self.scan, ["postgresql"], delete_data=True)
        with patch.object(cleanup, "require", side_effect=RuntimeError("dependency")) as run:
            with self.assertRaisesRegex(RuntimeError, "dependency"):
                cleanup.execute_plan(plan)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:3], ["rpm", "-e", "--test"])
        self.assertTrue((self.data / "valuable").exists())

    def test_remaining_process_aborts_before_package_or_data_deletion(self):
        plan = cleanup.make_plan(self.scan, ["postgresql"], delete_data=True)
        with patch.object(cleanup, "require", return_value="") as run, \
                patch.object(cleanup, "active_processes", return_value=["123:postgres"]):
            with self.assertRaisesRegex(RuntimeError, "Processes still running"):
                cleanup.execute_plan(plan)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertNotIn(["rpm", "-e", *plan.packages], commands)
        self.assertTrue((self.data / "valuable").exists())

    def test_failed_rpm_erase_leaves_data_intact(self):
        plan = cleanup.make_plan(self.scan, ["postgresql"], delete_data=True)

        def respond(argv, timeout=30):
            if argv == ["rpm", "-e", *plan.packages]:
                raise RuntimeError("RPM erase failed")
            return ""

        with patch.object(cleanup, "require", side_effect=respond), \
                patch.object(cleanup, "active_processes", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "RPM erase failed"):
                cleanup.execute_plan(plan)
        self.assertTrue((self.data / "valuable").exists())

    def test_restarted_process_blocks_file_deletion_after_rpm_erase(self):
        plan = cleanup.make_plan(self.scan, ["postgresql"], delete_data=True)
        with patch.object(cleanup, "require", return_value=""), \
                patch.object(cleanup, "active_processes", side_effect=[[], ["123:postgres"]]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Processes restarted"):
                cleanup.execute_plan(plan)
        self.assertTrue((self.data / "valuable").exists())

    def test_execute_deletes_only_reviewed_temp_path_and_preserves_backup(self):
        backup = self.root / "backup" / "pgbackrest"
        backup.mkdir(parents=True)
        (backup / "valuable").touch()
        self.scan.backups = [str(backup)]
        plan = cleanup.make_plan(self.scan, ["postgresql"], delete_data=True)
        actual_require = cleanup.require

        def simulate_system_commands(argv, timeout=30):
            if argv[0] == "rm":
                self.assertEqual(argv[-1], str(self.data))
                return actual_require(argv, timeout)
            return ""

        with patch.object(cleanup, "require", side_effect=simulate_system_commands), \
                patch.object(cleanup, "active_processes", return_value=[]):
            cleanup.execute_plan(plan)
        self.assertFalse(self.data.exists())
        self.assertTrue((backup / "valuable").exists())

    def test_check_never_prompts_or_mutates(self):
        with self.interact([]), patch("builtins.input") as prompt, \
                patch.object(cleanup, "execute_plan") as execute:
            cleanup.interactive_cleanup(args(check=True))
        prompt.assert_not_called()
        execute.assert_not_called()

    def test_noninteractive_live_invocation_is_rejected(self):
        with self.interact([]), patch.object(cleanup.sys.stdin, "isatty", return_value=False), \
                patch.object(cleanup, "execute_plan") as execute:
            with self.assertRaisesRegex(ValueError, "requires a terminal"):
                cleanup.interactive_cleanup(args())
        execute.assert_not_called()

    def test_default_menu_exits_without_mutation(self):
        with self.interact([""]), patch.object(cleanup, "execute_plan") as execute:
            cleanup.interactive_cleanup(args())
        execute.assert_not_called()

    def test_invalid_confirmation_does_not_mutate(self):
        with self.interact(["2", "2", "yes"]), patch.object(cleanup, "execute_plan") as execute:
            cleanup.interactive_cleanup(args())
        execute.assert_not_called()

    def test_dry_run_never_mutates_even_when_ha_state_is_unknown(self):
        self.scan.cluster_issues = ["Cannot verify etcd membership"]
        with self.interact(["3", "2", "y"]), patch.object(cleanup, "execute_plan") as execute:
            cleanup.interactive_cleanup(args(dry_run=True))
        execute.assert_not_called()

    def test_live_ha_or_unknown_state_blocked_without_decommission_assertion(self):
        self.scan.cluster_issues = ["etcd has 3 members"]
        with self.interact(["2", "2"]), patch.object(cleanup, "execute_plan") as execute:
            with self.assertRaisesRegex(ValueError, "decommissioned"):
                cleanup.interactive_cleanup(args())
        execute.assert_not_called()

    def test_decommissioned_still_requires_exact_host_confirmation(self):
        self.scan.cluster_issues = ["etcd has 3 members"]
        with self.interact(["2", "2", "UNINSTALL " + cleanup.socket.gethostname()]), \
                patch.object(cleanup, "execute_plan") as execute:
            cleanup.interactive_cleanup(args(decommissioned=True))
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0].selected, ["postgresql"])

    def test_verification_does_not_claim_success_if_rpm_query_failed(self):
        plan = cleanup.make_plan(self.scan, ["postgresql"])
        with patch.object(cleanup, "command", return_value=(127, "", "cannot execute")), \
                patch.object(cleanup, "active_processes", return_value=[]), \
                patch.object(cleanup.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                cleanup.verify_plan(plan)

    def test_backup_deletion_requires_pgbackrest_selection_and_separate_opt_in(self):
        backup = self.root / "backup" / "pgbackrest"
        backup.mkdir(parents=True)
        self.scan.backups = [str(backup)]
        with self.assertRaisesRegex(ValueError, "Select pgBackRest"):
            cleanup.make_plan(self.scan, ["postgresql"], delete_backups=True)
        self.assertNotIn(str(backup), cleanup.make_plan(self.scan, ["pgbackrest"], purge=True).paths)
        self.assertIn(str(backup), cleanup.make_plan(self.scan, ["pgbackrest"], purge=True, delete_backups=True).paths)

    def test_cli_cleanup_has_no_implicit_sample_inventory(self):
        with patch.object(sys, "argv", ["vde-install", "cleanup", "--check"]), \
                patch.object(main, "cmd_cleanup") as handler:
            main.main()
        self.assertIsNone(handler.call_args.args[0].config)


class ClusterCleanupTests(unittest.TestCase):
    def setUp(self):
        self.ha_cfg = {
            "cluster": {"name": "dbe-prod", "profile": "ha", "version": "16"},
            "topology": {
                "hosts": [
                    {"ip": "10.0.0.11", "name": "dbe-node-01", "local": True, "roles": ["db", "etcd", "balancer"]},
                    {"ip": "10.0.0.12", "name": "dbe-node-02", "local": False, "roles": ["db", "etcd", "balancer"]},
                    {"ip": "10.0.0.13", "name": "dbe-node-03", "local": False, "roles": ["db", "etcd", "balancer"]},
                ]
            },
            "settings": {"storage": {"data_dir": "/data/postgres/16/data", "wal_dir": "/data/postgres/16/wal", "backup_dir": "/data/backup"}},
            "effective_config": {"ports": {"patroni_rest": 8008, "etcd_client": 2379}},
        }

    def _make_scan_payload(self, hostname, role, has_db=True, has_etcd=True):
        findings = {
            key: {"packages": [], "units": {}, "binaries": [], "unmanaged": [], "configs": [], "data": [], "processes": []}
            for key in cleanup.COMPONENTS
        }
        findings["haproxy"]["packages"] = ["haproxy-2.8-1.x86_64"]
        findings["haproxy"]["units"] = {"haproxy.service": "active"}
        findings["haproxy"]["configs"] = ["/etc/haproxy"]
        if has_db:
            findings["patroni"]["packages"] = ["percona-patroni-4.0-1.noarch"]
            findings["patroni"]["units"] = {"patroni.service": "active"}
            findings["patroni"]["configs"] = ["/etc/patroni"]
            findings["postgresql"]["packages"] = ["percona-postgresql16-server-16.4-1.x86_64"]
            findings["postgresql"]["units"] = {"postgresql-16.service": "inactive"}
            findings["postgresql"]["data"] = ["/data/postgres/16/data", "/data/postgres/16/wal"]
        if has_etcd:
            findings["etcd"]["packages"] = ["etcd-3.5.12-1.x86_64"]
            findings["etcd"]["units"] = {"etcd.service": "active"}
            findings["etcd"]["configs"] = ["/etc/etcd"]
            findings["etcd"]["data"] = ["/var/lib/etcd/default.etcd"]
        return {
            "hostname": hostname,
            "findings": findings,
            "backups": ["/data/backup/pgbackrest"],
            "issues": [],
            "config_cluster_issues": ["Patroni config declares multiple DCS endpoints"] if has_db else [],
            "cluster_issues": ["YAML declares HA topology", "etcd has 3 members"] if has_etcd else ["YAML declares HA topology"],
            "has_pgbackrest_cron": False,
            "patroni_role": role,
            "patroni_members": [
                {"name": "dbe-node-01", "host": "10.0.0.11", "role": "leader"},
                {"name": "dbe-node-02", "host": "10.0.0.12", "role": "replica"},
                {"name": "dbe-node-03", "host": "10.0.0.13", "role": "sync_standby"},
            ] if has_db else [],
            "etcd_member_count": 3 if has_etcd else None,
        }

    def _cluster_args(self, **overrides):
        values = dict(
            config="cluster-ha.yml", cluster=True, check=False, dry_run=False,
            sudo=True, decommissioned=False, limit=None,
            data_dir=None, wal_dir=None, etcd_data_dir=None, backup_dir=None,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_order_cluster_nodes_non_db_then_replicas_then_primary_and_remote_before_local(self):
        hosts = [
            {"ip": "10.0.0.11", "name": "node-primary-local", "local": True, "roles": ["db", "etcd"]},
            {"ip": "10.0.0.12", "name": "node-replica-remote", "local": False, "roles": ["db", "etcd"]},
            {"ip": "10.0.0.13", "name": "node-balancer-remote", "local": False, "roles": ["balancer"]},
            {"ip": "10.0.0.14", "name": "node-replica-local", "local": True, "roles": ["db", "etcd"]},
        ]
        ctxs = [
            cleanup.NodeCleanupContext(hosts[0], "10.0.0.11", "n1", cleanup._scan_from_payload(self._cluster_args(), self._make_scan_payload("n1", "leader"), False, "ha"), patroni_role="leader"),
            cleanup.NodeCleanupContext(hosts[1], "10.0.0.12", "n2", cleanup._scan_from_payload(self._cluster_args(), self._make_scan_payload("n2", "replica"), True, "ha"), patroni_role="replica"),
            cleanup.NodeCleanupContext(hosts[2], "10.0.0.13", "n3", cleanup._scan_from_payload(self._cluster_args(), self._make_scan_payload("n3", None, has_db=False, has_etcd=False), True, "ha"), patroni_role=None),
            cleanup.NodeCleanupContext(hosts[3], "10.0.0.14", "n4", cleanup._scan_from_payload(self._cluster_args(), self._make_scan_payload("n4", "sync_standby"), False, "ha"), patroni_role="sync_standby"),
        ]
        ordered = cleanup.order_cluster_nodes(ctxs)
        self.assertEqual([c.host["name"] for c in ordered], [
            "node-balancer-remote",
            "node-replica-remote",
            "node-replica-local",
            "node-primary-local",
        ])

    def test_cluster_preflight_failure_aborts_before_any_stop(self):
        calls = []

        def fake_action(runner, target, action, payload, timeout=None):
            calls.append((target, action, payload))
            if action == "preflight" and target == "10.0.0.12":
                raise RuntimeError("[10.0.0.12] preflight failed: rpm dependency conflict")
            return {"ok": True}

        ctxs = []
        for h, role in zip(self.ha_cfg["topology"]["hosts"], ("leader", "replica", "sync_standby")):
            scan = cleanup._scan_from_payload(self._cluster_args(), self._make_scan_payload(h["name"], role), not h["local"], "ha")
            plan = cleanup.make_plan(scan, ["haproxy", "patroni", "postgresql", "etcd"])
            ctxs.append(cleanup.NodeCleanupContext(h, h["ip"], h["name"], scan, patroni_role=role, plan=plan))

        ordered = cleanup.order_cluster_nodes(ctxs)
        with patch.object(cleanup, "_run_host_action", side_effect=fake_action), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "dependency conflict"):
                cleanup.execute_cluster_plans(SimpleNamespace(use_sudo=True), ordered)
        self.assertTrue(all(action == "preflight" for _, action, _ in calls))

    def test_cluster_execute_runs_ordered_ha_phases_replicas_before_primary_and_etcd_last(self):
        events = []

        def fake_action(runner, target, action, payload, timeout=None):
            events.append((action, target, payload.get("units") or payload.get("selected")))
            return {"ok": True, "verified": True}

        ctxs = []
        for h, role in zip(self.ha_cfg["topology"]["hosts"], ("leader", "replica", "sync_standby")):
            scan = cleanup._scan_from_payload(self._cluster_args(), self._make_scan_payload(h["name"], role), not h["local"], "ha")
            plan = cleanup.make_plan(scan, ["haproxy", "patroni", "postgresql", "etcd"])
            ctxs.append(cleanup.NodeCleanupContext(h, h["ip"], h["name"], scan, patroni_role=role, plan=plan))

        ordered = cleanup.order_cluster_nodes(ctxs)
        with patch.object(cleanup, "_run_host_action", side_effect=fake_action), \
                contextlib.redirect_stdout(io.StringIO()):
            cleanup.execute_cluster_plans(SimpleNamespace(use_sudo=True), ordered)

        actions = [e[0] for e in events]
        # 3 preflight -> 3 access stop -> 3 db stop -> 3 etcd stop -> 3 erase -> 3 verify
        self.assertEqual(actions, [
            "preflight", "preflight", "preflight",
            "stop_units", "stop_units", "stop_units",
            "stop_units", "stop_units", "stop_units",
            "stop_units", "stop_units", "stop_units",
            "erase_and_delete", "erase_and_delete", "erase_and_delete",
            "verify", "verify", "verify",
        ])
        # Access tier stopped first on all 3 nodes
        self.assertEqual([e[2] for e in events[3:6]], [["haproxy.service"]] * 3)
        # DB tier stops replicas (10.0.0.12, 10.0.0.13) BEFORE primary (10.0.0.11)
        self.assertEqual([e[1] for e in events[6:9]], ["10.0.0.12", "10.0.0.13", "10.0.0.11"])
        self.assertEqual(events[6][2], ["patroni.service", "postgresql-16.service"])
        # etcd stopped AFTER patroni is stopped everywhere, with local coordinator last
        self.assertEqual([e[1] for e in events[9:12]], ["10.0.0.12", "10.0.0.13", "10.0.0.11"])
        self.assertEqual([e[2] for e in events[9:12]], [["etcd.service"]] * 3)

    def test_cluster_full_teardown_requires_exact_cluster_confirmation(self):
        roles = {"10.0.0.11": "leader", "10.0.0.12": "replica", "10.0.0.13": "sync_standby"}

        def fake_action(runner, target, action, payload, timeout=None):
            self.assertEqual(action, "scan")
            return self._make_scan_payload(payload["host_name"], roles[target])

        runner = SimpleNamespace(use_sudo=True, user="ops")
        # Wrong phrase cancels without executing
        with patch.object(cleanup, "_run_host_action", side_effect=fake_action), \
                patch.object(cleanup.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["2", "all", "UNINSTALL dbe-prod"]), \
                patch.object(cleanup, "execute_cluster_plans") as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            cleanup.interactive_cluster_cleanup(
                self._cluster_args(), self.ha_cfg, runner,
                self.ha_cfg["topology"]["hosts"], lambda h: h["ip"],
            )
        execute.assert_not_called()

        # Exact phrase executes full HA teardown without needing --decommissioned
        with patch.object(cleanup, "_run_host_action", side_effect=fake_action), \
                patch.object(cleanup.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["2", "all", "UNINSTALL CLUSTER dbe-prod"]), \
                patch.object(cleanup, "execute_cluster_plans") as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            cleanup.interactive_cluster_cleanup(
                self._cluster_args(), self.ha_cfg, runner,
                self.ha_cfg["topology"]["hosts"], lambda h: h["ip"],
            )
        execute.assert_called_once()
        self.assertEqual(len(execute.call_args.args[1]), 3)

    def test_cluster_partial_ha_subset_requires_decommissioned(self):
        def fake_action(runner, target, action, payload, timeout=None):
            self.assertEqual(action, "scan")
            return self._make_scan_payload(payload["host_name"], "replica")

        runner = SimpleNamespace(use_sudo=True, user="ops")
        subset = [self.ha_cfg["topology"]["hosts"][1]]  # only dbe-node-02

        with patch.object(cleanup, "_run_host_action", side_effect=fake_action), \
                patch.object(cleanup.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["2", "all"]), \
                patch.object(cleanup, "execute_cluster_plans") as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "decommissioned"):
                cleanup.interactive_cluster_cleanup(
                    self._cluster_args(limit="dbe-node-02"), self.ha_cfg, runner,
                    subset, lambda h: h["ip"],
                )
        execute.assert_not_called()

        # With --decommissioned + exact SUBSET phrase, partial HA cleanup proceeds
        with patch.object(cleanup, "_run_host_action", side_effect=fake_action), \
                patch.object(cleanup.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["2", "all", "UNINSTALL SUBSET dbe-prod"]), \
                patch.object(cleanup, "execute_cluster_plans") as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            cleanup.interactive_cluster_cleanup(
                self._cluster_args(limit="dbe-node-02", decommissioned=True), self.ha_cfg, runner,
                subset, lambda h: h["ip"],
            )
        execute.assert_called_once()

    def test_cluster_check_and_dry_run_never_mutate(self):
        roles = {"10.0.0.11": "leader", "10.0.0.12": "replica", "10.0.0.13": "sync_standby"}

        def fake_action(runner, target, action, payload, timeout=None):
            self.assertEqual(action, "scan")
            return self._make_scan_payload(payload["host_name"], roles[target])

        runner = SimpleNamespace(use_sudo=False, user="ops")
        with patch.object(cleanup, "_run_host_action", side_effect=fake_action), \
                patch("builtins.input") as prompt, \
                patch.object(cleanup, "execute_cluster_plans") as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            cleanup.interactive_cluster_cleanup(
                self._cluster_args(check=True, sudo=False), self.ha_cfg, runner,
                self.ha_cfg["topology"]["hosts"], lambda h: h["ip"],
            )
        prompt.assert_not_called()
        execute.assert_not_called()

        with patch.object(cleanup, "_run_host_action", side_effect=fake_action), \
                patch.object(cleanup.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["3", "all", "y", "n"]), \
                patch.object(cleanup, "execute_cluster_plans") as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            cleanup.interactive_cluster_cleanup(
                self._cluster_args(dry_run=True, sudo=False), self.ha_cfg, runner,
                self.ha_cfg["topology"]["hosts"], lambda h: h["ip"],
            )
        execute.assert_not_called()

    def test_cli_cluster_requires_config_and_limit_requires_cluster(self):
        with patch.object(sys, "argv", ["vde-install", "cleanup", "--cluster", "--check"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main.main()
            self.assertEqual(cm.exception.code, 1)

        with patch.object(sys, "argv", ["vde-install", "cleanup", "-l", "dbe-node-02", "--check"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main.main()
            self.assertEqual(cm.exception.code, 1)

    def test_host_agent_script_runs_preflight_over_local_runner(self):
        from core.ssh_runner import SSHRunner

        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp) / "pgdata"
            target_dir.mkdir()
            runner = SSHRunner(local_hosts={"127.0.0.1"})
            res = cleanup._run_host_action(runner, "127.0.0.1", "preflight", {
                "packages": [],
                "paths": [str(target_dir)],
            }, timeout=30)
            self.assertEqual(res, {"ok": True})
            with self.assertRaisesRegex(RuntimeError, "Unsafe cleanup path"):
                cleanup._run_host_action(runner, "127.0.0.1", "preflight", {
                    "packages": [],
                    "paths": ["/etc"],
                }, timeout=30)
            with self.assertRaisesRegex(RuntimeError, "Protected cleanup path"):
                cleanup._run_host_action(runner, "127.0.0.1", "preflight", {
                    "packages": [],
                    "paths": ["/data/postgres"],
                }, timeout=30)
            rc, _out, err = runner.run_script_capture("127.0.0.1", "sleep 2", timeout=0.2)
            self.assertEqual(rc, 124)
            self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
