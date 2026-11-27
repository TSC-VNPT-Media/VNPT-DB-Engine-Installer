import subprocess
import shlex
import os
import signal
import threading
import time
from typing import Iterable, Optional, Tuple

class SSHRunner:
    """
    Runs on the installation node locally; uses native SSH for other nodes.
    Inventory-selected local hosts may have real interface IPs (not loopback).
    """

    def __init__(self, user: str = "root", port: int = 22, key_file: str = None, use_sudo: bool = False, dry_run: bool = False, timeout: int = 3600, local_hosts: Optional[Iterable[str]] = None):
        self.user = user
        self.port = port
        self.key_file = key_file
        self.use_sudo = use_sudo
        self.dry_run = dry_run
        self.timeout = timeout
        self.local_hosts = set(local_hosts) if local_hosts is not None else {"127.0.0.1", "localhost"}

    def is_local(self, host: str) -> bool:
        return host in self.local_hosts

    def _execution_command(self, host: str, command: Optional[str] = None):
        if self.is_local(host):
            cmd = ["/bin/bash"]
            if self.use_sudo and os.geteuid() != 0:
                cmd = ["sudo", "-n"] + cmd
            return cmd + (["-c", command] if command is not None else [])

        ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
        if self.port and self.port != 22:
            ssh_opts.extend(["-p", str(self.port)])
        if self.key_file:
            key_path = os.path.expanduser(self.key_file)
            if not os.path.isfile(key_path):
                raise FileNotFoundError(f"SSH private key not found: {key_path}")
            ssh_opts.extend(["-i", key_path])
        remote_exec = command if command is not None else "/bin/bash"
        if self.use_sudo and self.user != "root":
            remote_exec = (f"sudo -n /bin/bash -c {shlex.quote(command)}"
                           if command is not None else "sudo -n /bin/bash")
        return ["ssh"] + ssh_opts + [f"{self.user}@{host}", remote_exec]

    def _run(self, cmd, prefix, script=None):
        """Stream output while enforcing a wall-clock deadline."""
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE if script is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, start_new_session=True)
        deadline = time.monotonic() + self.timeout
        lines = []

        def read_output():
            try:
                for line in process.stdout:
                    clean = line.strip()
                    if clean:
                        print(f"{prefix} {clean}", flush=True)
                        if script is None:
                            lines.append(clean)
            finally:
                process.stdout.close()

        def write_input():
            try:
                process.stdin.write(script)
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try: process.stdin.close()
                except (BrokenPipeError, OSError, ValueError): pass

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        writer = None
        if script is not None:
            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()
        try:
            process.wait(timeout=max(0, deadline-time.monotonic()))
            for thread in (writer, reader):
                if thread:
                    thread.join(timeout=max(0, deadline-time.monotonic()))
                    if thread.is_alive():
                        raise subprocess.TimeoutExpired(cmd, self.timeout)
            return process.returncode, "\n".join(lines)
        except subprocess.TimeoutExpired:
            # Closing SSH does not guarantee remote jobs have stopped.
            try: os.killpg(process.pid, signal.SIGTERM)
            except OSError: pass
            try: process.wait(timeout=0.5)
            except subprocess.TimeoutExpired: pass
            try: os.killpg(process.pid, signal.SIGKILL)
            except OSError: pass
            try: process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                print(f"{prefix} [ERROR] Process could not be terminated; inspect it before retrying.")
            for thread in (writer, reader):
                if thread: thread.join(timeout=1)
            print(f"{prefix} [ERROR] Command timed out after {self.timeout}s!" +
                  (" Verify remote processes before retrying." if cmd[0] == 'ssh' else
                   " Verify privileged or detached processes before retrying."))
            return 124, "Timeout"

    def run_command(self, host: str, command: str, component_name: str = "core") -> Tuple[int, str]:
        prefix = f"[{host}] [{'LOCAL' if self.is_local(host) else 'SSH'}] [{component_name}]"
        
        if self.dry_run:
            shown = command if len(command) <= 300 else command[:300] + " ...[truncated]"
            print(f"{prefix} [DRY-RUN] Would execute: {shown}")
            return 0, "[DRY-RUN SUCCESS]"

        try:
            cmd = self._execution_command(host, command)
            return self._run(cmd, prefix)
        except Exception as e:
            print(f"{prefix} [ERROR] Failed to execute: {str(e)}")
            return 1, str(e)

    def dispatch_script(self, host: str, script_path: str, env_vars: dict = None, component_name: str = "core") -> int:
        if not os.path.exists(script_path):
            raise FileNotFoundError(f"Script not found: {script_path}")

        with open(script_path, "r", encoding="utf-8") as f:
            script_content = f.read()

        env_prefix = ""
        if env_vars:
            exports = [f"export {k}={shlex.quote(str(v))}" for k, v in env_vars.items()]
            env_prefix = "\n".join(exports) + "\n"

        full_script = env_prefix + script_content

        prefix = f"[{host}] [{'LOCAL' if self.is_local(host) else 'SSH'}] [{component_name}]"
        if self.dry_run:
            print(f"{prefix} [DRY-RUN] Would dispatch script {script_path}")
            return 0

        try:
            cmd = self._execution_command(host)
            rc, _ = self._run(cmd, prefix, full_script)
            return rc
        except Exception as e:
            print(f"{prefix} [ERROR] Failed to dispatch script: {str(e)}")
            return 1

    def run_script_capture(self, host: str, script: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
        """Run a bash script on a local or remote host and capture stdout/stderr without streaming."""
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            cmd = self._execution_command(host)
        except Exception as exc:
            return 1, "", str(exc)
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            out, err = process.communicate(input=script, timeout=effective_timeout)
            return process.returncode, out, err
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                out, err = process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    out, err = process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    out = exc.stdout or ""
                    err = exc.stderr or ""
            return 124, out or "", (err or "").strip() or f"Command timed out after {effective_timeout}s"
        except Exception as exc:
            return 1, "", str(exc)
