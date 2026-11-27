import contextlib
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from core.ssh_runner import SSHRunner


class RunnerTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.runner = SSHRunner(timeout=0.3)

    def test_silent_and_streaming_commands_respect_deadline(self):
        for command in ('sleep 10', 'while :; do echo progress; sleep .05; done'):
            with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()):
                start = time.monotonic()
                rc, output = self.runner.run_command('127.0.0.1', command)
                self.assertEqual((rc, output), (124, 'Timeout'))
                self.assertLess(time.monotonic() - start, 3)

    def test_dispatched_script_timeout_kills_child_process_group(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as captured:
            path = Path(tmp) / 'run.sh'
            marker = Path(tmp) / 'survived'
            path.write_text(f'(sleep 1; touch {marker}) &\necho child-started\nwait\n')
            self.assertEqual(self.runner.dispatch_script('127.0.0.1', str(path)), 124)
            self.assertIn('child-started', captured.getvalue())
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_timeout_while_script_input_pipe_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / 'run.sh'
            path.write_text('sleep 10\n' + '# padding\n' * 200000)
            start = time.monotonic()
            self.assertEqual(self.runner.dispatch_script('127.0.0.1', str(path)), 124)
            self.assertLess(time.monotonic() - start, 3)

    def test_output_and_exit_code_are_preserved(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.timeout = 3
            rc, output = self.runner.run_command('127.0.0.1', 'echo hello; exit 7')
            self.assertEqual(rc, 7)
            self.assertEqual(output.splitlines()[-1], 'hello')


if __name__ == '__main__':
    unittest.main()
