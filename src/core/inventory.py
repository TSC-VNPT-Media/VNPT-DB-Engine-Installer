import os
import re
import yaml
from core.pmm import normalize_pmm, query_guc, memory_budget
from core.execution import CONFIG_TAGS
from typing import Dict, Any, List, Optional


_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def _ipv4(value):
    text = str(value or "")
    if not _IPV4.match(text):
        return False
    return all(0 <= int(part) <= 255 for part in text.split("."))


def _as_int(value, label):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{label} must be an integer")
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, int):
        return value
    raise ValueError(f"{label} must be an integer")


# ---------------------------------------------------------------------------
# Pigsty-style tuning templates (hardware smart defaults)
# ---------------------------------------------------------------------------
def _fmt_mb(mb: int) -> str:
    return f"{int(mb)}MB"


class InventoryManager:
    """
    Parses the single vde-cluster.yml (SSOT, Pigsty CMDB style) and resolves:
      * unified component DAG (standalone == 1-node Patroni+etcd cohort)
      * hardware smart defaults (oltp / olap / crit / tiny templates)
      * precedence: smart defaults < cluster settings.parameters
                    < per-host topology.hosts[].parameters
    No external profiles/*.yml files are read any more.
    """

    VALID_WORKLOADS = ("oltp", "olap", "crit", "tiny")

    def __init__(self, config_path: str, profiles_dir: str = None):
        # profiles_dir kept only for backward-compatible constructor calls.
        self.config_path = config_path
        self.raw_config: Dict[str, Any] = {}
        self.resolved_config: Dict[str, Any] = {}
        self.components: List[Dict[str, Any]] = []
        self.hardware_facts: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ load
    def load(self, hardware_facts: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration manifest not found: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.raw_config = yaml.safe_load(f) or {}
        if hardware_facts:
            self.hardware_facts = hardware_facts
        self._validate_syntax()
        self._normalize_topology()
        self.components = self._build_components_dag()
        self._calculate_smart_defaults()
        return self.resolved_config

    def apply_hardware_facts(self, hardware_facts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Re-resolve effective config after local/SSH hardware discovery.
        Also adopts discovered node_ip (real interface IP) per host unless
        the user explicitly declared node_ip in the YAML."""
        self.hardware_facts = hardware_facts or {}
        for h in self.raw_config["topology"]["hosts"]:
            f = self.hardware_facts.get(h["ip"]) or {}
            if f.get("node_ip") and not h.get("node_ip"):
                h["node_ip"] = f["node_ip"]
        self._calculate_smart_defaults()
        return self.resolved_config

    # ------------------------------------------------------------- validation
    def _validate_syntax(self):
        cluster = self.raw_config.get("cluster", {})
        if not cluster.get("name"):
            raise ValueError("Manifest missing required field: 'cluster.name'")
        if cluster.get("profile") not in ["standalone", "ha"]:
            raise ValueError("Invalid 'cluster.profile'. Must be 'standalone' or 'ha'")
        hosts = (self.raw_config.get("topology") or {}).get("hosts") or []
        if not hosts:
            raise ValueError("Manifest must define at least one host in 'topology.hosts'")
        for h in hosts:
            if not h.get("ip"):
                raise ValueError("Every topology.hosts[] entry must define 'ip'")
            if "local" in h and not isinstance(h["local"], bool):
                raise ValueError("topology.hosts[].local must be a YAML boolean (true/false)")
        if len({h["ip"] for h in hosts}) != len(hosts):
            raise ValueError("Every topology.hosts[].ip must be unique")
        if cluster.get("profile") == "standalone":
            if len(hosts) != 1:
                raise ValueError("Standalone runs locally and requires exactly one host")
            if hosts[0].get("local") is False:
                raise ValueError("Standalone must run locally; local: false is not supported")
        elif sum(h.get("local") is True for h in hosts) != 1:
            raise ValueError("HA requires exactly one host with local: true (the node running this tool)")
        for h in hosts:
            if cluster.get("profile") == "ha":
                fields = ("ip", "node_ip") if h.get("local") else ("ip", "ssh_host", "node_ip")
                if any(h.get(k) in ("127.0.0.1", "localhost", "::1") for k in fields):
                    raise ValueError("HA nodes must use routable IPs, not loopback")
        if cluster.get("profile") == "ha" and len(hosts) < 3:
            raise ValueError("HA profile requires at least 3 hosts in 'topology.hosts' for etcd quorum")
        bridge = cluster.get("package_bridge", False)
        if not isinstance(bridge, bool):
            raise ValueError("cluster.package_bridge must be true or false")
        bridge_port = cluster.get("package_bridge_port", 8888)
        if isinstance(bridge_port, bool) or not isinstance(bridge_port, int) or not 1 <= bridge_port <= 65535:
            raise ValueError("cluster.package_bridge_port must be an integer from 1 to 65535")
        workload = ((self.raw_config.get("settings") or {}).get("workload") or "oltp").lower()
        if workload not in self.VALID_WORKLOADS:
            raise ValueError(f"Invalid settings.workload '{workload}'. Must be one of {self.VALID_WORKLOADS}")

    VALID_ROLES = ("db", "etcd", "balancer")

    def _normalize_topology(self):
        hosts = self.raw_config["topology"]["hosts"]
        for idx, h in enumerate(hosts, 1):
            h.setdefault("name", f"dbe-node-{idx:02d}")
            h.setdefault("local", self.raw_config["cluster"]["profile"] == "standalone")
            if not h.get("roles"):
                h["roles"] = ["db", "etcd", "balancer"]
            elif not isinstance(h["roles"], list) or any(r not in self.VALID_ROLES for r in h["roles"]):
                raise ValueError(f"Invalid roles on host '{h['name']}'. Allowed roles: {self.VALID_ROLES}")
        if len({h["name"] for h in hosts}) != len(hosts):
            raise ValueError("Every topology.hosts[].name must be unique")
        targets = [h["ip"] if h["local"] else h.get("ssh_host") or h["ip"] for h in hosts]
        if len(set(targets)) != len(targets):
            raise ValueError("Execution targets must be unique; a remote SSH target cannot equal the local host IP")
        profile = self.raw_config["cluster"]["profile"]
        db_count = sum("db" in h["roles"] for h in hosts)
        etcd_count = sum("etcd" in h["roles"] for h in hosts)
        if profile == "standalone":
            if db_count != 1 or etcd_count != 1:
                raise ValueError("Standalone host must include both 'db' and 'etcd' roles")
        else:
            if db_count < 1:
                raise ValueError("HA profile requires at least 1 host with 'db' role")
            if etcd_count < 3 or etcd_count % 2 == 0:
                raise ValueError(
                    f"HA profile requires an odd number (2n+1, >= 3) of hosts with 'etcd' role for quorum (got {etcd_count})"
                )
        cluster = self.raw_config["cluster"]
        cluster.setdefault("version", "16")
        cluster.setdefault("package_source", "vnpt")
        cluster.setdefault("safeguard", True)
        cluster.setdefault("package_bridge", False)
        cluster.setdefault("package_bridge_port", 8888)

    # ------------------------------------------------------- normalized views
    def normalized_settings(self) -> Dict[str, Any]:
        s = self.raw_config.get("settings") or {}
        storage = s.get("storage") or {}
        auth = s.get("auth") or {}
        auth_cfg = {
            "postgres_password": auth.get("postgres_password", s.get("postgres_password", "postgres_password")),
            "replicator_password": auth.get("replicator_password", s.get("replicator_password", "replicator_password")),
            "monitor_password": auth.get("monitor_password", s.get("monitor_password", "")),
        }
        # Legacy aliases: backup -> pgbackrest, monitoring -> pmm
        pgbackrest = dict(s.get("pgbackrest") or s.get("backup") or {})
        if pgbackrest.get("tool") and "enabled" not in pgbackrest:
            pgbackrest.setdefault("enabled", True)
        pmm = normalize_pmm(s, self.raw_config["cluster"]["profile"], auth=auth_cfg)
        patroni = dict(s.get("patroni") or {})
        profile = self.raw_config["cluster"]["profile"]
        if "synchronous_mode" not in patroni:
            patroni["synchronous_mode"] = (profile == "ha")
        return {
            "auto_tune": s.get("auto_tune", True),
            "workload": (s.get("workload") or "oltp").lower(),
            "storage": {
                "data_dir": storage.get("data_dir", "/data/postgres/16/data"),
                "wal_dir": storage.get("wal_dir", "/data/postgres/16/wal"),
                "backup_dir": storage.get("backup_dir", "/data/backup"),
            },
            "auth": auth_cfg,
            "etcd": dict(s.get("etcd") or {}),
            "patroni": patroni,
            "pgbouncer": dict(s.get("pgbouncer") or {}),
            "haproxy": dict(s.get("haproxy") or {}),
            "keepalived": self._normalize_keepalived(s.get("keepalived") or {}, s.get("haproxy") or {}),
            "pgbackrest": pgbackrest,
            "pmm": pmm,
        }

    def _normalize_keepalived(self, raw, haproxy_cfg):
        """Off unless a free virtual IP is configured. The VIP is the client address."""
        enabled = self._enabled(raw, False)
        if not enabled:
            return {"enabled": False}
        if not self._enabled(haproxy_cfg, True):
            raise ValueError("settings.keepalived requires settings.haproxy.enabled")
        vip = str(raw.get("virtual_ip") or "").strip()
        address, _, prefix = vip.partition("/")
        if not _ipv4(address):
            raise ValueError("settings.keepalived.virtual_ip must be an IPv4 address, with an optional /prefix")
        if prefix and (not prefix.isdigit() or not 1 <= int(prefix) <= 32):
            raise ValueError("settings.keepalived.virtual_ip prefix must be between 1 and 32")
        hosts = self.raw_config["topology"]["hosts"]
        taken = {h["ip"] for h in hosts} | {h["node_ip"] for h in hosts if h.get("node_ip")}
        if address in taken:
            raise ValueError(f"settings.keepalived.virtual_ip {address} is already a node address")
        balancers = [h for h in hosts if "balancer" in (h.get("roles") or [])]
        if not balancers:
            raise ValueError("settings.keepalived requires at least one host with role balancer")
        password = str(raw.get("auth_pass") or "")
        if not re.fullmatch(r"[A-Za-z0-9]{1,8}", password):
            raise ValueError("settings.keepalived.auth_pass must be 1-8 letters or digits (keepalived PASS limit)")
        router_id = _as_int(raw.get("virtual_router_id", 51), "settings.keepalived.virtual_router_id")
        if not 1 <= router_id <= 255:
            raise ValueError("settings.keepalived.virtual_router_id must be from 1 to 255")
        interface = str(raw.get("interface") or "")
        if interface and not re.fullmatch(r"[A-Za-z0-9._:-]{1,15}", interface):
            raise ValueError("settings.keepalived.interface is not a network device name")
        priorities = raw.get("priorities") or {}
        if not isinstance(priorities, dict):
            raise ValueError("settings.keepalived.priorities must be a map of balancer name or IP to a priority")
        names = set()
        for host in balancers:
            names.update(value for value in (host.get("name"), host.get("ip"), host.get("node_ip"), host.get("ssh_host")) if value)
        normalized_priorities = {}
        for key, value in priorities.items():
            if str(key) not in names:
                raise ValueError(f"settings.keepalived.priorities key '{key}' is not a balancer host")
            priority = _as_int(value, f"settings.keepalived.priorities.{key}")
            if not 1 <= priority <= 255:
                raise ValueError(f"settings.keepalived.priorities.{key} must be from 1 to 255")
            normalized_priorities[str(key)] = priority
        return {
            "enabled": True,
            "address": address,
            "prefix": prefix,
            "auth_pass": password,
            "virtual_router_id": router_id,
            "interface": interface,
            "priorities": normalized_priorities,
        }

    @staticmethod
    def _enabled(comp_cfg: Dict[str, Any], default: bool) -> bool:
        v = comp_cfg.get("enabled", default)
        return bool(v)

    def _build_components_dag(self) -> List[Dict[str, Any]]:
        pkg_source = (self.raw_config.get("cluster", {}).get("package_source") or "vnpt").lower()
        is_percona = (pkg_source == "percona")
        repo_desc = "Percona PostgreSQL repository" if is_percona else "VNPT DB Engine internal repository"
        pg_desc = ("Percona PostgreSQL 16 binaries/extensions (Patroni-managed, never systemd)" if is_percona
                   else "VNPT PostgreSQL 16 binaries/extensions (Patroni-managed, never systemd)")
        pg_pkgs = (["percona-postgresql16-server", "percona-postgresql16-contrib",
                    "percona-pg_repack16", "percona-pgaudit16", "percona-wal2json16"] if is_percona
                   else ["postgresql16-server", "postgresql16-contrib",
                         "pg_repack_16", "pgaudit16", "wal2json_16"])
        patroni_pkgs = ["percona-patroni"] if is_percona else ["patroni"]
        pgb_pkgs = ["percona-pgbouncer"] if is_percona else ["pgbouncer"]
        haproxy_pkgs = ["percona-haproxy"] if is_percona else ["haproxy"]
        pgbackrest_pkgs = ["percona-pgbackrest"] if is_percona else ["pgbackrest"]
        ns = self.normalized_settings()
        comps: List[Dict[str, Any]] = [
            {"name": "os_baseline", "description": "OS baseline: sysctl, limits, softdog watchdog, postgres user, /data layout",
             "dependencies": [], "target_roles": ["all"], "tags": ["os_baseline", "os", "sysctl"],
             "packages": ["numactl", "sysstat", "acl", "cronie"], "services": []},
            {"name": "local_repo", "description": repo_desc,
             "dependencies": ["os_baseline"], "target_roles": ["all"], "tags": ["local_repo", "repo"],
             "packages": [], "services": []},
        ]
        etcd_enabled = self._enabled(ns["etcd"], True)
        if etcd_enabled:
            comps.append({"name": "etcd", "description": "etcd DCS (1-node standalone, 3-node HA, scale-out aware)",
                          "dependencies": ["local_repo"], "target_roles": ["etcd"], "tags": ["etcd", "dcs"],
                          "packages": ["etcd"], "services": ["etcd"]})
        comps.append({"name": "postgresql16", "description": pg_desc,
                      "dependencies": ["local_repo"], "target_roles": ["db"], "tags": ["postgresql16", "pg", "postgres"],
                      "packages": pg_pkgs,

                      "services": []})
        patroni_deps = (["etcd"] if etcd_enabled else []) + ["postgresql16"]
        comps.append({"name": "patroni", "description": "Patroni: PG lifecycle, DCS dynamic config, failover (both profiles)",
                      "dependencies": patroni_deps, "target_roles": ["db"],
                      "tags": ["patroni"],
                      "packages": patroni_pkgs, "services": ["patroni"]})
        comps.append({"name": "db_ready", "description": "DCS/SQL sync, optional restart and DB readiness",
                      "dependencies": ["patroni"], "target_roles": ["db"],
                      "tags": ["db_ready", *sorted(CONFIG_TAGS)],
                      "action": "db_ready", "packages": [], "services": []})
        if self._enabled(ns["pgbouncer"], True):
            comps.append({"name": "pgbouncer", "description": "PgBouncer pooler :6432 (transaction, prepared statements)",
                          "dependencies": ["db_ready"], "target_roles": ["db"], "tags": ["pgbouncer"],
                          "packages": pgb_pkgs, "services": ["pgbouncer"]})
        if self._enabled(ns["haproxy"], True):
            comps.append({"name": "haproxy", "description": "HAProxy :5433 write / :5434 read / :7000 stats via Patroni :8008",
                          "dependencies": ["db_ready"], "target_roles": ["balancer"], "tags": ["haproxy"],
                          "packages": haproxy_pkgs, "services": ["haproxy"]})
        if ns["keepalived"].get("enabled"):
            comps.append({"name": "keepalived", "description": "Keepalived virtual IP in front of HAProxy",
                          "dependencies": ["haproxy"], "target_roles": ["balancer"], "tags": ["keepalived", "vip"],
                          "packages": ["keepalived"], "services": ["keepalived"]})
        if self._enabled(ns["pgbackrest"], True):
            comps.append({"name": "pgbackrest", "description": "pgBackRest stanza, archive, cron (local or S3)",
                          "dependencies": ["db_ready"], "target_roles": ["db"], "tags": ["pgbackrest", "backup"],
                          "packages": pgbackrest_pkgs, "services": []})
        if self._enabled(ns["pmm"], False):
            deps = ["db_ready"]
            if ns["pmm"]["mode"] == "local":
                comps.append({"name": "pmm_server", "description": "Local PMM 3 Server with persistent volume",
                              "dependencies": ["db_ready"], "target_roles": ["db"],
                              "tags": ["pmm", "monitoring", "pmm_server"],
                              "packages": ["podman"] if ns["pmm"]["server_install"]["runtime"] == "podman" else [],
                              "services": ["vde-pmm-server"]})
                deps.append("pmm_server")
            comps.append({"name": "pmm", "description": "PMM 3 PostgreSQL and host monitoring client",
                          "dependencies": deps, "target_roles": ["db"], "tags": ["pmm", "monitoring"],
                          "packages": ["pmm-client"], "services": ["pmm-agent"]})
        return comps

    # ------------------------------------------------------------ smart tune
    def _cluster_hardware(self):
        """Smallest discovered node governs cluster-wide GUC (safe default)."""
        rams, cpus = [], []
        for h in self.raw_config["topology"]["hosts"]:
            f = self.hardware_facts.get(h["ip"]) or {}
            if f.get("ram_mb"):
                rams.append(int(f["ram_mb"]))
            if f.get("vcpu"):
                cpus.append(int(f["vcpu"]))
        # Placeholder noi bo cho validate/--offline (khong bao gio duoc apply:
        # plan/apply/enforce deu bat buoc SSH discovery strict, loi la dung).
        ram_mb = min(rams) if rams else 16 * 1024
        vcpu = min(cpus) if cpus else 4
        return ram_mb, vcpu

    def _calculate_smart_defaults(self):
        settings = self.raw_config.get("settings", {}) or {}
        ns = self.normalized_settings()
        auto_tune = ns["auto_tune"]
        workload = ns["workload"]
        profile = self.raw_config["cluster"]["profile"]
        total_ram_mb, vcpu = self._cluster_hardware()
        budget = memory_budget(total_ram_mb, ns["pmm"])
        ram_mb = budget["database_ram_mb"]
        ram_gb = ram_mb / 1024.0

        pgbackrest_enabled = self._enabled(ns["pgbackrest"], True)
        if pgbackrest_enabled:
            stanza = str(ns["pgbackrest"].get("stanza") or "vde_cluster").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", stanza):
                raise ValueError(f"Invalid settings.pgbackrest.stanza '{stanza}'")
            archive_guc = {
                "archive_mode": "on",
                "archive_command": f"pgbackrest --stanza={stanza} archive-push %p",
            }
        else:
            archive_guc = {
                "archive_mode": "off",
                "archive_command": "",
            }

        if auto_tune:
            # tiny chi khi user chon tiny, hoac RAM that su rat nho (<2GB).
            # VM 4GB bao ~3655MB kha dung van dung nhanh oltp chuan.
            if workload == "tiny" or ram_mb < 2048:
                shared_mb = max(128, int(ram_mb * 0.15))
                eff_mb = max(256, int(ram_mb * 0.40))
                work_mb, maint_mb, max_conn = 16, 256, 100
            elif workload == "olap":
                shared_mb = int(ram_mb * 0.25); eff_mb = int(ram_mb * 0.75)
                work_mb, maint_mb, max_conn = 128, min(2048, max(512, int(ram_mb * 0.06))), 200
            elif workload == "crit":
                shared_mb = int(ram_mb * 0.25); eff_mb = int(ram_mb * 0.75)
                # maintenance_work_mem scales with RAM: 4GB->512MB, >=8GB->1GB cap
                work_mb, maint_mb, max_conn = 32, min(1024, max(256, ram_mb // 8)), 500
            else:  # oltp
                shared_mb = int(ram_mb * 0.25); eff_mb = int(ram_mb * 0.75)
                work_mb, maint_mb, max_conn = 32, min(1024, max(256, ram_mb // 8)), 500

            default_guc: Dict[str, Any] = {
                "shared_buffers": _fmt_mb(shared_mb),
                "effective_cache_size": _fmt_mb(eff_mb),
                "work_mem": f"{work_mb}MB",
                "maintenance_work_mem": f"{maint_mb}MB",
                "max_connections": settings.get("max_connections", max_conn),
                "max_worker_processes": max(8, vcpu),
                "max_parallel_workers": vcpu,
                "max_parallel_workers_per_gather": 4 if workload == "olap" else 2,
                "wal_level": "replica",
                **archive_guc,
                **({"archive_timeout": "60"} if pgbackrest_enabled else {}),
                "checkpoint_completion_target": 0.9,
                "checkpoint_timeout": "30min" if workload == "olap" else "15min",
                "wal_buffers": "16MB",
                "wal_keep_size": "1GB",
                "max_wal_senders": 10,
                "max_replication_slots": 10,
                "hot_standby": "on",
                "random_page_cost": 1.1,
                "effective_io_concurrency": 200,
                "synchronous_commit": "on",
                "track_commit_timestamp": "off",
            }
            if workload == "crit":
                default_guc.update({"full_page_writes": "on",
                                    "synchronous_commit": "remote_apply" if profile == "ha" else "on"})
            default_sysctl = {
                "vm.overcommit_memory": 2, "vm.overcommit_ratio": 80, "vm.swappiness": 1,
                "net.core.somaxconn": 4096, "net.ipv4.tcp_max_syn_backlog": 4096,
            }
        else:
            default_guc = {"shared_buffers": "128MB", "max_connections": 100, "wal_level": "replica",
                           **archive_guc}
            default_sysctl = {}

        default_ports = {"postgresql": 5432, "pgbouncer": 6432, "haproxy_write": 5433,
                         "haproxy_read": 5434, "haproxy_stats": 7000, "patroni_rest": 8008,
                         "etcd_client": 2379, "etcd_peer": 2380}

        user_parameters = settings.get("parameters") or settings.get("guc") or {}
        user_sysctl = settings.get("sysctl") or {}
        user_ports = settings.get("ports") or {}
        effective_guc = query_guc({**default_guc, **user_parameters}, ns["pmm"])
        effective_sysctl = {**default_sysctl, **user_sysctl}
        effective_ports = {**default_ports, **user_ports}
        cluster = self.raw_config["cluster"]
        bridge_listen = int(cluster.get("package_bridge_port", 8888))
        if cluster.get("package_bridge") and bridge_listen in {int(v) for v in effective_ports.values()}:
            raise ValueError(f"cluster.package_bridge_port {bridge_listen} overlaps a service port")
        if ns["pmm"]["enabled"] and ns["pmm"]["mode"] == "local":
            ports = ["postgresql", "patroni_rest"]
            if self._enabled(ns["etcd"], True): ports += ["etcd_client", "etcd_peer"]
            if self._enabled(ns["pgbouncer"], True): ports += ["pgbouncer"]
            if self._enabled(ns["haproxy"], True): ports += ["haproxy_write", "haproxy_read", "haproxy_stats"]
            if any(int(effective_ports[k]) == ns["pmm"]["server_install"]["https_port"] for k in ports):
                raise ValueError("PMM HTTPS port conflicts with an enabled database-stack port")
            shared_values = [effective_guc.get("shared_buffers", "128MB")]
            shared_values += [h.get("parameters", {}).get("shared_buffers", shared_values[0])
                              for h in self.raw_config["topology"]["hosts"]]
            for shared in shared_values:
                match = re.fullmatch(r"([0-9]+)(kB|MB|GB)", str(shared), re.I)
                if not match:
                    raise ValueError("Co-located PMM requires shared_buffers with explicit kB/MB/GB units")
                shared_mb = int(match[1]) * {"kb": 1/1024, "mb": 1, "gb": 1024}[match[2].lower()]
                if shared_mb >= ram_mb:
                    raise ValueError("shared_buffers exceeds the DB RAM budget after reserving PMM and OS memory")

        # ---- hba_rules: strings or dicts -> ordered pg_hba lines (Pigsty style)
        hosts = self.raw_config["topology"]["hosts"]
        default_hba = ["local all all peer",
                       "host all all 127.0.0.1/32 scram-sha-256",
                       "host all all ::1/128 scram-sha-256"]
        for h in hosts:
            cip = h.get("node_ip") or h["ip"]
            default_hba.append(f"host all all {cip}/32 scram-sha-256")
            default_hba.append(f"host replication replicator {cip}/32 scram-sha-256")
        default_hba.append("host all all 0.0.0.0/0 scram-sha-256")
        raw_hba = settings.get("hba_rules") or settings.get("pg_hba_rules")
        hba_rules: List[str] = []
        if raw_hba:
            for r in raw_hba:
                if isinstance(r, str):
                    hba_rules.append(r)
                elif isinstance(r, dict):
                    addr = r.get("address", "")
                    hba_rules.append(" ".join(str(x) for x in
                        [r.get("type", "host"), r.get("database", "all"), r.get("user", "all"),
                         addr, r.get("method", "scram-sha-256")] if str(x) != ""))
        else:
            hba_rules = default_hba

        cluster_info = dict(self.raw_config["cluster"])
        ssh_info = self.raw_config.get("ssh") or self.raw_config.get("topology", {}).get("ssh", {})

        self.resolved_config = {
            "cluster": cluster_info,
            "topology": self.raw_config["topology"],
            "ssh": ssh_info,
            "settings": settings,
            "settings_normalized": ns,
            "hardware_facts": self.hardware_facts,
            "effective_config": {
                "guc": effective_guc, "sysctl": effective_sysctl, "ports": effective_ports,
                "hba_rules": hba_rules,
                "users": settings.get("users") or [],
                "databases": settings.get("databases") or [],
                "extensions": list(dict.fromkeys(list(settings.get("extensions") or []) +
                    (["pg_stat_statements"] if ns["pmm"]["enabled"] and ns["pmm"]["query_source"] == "pgstatements" else []))),
            },
            "user_overrides": {"guc": user_parameters, "sysctl": user_sysctl, "ports": user_ports},
            "smart_defaults": {"enabled": auto_tune, "workload": workload,
                               "ram_mb": total_ram_mb, "vcpu": vcpu, "memory_budget": budget,
                               "guc": default_guc, "sysctl": default_sysctl, "ports": default_ports},
            "components": self.components,
        }

    # ------------------------------------------------------------- per-host
    def host_effective_guc(self, host: Dict[str, Any]) -> Dict[str, Any]:
        """Cluster GUC merged with per-host parameters override (highest)."""
        guc = dict(self.resolved_config["effective_config"]["guc"])
        guc.update(host.get("parameters") or {})
        return query_guc(guc, self.resolved_config["settings_normalized"]["pmm"])
