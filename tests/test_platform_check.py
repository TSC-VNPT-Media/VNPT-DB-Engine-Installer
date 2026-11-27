import contextlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from core.platform_check import X86_64_V2_FLAGS, minimum_requirements, requirement_failures
from core.pmm import normalize_pmm


def host(**overrides):
    facts = {
        "os_id": "rocky", "os_version_id": "9.7", "os_pretty": "Rocky Linux 9.7",
        "arch": "x86_64", "cpu_model": "Intel Xeon",
        "cpu_flags": " ".join(X86_64_V2_FLAGS), "vcpu": 4, "ram_mb": 8192,
    }
    facts.update(overrides)
    return facts


PMM_OFF = {"enabled": False, "mode": "external"}


class RequirementTests(unittest.TestCase):
    def test_supported_el_releases_pass(self):
        for os_id, version in (("rhel", "8.10"), ("rocky", "9.7"), ("almalinux", "8")):
            with self.subTest(os_id=os_id, version=version):
                self.assertEqual(requirement_failures(host(os_id=os_id, os_version_id=version), PMM_OFF), [])

    def test_unsupported_os_cpu_and_size_are_all_reported(self):
        reasons = requirement_failures(host(
            os_id="ubuntu", os_version_id="24.04", os_pretty="Ubuntu 24.04",
            arch="aarch64", cpu_flags="", vcpu=1, ram_mb=1024), PMM_OFF)
        text = "\n".join(reasons)
        self.assertIn("Ubuntu 24.04", text)
        self.assertIn("aarch64", text)
        self.assertIn("Số CPU: 1", text)
        self.assertIn("1024 MiB", text)
        self.assertNotIn("x86-64-v2", text)

    def test_old_x86_64_generation_is_rejected(self):
        reasons = requirement_failures(host(cpu_flags="cx16 lahf_lm popcnt ssse3"), PMM_OFF)
        self.assertEqual(len(reasons), 1)
        self.assertIn("sse4_2", reasons[0])

    def test_unreadable_os_fails_closed(self):
        reasons = requirement_failures(host(os_id="", os_version_id=""), PMM_OFF)
        self.assertIn("/etc/os-release", reasons[0])

    def test_el7_and_el10_are_rejected(self):
        self.assertTrue(requirement_failures(host(os_id="rhel", os_version_id="7.9"), PMM_OFF))
        self.assertTrue(requirement_failures(host(os_id="rhel", os_version_id="10"), PMM_OFF))

    def test_ram_floor_rises_when_pmm_runs_on_the_same_node(self):
        pmm = normalize_pmm({"pmm": {"enabled": True, "mode": "local"}}, "standalone")
        self.assertEqual(requirement_failures(host(ram_mb=6144), pmm), [])
        reasons = requirement_failures(host(ram_mb=6143), pmm)
        self.assertIn("6144 MiB", reasons[0])

    def test_two_vcpu_and_two_gib_pass_without_local_pmm(self):
        self.assertEqual(requirement_failures(host(vcpu=2, ram_mb=2048), PMM_OFF), [])
        self.assertTrue(requirement_failures(host(vcpu=2, ram_mb=2047), PMM_OFF))


def probe(**overrides):
    lines = {
        "mem": "8192", "vcpu": "4", "iface": "192.0.2.11/24",
        "os_id": "OS_ID=rocky", "os_version": "OS_VERSION_ID=9.7",
        "pretty": "OS_PRETTY_NAME=Rocky Linux 9.7", "arch": "ARCH=x86_64",
        "model": "CPU_MODEL=Intel Xeon",
        "flags": "CPU_FLAGS=" + " ".join(X86_64_V2_FLAGS),
        "route": "192.0.2.11",
    }
    lines.update(overrides)
    return "\n".join(lines.values())


class DiscoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = yaml.safe_load("""
cluster: {name: test, profile: standalone}
topology:
  hosts: [{ip: 192.0.2.11, name: db1}]
""")
        # load() fills normalized settings the gate reads
        from core.inventory import InventoryManager
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "cluster.yml"
        path.write_text(yaml.safe_dump(self.cfg))
        self.cfg = InventoryManager(str(path)).load()

    def discover(self, output):
        runner = main._runner(type("A", (), {})(), self.cfg)
        with patch.object(runner, "run_command", return_value=(0, output)), contextlib.redirect_stdout(io.StringIO()):
            return main._discover_hardware(self.cfg, runner, strict=True)

    def test_matching_platform_is_recorded(self):
        facts = self.discover(probe())
        self.assertEqual(facts["192.0.2.11"]["os_pretty"], "Rocky Linux 9.7")
        self.assertEqual(facts["192.0.2.11"]["vcpu"], 4)

    def test_unsuitable_os_stops_before_install_facts_are_returned(self):
        output = probe(os_id="OS_ID=ubuntu", os_version="OS_VERSION_ID=24.04",
                       pretty="OS_PRETTY_NAME=Ubuntu 24.04.2 LTS")
        with self.assertRaisesRegex(RuntimeError, "Không thể start"):
            self.discover(output)


class InitSanityCheckTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = str(Path(self.tmp.name) / "cluster.yml")

    def run_init(self, profile="standalone", ips="192.0.2.11", probe_out=None, offline=False):
        args = SimpleNamespace(
            output=self.output, force=True, ips=ips,
            profile=profile, name="test", offline=offline,
        )
        buf = io.StringIO()
        with patch.object(main, "_probe_local_for_init", return_value=(0, probe_out or "")), \
                contextlib.redirect_stdout(buf):
            main.cmd_init(args)
        return buf.getvalue()

    def test_minimum_requirements_reflects_pmm_mode(self):
        pmm_local = normalize_pmm({"pmm": {"enabled": True, "mode": "local"}}, "standalone")
        self.assertEqual(minimum_requirements(pmm_local)["min_ram_mb"], 6144)
        self.assertEqual(minimum_requirements(PMM_OFF)["min_ram_mb"], 2048)

    def test_init_warns_without_blocking_when_local_ram_below_pmm_floor(self):
        out = self.run_init(profile="standalone", probe_out=probe(mem="4096"))
        self.assertTrue(Path(self.output).exists())
        self.assertIn("[+] [sanity-check] YAML hop le", out)
        self.assertIn("RAM>=6144 MiB", out)
        self.assertIn("[WARN] [sanity-check]", out)
        self.assertIn("settings.pmm.enabled: false", out)

    def test_init_passes_sanity_check_when_local_hardware_meets_floor(self):
        out = self.run_init(profile="standalone", probe_out=probe(mem="8192"))
        self.assertIn("[+] [sanity-check] May local dat yeu cau toi thieu", out)
        self.assertNotIn("[WARN] [sanity-check]", out)

    def test_init_skips_local_hardware_warning_for_remote_ip_or_offline(self):
        remote_out = self.run_init(profile="standalone", ips="198.51.100.50", probe_out=probe(mem="4096"))
        self.assertIn("Khong giu IP local".lower(), remote_out.lower())
        self.assertNotIn("[WARN] [sanity-check]", remote_out)
        offline_out = self.run_init(profile="ha", ips="192.0.2.11,192.0.2.12,192.0.2.13", offline=True)
        self.assertIn("--offline: bo qua kiem tra phan cung local", offline_out)
        self.assertIn("RAM>=2048 MiB", offline_out)

    def test_init_prompts_before_overwriting_existing_file_on_tty(self):
        Path(self.output).write_text("existing-content\n")
        args = SimpleNamespace(
            output=self.output, force=False, ips="192.0.2.11",
            profile="standalone", name="test", offline=True,
        )
        buf = io.StringIO()
        with patch.object(main.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(buf):
            main.cmd_init(args)
        self.assertEqual(Path(self.output).read_text(), "existing-content\n")
        self.assertIn("Cancelled; kept existing file", buf.getvalue())

        buf = io.StringIO()
        with patch.object(main.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="y"), \
                contextlib.redirect_stdout(buf):
            main.cmd_init(args)
        self.assertIn("dbe-node-01", Path(self.output).read_text())

    def test_init_refuses_overwrite_without_force_when_noninteractive(self):
        Path(self.output).write_text("existing-content\n")
        args = SimpleNamespace(
            output=self.output, force=False, ips="192.0.2.11",
            profile="standalone", name="test", offline=True,
        )
        buf = io.StringIO()
        with patch.object(main.sys.stdin, "isatty", return_value=False), \
                contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                main.cmd_init(args)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(Path(self.output).read_text(), "existing-content\n")
