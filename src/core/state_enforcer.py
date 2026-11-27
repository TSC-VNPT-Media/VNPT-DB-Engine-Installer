import hashlib
import json
import os
import shlex
import time
from typing import Dict, Any, Tuple, List


_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENTS_DIR = os.path.join(_SRC_DIR, "components")

# Env vars each run.sh actually renders into node configuration.
# db_ready has no script; its hash is _db_ready_inputs().
COMPONENT_INPUTS = {
    "os_baseline": ("VDE_BACKUP_DIR", "VDE_PGDATA", "VDE_PGWAL", "VDE_SYSCTL_CONF_LINES"),
    "local_repo": ("VDE_GPG_KEY_URL", "VDE_PACKAGE_SOURCE", "VDE_PG_VERSION", "VDE_RELEASE_RPM_URL", "VDE_REPO_URL"),
    "etcd": (
        "VDE_ETCD_CLIENT_PORT", "VDE_ETCD_INITIAL_CLUSTER", "VDE_ETCD_NAME", "VDE_ETCD_PEER_PORT",
        "VDE_ETCD_TOKEN", "VDE_NODE_IP", "VDE_PGDATA",
    ),
    "postgresql16": ("VDE_PACKAGE_SOURCE", "VDE_PGDATA", "VDE_PG_VERSION"),
    "patroni": (
        "VDE_CLUSTER_NAME", "VDE_ETCD_HOSTS", "VDE_NODE_IP", "VDE_NODE_NAME", "VDE_PACKAGE_SOURCE",
        "VDE_PATRONI_REST_PORT", "VDE_PGDATA", "VDE_PGWAL", "VDE_PG_PASS", "VDE_REPL_PASS",
        "VDE_PG_PORT", "VDE_PG_VERSION",
    ),
    "pgbouncer": (
        "VDE_PACKAGE_SOURCE", "VDE_PGBOUNCER_DEFAULT_POOL_SIZE", "VDE_PGBOUNCER_MAX_CLIENT_CONN",
        "VDE_PGBOUNCER_MAX_PREPARED", "VDE_PGBOUNCER_POOL_MODE", "VDE_PGBOUNCER_PORT",
        "VDE_PG_PASS", "VDE_PG_PORT", "VDE_PG_VERSION",
    ),
    "haproxy": (
        "VDE_DB_NODES", "VDE_HAPROXY_BACKEND_PORT", "VDE_HAPROXY_READ_PORT", "VDE_HAPROXY_STATS_PORT",
        "VDE_HAPROXY_WRITE_PORT", "VDE_PACKAGE_SOURCE", "VDE_PATRONI_REST_PORT",
    ),
    "keepalived": (
        "VDE_HAPROXY_WRITE_PORT", "VDE_KEEPALIVED_AUTH_PASS", "VDE_KEEPALIVED_INTERFACE",
        "VDE_KEEPALIVED_PEERS", "VDE_KEEPALIVED_PREFIX", "VDE_KEEPALIVED_PRIORITY",
        "VDE_KEEPALIVED_ROUTER_ID", "VDE_KEEPALIVED_STATE", "VDE_KEEPALIVED_VIP", "VDE_NODE_IP",
    ),
    "pgbackrest": (
        "VDE_BACKUP_DEST", "VDE_BACKUP_DIR", "VDE_BACKUP_RETENTION_DIFF", "VDE_BACKUP_RETENTION_FULL",
        "VDE_BACKUP_S3", "VDE_BACKUP_S3_BUCKET", "VDE_BACKUP_S3_KEY", "VDE_BACKUP_S3_REGION",
        "VDE_BACKUP_S3_SECRET", "VDE_BACKUP_S3_URI_STYLE", "VDE_BACKUP_S3_VERIFY_TLS",
        "VDE_BACKUP_STANZA", "VDE_PACKAGE_SOURCE", "VDE_PATRONI_REST_PORT", "VDE_PGDATA",
        "VDE_PG_PORT", "VDE_PROFILE",
    ),
    "pmm_server": ("VDE_PMM_CONFIG", "VDE_PROFILE"),
    "pmm": ("VDE_NODE_IP", "VDE_NODE_NAME", "VDE_PACKAGE_SOURCE", "VDE_PG_PORT", "VDE_PG_VERSION", "VDE_PMM_CONFIG"),
}

# Referenced by a run.sh but intentionally excluded from that component's hash.
IGNORED_INPUTS = {
    "VDE_FORCE_UPDATE": "execution flag, not desired configuration",
    "VDE_ETCD_PHASE": "bootstrap phase flag; the rendered etcd config is hashed, not the phase",
    "VDE_PATRONI_EXPECT_PRIMARY": "bootstrap ordering flag, not desired configuration",
    "VDE_SSH_TARGET": "control-node transport identity; moving local: true must not cause drift",
    "VDE_PMM_PASSWORD": "secret resolved from the environment; the variable name lives in VDE_PMM_CONFIG",
    "VDE_PMM_DB_PASSWORD": "secret resolved from the environment; the variable name lives in VDE_PMM_CONFIG",
    "VDE_PATRONI_GUC_YAML": "bootstrap.dcs only; live GUC sync is owned by db_ready",
    "VDE_HBA_LINES": "bootstrap pg_hba only; live HBA sync is owned by db_ready",
    "VDE_DB_OBJECTS_SQL": "bootstrap SQL only; live object sync is owned by db_ready",
    "VDE_DB_EXTENSIONS_SQL": "bootstrap SQL only; live extension sync is owned by db_ready",
    "VDE_DB_DATABASES": "bootstrap SQL only; live database sync is owned by db_ready",
    "VDE_PATRONI_TTL": "bootstrap.dcs only; live DCS sync is owned by db_ready",
    "VDE_PATRONI_LOOP_WAIT": "bootstrap.dcs only; live DCS sync is owned by db_ready",
    "VDE_PATRONI_RETRY_TIMEOUT": "bootstrap.dcs only; live DCS sync is owned by db_ready",
    "VDE_PATRONI_SYNC_MODE": "bootstrap.dcs only; live DCS sync is owned by db_ready",
    "VDE_PATRONI_SYNC_STRICT": "bootstrap.dcs only; live DCS sync is owned by db_ready",
}


class StateEnforcer:
    """
    SSOT lifecycle + Pigsty-style live config sync (unified for standalone & HA).

    Both profiles run Patroni + etcd, so Day-2 sync is always:
      PATCH :8008/config (DCS) -> POST :8008/reload -> sync DB objects
      -> check pg_settings.pending_restart -> optional rolling restart.
    /etc/vde/baseline.json schema 3 stores one hash per component for drift detection.
    """

    def __init__(self, resolved_config: Dict[str, Any]):
        self.config = resolved_config
        self.desired_hash = self._calculate_desired_hash()

    # ------------------------------------------------------------------ hash
    def _calculate_desired_hash(self) -> str:
        # Changing the node that runs the CLI changes transport, not desired
        # service configuration. Do not cause drift merely by moving local: true.
        topology = dict(self.config.get("topology", {}))
        topology["hosts"] = [{k: v for k, v in h.items() if k != "local"}
                             for h in topology.get("hosts", [])]
        # package_bridge is how packages are fetched, not the service configuration.
        cluster = dict(self.config.get("cluster", {}))
        cluster.pop("package_bridge", None)
        cluster.pop("package_bridge_port", None)
        stable = {
            "cluster": cluster,
            "topology": topology,
            "settings": self.config.get("settings", {}),
            "effective_config": self.config.get("effective_config", {}),
            "components": [c.get("name") for c in self.config.get("components", [])],
        }
        return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()

    def get_desired_hash(self) -> str:
        return self.desired_hash

    # ------------------------------------------------------- per-component hash
    def _digest(self, payload: Any) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def _script_digest(self, name: str) -> str:
        path = os.path.join(COMPONENTS_DIR, name, "run.sh")
        try:
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return "MISSING"

    def _db_ready_inputs(self, host: Dict[str, Any]) -> Dict[str, Any]:
        # Live DCS/SQL/extension sync. Passwords that exist only in the process
        # environment are represented by the variable name, never the value.
        eff = self.config.get("effective_config") or {}
        ns = self.config.get("settings_normalized") or {}
        pmm = ns.get("pmm") or {}
        pgb = ns.get("pgbouncer") or {}
        return {
            "patroni": self.patroni_config_payload(),
            "users": eff.get("users") or [],
            "databases": eff.get("databases") or [],
            "extensions": eff.get("extensions") or [],
            "auth": ns.get("auth") or {},
            "pmm_database_password_env": pmm.get("database_password_env") or "",
            "pgbouncer_enabled": bool(pgb.get("enabled", True)),
            "host_parameters": host.get("parameters") or {},
        }

    def component_hashes(self, host: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, str]:
        """Desired hash of every component that applies to this host."""
        out: Dict[str, str] = {}
        for comp in self.config.get("components", []):
            if not self._component_applies(comp, host.get("ip")):
                continue
            name = comp["name"]
            if name == "db_ready":
                payload = {"config": self._db_ready_inputs(host)}
            else:
                keys = COMPONENT_INPUTS.get(name)
                if keys is None:
                    raise KeyError(f"No COMPONENT_INPUTS declared for component '{name}'")
                payload = {"inputs": {k: env.get(k) for k in keys}, "script": self._script_digest(name)}
            out[name] = self._digest(payload)
        return out

    # ----------------------------------------------------------------- drift
    def _host_obj(self, target: str) -> Dict[str, Any]:
        hosts = self.config.get("topology", {}).get("hosts", [])
        host = next((h for h in hosts if target in (h.get("ip"), h.get("ssh_host"))), None)
        if host is None:
            raise ValueError(f"Host not in inventory: {target}")
        return host

    def _read_baseline(self, host: str, runner: Any) -> Dict[str, Any]:
        cmd = "cat /etc/vde/baseline.json 2>/dev/null || echo '{}'"
        _rc, output = runner.run_command(host, cmd, component_name="ssot-check")
        if not output or output == "[DRY-RUN SUCCESS]":
            return {}
        try:
            observed = json.loads(output)
        except Exception:
            return {}
        return observed if isinstance(observed, dict) else {}

    def _schema2_covers(self, observed: Dict[str, Any], host: str) -> bool:
        # A schema 2 file proves every component only while its single manifest
        # hash still matches. A later edit cannot be attributed to one component.
        if observed.get("schema_version") != 2 or observed.get("scope") != "full":
            return False
        if observed.get("manifest_hash") != self.desired_hash:
            return False
        completed = observed.get("completed_components", [])
        if not isinstance(completed, list) or not all(isinstance(v, str) for v in completed):
            return False
        expected = {c["name"] for c in self.config.get("components", []) if self._component_applies(c, host)}
        return expected <= set(completed)

    def _recorded_from(self, observed: Dict[str, Any], host: str, desired: Dict[str, str]) -> Dict[str, str]:
        """Hashes to keep. A still-matching schema 2 baseline expands to every desired hash."""
        if observed.get("schema_version") == 3 and isinstance(observed.get("components_hash"), dict):
            return {k: v for k, v in observed["components_hash"].items()
                    if isinstance(k, str) and isinstance(v, str)}
        if self._schema2_covers(observed, host):
            return dict(desired)
        return {}

    def recorded_hashes(self, host: str, runner: Any, desired: Dict[str, str]) -> Dict[str, str]:
        return self._recorded_from(self._read_baseline(host, runner), host, desired)

    def inspect_state(self, host: str, runner: Any, env: Dict[str, Any]) -> Dict[str, Any]:
        host_obj = self._host_obj(host)
        desired = self.component_hashes(host_obj, env)
        raw = self._read_baseline(host, runner)
        recorded = self._recorded_from(raw, host, desired)
        missing, changed = [], []
        observed_hashes: Dict[str, str] = {}
        for name in sorted(desired):
            if name not in recorded:
                missing.append(name)
                observed_hashes[name] = "NONE"
            elif recorded[name] != desired[name]:
                changed.append(name)
                observed_hashes[name] = recorded[name]
            else:
                observed_hashes[name] = recorded[name]
        legacy = raw.get("manifest_hash", "NONE")
        if not isinstance(legacy, str):
            legacy = "NONE"
        return {
            "has_drift": bool(missing or changed),
            "drifted": sorted(missing + changed),
            "missing": missing,
            "changed": changed,
            "observed": observed_hashes,
            "desired": desired,
            "observed_hash": "per-component" if raw.get("schema_version") == 3 else legacy,
            "desired_hash": self.desired_hash,
            "observed_profile": raw.get("profile"),
        }

    def inspect_drift(self, host: str, runner: Any, env: Dict[str, Any]) -> Tuple[bool, str, str]:
        s = self.inspect_state(host, runner, env)
        return s["has_drift"], s["observed_hash"], s["desired_hash"]

    def _component_applies(self, component, target):
        hosts = self.config.get("topology", {}).get("hosts", [])
        host = next((h for h in hosts if target in (h["ip"], h.get("ssh_host"))), None)
        roles = component.get("target_roles", ["all"])
        return host is None or "all" in roles or bool(set(roles) & set(host.get("roles", [])))

    def _checked(self, host, runner, command, component):
        rc, output = runner.run_command(host, command, component_name=component)
        if rc:
            raise RuntimeError(f"{component} failed on {host} (rc={rc})")
        return output

    def enforce_baseline(self, host: str, runner: Any, components_hash: Dict[str, str]) -> int:
        # Records the hash of components applied to this host, not live-state equality.
        # Do not persist SQL/user passwords or provider credentials here.
        if not isinstance(components_hash, dict):
            raise TypeError("components_hash must be a map of component name to hash")
        payload = {
            "schema_version": 3,
            "cluster_name": self.config["cluster"]["name"],
            "profile": self.config["cluster"]["profile"],
            "components_hash": {k: components_hash[k] for k in sorted(components_hash)},
        }
        cmd = ("set -e; umask 077; mkdir -p /etc/vde; "
               "TMP=$(mktemp /etc/vde/baseline.XXXXXX); trap 'rm -f \"$TMP\"' EXIT; "
               "printf '%s\\n' " + shlex.quote(json.dumps(payload, sort_keys=True)) + " > \"$TMP\"; "
               "chmod 600 \"$TMP\"; mv \"$TMP\" /etc/vde/baseline.json")
        self._checked(host, runner, cmd, "ssot-enforce")
        return 0

    # ------------------------------------------------------- patroni payload
    def patroni_config_payload(self) -> Dict[str, Any]:
        ns = self.config.get("settings_normalized", {}) or {}
        pat = ns.get("patroni", {}) or {}
        eff = self.config.get("effective_config", {}) or {}
        profile = self.config["cluster"]["profile"]
        db_hosts = [h for h in self.config["topology"]["hosts"] if "db" in h.get("roles", [])]
        sync_mode = bool(pat.get("synchronous_mode", profile == "ha")) and len(db_hosts) > 1 and profile == "ha"
        params: Dict[str, Any] = {}
        for k, v in (eff.get("guc") or {}).items():
            params[k] = v
        return {
            "ttl": int(pat.get("ttl", 30)),
            "loop_wait": int(pat.get("loop_wait", 10)),
            "retry_timeout": int(pat.get("retry_timeout", 10)),
            "maximum_lag_on_failover": 1048576,
            "synchronous_mode": sync_mode,
            "synchronous_mode_strict": bool(pat.get("synchronous_mode_strict", False)) and sync_mode,
            "postgresql": {"use_pg_rewind": True, "use_slots": True,
                           "parameters": params, "pg_hba": eff.get("hba_rules") or []},
        }

    # ------------------------------------------------------------ DB objects
    @staticmethod
    def _esc(v: Any) -> str:
        return str(v).replace("'", "''")

    def build_objects_sql(self) -> str:
        eff = self.config.get("effective_config", {}) or {}
        ns = self.config.get("settings_normalized", {}) or {}
        auth = ns.get("auth", {}) or {}
        pmm = ns.get("pmm", {}) or {}
        lines: List[str] = []
        # Built-in roles always kept in sync (needed for HA scale-out too)
        builtin = [
            {"name": "postgres", "password": auth.get("postgres_password", "postgres_password"), "superuser": True},
            {"name": "replicator", "password": auth.get("replicator_password", "replicator_password"), "replication": True},
        ]
        monitor_pass = auth.get("monitor_password") or ""
        if not monitor_pass and pmm.get("enabled") and pmm.get("database_user") == "monitor" and pmm.get("database_password_env"):
            monitor_pass = os.environ.get(pmm["database_password_env"], "")
        if monitor_pass:
            builtin.append({"name": "monitor", "password": monitor_pass, "roles": ["pg_monitor"]})
        all_users = builtin + list(eff.get("users") or [])
        seen = set()
        for u in all_users:
            name = u.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            opts = []
            if u.get("superuser"):
                opts.append("SUPERUSER")
            if u.get("replication"):
                opts.append("REPLICATION")
            if u.get("createdb"):
                opts.append("CREATEDB")
            if u.get("conn_limit") is not None:
                opts.append(f"CONNECTION LIMIT {int(u['conn_limit'])}")
            opt_s = (" " + " ".join(opts)) if opts else ""
            qname = str(name).replace('"', '""')
            lines.append(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{self._esc(name)}') THEN "
                f"CREATE ROLE \"{qname}\" LOGIN{opt_s}; END IF; END $$;")
            if u.get("password"):
                lines.append(f"ALTER ROLE \"{qname}\" WITH LOGIN{opt_s} PASSWORD '{self._esc(u['password'])}';")
            elif opt_s:
                lines.append(f"ALTER ROLE \"{qname}\" WITH LOGIN{opt_s};")
            for role in (u.get("roles") or []):
                if not isinstance(role, str) or not role.strip():
                    raise ValueError(f"Invalid role name in user '{name}': {role!r}")
                qrole = role.strip().replace('"', '""')
                lines.append(f'GRANT "{qrole}" TO "{qname}";')
        databases = list(eff.get("databases") or [])
        global_ext = list(eff.get("extensions") or [])
        for db in databases:
            dbn = db.get("name"); owner = db.get("owner")
            if not dbn:
                continue
            owner_s = f" OWNER \"{owner}\"" if owner else ""
            lines.append(f"SELECT 'CREATE DATABASE \"{dbn}\"{owner_s}' WHERE NOT EXISTS "
                         f"(SELECT 1 FROM pg_database WHERE datname='{self._esc(dbn)}') \\gexec")
        # Extensions are emitted as a separate per-db script marker; psql runs
        # them against each database in build_objects_commands().
        return "\n".join(lines)

    def build_extension_sql(self) -> str:
        eff = self.config.get("effective_config", {}) or {}
        exts = list(eff.get("extensions") or [])
        for db in eff.get("databases") or []:
            for e in db.get("extensions") or []:
                if e not in exts:
                    exts.append(e)
        return "\n".join(f'CREATE EXTENSION IF NOT EXISTS "{e}";' for e in exts)

    def _pg_port(self) -> str:
        ports = (self.config.get("effective_config") or {}).get("ports") or {}
        return str(ports.get("postgresql", 5432))

    def _rest_port(self) -> str:
        ports = (self.config.get("effective_config") or {}).get("ports") or {}
        return str(ports.get("patroni_rest", 8008))

    def _rest_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._rest_port()}{path}"

    def _psql(self, runner, host, sql, db="postgres"):
        version = str(self.config["cluster"].get("version", "16"))
        cmd = ("set -e; umask 077; SQL_FILE=$(mktemp /tmp/vde-sync.XXXXXX); "
               "trap 'rm -f \"$SQL_FILE\"' EXIT; "
               "printf '%s\\n' " + shlex.quote(sql) + " > \"$SQL_FILE\"; "
               "chown postgres:postgres \"$SQL_FILE\"; "
               f"runuser -u postgres -- /usr/pgsql-{version}/bin/psql -X -w -p {self._pg_port()} "
               f"-v ON_ERROR_STOP=1 -d {shlex.quote(db)} -f \"$SQL_FILE\"")
        return self._checked(host, runner, cmd, "db-objects-sync")

    # ------------------------------------------------------------- live sync
    def is_leader(self, host: str, runner: Any) -> bool:
        rc, _ = runner.run_command(
            host, f"curl -sf {self._rest_url('/primary')} >/dev/null 2>&1",
            component_name="patroni-role")
        return rc == 0

    def wait_database(self, host, runner, timeout=30):
        # Replica clone (pg_basebackup) can outlast the standalone 30s gate.
        # Print Patroni state while waiting so a long HA clone is visible.
        limit = max(1, int(timeout))
        health = self._rest_url("/health")
        patroni = self._rest_url("/patroni")
        cmd = (f"for i in $(seq 1 {limit}); do "
               f"curl -fsS --connect-timeout 2 --max-time 3 {health} >/dev/null 2>&1 && exit 0; "
               f"if [ $(((i % 15))) -eq 0 ]; then echo \"still waiting for PostgreSQL ($i/{limit})\"; "
               f"curl -sS --connect-timeout 2 --max-time 3 {patroni} | head -c 400 || true; echo; fi; "
               "sleep 1; done; "
               f"echo '--- patroni state ---'; curl -sS --connect-timeout 2 --max-time 3 {patroni} || true; exit 1")
        self._checked(host, runner, cmd, "db-ready")

    def _query(self, host, runner, sql, component):
        version=str(self.config['cluster'].get('version','16'))
        binary=shlex.quote('/usr/pgsql-'+version+'/bin/psql')
        command=(f"PGCONNECT_TIMEOUT=3 runuser -u postgres -- {binary} -X -w -p {self._pg_port()} "
                 f"-At -v ON_ERROR_STOP=1 -d postgres -c {shlex.quote(sql)}")
        return self._checked(host,runner,command,component).strip()

    def _wait_boolean(self, host, runner, sql, component, timeout=30):
        deadline=time.monotonic()+timeout
        while True:
            value=self._query(host,runner,sql,component)
            if value in ('t','[DRY-RUN SUCCESS]'): return
            if time.monotonic()>=deadline:
                raise RuntimeError(component+' did not converge on '+host)
            time.sleep(1)

    def wait_configuration_reload(self, host, runner):
        # Patroni accepts reload asynchronously. Wait for the desired GUCs to
        # reach the files, then explicitly reload PG and wait for its generation
        # to advance before trusting pg_settings.pending_restart.
        parameters=self.patroni_config_payload()['postgresql']['parameters']
        if parameters:
            expected=','.join("('"+self._esc(k)+"','"+self._esc(('on' if v else 'off') if isinstance(v,bool) else v)+"')" for k,v in parameters.items())
            sql=("WITH expected(name,setting) AS (VALUES "+expected+"), written AS "
                 "(SELECT DISTINCT ON(name) name,setting FROM pg_file_settings ORDER BY name,seqno DESC) "
                 "SELECT NOT EXISTS (SELECT 1 FROM expected e LEFT JOIN written w USING(name) WHERE w.setting IS DISTINCT FROM e.setting);")
            self._wait_boolean(host,runner,sql,'db-config-files',timeout=60)
        value=self._query(host,runner,'SELECT EXTRACT(EPOCH FROM pg_conf_load_time()); SELECT pg_reload_conf();','db-config-reload')
        if value=='[DRY-RUN SUCCESS]': return
        lines=value.splitlines()
        if len(lines)!=2 or lines[1]!='t':
            raise RuntimeError('PostgreSQL rejected configuration reload on '+host)
        # Accept only a numeric timestamp before embedding it into SQL.
        stamp=float(lines[0])
        if not 0<stamp<float('inf'):
            raise RuntimeError('Invalid PostgreSQL configuration timestamp')
        self._wait_boolean(host,runner,f'SELECT pg_conf_load_time() > to_timestamp({stamp});','db-config-ack')

    def patch_dcs(self, host: str, runner: Any) -> None:
        """Push the cluster DCS document once. Any node accepts the PATCH; call it on the leader."""
        payload = json.dumps(self.patroni_config_payload())
        patch_cmd = ("curl -fsS --connect-timeout 3 --max-time 10 -X PATCH -H 'Content-Type: application/json' "
                     f"-d {shlex.quote(payload)} {self._rest_url('/config')} >/dev/null")
        self._checked(host, runner, patch_cmd, "patroni-config-sync")

    def reload_and_check(self, host: str, runner: Any) -> List[str]:
        """Ask this node to reload and report settings that still need a restart."""
        reload_cmd = ("curl -fsS --connect-timeout 3 --max-time 10 -X POST "
                      f"{self._rest_url('/reload')} >/dev/null")
        self._checked(host, runner, reload_cmd, "patroni-reload")
        self.wait_configuration_reload(host, runner)
        return self.check_pending_restart(host, runner)

    def reconcile_live_config(self, host: str, runner: Any, allow_restart: bool = False) -> Dict[str, Any]:
        """Patch and reload one node. Multi-node HA patches once, then reloads each node."""
        self.patch_dcs(host, runner)
        # Keep the single-node event stream identical: reload is part of the same step.
        reload_cmd = ("curl -fsS --connect-timeout 3 --max-time 10 -X POST "
                      f"{self._rest_url('/reload')} >/dev/null")
        self._checked(host, runner, reload_cmd, "patroni-config-sync")
        self.wait_configuration_reload(host, runner)
        return {"host": host, "patched": True,
                "pending_restart": self.check_pending_restart(host, runner)}

    def sync_database_objects(self, host, runner):
        if not self.is_leader(host, runner):
            return False
        sql = self.build_objects_sql()
        if sql.strip(): self._psql(runner, host, sql)
        extension_sql = self.build_extension_sql()
        if extension_sql.strip():
            dbs = ["postgres"] + [d["name"] for d in self.config.get("effective_config", {}).get("databases", []) if d.get("name")]
            for db in dict.fromkeys(dbs): self._psql(runner, host, extension_sql, db)
        return True

    def check_pending_restart(self, host: str, runner: Any) -> List[str]:
        version = str(self.config["cluster"].get("version", "16"))
        cmd = (f"PGCONNECT_TIMEOUT=3 runuser -u postgres -- /usr/pgsql-{version}/bin/psql -X -w "
               f"-p {self._pg_port()} -At -d postgres -c "
               "\"SELECT name FROM pg_settings WHERE pending_restart ORDER BY name;\"")
        out = (self._checked(host, runner, cmd, "pending-restart") or "").strip()
        if not out or out == "[DRY-RUN SUCCESS]": return []
        return [v.strip() for line in out.splitlines() for v in line.split(',') if v.strip()]

    def wait_extensions(self, host, runner):
        names=self.config.get('effective_config',{}).get('extensions',[])
        if not names: return
        version=str(self.config['cluster'].get('version','16'))
        names_sql=','.join("'"+self._esc(v)+"'" for v in names)
        sql="SELECT NOT EXISTS (SELECT 1 FROM unnest(ARRAY["+names_sql+"]) AS required(name) WHERE NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname=required.name));"
        cmd=(f"PGCONNECT_TIMEOUT=3 runuser -u postgres -- /usr/pgsql-{version}/bin/psql -X -w "
             f"-p {self._pg_port()} -At -d postgres -c {shlex.quote(sql)}")
        for attempt in range(30):
            value=self._checked(host,runner,cmd,'db-extensions').strip()
            if value in ('t','[DRY-RUN SUCCESS]'): return
            if attempt<29: time.sleep(1)
        raise RuntimeError('Required PostgreSQL extensions are not ready on '+host)

    def rolling_restart(self, hosts: List[str], runner: Any) -> None:
        leaders, replicas = [], []
        for host in hosts:
            (leaders if self.is_leader(host, runner) else replicas).append(host)
        for host in replicas + leaders:
            role = "primary" if host in leaders else "replica"
            print(f"[rolling-restart] restarting PostgreSQL on {host} ({role})")
            cmd = ("curl -fsS --connect-timeout 3 --max-time 10 -X POST -H 'Content-Type: application/json' "
                   f"-d '{{}}' {self._rest_url('/restart')} >/dev/null || systemctl restart patroni")
            self._checked(host, runner, cmd, "rolling-restart")
            if runner.dry_run: continue
            deadline = time.monotonic() + 120
            while True:
                # /health returns only after the node is streaming again, so the next
                # replica (and the primary) is not restarted into a quorum gap.
                self.wait_database(host, runner, timeout=120)
                if not self.check_pending_restart(host, runner): break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"PostgreSQL still pending restart on {host}")
                time.sleep(1)

    def sync_pgbouncer_userlist(self, host: str, runner: Any) -> int:
        eff = self.config.get("effective_config", {}) or {}
        names = ["postgres"] + [u["name"] for u in eff.get("users", []) if u.get("pgbouncer") and u.get("name")]
        names_sql = ",".join(f"'{self._esc(n)}'" for n in dict.fromkeys(names))
        sql = "SELECT chr(34) || usename || chr(34) || ' ' || chr(34) || passwd || chr(34) FROM pg_shadow WHERE usename IN (" + names_sql + ");"
        version = str(self.config["cluster"].get("version", "16"))
        cmd = ("set -e; umask 077; FILE=$(mktemp /etc/pgbouncer/userlist.XXXXXX); "
               "trap 'rm -f \"$FILE\"' EXIT; "
               f"runuser -u postgres -- /usr/pgsql-{version}/bin/psql -X -w -p {self._pg_port()} -At -d postgres "
               f"-c {shlex.quote(sql)} > \"$FILE\"; "
               "chown pgbouncer:pgbouncer \"$FILE\"; chmod 600 \"$FILE\"; "
               "mv \"$FILE\" /etc/pgbouncer/userlist.txt; "
               "systemctl reload pgbouncer || systemctl restart pgbouncer")
        self._checked(host, runner, cmd, "pgbouncer-userlist")
        return 0
