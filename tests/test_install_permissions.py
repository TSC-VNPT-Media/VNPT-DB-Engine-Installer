import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallPermissionTests(unittest.TestCase):
    def run_bash(self, script):
        return subprocess.run(['bash', '-c', 'umask 077\n' + script],
                              text=True, capture_output=True, check=True)

    def test_etcd_config_readable_under_restrictive_umask_and_on_rerun(self):
        source = (ROOT / 'src/components/etcd/run.sh').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Execute the actual rendering block in an isolated filesystem.
            block = source[source.index('# Hash config truoc'):source.index('systemctl daemon-reload')]
            for path in ('/etc/etcd', '/etc/systemd/system/etcd.service.d'):
                block = block.replace(path, str(root) + path)
            (root / 'etc/etcd').mkdir(parents=True)
            setup = source[:source.index('echo "==> [etcd]')]
            script = (setup + 'NODE_IP=192.0.2.1\nETCD_NAME=etcd1\n'
                      'DATA_DIR=/unused\nINITIAL_CLUSTER=etcd1=http://192.0.2.1:2380\n'
                      'TOKEN=test\nSTATE=new\nid() { return 1; }\n' + block + '\ntrue\n')
            self.run_bash(script)
            paths = [root / 'etc/etcd/etcd.yaml', root / 'etc/etcd/etcd.conf',
                     root / 'etc/systemd/system/etcd.service.d/10-vde.conf']
            for path in paths:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                path.chmod(0o600)
            self.run_bash(script)
            self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o644 for p in paths))

    def test_storage_parents_created_with_traversal_and_private_pgdata(self):
        source = (ROOT / 'src/components/os_baseline/run.sh').read_text()
        block = source[source.index('mkdir -p "${PGDATA}"'):source.index('systemctl enable cronie')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            setup = source[:source.index('echo "==> [OS Baseline]')]
            script = (setup + f'PGDATA={data}/postgres/16/data\n'
                      f'PGWAL={data}/postgres/16/wal\nBACKUP_DIR={data}/backup\n'
                      'chown() { :; }\nrunuser() { shift 3; "$@"; }\n' +
                      block.replace('/etc/vde', str(root / 'vde')))
            self.run_bash(script)
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((data / 'postgres/16/data').stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((data / 'postgres/16/wal').stat().st_mode), 0o700)
            data.chmod(0o700)
            self.run_bash(script)
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)
            denied = script.replace('runuser() { shift 3; "$@"; }', 'runuser() { return 1; }')
            result = subprocess.run(['bash', '-c', 'umask 077\n' + denied],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('postgres cannot traverse', result.stderr)
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)


if __name__ == '__main__':
    unittest.main()
