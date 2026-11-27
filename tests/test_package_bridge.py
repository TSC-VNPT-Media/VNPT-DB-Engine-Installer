import contextlib
import io
import socket
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from core.inventory import InventoryManager
from core.package_bridge import PackageBridge, extra_firewall_ports
from core.ssh_runner import SSHRunner
from core.state_enforcer import StateEnforcer


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok-rpm"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class PackageBridgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "cluster.yml"

    def inventory(self, profile="ha", hosts=None, cluster=None):
        if hosts is None:
            hosts = [{"ip": f"192.0.2.{11 + i}", "local": i == 0} for i in range(3)]
        body = {"cluster": {"name": "test", "profile": profile, **(cluster or {})},
                "topology": {"hosts": hosts}}
        self.path.write_text(yaml.safe_dump(body))
        inv = InventoryManager(str(self.path))
        return inv, inv.load()

    def test_http_and_connect_forward_only_for_allowed_clients(self):
        origin = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
        thread = threading.Thread(target=origin.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)
        echo = socket.socket()
        echo.bind(("127.0.0.1", 0))
        echo.listen(1)
        echo_port = echo.getsockname()[1]
        self.addCleanup(echo.close)

        def serve_echo():
            conn, _addr = echo.accept()
            data = conn.recv(32)
            conn.sendall(b"echo:" + data)
            conn.close()

        threading.Thread(target=serve_echo, daemon=True).start()
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        bridge = PackageBridge("127.0.0.1", port, {"127.0.0.1"})
        bridge.start()
        self.addCleanup(bridge.stop)

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"}))
        with opener.open(f"http://127.0.0.1:{origin.server_address[1]}/pkg.rpm", timeout=5) as resp:
            self.assertEqual(resp.read(), b"ok-rpm")

        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\nHost: 127.0.0.1:{echo_port}\r\n\r\n".encode())
        status = client.recv(64)
        self.assertTrue(status.startswith(b"HTTP/1.1 200"))
        client.sendall(b"ping")
        self.assertEqual(client.recv(32), b"echo:ping")
        client.close()

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        denied_port = probe.getsockname()[1]
        probe.close()
        closed = PackageBridge("127.0.0.1", denied_port, {"192.0.2.9"})
        closed.start()
        self.addCleanup(closed.stop)
        denied = socket.create_connection(("127.0.0.1", closed.port), timeout=5)
        denied.sendall(b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        denied.settimeout(2)
        self.assertEqual(denied.recv(64), b"")
        denied.close()

    def test_flag_defaults_and_rejects_bad_values(self):
        _inv, cfg = self.inventory()
        self.assertIs(cfg["cluster"]["package_bridge"], False)
        self.assertEqual(cfg["cluster"]["package_bridge_port"], 8888)
        with self.assertRaisesRegex(ValueError, "package_bridge must be true or false"):
            self.inventory(cluster={"package_bridge": "enabled"})
        with self.assertRaisesRegex(ValueError, "package_bridge_port"):
            self.inventory(cluster={"package_bridge_port": 0})
        with self.assertRaisesRegex(ValueError, "overlaps a service port"):
            self.inventory(cluster={"package_bridge": True, "package_bridge_port": 5432})

    def test_bridge_does_not_change_service_hash_and_proxy_is_remote_only(self):
        inv, cfg = self.inventory()
        before = StateEnforcer(cfg).get_desired_hash()
        cfg["cluster"]["package_bridge"] = True
        cfg["cluster"]["package_bridge_port"] = 9999
        self.assertEqual(StateEnforcer(cfg).get_desired_hash(), before)
        local = cfg["topology"]["hosts"][0]
        remote = cfg["topology"]["hosts"][1]
        self.assertNotIn("http_proxy", main._build_host_env(cfg, inv, local))
        env = main._build_host_env(cfg, inv, remote)
        self.assertEqual(env["http_proxy"], "http://192.0.2.11:9999")
        self.assertEqual(env["https_proxy"], env["http_proxy"])
        self.assertIn("192.0.2.12", env["no_proxy"])
        self.assertIn("127.0.0.1", env["no_proxy"])
        self.assertEqual(extra_firewall_ports(cfg, local), [9999])
        self.assertEqual(extra_firewall_ports(cfg, remote), [])

    def test_apply_starts_bridge_for_remotes_and_stops_it(self):
        inv, cfg = self.inventory(cluster={"package_bridge": True, "package_bridge_port": 8888})
        started = []

        class FakeBridge:
            def __init__(self, bind_ip, port, allowed):
                self.bind_ip = bind_ip
                self.port = port
                self.allowed = allowed

            def start(self):
                started.append(("start", self.bind_ip, self.port))

            def stop(self):
                started.append(("stop", self.bind_ip, self.port))

        class Runner(SSHRunner):
            def __init__(self):
                super().__init__(local_hosts=["192.0.2.11"])

            def run_command(self, host, command, component_name="core"):
                return 0, "{}"

            def dispatch_script(self, host, script_path, env_vars=None, component_name="core"):
                return 0

        with patch_bridge(FakeBridge), contextlib.redirect_stdout(io.StringIO()) as out:
            main._run_pipeline(cfg, inv, Runner(), cfg["topology"]["hosts"], tags="os_baseline")
        self.assertEqual(started, [("start", "192.0.2.11", 8888), ("stop", "192.0.2.11", 8888)])
        self.assertIn("forwarding dnf/curl", out.getvalue())

    def test_unreachable_repo_does_not_leave_a_listener(self):
        _inv, cfg = self.inventory(cluster={"package_bridge": True})

        class Runner(SSHRunner):
            def __init__(self):
                super().__init__(local_hosts=["192.0.2.11"])

            def run_command(self, host, command, component_name="core"):
                if "curl" in command:
                    return 1, ""
                return 0, ""

        with self.assertRaisesRegex(RuntimeError, "needs the install node"):
            main._start_package_bridge(cfg, Runner(), cfg["topology"]["hosts"])


def patch_bridge(fake):
    return patch.object(main, "PackageBridge", fake)


if __name__ == "__main__":
    unittest.main()
