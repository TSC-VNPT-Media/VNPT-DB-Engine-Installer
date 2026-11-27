"""Interactive RPM/systemd decommissioning for local nodes and HA clusters."""

import base64
from dataclasses import dataclass, field
import configparser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import urllib.request

import yaml


COMPONENTS = {
    "patroni": (r"(?:percona-)?patroni(?:-|$)", r"patroni(?:@.+)?\.service", ("patroni", "patronictl")),
    "postgresql": (r"(?:(?:percona-)?postgresql\d*|(?:percona-)?(?:pg_repack_?\d*|pgaudit_?\d*|wal2json_?\d*|pgbadger|pg_gather))(?:-|$)",
                   r"(?:postgresql|postgres)(?:[-@].+)?\.service", ("postgres", "psql")),
    "etcd": (r"etcd(?:ctl)?(?:-|$)", r"etcd(?:@.+)?\.service", ("etcd", "etcdctl")),
    "pgbouncer": (r"(?:percona-)?pgbouncer(?:-|$)", r"pgbouncer(?:@.+)?\.service", ("pgbouncer",)),
    "haproxy": (r"(?:percona-)?haproxy(?:-|$)", r"haproxy(?:@.+)?\.service", ("haproxy",)),
    "keepalived": (r"keepalived(?:-|$)", r"keepalived(?:@.+)?\.service", ("keepalived",)),
    "pgbackrest": (r"(?:percona-)?pgbackrest(?:-|$)", r"pgbackrest.*\.(?:service|timer)", ("pgbackrest",)),
    "pmm": (r"pmm[23]?-client(?:-|$)", r"pmm-agent\.service", ("pmm-admin", "pmm-agent")),
}
PROCESS_NAMES = {
    "patroni": {"patroni"}, "postgresql": {"postgres", "postmaster"},
    "etcd": {"etcd"}, "pgbouncer": {"pgbouncer"}, "haproxy": {"haproxy"}, "keepalived": {"keepalived"},
    "pgbackrest": {"pgbackrest"}, "pmm": {"pmm-agent", "pmm-managed"},
}
ACCESS_TIER = ("keepalived", "haproxy", "pgbouncer", "pgbackrest", "pmm")
DB_TIER = ("patroni", "postgresql")
DCS_TIER = ("etcd",)
ORDER = ACCESS_TIER + DB_TIER + DCS_TIER
LEADER_ROLES = {"leader", "master", "primary"}
PROTECTED = {"/", "/etc", "/var", "/var/lib", "/var/log", "/var/run", "/run",
             "/data", "/data/postgres", "/data/backup", "/usr", "/usr/local",
             "/opt", "/srv", "/mnt", "/media", "/home", "/root", "/tmp", "/backup"}


def command(argv, timeout=30):
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**os.environ, "LC_ALL": "C"},
                                text=True, timeout=timeout, check=False)
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def require(argv, timeout=30):
    rc, out, err = command(argv, timeout)
    if rc:
        raise RuntimeError(f"{argv[0]} failed (rc={rc}): {(err or out).strip()}")
    return out


def package_component(name):
    return next((key for key, (pattern, _, _) in COMPONENTS.items() if re.fullmatch(pattern + r".*", name)), None)


def validate_path_static(value):
    """Static path safety checks usable before contacting a target host."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
        raise ValueError(f"Unsafe cleanup path: {value}")
    normalized = os.path.normpath(value)
    if normalized in PROTECTED or normalized.startswith(("/proc/", "/sys/", "/dev/")):
        raise ValueError(f"Protected cleanup path: {value}")
    return normalized


def validate_path(value):
    """Never follow directory symlinks or cross mounts during recursive cleanup."""
    normalized = validate_path_static(value)
    path = Path(value)
    # Unlinking a final symlink is safe; following a symlink in its parents is not.
    if str(path.parent.resolve()) != str(path.parent):
        raise ValueError(f"Symlink in cleanup path parents: {value}")
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
        if mount == normalized or mount.startswith(normalized + "/"):
            mounts.append(mount)
    if mounts:
        raise ValueError(f"Unmount before deleting {value}: {', '.join(mounts)}")
    return normalized


def present(path):
    # lstat surfaces permission errors instead of treating an inaccessible path as absent.
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False


@dataclass
class Finding:
    packages: list = field(default_factory=list)
    units: dict = field(default_factory=dict)
    binaries: list = field(default_factory=list)
    unmanaged: list = field(default_factory=list)
    configs: list = field(default_factory=list)
    data: list = field(default_factory=list)
    processes: list = field(default_factory=list)

    def detected(self):
        return bool(self.packages or self.units or self.binaries or self.configs or self.data or self.processes)


class LocalCleanup:
    def __init__(self, args):
        self.args = args
        self.findings = {key: Finding() for key in COMPONENTS}
        self.issues = []
        self.cluster_issues = []
        self.config_cluster_issues = []
        self.backups = []
        self.profile = None
        self.etcd_endpoint = "http://127.0.0.1:2379"
        self.patroni_port = 8008
        self.remote = False
        self.has_pgbackrest_cron = None

    def read_yaml(self, path):
        try:
            if not present(path):
                return {}
            value = yaml.safe_load(Path(path).read_text()) or {}
            if not isinstance(value, dict):
                raise ValueError("expected mapping")
            return value
        except (OSError, ValueError, yaml.YAMLError):
            # Do not print parser errors: they can include passwords from YAML.
            self.issues.append(f"Cannot read/parse config: {path}")
            return {}

    def discover_paths(self):
        cfg = self.read_yaml(self.args.config) if self.args.config else {}
        if self.args.config and not present(self.args.config):
            self.issues.append(f"Config not found: {self.args.config}")
        settings = cfg.get("settings") or {}
        storage = settings.get("storage") or {}
        self.profile = (cfg.get("cluster") or {}).get("profile")
        pat = self.read_yaml("/etc/patroni/patroni.yml")
        if not pat:
            pat = self.read_yaml("/etc/patroni/patroni.yaml")
        pg = pat.get("postgresql") or {}
        for backend in ("etcd", "etcd3", "consul", "zookeeper", "kubernetes", "raft"):
            if backend in pat and backend not in {"etcd", "etcd3"}:
                self.config_cluster_issues.append(f"Patroni uses {backend}; local cleanup cannot verify DCS membership")
            elif backend in pat:
                hosts = (pat[backend] or {}).get("hosts", [])
                if isinstance(hosts, str):
                    hosts = hosts.split(",")
                if len(hosts) > 1:
                    self.config_cluster_issues.append("Patroni config declares multiple DCS endpoints")
        etcd = self.read_yaml("/etc/etcd/etcd.yaml")
        listen = (pat.get("restapi") or {}).get("listen", "0.0.0.0:8008")
        try:
            self.patroni_port = int(str(listen).rsplit(":", 1)[1])
        except (ValueError, IndexError):
            self.issues.append("Cannot determine Patroni REST port")
        urls = str(etcd.get("listen-client-urls", "http://127.0.0.1:2379")).split(",")
        self.etcd_endpoint = next((url for url in urls if "127.0.0.1:" in url), urls[0])
        pgdata = self.args.data_dir or pg.get("data_dir") or storage.get("data_dir", "/data/postgres/16/data")
        wal = self.args.wal_dir or storage.get("wal_dir", "/data/postgres/16/wal")
        # Include WAL/tablespace links even when the original YAML was lost.
        pg_paths = [pgdata, wal]
        pg_paths += [str(Path(p).resolve()) for p in (pgdata, wal) if Path(p).is_symlink()]
        link = Path(pgdata) / "pg_wal"
        if link.is_symlink():
            pg_paths.append(str(link.resolve()))
        for link in sorted((Path(pgdata) / "pg_tblspc").glob("*")):
            if link.is_symlink():
                # A tablespace root can be shared by different PG versions.
                # Only this cluster's version subdirectory is eligible for deletion.
                try:
                    version = (Path(pgdata) / "PG_VERSION").read_text().strip()
                    if not re.fullmatch(r"\d+(?:\.\d+)?", version):
                        raise ValueError("invalid PG version")
                    pg_paths.extend(str(p) for p in link.resolve().glob(f"PG_{version}_*"))
                except (OSError, ValueError):
                    self.issues.append(f"Cannot resolve tablespace version under: {pgdata}")
        data = {
            "postgresql": pg_paths,
            "etcd": [self.args.etcd_data_dir or etcd.get("data-dir", "/var/lib/etcd/default.etcd")],
        }
        # Old rescue data is reported and requires the same explicit data opt-in.
        for path in list(data["etcd"]):
            data["etcd"].extend(str(p) for p in Path(path).parent.glob(Path(path).name + ".bak.*"))
            if Path(path).is_symlink():
                data["etcd"].append(str(Path(path).resolve()))
        configs = {
            "patroni": ["/etc/patroni", "/etc/systemd/system/patroni.service.d"],
            "postgresql": ["/etc/profile.d/postgresql.sh"],
            "etcd": ["/etc/etcd", "/etc/systemd/system/etcd.service.d"],
            "pgbouncer": ["/etc/pgbouncer", "/var/log/pgbouncer", "/run/pgbouncer"],
            "haproxy": ["/etc/haproxy", "/var/lib/haproxy"],
            "keepalived": ["/etc/keepalived"],
            "pgbackrest": ["/etc/pgbackrest", "/var/log/pgbackrest", "/var/spool/pgbackrest"],
            "pmm": ["/usr/local/percona/pmm2", "/usr/local/percona/pmm"],
        }
        for key, finding in self.findings.items():
            finding.configs = [p for p in configs.get(key, []) if present(p)]
            finding.data = sorted({str(p) for p in data.get(key, []) if present(p)})
        backup_paths = {str(Path(storage.get("backup_dir", "/data/backup")) / "pgbackrest")}
        if self.args.backup_dir:
            backup_paths.add(str(Path(self.args.backup_dir) / "pgbackrest"))
        if present("/etc/pgbackrest/pgbackrest.conf"):
            try:
                config = configparser.ConfigParser(interpolation=None)
                config.read_string(Path("/etc/pgbackrest/pgbackrest.conf").read_text())
                for section in config.sections():
                    for option, value in config.items(section):
                        if re.fullmatch(r"repo\d+-path", option):
                            repo = option.split("-")[0]
                            repo_type = config.get(section, repo + "-type",
                                                   fallback=config.get("global", repo + "-type", fallback="posix"))
                            repo_host = config.get(section, repo + "-host",
                                                   fallback=config.get("global", repo + "-host", fallback=""))
                            if repo_type == "posix" and not repo_host:
                                backup_paths.add(value)
            except (OSError, configparser.Error):
                self.issues.append("Cannot inspect pgBackRest local repository paths")
        backup_paths |= {str(Path(p).resolve()) for p in backup_paths if Path(p).is_symlink()}
        self.backups = sorted(p for p in backup_paths if present(p))

    def scan(self):
        rc, out, _ = command(["rpm", "-qa", "--qf", "%{NAME}\t%{NVRA}\n"])
        if rc:
            self.issues.append("RPM inventory unavailable; supported target: RHEL/Rocky/AlmaLinux with RPM")
        for line in out.splitlines():
            fields = line.split("\t")
            if len(fields) == 2:
                key = package_component(fields[0])
                if key:
                    self.findings[key].packages.append(fields[1])
        rc, out, _ = command(["systemctl", "list-unit-files", "--no-legend", "--no-pager"])
        if rc:
            self.issues.append("systemd unit inventory unavailable")
        for line in out.splitlines():
            unit = line.split()[0] if line.split() else ""
            for key, (_, pattern, _) in COMPONENTS.items():
                if re.fullmatch(pattern, unit):
                    rc, state, _ = command(["systemctl", "show", "-p", "ActiveState", "--value", unit])
                    self.findings[key].units[unit] = state.strip() if not rc else "unknown"
                    if rc:
                        self.issues.append(f"Cannot inspect unit: {unit}")
        self.discover_paths()
        try:
            for process in active_processes(list(COMPONENTS)):
                names = set(process.split(":", 1)[1].split(","))
                for key in COMPONENTS:
                    if names & PROCESS_NAMES[key]:
                        self.findings[key].processes.append(process)
        except OSError:
            self.issues.append("Cannot inspect all local processes; rerun as root/--sudo")
        for key, (_, _, names) in COMPONENTS.items():
            paths = {shutil.which(name) for name in names} - {None}
            if key == "postgresql":
                paths.update(str(p) for p in Path("/usr").glob("pgsql-*/bin/postgres"))
            for path in sorted(paths):
                self.findings[key].binaries.append(path)
                rc, owner, _ = command(["rpm", "-qf", "--qf", "%{NVRA}\n", str(Path(path).resolve())])
                if rc or not owner.strip() or not set(owner.splitlines()) <= set(self.findings[key].packages):
                    self.findings[key].unmanaged.append(path)
        for runtime in ("docker", "podman"):
            if shutil.which(runtime):
                rc, out, _ = command([runtime, "ps", "-a", "--format", "{{.ID}} {{.Image}} {{.Names}}"])
                if rc:
                    self.issues.append(f"Cannot inspect {runtime} containers")
                elif re.search(r"postgres|patroni|etcd|pgbouncer|haproxy|keepalived|pgbackrest|pmm", out, re.I):
                    self.issues.append(f"Related {runtime} containers detected; manage these separately")
        return self

    def inspect_cluster(self):
        self.cluster_issues = list(self.config_cluster_issues)
        if self.profile == "ha":
            self.cluster_issues.append("YAML declares HA topology")
        if self.findings["etcd"].detected():
            rc, out, _ = command(["etcdctl", "--endpoints=" + self.etcd_endpoint,
                                   "--dial-timeout=2s", "--command-timeout=3s", "member", "list", "-w", "json"])
            try:
                members = json.loads(out)["members"] if not rc else []
                if not isinstance(members, list):
                    members = []
            except (ValueError, KeyError, TypeError):
                members = []
            if not members:
                self.cluster_issues.append("Cannot verify etcd membership (offline, TLS/auth, or unavailable)")
            elif len(members) != 1:
                self.cluster_issues.append(f"etcd has {len(members)} members")
        if self.findings["patroni"].detected():
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"http://127.0.0.1:{self.patroni_port}/cluster", timeout=3) as response:
                    members = json.load(response)["members"]
                if not isinstance(members, list):
                    raise ValueError("Invalid membership response")
                if len(members) != 1:
                    self.cluster_issues.append(f"Patroni reports {len(members)} members")
            except (OSError, ValueError, KeyError, TypeError):
                self.cluster_issues.append("Cannot verify Patroni cluster membership")

    def show(self):
        print(f"\nCleanup LOCAL: {socket.gethostname()} (no SSH)\n")
        print(f"{'#':<4}{'COMPONENT':<14}{'RPM':<6}{'UNITS':<7}STATE")
        for index, (key, finding) in enumerate(self.findings.items(), 1):
            states = ", ".join(f"{u}={s}" for u, s in finding.units.items())
            print(f"{index:<4}{key:<14}{len(finding.packages):<6}{len(finding.units):<7}{states or '-'}")
            for label, values in (("RPM", finding.packages), ("Process PID:name", finding.processes), ("Binary", finding.binaries),
                                  ("Config/log", finding.configs), ("Data", finding.data)):
                for value in values:
                    print(f"    {label}: {value}")
            for path in finding.unmanaged:
                print(f"    UNMANAGED binary (not owned by this component's RPM set): {path}")
        for path in self.backups:
            print(f"Backup (keep by default): {path}")
        for issue in self.issues:
            print(f"UNVERIFIED: {issue}")
        for issue in self.cluster_issues:
            print(f"CLUSTER: {issue}")


@dataclass
class CleanupPlan:
    selected: list
    packages: list
    units: list
    paths: list
    binaries: list = field(default_factory=list)


def make_plan(scan, selected, purge=False, delete_data=False, delete_backups=False):
    selected = [key for key in ORDER if key in selected]
    if not selected:
        raise ValueError("No components selected")
    if scan.issues:
        raise ValueError("Resolve UNVERIFIED scan results before removing software")
    if "postgresql" in selected and scan.findings["patroni"].detected() and "patroni" not in selected:
        raise ValueError("Select Patroni together with PostgreSQL; Patroni supervises PostgreSQL")
    if "etcd" in selected and scan.findings["patroni"].detected() and "patroni" not in selected:
        raise ValueError("Select Patroni together with etcd, or decommission Patroni separately first")
    for key in selected:
        if scan.findings[key].unmanaged:
            raise ValueError(f"{key}: non-RPM / foreign-owned binaries require manual removal")
    packages = sorted({p for key in selected for p in scan.findings[key].packages})
    units = [u for key in selected for u in scan.findings[key].units]
    paths = []
    if purge:
        paths += [p for key in selected for p in scan.findings[key].configs]
    if delete_data:
        paths += [p for key in selected for p in scan.findings[key].data]
    if delete_backups:
        if "pgbackrest" not in selected:
            raise ValueError("Select pgBackRest to remove its local backup repository")
        paths += scan.backups
    has_cron = (scan.has_pgbackrest_cron if getattr(scan, "has_pgbackrest_cron", None) is not None
                else present("/etc/cron.d/pgbackrest"))
    if "pgbackrest" in selected and has_cron:
        paths.append("/etc/cron.d/pgbackrest")
    validator = validate_path_static if getattr(scan, "remote", False) else validate_path
    paths = sorted({validator(p) for p in paths})
    # Never delete a parent that contains another component's retained data/backup.
    retained = [p for key, f in scan.findings.items() for p in f.data
                if not delete_data or key not in selected]
    if not delete_backups:
        retained += scan.backups
    for path in paths:
        if any(p == path or p.startswith(path + "/") or path.startswith(p + "/") for p in retained):
            raise ValueError(f"Cleanup path contains retained data: {path}")
    binaries = sorted({p for key in selected for p in scan.findings[key].binaries})
    return CleanupPlan(selected, packages, units, paths, binaries)


def active_processes(selected):
    names = set().union(*(PROCESS_NAMES[key] for key in selected))
    found = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = path.read_text().strip()
            argv = (path.parent / "cmdline").read_bytes().split(b"\0")
            # Also catch `python3 /usr/bin/patroni` and `python3 -m patroni`.
            executables = {os.path.basename(os.fsdecode(arg)) for arg in argv[:3] if arg}
            matches = names & ({name} | executables)
            if matches:
                found.append(f"{path.parent.name}:{','.join(sorted(matches))}")
        except FileNotFoundError:
            pass
    return found


def show_plan(plan):
    print(f"\nPLAN LOCAL {socket.gethostname()}")
    print("Components: " + ", ".join(plan.selected))
    for label, values in (("Stop/disable", plan.units), ("Erase RPM", plan.packages), ("Delete", plan.paths)):
        for value in values:
            print(f"  {label}: {value}")
    print("Keep: unselected data/backups, OS tuning, users, repositories, remote backup and DCS metadata.")


def execute_plan(plan):
    # Dependency test happens before stopping anything. Erase only the reviewed RPMs;
    # never use --nodeps or an automatic dependency-removal transaction.
    if plan.packages:
        require(["rpm", "-e", "--test", *plan.packages], timeout=120)
    for path in plan.paths:
        validate_path(path)
    # Stop timers before their services, then proxies, Patroni, PostgreSQL and etcd.
    units = sorted(plan.units, key=lambda unit: not unit.endswith(".timer"))
    for unit in units:
        print(f"Stopping/disabling {unit}")
        require(["systemctl", "stop", unit], timeout=120)
        require(["systemctl", "disable", unit])
    if "pgbackrest" in plan.selected and "/etc/cron.d/pgbackrest" in plan.paths:
        Path("/etc/cron.d/pgbackrest").unlink(missing_ok=True)
    running = active_processes(plan.selected)
    if running:
        raise RuntimeError("Processes still running; no RPM/data deletion performed: " + ", ".join(running))
    if plan.packages:
        print("Erasing selected RPMs...")
        require(["rpm", "-e", *plan.packages], timeout=300)
    for value in plan.paths:
        running = active_processes(plan.selected)
        if running:
            raise RuntimeError("Processes restarted; aborting file deletion: " + ", ".join(running))
        validate_path(value)
        path = Path(value)
        print(f"Deleting {path}")
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif present(path):
            # GNU rm also enforces the filesystem boundary if mounts change after validation.
            require(["rm", "-rf", "--one-file-system", "--", str(path)], timeout=300)
    require(["systemctl", "daemon-reload"])


def verify_plan(plan):
    remaining = []
    for package in plan.packages:
        rc, out, err = command(["rpm", "-q", package])
        if rc == 0:
            remaining.append(f"RPM remains: {package}")
        elif rc != 1 or "not installed" not in (out + err):
            remaining.append(f"Cannot verify RPM absence: {package}")
    for path in plan.paths:
        if present(path):
            remaining.append(f"Path remains: {path}")
    for unit in plan.units:
        rc, out, _ = command(["systemctl", "is-active", unit])
        if rc == 0 or out.strip() in {"active", "activating", "reloading", "deactivating"}:
            remaining.append(f"Unit still running: {unit}")
        elif rc not in {3, 4}:
            remaining.append(f"Cannot verify unit stopped: {unit}")
    remaining += [f"Process remains: {p}" for p in active_processes(plan.selected)]
    for path in plan.binaries:
        if present(path):
            remaining.append(f"Binary remains: {path}")
    for key in plan.selected:
        for name in COMPONENTS[key][2]:
            if shutil.which(name):
                remaining.append(f"Binary remains in PATH: {name}")
    if remaining:
        raise RuntimeError("Cleanup incomplete:\n" + "\n".join(remaining))
    print("Verified: selected RPMs absent, services/processes stopped, selected paths absent.")
    print("This verifies the displayed plan; retained files and unsupported installations are not a full-disk wipe.")


def ask_yes(prompt):
    return input(prompt + " [y/N]: ").strip().lower() in {"y", "yes"}


def interactive_cleanup(args):
    if not args.check and not args.dry_run and os.geteuid() != 0:
        raise ValueError("Live cleanup requires root or --sudo")
    scan = LocalCleanup(args).scan()
    scan.inspect_cluster()
    scan.show()
    if args.check:
        if scan.issues:
            raise ValueError("Inspection incomplete; see UNVERIFIED results")
        return
    if not sys.stdin.isatty():
        raise ValueError("Interactive cleanup requires a terminal; use --check for read-only inspection")
    print("\n1. Chi kiem tra\n2. Go phan mem, giu data/backup\n3. Purge phan mem + config/log (tuy chon xoa data)")
    mode = input("Chon [1]: ").strip() or "1"
    if mode == "1":
        return
    if mode not in {"2", "3"}:
        raise ValueError("Invalid mode")
    choices = input("Chon so component (vd 1,2,3), 'all', hoac Enter de huy: ").strip()
    if not choices:
        print("Cancelled; no changes.")
        return
    keys = list(COMPONENTS)
    if choices.lower() == "all":
        selected = [key for key in keys if scan.findings[key].detected()]
    else:
        indices = choices.split(",")
        if any(not i.strip().isdigit() or not 1 <= int(i) <= len(keys) for i in indices):
            raise ValueError("Invalid component selection")
        selected = list(dict.fromkeys(keys[int(i) - 1] for i in indices))
    delete_data = mode == "3" and any(scan.findings[k].data for k in selected) and ask_yes("Xoa VINH VIEN data/WAL/tablespaces/etcd da liet ke?")
    delete_backups = mode == "3" and "pgbackrest" in selected and bool(scan.backups) and ask_yes("Xoa VINH VIEN backup repository local da liet ke?")
    plan = make_plan(scan, selected, purge=mode == "3", delete_data=delete_data, delete_backups=delete_backups)
    show_plan(plan)
    if set(selected) & {"patroni", "postgresql", "etcd"} and scan.cluster_issues:
        if not args.decommissioned:
            if not args.dry_run:
                raise ValueError(
                    "HA/unknown cluster state: use --cluster -c <ha.yml> to tear down the whole HA cluster, "
                    "or detach this node / retire the cluster first and rerun with --decommissioned. No changes made."
                )
            print("BLOCKED for live execution: use --cluster -c <ha.yml> for full HA teardown, or detach node and use --decommissioned.")
        else:
            print("--decommissioned: operator asserts node detached / cluster retired; no membership or DCS cleanup is performed.")
    if args.dry_run:
        print("DRY-RUN: discovery only; no services, packages or files changed.")
        return
    phrase = f"PURGE {socket.gethostname()}" if mode == "3" else f"UNINSTALL {socket.gethostname()}"
    if input(f"Nhap chinh xac '{phrase}' de thuc thi: ").strip() != phrase:
        print("Cancelled; no changes.")
        return
    # Recheck the cluster immediately before mutations; no stale health authorization.
    scan.inspect_cluster()
    if set(selected) & {"patroni", "postgresql", "etcd"} and scan.cluster_issues and not args.decommissioned:
        raise ValueError("Cluster state changed; cleanup cancelled before mutations")
    execute_plan(plan)
    verify_plan(plan)


# ---------------------------------------------------------------------------
# HA / Multi-node cluster cleanup (orchestrated over local bash + native SSH)
# ---------------------------------------------------------------------------

_HOST_AGENT_PY = r"""
import base64
import configparser
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import urllib.request

COMPONENTS = {
    "patroni": (r"(?:percona-)?patroni(?:-|$)", r"patroni(?:@.+)?\.service", ("patroni", "patronictl")),
    "postgresql": (r"(?:(?:percona-)?postgresql\d*|(?:percona-)?(?:pg_repack_?\d*|pgaudit_?\d*|wal2json_?\d*|pgbadger|pg_gather))(?:-|$)",
                   r"(?:postgresql|postgres)(?:[-@].+)?\.service", ("postgres", "psql")),
    "etcd": (r"etcd(?:ctl)?(?:-|$)", r"etcd(?:@.+)?\.service", ("etcd", "etcdctl")),
    "pgbouncer": (r"(?:percona-)?pgbouncer(?:-|$)", r"pgbouncer(?:@.+)?\.service", ("pgbouncer",)),
    "haproxy": (r"(?:percona-)?haproxy(?:-|$)", r"haproxy(?:@.+)?\.service", ("haproxy",)),
    "keepalived": (r"keepalived(?:-|$)", r"keepalived(?:@.+)?\.service", ("keepalived",)),
    "pgbackrest": (r"(?:percona-)?pgbackrest(?:-|$)", r"pgbackrest.*\.(?:service|timer)", ("pgbackrest",)),
    "pmm": (r"pmm[23]?-client(?:-|$)", r"pmm-agent\.service", ("pmm-admin", "pmm-agent")),
}
PROCESS_NAMES = {
    "patroni": {"patroni"}, "postgresql": {"postgres", "postmaster"},
    "etcd": {"etcd"}, "pgbouncer": {"pgbouncer"}, "haproxy": {"haproxy"}, "keepalived": {"keepalived"},
    "pgbackrest": {"pgbackrest"}, "pmm": {"pmm-agent", "pmm-managed"},
}
PROTECTED = {
    "/", "/etc", "/var", "/var/lib", "/var/log", "/var/run", "/run",
    "/data", "/data/postgres", "/data/backup", "/usr", "/usr/local",
    "/opt", "/srv", "/mnt", "/media", "/home", "/root", "/tmp", "/backup",
}


def run_cmd(argv, timeout=30):
    try:
        res = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, LC_ALL="C"),
            universal_newlines=True, timeout=timeout, check=False,
        )
        return res.returncode, res.stdout, res.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def require_cmd(argv, timeout=30):
    rc, out, err = run_cmd(argv, timeout)
    if rc:
        raise RuntimeError("{0} failed (rc={1}): {2}".format(argv[0], rc, (err or out).strip()))
    return out


def present(path):
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False


def validate_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
        raise ValueError("Unsafe cleanup path: " + str(value))
    normalized = os.path.normpath(value)
    if normalized in PROTECTED or normalized.startswith(("/proc/", "/sys/", "/dev/")):
        raise ValueError("Protected cleanup path: " + str(value))
    if str(path.parent.resolve()) != str(path.parent):
        raise ValueError("Symlink in cleanup path parents: " + str(value))
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), line.split()[4])
        if mount == normalized or mount.startswith(normalized + "/"):
            mounts.append(mount)
    if mounts:
        raise ValueError("Unmount before deleting {0}: {1}".format(value, ", ".join(mounts)))
    return normalized


def package_component(name):
    for key, (pattern, _, _) in COMPONENTS.items():
        if re.fullmatch(pattern + r".*", name):
            return key
    return None


def active_processes(selected):
    names = set()
    for key in selected:
        names.update(PROCESS_NAMES.get(key, set()))
    found = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = path.read_text().strip()
            argv = (path.parent / "cmdline").read_bytes().split(b"\0")
            executables = {os.path.basename(os.fsdecode(arg)) for arg in argv[:3] if arg}
            matches = names & ({name} | executables)
            if matches:
                found.append("{0}:{1}".format(path.parent.name, ",".join(sorted(matches))))
        except FileNotFoundError:
            pass
    return found


def read_simple_yaml(path):
    if not present(path):
        return {}, None
    try:
        text = Path(path).read_text()
    except OSError:
        return {}, "Cannot read/parse config: " + path
    try:
        import yaml as _yaml  # type: ignore
        val = _yaml.safe_load(text) or {}
        if not isinstance(val, dict):
            return {}, "Cannot read/parse config: " + path
        return val, None
    except ImportError:
        pass
    except Exception:
        return {}, "Cannot read/parse config: " + path

    # Stdlib fallback parser for patroni.yml / etcd.yaml when PyYAML is absent on remote hosts.
    data = {}
    section = None
    in_hosts = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            in_hosts = False
            m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", stripped)
            if m:
                key, val = m.group(1), m.group(2).strip().strip("'\"")
                if val:
                    data[key] = val
                    section = None
                else:
                    data.setdefault(key, {})
                    section = key
            continue
        if section in ("etcd", "etcd3", "consul", "zookeeper", "kubernetes", "raft"):
            if stripped.startswith("hosts:"):
                rest = stripped.split(":", 1)[1].strip()
                if rest:
                    cleaned = rest.strip("[]")
                    items = [x.strip().strip("'\"") for x in cleaned.split(",") if x.strip()]
                    data[section]["hosts"] = items
                    in_hosts = False
                else:
                    data[section]["hosts"] = []
                    in_hosts = True
                continue
            if in_hosts and stripped.startswith("-"):
                item = stripped[1:].strip().strip("'\"")
                if item:
                    data[section].setdefault("hosts", []).append(item)
                continue
            in_hosts = False
        elif section == "restapi":
            m = re.match(r"^listen\s*:\s*(.+)$", stripped)
            if m:
                data[section]["listen"] = m.group(1).strip().strip("'\"")
        elif section == "postgresql":
            m = re.match(r"^data_dir\s*:\s*(.+)$", stripped)
            if m:
                data[section]["data_dir"] = m.group(1).strip().strip("'\"")
    return data, None


def do_scan(payload):
    findings = {}
    for key in COMPONENTS:
        findings[key] = {
            "packages": [], "units": {}, "binaries": [], "unmanaged": [],
            "configs": [], "data": [], "processes": [],
        }
    issues = []
    config_cluster_issues = []

    rc, out, _ = run_cmd(["rpm", "-qa", "--qf", "%{NAME}\t%{NVRA}\n"])
    if rc:
        issues.append("RPM inventory unavailable; supported target: RHEL/Rocky/AlmaLinux with RPM")
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) == 2:
            key = package_component(fields[0])
            if key:
                findings[key]["packages"].append(fields[1])

    rc, out, _ = run_cmd(["systemctl", "list-unit-files", "--no-legend", "--no-pager"])
    if rc:
        issues.append("systemd unit inventory unavailable")
    for line in out.splitlines():
        unit = line.split()[0] if line.split() else ""
        for key, (_, pattern, _) in COMPONENTS.items():
            if re.fullmatch(pattern, unit):
                rc_s, state, _ = run_cmd(["systemctl", "show", "-p", "ActiveState", "--value", unit])
                findings[key]["units"][unit] = state.strip() if not rc_s else "unknown"
                if rc_s:
                    issues.append("Cannot inspect unit: " + unit)

    pat, pat_err = read_simple_yaml("/etc/patroni/patroni.yml")
    if not pat and not pat_err:
        pat, pat_err = read_simple_yaml("/etc/patroni/patroni.yaml")
    if pat_err:
        issues.append(pat_err)
    pg = pat.get("postgresql") if isinstance(pat.get("postgresql"), dict) else {}
    for backend in ("etcd", "etcd3", "consul", "zookeeper", "kubernetes", "raft"):
        if backend in pat and backend not in ("etcd", "etcd3"):
            config_cluster_issues.append("Patroni uses {0}; cleanup cannot verify DCS membership".format(backend))
        elif backend in pat:
            bval = pat.get(backend) or {}
            hosts = bval.get("hosts", []) if isinstance(bval, dict) else []
            if isinstance(hosts, str):
                hosts = [h for h in hosts.split(",") if h.strip()]
            if len(hosts) > 1:
                config_cluster_issues.append("Patroni config declares multiple DCS endpoints")

    etcd_cfg, etcd_err = read_simple_yaml("/etc/etcd/etcd.yaml")
    if etcd_err:
        issues.append(etcd_err)

    patroni_port = int(payload.get("patroni_port_default", 8008))
    restapi = pat.get("restapi") if isinstance(pat.get("restapi"), dict) else {}
    if restapi.get("listen"):
        try:
            patroni_port = int(str(restapi["listen"]).rsplit(":", 1)[1])
        except (ValueError, IndexError):
            issues.append("Cannot determine Patroni REST port")

    etcd_port = int(payload.get("etcd_port_default", 2379))
    urls = str(etcd_cfg.get("listen-client-urls", "http://127.0.0.1:{0}".format(etcd_port))).split(",")
    etcd_endpoint = next((u.strip() for u in urls if "127.0.0.1:" in u), urls[0].strip())

    pgdata = (payload.get("data_dir_override") or pg.get("data_dir")
              or payload.get("storage_data_dir") or "/data/postgres/16/data")
    wal = (payload.get("wal_dir_override")
           or payload.get("storage_wal_dir") or "/data/postgres/16/wal")
    pg_paths = [pgdata, wal]
    pg_paths += [str(Path(p).resolve()) for p in (pgdata, wal) if Path(p).is_symlink()]
    wal_link = Path(pgdata) / "pg_wal"
    if wal_link.is_symlink():
        pg_paths.append(str(wal_link.resolve()))
    tblspc_dir = Path(pgdata) / "pg_tblspc"
    if tblspc_dir.is_dir():
        for link in sorted(tblspc_dir.glob("*")):
            if link.is_symlink():
                try:
                    version = (Path(pgdata) / "PG_VERSION").read_text().strip()
                    if not re.fullmatch(r"\d+(?:\.\d+)?", version):
                        raise ValueError("invalid PG version")
                    pg_paths.extend(str(p) for p in link.resolve().glob("PG_{0}_*".format(version)))
                except (OSError, ValueError):
                    issues.append("Cannot resolve tablespace version under: " + pgdata)

    etcd_data = (payload.get("etcd_data_dir_override")
                 or etcd_cfg.get("data-dir") or "/var/lib/etcd/default.etcd")
    etcd_paths = [etcd_data]
    for p in list(etcd_paths):
        parent = Path(p).parent
        if parent.is_dir():
            etcd_paths.extend(str(b) for b in parent.glob(Path(p).name + ".bak.*"))
        if Path(p).is_symlink():
            etcd_paths.append(str(Path(p).resolve()))

    data_map = {"postgresql": pg_paths, "etcd": etcd_paths}
    configs_map = {
        "patroni": ["/etc/patroni", "/etc/systemd/system/patroni.service.d"],
        "postgresql": ["/etc/profile.d/postgresql.sh"],
        "etcd": ["/etc/etcd", "/etc/systemd/system/etcd.service.d"],
        "pgbouncer": ["/etc/pgbouncer", "/var/log/pgbouncer", "/run/pgbouncer"],
        "haproxy": ["/etc/haproxy", "/var/lib/haproxy"],
        "keepalived": ["/etc/keepalived"],
        "pgbackrest": ["/etc/pgbackrest", "/var/log/pgbackrest", "/var/spool/pgbackrest"],
        "pmm": ["/usr/local/percona/pmm2", "/usr/local/percona/pmm"],
    }
    for key in COMPONENTS:
        findings[key]["configs"] = [p for p in configs_map.get(key, []) if present(p)]
        findings[key]["data"] = sorted({str(p) for p in data_map.get(key, []) if present(p)})

    backup_paths = {str(Path(payload.get("storage_backup_dir") or "/data/backup") / "pgbackrest")}
    if payload.get("backup_dir_override"):
        backup_paths.add(str(Path(payload["backup_dir_override"]) / "pgbackrest"))
    if present("/etc/pgbackrest/pgbackrest.conf"):
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read_string(Path("/etc/pgbackrest/pgbackrest.conf").read_text())
            for sec in cp.sections():
                for opt, val in cp.items(sec):
                    if re.fullmatch(r"repo\d+-path", opt):
                        repo = opt.split("-")[0]
                        rtype = cp.get(sec, repo + "-type",
                                       fallback=cp.get("global", repo + "-type", fallback="posix"))
                        rhost = cp.get(sec, repo + "-host",
                                       fallback=cp.get("global", repo + "-host", fallback=""))
                        if rtype == "posix" and not rhost:
                            backup_paths.add(val)
        except (OSError, configparser.Error):
            issues.append("Cannot inspect pgBackRest local repository paths")
    backup_paths |= {str(Path(p).resolve()) for p in backup_paths if Path(p).is_symlink()}
    backups = sorted(p for p in backup_paths if present(p))

    try:
        for proc in active_processes(list(COMPONENTS)):
            names = set(proc.split(":", 1)[1].split(","))
            for key in COMPONENTS:
                if names & PROCESS_NAMES[key]:
                    findings[key]["processes"].append(proc)
    except OSError:
        issues.append("Cannot inspect all local processes; rerun as root/--sudo")

    for key, (_, _, names) in COMPONENTS.items():
        paths = {shutil.which(n) for n in names} - {None}
        if key == "postgresql":
            paths.update(str(p) for p in Path("/usr").glob("pgsql-*/bin/postgres"))
        for bpath in sorted(paths):
            findings[key]["binaries"].append(bpath)
            rc_q, owner, _ = run_cmd(["rpm", "-qf", "--qf", "%{NVRA}\n", str(Path(bpath).resolve())])
            if rc_q or not owner.strip() or not set(owner.splitlines()) <= set(findings[key]["packages"]):
                findings[key]["unmanaged"].append(bpath)

    for runtime in ("docker", "podman"):
        if shutil.which(runtime):
            rc_c, out_c, _ = run_cmd([runtime, "ps", "-a", "--format", "{{.ID}} {{.Image}} {{.Names}}"])
            if rc_c:
                issues.append("Cannot inspect {0} containers".format(runtime))
            elif re.search(r"postgres|patroni|etcd|pgbouncer|haproxy|keepalived|pgbackrest|pmm", out_c, re.I):
                issues.append("Related {0} containers detected; manage these separately".format(runtime))

    cluster_issues = list(config_cluster_issues)
    if payload.get("profile") == "ha":
        cluster_issues.append("YAML declares HA topology")

    def detected(k):
        f = findings[k]
        return bool(f["packages"] or f["units"] or f["binaries"] or f["configs"] or f["data"] or f["processes"])

    etcd_member_count = None
    if detected("etcd"):
        rc_e, out_e, _ = run_cmd([
            "etcdctl", "--endpoints=" + etcd_endpoint,
            "--dial-timeout=2s", "--command-timeout=3s", "member", "list", "-w", "json",
        ])
        try:
            members = json.loads(out_e)["members"] if not rc_e else []
            if not isinstance(members, list):
                members = []
        except (ValueError, KeyError, TypeError):
            members = []
        etcd_member_count = len(members)
        if not members:
            cluster_issues.append("Cannot verify etcd membership (offline, TLS/auth, or unavailable)")
        elif len(members) != 1:
            cluster_issues.append("etcd has {0} members".format(len(members)))

    patroni_members = []
    patroni_role = None
    patroni_node_name = pat.get("name") if isinstance(pat.get("name"), str) else None
    if detected("patroni"):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open("http://127.0.0.1:{0}/cluster".format(patroni_port), timeout=3) as resp:
                members = json.load(resp)["members"]
            if not isinstance(members, list):
                raise ValueError("Invalid membership response")
            patroni_members = members
            if len(members) != 1:
                cluster_issues.append("Patroni reports {0} members".format(len(members)))
            match_names = {x for x in (patroni_node_name, payload.get("host_name"), socket.gethostname()) if x}
            match_ips = {x for x in (payload.get("cluster_ip"), payload.get("host_ip")) if x}
            for m in members:
                if not isinstance(m, dict):
                    continue
                if m.get("name") in match_names or m.get("host") in match_ips:
                    patroni_role = str(m.get("role", "")).lower() or None
                    break
        except (OSError, ValueError, KeyError, TypeError):
            cluster_issues.append("Cannot verify Patroni cluster membership")

    return {
        "hostname": socket.gethostname(),
        "findings": findings,
        "backups": backups,
        "issues": issues,
        "config_cluster_issues": config_cluster_issues,
        "cluster_issues": cluster_issues,
        "has_pgbackrest_cron": present("/etc/cron.d/pgbackrest"),
        "patroni_role": patroni_role,
        "patroni_members": patroni_members,
        "etcd_member_count": etcd_member_count,
    }


def do_preflight(payload):
    packages = payload.get("packages") or []
    paths = payload.get("paths") or []
    if packages:
        require_cmd(["rpm", "-e", "--test"] + list(packages), timeout=120)
    for p in paths:
        validate_path(p)
    return {"ok": True}


def do_stop_units(payload):
    units = sorted(payload.get("units") or [], key=lambda u: not u.endswith(".timer"))
    stopped = []
    for unit in units:
        require_cmd(["systemctl", "stop", unit], timeout=120)
        require_cmd(["systemctl", "disable", unit], timeout=60)
        stopped.append(unit)
    if payload.get("unlink_pgbackrest_cron"):
        try:
            Path("/etc/cron.d/pgbackrest").unlink()
        except FileNotFoundError:
            pass
    return {"stopped": stopped}


def do_erase_and_delete(payload):
    selected = payload.get("selected") or []
    packages = payload.get("packages") or []
    paths = payload.get("paths") or []
    running = active_processes(selected)
    if running:
        raise RuntimeError("Processes still running; no RPM/data deletion performed: " + ", ".join(running))
    if packages:
        require_cmd(["rpm", "-e"] + list(packages), timeout=300)
    for value in paths:
        running = active_processes(selected)
        if running:
            raise RuntimeError("Processes restarted; aborting file deletion: " + ", ".join(running))
        validate_path(value)
        path = Path(value)
        if path.is_symlink() or path.is_file():
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        elif present(value):
            require_cmd(["rm", "-rf", "--one-file-system", "--", str(path)], timeout=300)
    require_cmd(["systemctl", "daemon-reload"], timeout=60)
    return {"erased": len(packages), "deleted": len(paths)}


def do_verify(payload):
    selected = payload.get("selected") or []
    packages = payload.get("packages") or []
    units = payload.get("units") or []
    paths = payload.get("paths") or []
    binaries = payload.get("binaries") or []
    remaining = []
    for pkg in packages:
        rc, out, err = run_cmd(["rpm", "-q", pkg])
        if rc == 0:
            remaining.append("RPM remains: " + pkg)
        elif rc != 1 or "not installed" not in (out + err):
            remaining.append("Cannot verify RPM absence: " + pkg)
    for p in paths:
        if present(p):
            remaining.append("Path remains: " + p)
    for unit in units:
        rc, out, _ = run_cmd(["systemctl", "is-active", unit])
        if rc == 0 or out.strip() in ("active", "activating", "reloading", "deactivating"):
            remaining.append("Unit still running: " + unit)
        elif rc not in (3, 4):
            remaining.append("Cannot verify unit stopped: " + unit)
    for proc in active_processes(selected):
        remaining.append("Process remains: " + proc)
    for bpath in binaries:
        if present(bpath):
            remaining.append("Binary remains: " + bpath)
    for key in selected:
        for name in COMPONENTS.get(key, (None, None, ()))[2]:
            if shutil.which(name):
                remaining.append("Binary remains in PATH: " + name)
    if remaining:
        raise RuntimeError("Cleanup incomplete:\n" + "\n".join(remaining))
    return {"verified": True}


def main():
    raw = os.environ.get("VDE_CLEANUP_PAYLOAD", "")
    req = json.loads(base64.b64decode(raw.encode("ascii")).decode("utf-8"))
    action = req["action"]
    payload = req.get("payload") or {}
    handlers = {
        "scan": do_scan,
        "preflight": do_preflight,
        "stop_units": do_stop_units,
        "erase_and_delete": do_erase_and_delete,
        "verify": do_verify,
    }
    if action not in handlers:
        raise ValueError("Unknown cleanup action: " + str(action))
    result = handlers[action](payload)
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)
"""


def _build_host_script(action, payload):
    encoded = base64.b64encode(
        json.dumps({"action": action, "payload": payload}).encode("utf-8")
    ).decode("ascii")
    return (
        f"export VDE_CLEANUP_PAYLOAD={shlex.quote(encoded)}\n"
        "python3 - <<'VDE_CLEANUP_AGENT_EOF'\n"
        f"{_HOST_AGENT_PY}\n"
        "VDE_CLEANUP_AGENT_EOF\n"
    )


def _run_host_action(runner, target, action, payload, timeout=None):
    script = _build_host_script(action, payload)
    rc, out, err = runner.run_script_capture(target, script, timeout=timeout)
    if rc != 0:
        detail = (err or out or f"rc={rc}").strip()
        raise RuntimeError(f"[{target}] {action} failed: {detail}")
    try:
        return json.loads((out or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"[{target}] Invalid JSON response from {action}: {exc}")


@dataclass
class NodeCleanupContext:
    host: dict
    target: str
    hostname: str
    scan: LocalCleanup
    patroni_role: str = None
    patroni_members: list = field(default_factory=list)
    etcd_member_count: int = None
    plan: CleanupPlan = None

    @property
    def label(self):
        name = self.host.get("name") or self.hostname or self.target
        transport = "LOCAL" if self.host.get("local") else "SSH"
        return f"{name} ({self.target}, {transport})"


def _scan_from_payload(args, data, is_remote, profile):
    scan = LocalCleanup(args)
    scan.remote = is_remote
    scan.profile = profile
    scan.backups = list(data.get("backups") or [])
    scan.issues = list(data.get("issues") or [])
    scan.config_cluster_issues = list(data.get("config_cluster_issues") or [])
    scan.cluster_issues = list(data.get("cluster_issues") or [])
    scan.has_pgbackrest_cron = bool(data.get("has_pgbackrest_cron", False))
    findings_data = data.get("findings") or {}
    for key in COMPONENTS:
        raw = findings_data.get(key) or {}
        scan.findings[key] = Finding(
            packages=list(raw.get("packages") or []),
            units=dict(raw.get("units") or {}),
            binaries=list(raw.get("binaries") or []),
            unmanaged=list(raw.get("unmanaged") or []),
            configs=list(raw.get("configs") or []),
            data=list(raw.get("data") or []),
            processes=list(raw.get("processes") or []),
        )
    return scan


def order_cluster_nodes(contexts):
    """Order nodes safely for HA teardown: non-DB first, replicas next, primary last; remote before local."""
    def sort_key(item):
        idx, ctx = item
        roles = set(ctx.host.get("roles") or [])
        has_db = "db" in roles or ctx.scan.findings["patroni"].detected() or ctx.scan.findings["postgresql"].detected()
        role_lower = (ctx.patroni_role or "").lower()
        if not has_db:
            tier = 0
        elif role_lower in LEADER_ROLES:
            tier = 2
        else:
            tier = 1
        is_local = 1 if ctx.host.get("local") else 0
        return (tier, is_local, idx)

    return [ctx for _, ctx in sorted(enumerate(contexts), key=sort_key)]


def scan_cluster_nodes(args, cfg, runner, selected_hosts, target_fn):
    storage = (cfg.get("settings") or {}).get("storage") or {}
    ports = (cfg.get("effective_config") or {}).get("ports") or {}
    profile = (cfg.get("cluster") or {}).get("profile")
    contexts = []
    for host in selected_hosts:
        target = target_fn(host)
        payload = {
            "profile": profile,
            "host_name": host.get("name"),
            "host_ip": host.get("ip"),
            "cluster_ip": host.get("node_ip") or host.get("ip"),
            "storage_data_dir": storage.get("data_dir", "/data/postgres/16/data"),
            "storage_wal_dir": storage.get("wal_dir", "/data/postgres/16/wal"),
            "storage_backup_dir": storage.get("backup_dir", "/data/backup"),
            "data_dir_override": getattr(args, "data_dir", None),
            "wal_dir_override": getattr(args, "wal_dir", None),
            "etcd_data_dir_override": getattr(args, "etcd_data_dir", None),
            "backup_dir_override": getattr(args, "backup_dir", None),
            "patroni_port_default": ports.get("patroni_rest", 8008),
            "etcd_port_default": ports.get("etcd_client", 2379),
        }
        try:
            data = _run_host_action(runner, target, "scan", payload, timeout=120)
            scan = _scan_from_payload(args, data, is_remote=not bool(host.get("local")), profile=profile)
            contexts.append(NodeCleanupContext(
                host=host,
                target=target,
                hostname=data.get("hostname") or host.get("name") or target,
                scan=scan,
                patroni_role=data.get("patroni_role"),
                patroni_members=list(data.get("patroni_members") or []),
                etcd_member_count=data.get("etcd_member_count"),
            ))
        except Exception as exc:
            scan = LocalCleanup(args)
            scan.remote = not bool(host.get("local"))
            scan.profile = profile
            scan.issues.append(str(exc))
            contexts.append(NodeCleanupContext(
                host=host, target=target,
                hostname=host.get("name") or target, scan=scan,
            ))

    # Cross-populate Patroni role from any reachable member payload if a node's local probe failed.
    all_members = []
    for ctx in contexts:
        if ctx.patroni_members:
            all_members = ctx.patroni_members
            break
    if all_members:
        for ctx in contexts:
            if ctx.patroni_role:
                continue
            match_names = {x for x in (ctx.host.get("name"), ctx.hostname) if x}
            match_ips = {x for x in (ctx.host.get("node_ip"), ctx.host.get("ip")) if x}
            for m in all_members:
                if not isinstance(m, dict):
                    continue
                if m.get("name") in match_names or m.get("host") in match_ips:
                    ctx.patroni_role = str(m.get("role", "")).lower() or None
                    break
    return contexts


def show_cluster_scan(cfg, contexts, is_full_cluster):
    cluster_name = (cfg.get("cluster") or {}).get("name", "unknown")
    profile = (cfg.get("cluster") or {}).get("profile", "unknown")
    scope = "FULL CLUSTER" if is_full_cluster else f"SUBSET ({len(contexts)}/{len(cfg['topology']['hosts'])} hosts)"
    print(f"\nCleanup CLUSTER: {cluster_name} (profile={profile}, scope={scope})\n")

    print(f"{'#':<4}{'COMPONENT':<14}{'DETECTED ON HOSTS'}")
    for idx, key in enumerate(COMPONENTS, 1):
        detected_hosts = [ctx.host.get("name") or ctx.target for ctx in contexts if ctx.scan.findings[key].detected()]
        print(f"{idx:<4}{key:<14}{', '.join(detected_hosts) if detected_hosts else '-'}")

    for ctx in contexts:
        role_note = f"  patroni_role={ctx.patroni_role}" if ctx.patroni_role else ""
        print(f"\n--- HOST {ctx.label} [hostname={ctx.hostname}]{role_note} ---")
        print(f"{'#':<4}{'COMPONENT':<14}{'RPM':<6}{'UNITS':<7}STATE")
        for index, (key, finding) in enumerate(ctx.scan.findings.items(), 1):
            states = ", ".join(f"{u}={s}" for u, s in finding.units.items())
            print(f"{index:<4}{key:<14}{len(finding.packages):<6}{len(finding.units):<7}{states or '-'}")
            for label, values in (("RPM", finding.packages), ("Process PID:name", finding.processes),
                                  ("Binary", finding.binaries), ("Config/log", finding.configs),
                                  ("Data", finding.data)):
                for value in values:
                    print(f"    {label}: {value}")
            for path in finding.unmanaged:
                print(f"    UNMANAGED binary (not owned by this component's RPM set): {path}")
        for path in ctx.scan.backups:
            print(f"  Backup (keep by default): {path}")
        for issue in ctx.scan.issues:
            print(f"  UNVERIFIED: {issue}")
        for issue in ctx.scan.cluster_issues:
            print(f"  CLUSTER: {issue}")


def show_cluster_plan(cfg, ordered_contexts, is_full_cluster):
    cluster_name = (cfg.get("cluster") or {}).get("name", "unknown")
    scope = "FULL CLUSTER" if is_full_cluster else "SUBSET"
    print(f"\nPLAN CLUSTER {cluster_name} ({scope})")
    order_str = " -> ".join(
        f"{ctx.host.get('name') or ctx.target}" + (f"[{ctx.patroni_role}]" if ctx.patroni_role else "")
        for ctx in ordered_contexts if ctx.plan is not None
    )
    print(f"Teardown order (replicas before primary, remote before local): {order_str or '(none)'}")
    print("Phases: Pre-flight(all) -> Stop access/backup/pmm -> Stop patroni/postgresql (ordered) -> Stop etcd -> Erase RPM & delete paths -> Verify(all)")
    for ctx in ordered_contexts:
        if ctx.plan is None:
            print(f"\n  [{ctx.label}] SKIP (no selected components detected)")
            continue
        print(f"\n  [{ctx.label}] Components: {', '.join(ctx.plan.selected)}")
        for label, values in (("Stop/disable", ctx.plan.units), ("Erase RPM", ctx.plan.packages), ("Delete", ctx.plan.paths)):
            for value in values:
                print(f"    {label}: {value}")
    print("\nKeep: unselected data/backups, OS tuning, users, repositories, remote/S3 backup and container volumes.")


def _units_for_tier(ctx, tier_components):
    units = []
    for key in tier_components:
        if key in ctx.plan.selected:
            units.extend(ctx.scan.findings[key].units.keys())
    return units


def execute_cluster_plans(runner, ordered_contexts):
    planned = [ctx for ctx in ordered_contexts if ctx.plan is not None]
    if not planned:
        raise ValueError("No components to remove on any selected host")

    # Phase 0: Pre-flight across ALL hosts before touching any service/package/path.
    print("\n==> [Phase 0/4] Pre-flight RPM dependency & path validation across all nodes...")
    for ctx in planned:
        print(f"  [{ctx.label}] checking {len(ctx.plan.packages)} RPM(s), {len(ctx.plan.paths)} path(s)...")
        _run_host_action(runner, ctx.target, "preflight", {
            "packages": ctx.plan.packages,
            "paths": ctx.plan.paths,
        }, timeout=180)

    # Phase 1: Ordered service stop across cluster to avoid HA failover storms.
    print("\n==> [Phase 1/4] Stopping & disabling services in HA-safe tier order...")
    for ctx in planned:
        units = _units_for_tier(ctx, ACCESS_TIER)
        unlink_cron = "pgbackrest" in ctx.plan.selected and "/etc/cron.d/pgbackrest" in ctx.plan.paths
        if units or unlink_cron:
            print(f"  [{ctx.label}] stopping access/backup/pmm: {', '.join(units) or 'pgbackrest cron'}")
            _run_host_action(runner, ctx.target, "stop_units", {
                "units": units,
                "unlink_pgbackrest_cron": unlink_cron,
            }, timeout=180)

    for ctx in planned:
        units = _units_for_tier(ctx, DB_TIER)
        if units:
            role_tag = f" ({ctx.patroni_role})" if ctx.patroni_role else ""
            print(f"  [{ctx.label}]{role_tag} stopping database tier: {', '.join(units)}")
            _run_host_action(runner, ctx.target, "stop_units", {
                "units": units,
                "unlink_pgbackrest_cron": False,
            }, timeout=180)

    etcd_order = sorted(planned, key=lambda c: (1 if c.host.get("local") else 0))
    for ctx in etcd_order:
        units = _units_for_tier(ctx, DCS_TIER)
        if units:
            print(f"  [{ctx.label}] stopping DCS tier: {', '.join(units)}")
            _run_host_action(runner, ctx.target, "stop_units", {
                "units": units,
                "unlink_pgbackrest_cron": False,
            }, timeout=180)

    # Phase 2: Process check, RPM erase and path deletion per node.
    print("\n==> [Phase 2/4] Erasing RPMs and removing selected paths per node...")
    for ctx in planned:
        print(f"  [{ctx.label}] erasing {len(ctx.plan.packages)} RPM(s), deleting {len(ctx.plan.paths)} path(s)...")
        _run_host_action(runner, ctx.target, "erase_and_delete", {
            "selected": ctx.plan.selected,
            "packages": ctx.plan.packages,
            "paths": ctx.plan.paths,
        }, timeout=600)

    # Phase 3: Post-cleanup verification across ALL hosts.
    print("\n==> [Phase 3/4] Verifying cleanup results across all nodes...")
    failures = []
    for ctx in planned:
        try:
            _run_host_action(runner, ctx.target, "verify", {
                "selected": ctx.plan.selected,
                "packages": ctx.plan.packages,
                "units": ctx.plan.units,
                "paths": ctx.plan.paths,
                "binaries": ctx.plan.binaries,
            }, timeout=120)
            print(f"  [{ctx.label}] Verified OK.")
        except Exception as exc:
            failures.append(str(exc))
    if failures:
        raise RuntimeError("Cluster cleanup verification failed:\n" + "\n".join(failures))
    print("\nVerified: selected RPMs absent, services/processes stopped, and selected paths absent on all planned nodes.")
    print("This verifies the displayed plan; retained files, containers, and unsupported installations are not a full-disk wipe.")


def interactive_cluster_cleanup(args, cfg, runner, selected_hosts, target_fn):
    all_hosts = cfg["topology"]["hosts"]
    is_full_cluster = len(selected_hosts) == len(all_hosts)
    cluster_name = (cfg.get("cluster") or {}).get("name", "cluster")
    profile = (cfg.get("cluster") or {}).get("profile", "ha")

    if not args.check and not args.dry_run:
        if any(h.get("local") for h in selected_hosts) and os.geteuid() != 0 and not runner.use_sudo:
            raise ValueError("Live cluster cleanup requires root or --sudo (or ssh.use_sudo: true)")
        if any(not h.get("local") for h in selected_hosts) and runner.user != "root" and not runner.use_sudo:
            raise ValueError("Live remote cleanup requires root SSH user or --sudo (or ssh.use_sudo: true)")

    contexts = scan_cluster_nodes(args, cfg, runner, selected_hosts, target_fn)
    show_cluster_scan(cfg, contexts, is_full_cluster)

    unverified_nodes = [ctx.label for ctx in contexts if ctx.scan.issues]
    if args.check:
        if unverified_nodes:
            raise ValueError("Inspection incomplete on: " + ", ".join(unverified_nodes))
        return

    if not sys.stdin.isatty():
        raise ValueError("Interactive cleanup requires a terminal; use --check for read-only inspection")

    print("\n1. Chi kiem tra\n2. Go phan mem tren cum, giu data/backup\n3. Purge phan mem + config/log tren cum (tuy chon xoa data)")
    mode = input("Chon [1]: ").strip() or "1"
    if mode == "1":
        return
    if mode not in {"2", "3"}:
        raise ValueError("Invalid mode")

    choices = input("Chon so component ap dung cho cac node da chon (vd 1,2,3), 'all', hoac Enter de huy: ").strip()
    if not choices:
        print("Cancelled; no changes.")
        return

    keys = list(COMPONENTS)
    if choices.lower() == "all":
        requested = [key for key in keys if any(ctx.scan.findings[key].detected() for ctx in contexts)]
    else:
        indices = choices.split(",")
        if any(not i.strip().isdigit() or not 1 <= int(i) <= len(keys) for i in indices):
            raise ValueError("Invalid component selection")
        requested = list(dict.fromkeys(keys[int(i) - 1] for i in indices))
    if not requested:
        raise ValueError("No components detected across selected hosts")

    if unverified_nodes:
        raise ValueError("Resolve UNVERIFIED scan results before removing software: " + ", ".join(unverified_nodes))

    any_data = any(ctx.scan.findings[k].data for ctx in contexts for k in requested if ctx.scan.findings[k].detected())
    any_backups = "pgbackrest" in requested and any(ctx.scan.backups for ctx in contexts if ctx.scan.findings["pgbackrest"].detected())

    delete_data = mode == "3" and any_data and ask_yes("Xoa VINH VIEN data/WAL/tablespaces/etcd da liet ke tren TAT CA node da chon?")
    delete_backups = mode == "3" and any_backups and ask_yes("Xoa VINH VIEN backup repository local da liet ke tren TAT CA node da chon?")

    for ctx in contexts:
        node_selected = [k for k in requested if ctx.scan.findings[k].detected()]
        if not node_selected:
            ctx.plan = None
            continue
        node_delete_backups = delete_backups and "pgbackrest" in node_selected
        ctx.plan = make_plan(
            ctx.scan, node_selected,
            purge=mode == "3",
            delete_data=delete_data,
            delete_backups=node_delete_backups,
        )

    ordered = order_cluster_nodes(contexts)
    planned = [ctx for ctx in ordered if ctx.plan is not None]
    if not planned:
        raise ValueError("No matching components detected on any selected host")

    show_cluster_plan(cfg, ordered, is_full_cluster)

    touches_core = any(set(ctx.plan.selected) & {"patroni", "postgresql", "etcd"} for ctx in planned)
    has_ha_risk = profile == "ha" or any(ctx.scan.cluster_issues for ctx in planned)
    if not is_full_cluster and touches_core and has_ha_risk:
        if not args.decommissioned:
            if not args.dry_run:
                raise ValueError(
                    "Partial HA cleanup (--limit) affecting PostgreSQL/Patroni/etcd requires --decommissioned "
                    "(or omit --limit to tear down the entire cluster). No changes made."
                )
            print("BLOCKED for live execution: partial HA cleanup requires --decommissioned (or omit --limit for full cluster teardown).")
        else:
            print("--decommissioned: operator asserts limited node(s) already detached; no membership or DCS cleanup is performed.")

    if args.dry_run:
        print("DRY-RUN: discovery and cluster teardown plan only; no services, packages or files changed.")
        return

    action_word = "PURGE" if mode == "3" else "UNINSTALL"
    scope_word = "CLUSTER" if is_full_cluster else "SUBSET"
    phrase = f"{action_word} {scope_word} {cluster_name}"
    if input(f"Nhap chinh xac '{phrase}' de thuc thi tren {len(planned)} node: ").strip() != phrase:
        print("Cancelled; no changes.")
        return

    execute_cluster_plans(runner, ordered)
