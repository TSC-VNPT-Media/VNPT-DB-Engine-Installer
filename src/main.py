#!/usr/bin/env python3
import sys
import os
import re
import json
import argparse
import getpass
import random
import shutil
import shlex
import socket
import time
import subprocess
from datetime import datetime

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from core.inventory import InventoryManager
from core.engine import DAGEngine
from core.ssh_runner import SSHRunner
from core.state_enforcer import StateEnforcer
from core.cleanup import interactive_cleanup, interactive_cluster_cleanup
from core.pmm import secret_value, validate_live_pmm
from core.platform_check import (
    firewall_open_command, minimum_requirements, missing_firewall_ports, parse_probe_meta,
    refusal_message, required_firewall_ports, requirement_failures, vrrp_allowed,
)
from core.execution import selection, execution_batches, is_full_scope
from core.package_bridge import PackageBridge, bridge_port, extra_firewall_ports, proxy_env

# --------------------------------------------------------------------------
# Branding / version (in o dau moi lenh va ket thuc apply)
# --------------------------------------------------------------------------
PROJECT_NAME = "VNPT DB Engine"
TOOL_NAME = "vde-install"
TOOL_VERSION = "dev-0.0.1"
LOGO = r"""        #######    ####
    ###############  ##
  ###  ################
 #      ##############
       ##############
      #############  #
     ############  ###
   ############  ####
   ########## ####
   ######"""

_BANNER_TS = None  # moc bat dau de tinh elapsed o footer


def _print_banner(subtitle, args=None, cfg=None):
    """Banner logo + thong tin phien chay. Khong bao gio in mat khau/secret."""
    global _BANNER_TS
    _BANNER_TS = time.monotonic()
    print("=" * 64)
    print(LOGO)
    print("-" * 64)
    print(f"  {PROJECT_NAME}  |  {TOOL_NAME} {TOOL_VERSION}")
    print("  PostgreSQL Lifecycle Engine - Patroni + etcd (VNPT DB Engine RPM)")
    print(f"  Command : {subtitle}")
    if args is not None and getattr(args, "config", None):
        print(f"  Config  : {args.config}")
    now = datetime.now().astimezone()
    print(f"  Started : {now.strftime('%Y-%m-%d %H:%M:%S %z')}   "
          f"Operator: {getpass.getuser()}@{socket.gethostname()}")
    if cfg is not None:
        c = cfg["cluster"]
        hosts = cfg["topology"]["hosts"]
        names = ", ".join(h.get("name", "?") for h in hosts)
        print(f"  Cluster : {c['name']}  profile={c['profile']}  PG={c.get('version', '16')}  "
              f"hosts={len(hosts)} ({names})")
    if args is not None:
        run_bits = []
        if hasattr(args, "dry_run"):
            mode = "READ-ONLY" if getattr(args, "check", False) else ("DRY-RUN" if args.dry_run else "LIVE")
            run_bits.append(f"mode={mode}")
        if hasattr(args, "force"):
            run_bits.append(f"force={bool(args.force)}")
        if getattr(args, "tags", None):
            run_bits.append(f"tags={args.tags}")
        if getattr(args, "limit", None):
            run_bits.append(f"limit={args.limit}")
        if getattr(args, "restart", None):
            run_bits.append("restart=True")
        if run_bits:
            print(f"  Run     : {'  '.join(run_bits)}")
    print("=" * 64)


def _print_footer(title):
    elapsed = ""
    if _BANNER_TS is not None:
        secs = int(time.monotonic() - _BANNER_TS)
        elapsed = f"  Elapsed: {secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"  Elapsed: {secs}s"
    print("\n" + "=" * 64)
    print(f"  {PROJECT_NAME}  |  {title}")
    print(f"  {TOOL_NAME} {TOOL_VERSION}   Finished: "
          f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}{elapsed}")
    print("=" * 64)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _runner(args, cfg=None):
    # SSH connection may be declared in the SSOT (ssh: user/port/use_sudo);
    # explicit CLI flags always win over the file.
    ssh = ((cfg or {}).get("ssh") or {})
    user = getattr(args, "user", None) or ssh.get("user") or "root"
    port = getattr(args, "port", None)
    port = int(port if port is not None else ssh.get("port", 22))
    use_sudo = getattr(args, "sudo", False) or bool(ssh.get("use_sudo", False))
    local_hosts = None
    if cfg is not None:
        local_hosts = [_execution_target(h) for h in cfg["topology"]["hosts"] if h["local"]]
    return SSHRunner(user=user, port=port,
                     key_file=getattr(args, "key", None) or ssh.get("key_file"), use_sudo=use_sudo,
                     dry_run=getattr(args, "dry_run", False), timeout=getattr(args, "timeout", 3600),
                     local_hosts=local_hosts)


def _parse_csv(v):
    if not v:
        return None
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _execution_target(h):
    """Local node is identified by inventory IP; SSH aliases only apply remotely."""
    return h["ip"] if h.get("local") else h.get("ssh_host") or h["ip"]


def _cluster_ip(h):
    """IP cluster-facing (Patroni connect_address, etcd advertise, HAProxy
    backend, replication): la IP interface THAT cua node. Khai bao bang
    node_ip; neu khong, preflight tu phat hien ben trong node."""
    return h.get("node_ip") or h["ip"]


def _select_hosts(cfg, limit=None):
    hosts = cfg["topology"]["hosts"]
    if limit:
        wanted = set(_parse_csv(limit))
        hosts = [h for h in hosts
                 if wanted & {h["ip"], h.get("name"), h.get("ssh_host"), h.get("node_ip")}]
        if not hosts:
            raise ValueError(f"--limit {limit} matched no hosts")
    return hosts


def _hosts_for_component(cfg, comp, hosts):
    roles = comp.get("target_roles") or ["all"]
    if "all" in roles:
        return list(hosts)
    return [h for h in hosts if set(roles) & set(h.get("roles", []))]


_HARDWARE_PROBE_CMD = (
    "free -m | awk '/^Mem:/{print $2}'; nproc; "
    "ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}'; "
    "awk -F= '/^(ID|VERSION_ID|PRETTY_NAME)=/{v=$0; sub(/^[^=]*=/,\"\",v); gsub(/\"/,\"\",v); print \"OS_\" $1 \"=\" v}' /etc/os-release 2>/dev/null || true; "
    "awk '/^model name/{sub(/^model name[ \\t]*:[ \\t]*/,\"\"); print \"CPU_MODEL=\" $0; exit}' /proc/cpuinfo 2>/dev/null || true; "
    "awk '/^flags/{sub(/^flags[ \\t]*:[ \\t]*/,\"\"); print \"CPU_FLAGS=\" $0; exit}' /proc/cpuinfo 2>/dev/null || true; "
    "echo ARCH=$(uname -m 2>/dev/null || echo unknown); "
    "ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i==\"src\") print $(i+1)}'; "
    "echo EPOCH=$(date +%s); "
    "fw=$(systemctl is-active firewalld 2>/dev/null || true); echo FIREWALLD=${fw:-unknown}; "
    "if [ \"$fw\" = active ]; then "
    "ports=$(firewall-cmd --list-ports 2>/dev/null | tr '\\n' ' '); "
    "rich=$(firewall-cmd --list-rich-rules 2>/dev/null | tr '\\n' ' '); "
    "svcs=$(firewall-cmd --list-services 2>/dev/null | tr '\\n' ' '); "
    "protos=$(firewall-cmd --list-protocols 2>/dev/null | tr '\\n' ' '); "
    "zones=$(firewall-cmd --get-active-zones 2>/dev/null | awk 'NF==1 {print $1}' | tr '\\n' ' '); "
    "for z in $zones; do "
    "ports=\"$ports $(firewall-cmd --zone=$z --list-ports 2>/dev/null | tr '\\n' ' ')\"; "
    "rich=\"$rich $(firewall-cmd --zone=$z --list-rich-rules 2>/dev/null | tr '\\n' ' ')\"; "
    "svcs=\"$svcs $(firewall-cmd --zone=$z --list-services 2>/dev/null | tr '\\n' ' ')\"; "
    "protos=\"$protos $(firewall-cmd --zone=$z --list-protocols 2>/dev/null | tr '\\n' ' ')\"; "
    "done; echo \"FW_PORTS=$ports\"; echo \"FW_RICH=$rich\"; echo \"FW_ZONES=$zones\"; "
    "echo \"FW_SERVICES=$svcs\"; echo \"FW_PROTOCOLS=$protos\"; fi"
)
_PROBE_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?$")


def _parse_probe_output(out):
    mem_ram = None; digits = []; ifaces = []; route_src = None; meta = {}
    for line in (out or "").splitlines():
        s = line.strip()
        labeled = parse_probe_meta(s)
        if labeled:
            meta[labeled[0]] = labeled[1]
            continue
        if s.startswith("Mem:"):
            parts = s.split()
            if len(parts) >= 2 and parts[1].isdigit():
                mem_ram = int(parts[1])
        elif s.isdigit():
            digits.append(int(s))
        elif _PROBE_IP_RE.match(s):
            if "/" in s:
                ifaces.append(s.split("/")[0])
            elif route_src is None:
                route_src = s
    if mem_ram is not None:
        ram_mb = mem_ram; vcpu = digits[0] if digits else None
    else:
        ram_mb = digits[0] if len(digits) >= 1 else None
        vcpu = digits[1] if len(digits) >= 2 else None
    snapshot = {
        "ram_mb": ram_mb, "vcpu": vcpu,
        "os_id": meta.get("OS_ID", ""), "os_version_id": meta.get("OS_VERSION_ID", ""),
        "os_pretty": meta.get("OS_PRETTY_NAME", ""), "arch": meta.get("ARCH", ""),
        "cpu_model": meta.get("CPU_MODEL", ""), "cpu_flags": meta.get("CPU_FLAGS", ""),
        "epoch": int(meta["EPOCH"]) if str(meta.get("EPOCH", "")).isdigit() else None,
        "firewalld": meta.get("FIREWALLD", ""),
        "fw_ports": meta.get("FW_PORTS", ""),
        "fw_rich": meta.get("FW_RICH", ""),
        "fw_services": meta.get("FW_SERVICES", ""),
        "fw_protocols": meta.get("FW_PROTOCOLS", ""),
    }
    return snapshot, ifaces, route_src


def _require_ssh_client(cfg):
    """Remote nodes are reached with the system ssh binary, not a Python library."""
    if any(not h.get("local") for h in cfg["topology"]["hosts"]) and shutil.which("ssh") is None:
        raise RuntimeError(
            "Remote nodes require the OpenSSH client (ssh) on the node running this tool. "
            "Install openssh-clients and retry.")


def _clock_skew_seconds(samples):
    """samples: (label, remote_epoch - local_midpoint). Difference is inter-node skew."""
    if len(samples) < 2:
        return 0.0
    offsets = [offset for _label, offset in samples]
    return max(offsets) - min(offsets)


def _gap_token(item):
    return "vrrp" if item == "vrrp" else f"{item}/tcp"


def _firewall_block_message(gaps):
    lines = ["firewalld is active and is missing cluster ports. The installer does not change the firewall."]
    for host, ports in gaps:
        label = f"{host.get('name', host['ip'])} ({host['ip']})"
        lines.append(f"  - {label}: missing {', '.join(_gap_token(port) for port in ports)}")
        lines.append(f"    {firewall_open_command(ports)}")
    lines.append("Open the ports, then rerun plan/apply.")
    return "\n".join(lines)


def _discover_hardware(cfg, runner, strict=False):
    """
    Read OS and hardware locally, then via SSH on remote nodes.
    Verify local inventory identity before contacting other nodes. Prefer the
    configured cluster IP on multi-NIC hosts rather than the default route.
    strict=True: a missing probe or a node below the OS/CPU/RAM floor stops
    the run before any component script. No fallback hardware values.
    """
    facts = {}
    missing = []
    unfit = []
    firewall_gaps = []
    clock_samples = []
    if runner.dry_run:
        return facts
    _require_ssh_client(cfg)
    pmm = (cfg.get("settings_normalized") or {}).get("pmm") or {}
    ha = cfg["cluster"]["profile"] == "ha"
    for h in sorted(cfg["topology"]["hosts"], key=lambda h: not h["local"]):
        target = _execution_target(h)
        started = time.time()
        rc, out = runner.run_command(target, _HARDWARE_PROBE_CMD, component_name="preflight")
        finished = time.time()
        snapshot, ifaces, route_src = _parse_probe_output(out)
        if snapshot.get("epoch") is not None:
            clock_samples.append((h.get("name") or target, snapshot["epoch"] - (started + finished) / 2))
        if strict and ha:
            needed = list(dict.fromkeys(
                required_firewall_ports(h, cfg["effective_config"]["ports"], cfg["settings_normalized"])
                + extra_firewall_ports(cfg, h)))
            closed = missing_firewall_ports(
                snapshot.get("firewalld"), snapshot.get("fw_ports"), snapshot.get("fw_rich"), needed)
            keepalived_on = (cfg.get("settings_normalized") or {}).get("keepalived", {}).get("enabled")
            if keepalived_on and "balancer" in (h.get("roles") or []) and not vrrp_allowed(
                    snapshot.get("firewalld"), snapshot.get("fw_services"), snapshot.get("fw_protocols")):
                closed.append("vrrp")
            if closed:
                firewall_gaps.append((h, closed))
                print(f"[!] [preflight] {target}: firewalld is missing {', '.join(_gap_token(p) for p in closed)}")
        ram_mb = snapshot["ram_mb"]
        vcpu = snapshot["vcpu"]

        explicit_ip = h.get("node_ip")
        if h["local"]:
            expected_ip = explicit_ip or h["ip"]
            if expected_ip not in ("127.0.0.1", "localhost") and expected_ip not in ifaces:
                raise RuntimeError(
                    f"Local host {h['name']} expects {expected_ip}, but this machine has "
                    f"{', '.join(ifaces) or 'no discovered IPv4 interfaces'}. "
                    "Run the tool on that node or correct local: true / node_ip in the inventory.")
        node_ip = explicit_ip or (h["ip"] if h["ip"] in ifaces else None) or route_src or (ifaces[0] if ifaces else None)
        ok = (rc == 0 and ram_mb and vcpu and node_ip)
        if explicit_ip and ifaces and explicit_ip not in ifaces:
            print(f"[!] [preflight] {target}: node_ip khai bao {explicit_ip} KHONG nam tren interface nao "
                  f"cua node (interfaces: {', '.join(ifaces) or '?'}). Patroni/etcd se khong bind duoc IP nay.")
            ok = False
        if ok:
            reasons = requirement_failures(snapshot, pmm)
            if reasons:
                label = f"{h.get('name', target)} ({h['ip']})"
                unfit.append((label, reasons))
                print(f"[!] [preflight] {target}: không đạt cấu hình tối thiểu")
                for reason in reasons:
                    print(f"    - {reason}")
            else:
                facts[h["ip"]] = {**snapshot, "node_ip": node_ip, "interfaces": ifaces, "ssh_host": target}
                note = " [LOCAL]" if h["local"] else " [SSH]"
                if node_ip != h["ip"]:
                    note += f" [cluster IP={node_ip}]"
                pretty = snapshot["os_pretty"] or f"{snapshot['os_id']} {snapshot['os_version_id']}"
                print(f"[*] [preflight] {target}: OS={pretty} arch={snapshot['arch']} "
                      f"CPU={snapshot['cpu_model']} RAM={ram_mb}MB vCPU={vcpu} node_ip={node_ip}{note}")
        else:
            missing.append(target)
            print(f"[!] [preflight] {target}: KHONG doc duoc RAM/vCPU/node_ip (rc={rc})")
    skew = _clock_skew_seconds(clock_samples) if strict and ha else 0.0
    if strict and ha and skew > 2:
        level = "[!]" if skew > 10 else "[WARN]"
        print(f"{level} [preflight] Clock skew between nodes is {skew:.1f}s. "
              "etcd leases and Patroni failover need chronyd; >10s stops the run.")
    if strict and (unfit or missing or firewall_gaps or skew > 10):
        parts = []
        if unfit:
            parts.append(refusal_message(unfit))
        if missing:
            parts.append(
                f"Cannot discover RAM/vCPU/IP on: {', '.join(missing)}. No fallback values will be applied. "
                "Check free/nproc/ip and local permissions; for remote nodes check SSH user/port/key, "
                "BatchMode login, sudo and firewall.")
        if firewall_gaps:
            parts.append(_firewall_block_message(firewall_gaps))
        if skew > 10:
            parts.append(f"Clock skew of {skew:.1f}s exceeds 10s. Synchronize time before installing HA.")
        raise RuntimeError("\n".join(parts))
    return facts


def _keepalived_balancers(cfg):
    return [host for host in cfg["topology"]["hosts"] if "balancer" in (host.get("roles") or [])]


def _keepalived_priority(cfg, host):
    configured = (cfg["settings_normalized"].get("keepalived") or {}).get("priorities") or {}
    for key in (host.get("name"), host.get("ip"), host.get("node_ip"), host.get("ssh_host")):
        if key and key in configured:
            return int(configured[key])
    balancers = _keepalived_balancers(cfg)
    index = next(i for i, item in enumerate(balancers) if item["ip"] == host["ip"])
    return max(50, 150 - index * 10)


def _keepalived_state(cfg, host):
    balancers = _keepalived_balancers(cfg)
    ranked = sorted(balancers, key=lambda item: (-_keepalived_priority(cfg, item), balancers.index(item)))
    return "MASTER" if ranked and ranked[0]["ip"] == host["ip"] else "BACKUP"


def _keepalived_env(cfg, host):
    ka = cfg["settings_normalized"].get("keepalived") or {}
    blank = {
        "VDE_KEEPALIVED_VIP": "", "VDE_KEEPALIVED_PREFIX": "", "VDE_KEEPALIVED_ROUTER_ID": "",
        "VDE_KEEPALIVED_AUTH_PASS": "", "VDE_KEEPALIVED_INTERFACE": "", "VDE_KEEPALIVED_PRIORITY": "",
        "VDE_KEEPALIVED_STATE": "", "VDE_KEEPALIVED_PEERS": "",
    }
    if not ka.get("enabled") or "balancer" not in (host.get("roles") or []):
        return blank
    peers = [_cluster_ip(item) for item in _keepalived_balancers(cfg) if item["ip"] != host["ip"]]
    return {
        "VDE_KEEPALIVED_VIP": ka.get("address", ""),
        "VDE_KEEPALIVED_PREFIX": ka.get("prefix") or "",
        "VDE_KEEPALIVED_ROUTER_ID": str(ka.get("virtual_router_id", 51)),
        "VDE_KEEPALIVED_AUTH_PASS": ka.get("auth_pass", ""),
        "VDE_KEEPALIVED_INTERFACE": ka.get("interface") or "",
        "VDE_KEEPALIVED_PRIORITY": str(_keepalived_priority(cfg, host)),
        "VDE_KEEPALIVED_STATE": _keepalived_state(cfg, host),
        "VDE_KEEPALIVED_PEERS": ",".join(peers),
    }


def _build_host_env(cfg, inv, host_obj, force=False):
    ns = cfg["settings_normalized"]
    eff = cfg["effective_config"]
    ports = eff["ports"]
    storage = ns["storage"]
    hosts = cfg["topology"]["hosts"]
    db_hosts = [h for h in hosts if "db" in h.get("roles", [])]
    etcd_hosts = [h for h in hosts if "etcd" in h.get("roles", [])]
    etcd_name, initial = "", []
    for i, eh in enumerate(etcd_hosts, 1):
        nm = f"etcd{i}"
        initial.append(f"{nm}=http://{_cluster_ip(eh)}:{ports['etcd_peer']}")
        if eh["ip"] == host_obj["ip"]:
            etcd_name = nm
    guc = inv.host_effective_guc(host_obj) if hasattr(inv, "host_effective_guc") else eff["guc"]
    patroni_guc_yaml = "\n".join(
        f"        {k}: {json.dumps(v) if isinstance(v, bool) else v}" for k, v in guc.items())
    enforcer = StateEnforcer(cfg)
    pgb, pat, br, pmm = ns["pgbouncer"], ns["patroni"], ns["pgbackrest"], ns["pmm"]
    profile = cfg["cluster"]["profile"]
    sync_mode = bool(pat.get("synchronous_mode", profile == "ha")) and profile == "ha" and len(db_hosts) > 1
    return {
        "VDE_CLUSTER_NAME": cfg["cluster"]["name"], "VDE_PROFILE": profile,
        "VDE_PG_VERSION": str(cfg["cluster"].get("version", "16")),
        "VDE_PACKAGE_SOURCE": cfg["cluster"].get("package_source", "vnpt"),
        "VDE_REPO_URL": cfg["cluster"].get("repo_url", ""),
        "VDE_GPG_KEY_URL": cfg["cluster"].get("gpg_key_url", ""),
        "VDE_RELEASE_RPM_URL": cfg["cluster"].get("release_rpm_url", "http://dbengine-repo.ttpmhcm.com/vnpt-db-engine-release.rpm"),
        "VDE_FORCE_UPDATE": "1" if force else "0",
        "VDE_SAFEGUARD": "1" if cfg["cluster"].get("safeguard", True) else "0",
        "VDE_NODE_IP": _cluster_ip(host_obj), "VDE_NODE_NAME": host_obj.get("name", ""),
        "VDE_SSH_TARGET": _execution_target(host_obj),
        "VDE_DB_NODES": ",".join(_cluster_ip(h) for h in db_hosts),
        "VDE_ETCD_NAME": etcd_name or "etcd1",
        "VDE_ETCD_HOSTS": ",".join(f"{_cluster_ip(h)}:{ports['etcd_client']}" for h in etcd_hosts),
        "VDE_ETCD_INITIAL_CLUSTER": ",".join(initial),
        "VDE_ETCD_TOKEN": (ns["etcd"].get("token") or "vde-etcd-cluster"),
        "VDE_ETCD_CLIENT_PORT": str(ports["etcd_client"]),
        "VDE_ETCD_PEER_PORT": str(ports["etcd_peer"]),
        "VDE_PATRONI_REST_PORT": str(ports["patroni_rest"]),
        "VDE_PGDATA": storage["data_dir"], "VDE_PGWAL": storage["wal_dir"],
        "VDE_BACKUP_DIR": storage["backup_dir"],
        "VDE_PG_PASS": ns["auth"]["postgres_password"], "VDE_REPL_PASS": ns["auth"]["replicator_password"],
        "VDE_SHARED_BUFFERS": str(guc.get("shared_buffers", "4096MB")),
        "VDE_MAX_CONNECTIONS": str(guc.get("max_connections", 500)),
        "VDE_WORK_MEM": str(guc.get("work_mem", "32MB")),
        "VDE_EFFECTIVE_CACHE_SIZE": str(guc.get("effective_cache_size", "12288MB")),
        "VDE_MAINTENANCE_WORK_MEM": str(guc.get("maintenance_work_mem", "1024MB")),
        "VDE_PATRONI_GUC_YAML": patroni_guc_yaml,
        "VDE_GUC_CONF_LINES": "\n".join(f"{k} = {v}" for k, v in guc.items()),
        "VDE_SYSCTL_CONF_LINES": "\n".join(f"{k} = {v}" for k, v in eff["sysctl"].items()),
        "VDE_HBA_LINES": "\n".join(eff.get("hba_rules") or []),
        "VDE_PATRONI_SYNC_MODE": "true" if sync_mode else "false",
        "VDE_PATRONI_SYNC_STRICT": "true" if (sync_mode and pat.get("synchronous_mode_strict")) else "false",
        "VDE_PATRONI_TTL": str(pat.get("ttl", 30)), "VDE_PATRONI_LOOP_WAIT": str(pat.get("loop_wait", 10)),
        "VDE_PATRONI_RETRY_TIMEOUT": str(pat.get("retry_timeout", 10)),
        "VDE_DB_OBJECTS_SQL": enforcer.build_objects_sql(),
        "VDE_DB_EXTENSIONS_SQL": enforcer.build_extension_sql(),
        "VDE_DB_DATABASES": ",".join(d["name"] for d in (eff.get("databases") or []) if d.get("name")),
        "VDE_PG_PORT": str(ports["postgresql"]), "VDE_PGBOUNCER_PORT": str(ports["pgbouncer"]),
        "VDE_HAPROXY_BACKEND_PORT": str(ports["pgbouncer"] if pgb.get("enabled", True) else ports["postgresql"]),
        "VDE_HAPROXY_WRITE_PORT": str(ports["haproxy_write"]),
        "VDE_HAPROXY_READ_PORT": str(ports["haproxy_read"]),
        "VDE_HAPROXY_STATS_PORT": str(ports["haproxy_stats"]),
        **_keepalived_env(cfg, host_obj),
        "VDE_PGBOUNCER_POOL_MODE": pgb.get("pool_mode", "transaction"),
        "VDE_PGBOUNCER_MAX_CLIENT_CONN": str(pgb.get("max_client_conn", 1000)),
        "VDE_PGBOUNCER_DEFAULT_POOL_SIZE": str(pgb.get("default_pool_size", 50)),
        "VDE_PGBOUNCER_MAX_PREPARED": str(pgb.get("max_prepared_statements", 200)),
        "VDE_BACKUP_DEST": br.get("destination", "local"), "VDE_BACKUP_STANZA": (br.get("stanza") or "vde_cluster"),
        "VDE_BACKUP_RETENTION_FULL": str(br.get("retention_full", 2)),
        "VDE_BACKUP_RETENTION_DIFF": str(br.get("retention_diff", 7)),
        "VDE_BACKUP_S3": br.get("s3_endpoint", ""), "VDE_BACKUP_S3_BUCKET": br.get("s3_bucket", ""),
        "VDE_BACKUP_S3_KEY": br.get("s3_key", ""), "VDE_BACKUP_S3_SECRET": br.get("s3_key_secret", ""),
        "VDE_BACKUP_S3_REGION": br.get("s3_region") or "us-east-1",
        "VDE_BACKUP_S3_URI_STYLE": br.get("s3_uri_style") or "path",
        "VDE_BACKUP_S3_VERIFY_TLS": "n" if br.get("s3_verify_tls") is False or str(br.get("s3_verify_tls", "y")).lower() in ("0", "false", "n", "no") else "y",
        "VDE_PMM_ENABLED": "1" if pmm["enabled"] else "0",
        "VDE_PMM_CONFIG": json.dumps(pmm),
        "VDE_PMM_PASSWORD": secret_value(pmm),
        "VDE_PMM_DB_PASSWORD": (
            os.environ.get(pmm["database_password_env"], "")
            if pmm["database_password_env"]
            else (ns["auth"]["monitor_password"] if pmm.get("database_user") == "monitor" else ns["auth"]["postgres_password"])
        ),
        **proxy_env(cfg, host_obj),
    }


def _order_patroni_hosts(cfg, comp_hosts, runner):
    """Existing-data / leader hosts first so scale-out never restarts Node 1."""
    if runner.dry_run or len(comp_hosts) <= 1:
        return comp_hosts
    data_dir = cfg["settings_normalized"]["storage"]["data_dir"]
    existing, fresh = [], []
    for h in comp_hosts:
        rc, _ = runner.run_command(_execution_target(h), f"test -f {data_dir}/PG_VERSION", component_name="patroni-order")
        (existing if rc == 0 else fresh).append(h)
    return existing + fresh


def _patroni_expectations(cfg, ordered_hosts, runner):
    """Existing data nodes, and the first fresh node, must reach primary or health.
    Later fresh nodes are replicas and only need a live Patroni state."""
    if not ordered_hosts:
        return {}
    if runner.dry_run:
        return {h["ip"]: ("1" if i == 0 else "0") for i, h in enumerate(ordered_hosts)}
    data_dir = cfg["settings_normalized"]["storage"]["data_dir"]
    existing = set()
    for host in ordered_hosts:
        rc, _ = runner.run_command(
            _execution_target(host), f"test -f {data_dir}/PG_VERSION", component_name="patroni-order")
        if rc == 0:
            existing.add(host["ip"])
    expectations = {}
    named_bootstrap = False
    for host in ordered_hosts:
        if host["ip"] in existing or (not existing and not named_bootstrap):
            expectations[host["ip"]] = "1"
            named_bootstrap = True
        else:
            expectations[host["ip"]] = "0"
    return expectations


def _order_primary_first(cfg, comp_hosts, runner):
    """Run backup setup on the current primary before replicas."""
    if runner.dry_run or len(comp_hosts) <= 1:
        return comp_hosts
    rest = cfg["effective_config"]["ports"]["patroni_rest"]
    primary, replicas = [], []
    for host in comp_hosts:
        rc, _ = runner.run_command(
            _execution_target(host),
            f"curl -sf http://127.0.0.1:{rest}/primary >/dev/null 2>&1",
            component_name="pgbackrest-order")
        (primary if rc == 0 else replicas).append(host)
    return primary + replicas


def _prepare_database(cfg, enforcer, runner, hosts, allow_restart):
    targets = [_execution_target(h) for h in hosts if "db" in h.get("roles", [])]
    profile = cfg["cluster"]["profile"]
    # A new HA replica may still be cloning when db_ready starts. Standalone stays on the short gate.
    timeout = 300 if profile == "ha" and len(targets) > 1 else 30
    pending = []
    for target in targets:
        # Partial monitoring/config runs require an existing DB. Do not bootstrap
        # unselected dependencies or start containers before this check succeeds.
        enforcer.wait_database(target, runner, timeout=timeout)
    if len(targets) <= 1:
        for target in targets:
            result = enforcer.reconcile_live_config(target, runner, allow_restart=allow_restart)
            if result["pending_restart"]:
                pending.append(target)
    else:
        leader = next((target for target in targets if enforcer.is_leader(target, runner)), targets[0])
        print(f"[db-ready] Patching Patroni DCS once via {leader}")
        enforcer.patch_dcs(leader, runner)
        for target in targets:
            if enforcer.reload_and_check(target, runner):
                pending.append(target)
    if pending:
        if not allow_restart:
            raise RuntimeError("Database config requires restart on " + ", ".join(pending) + "; rerun with --restart")
        enforcer.rolling_restart(pending, runner)
    pat = cfg["settings_normalized"]["patroni"]
    if pat.get("synchronous_mode_strict") and profile == "ha" and len(targets) > 1:
        print("[db-ready] synchronous_mode_strict: waiting until every DB node is healthy before DDL")
        for target in targets:
            enforcer.wait_database(target, runner, timeout=timeout)
    objects_synced = False
    for target in targets:
        enforcer.wait_database(target, runner, timeout=timeout)
        if enforcer.check_pending_restart(target, runner):
            raise RuntimeError("Database is still pending restart on " + target)
        objects_synced = enforcer.sync_database_objects(target, runner) or objects_synced
        enforcer.wait_database(target, runner, timeout=timeout)
    if targets and not objects_synced:
        raise RuntimeError("No writable primary among selected DB hosts; include the primary for config/extension sync")
    # DDL must have reached every selected replica before exporters are started.
    for target in targets:
        enforcer.wait_extensions(target, runner)


def _persist_component_baselines(cfg, inv, enforcer, runner, hosts, completed):
    """Merge hashes of components that finished on each host into baseline.json."""
    for host in hosts:
        target = _execution_target(host)
        desired = enforcer.component_hashes(host, _build_host_env(cfg, inv, host))
        recorded = enforcer.recorded_hashes(target, runner, desired)
        done = []
        for name in completed.get(target, []):
            if name in desired:
                recorded[name] = desired[name]
                done.append(name)
        recorded = {k: recorded[k] for k in desired if k in recorded}
        print(f">>> [SSOT] {target}: recording hashes for {', '.join(done) or '(no newly completed components)'}")
        enforcer.enforce_baseline(target, runner, recorded)


def _repo_probe_url(cfg):
    source = (cfg["cluster"].get("package_source") or "vnpt").lower()
    if source == "percona":
        return "https://repo.percona.com/yum/percona-release-latest.noarch.rpm"
    return cfg["cluster"].get("release_rpm_url") or "http://dbengine-repo.ttpmhcm.com/vnpt-db-engine-release.rpm"


def _bridge_allowed_ips(cfg):
    allowed = set()
    for host in cfg["topology"]["hosts"]:
        allowed.add(host["ip"])
        if host.get("node_ip"):
            allowed.add(host["node_ip"])
        ssh_host = host.get("ssh_host")
        if ssh_host and _PROBE_IP_RE.match(str(ssh_host).split("/")[0]):
            allowed.add(str(ssh_host))
    return allowed


def _start_package_bridge(cfg, runner, hosts):
    """Listen on the install node and require every selected remote to reach it."""
    if runner.dry_run or not cfg["cluster"].get("package_bridge"):
        return None
    remotes = [h for h in hosts if not h.get("local")]
    if not remotes:
        return None
    local = next(h for h in cfg["topology"]["hosts"] if h.get("local"))
    listen = _cluster_ip(local)
    port = bridge_port(cfg["cluster"])
    url = _repo_probe_url(cfg)
    probe = f"curl -fsS -o /dev/null --max-time 20 --range 0-0 {shlex.quote(url)}"
    rc, _ = runner.run_command(_execution_target(local), probe, component_name="package-bridge")
    if rc != 0:
        raise RuntimeError(
            f"package_bridge needs the install node {listen} to reach {url}. "
            "That node is the one expected to have a route to the repository.")
    bridge = PackageBridge(listen, port, _bridge_allowed_ips(cfg))
    try:
        bridge.start()
    except OSError as exc:
        raise RuntimeError(f"Cannot listen for package_bridge on {listen}:{port}: {exc}") from exc
    blocked = []
    try:
        check = f"timeout 5 bash -c 'echo >/dev/tcp/{listen}/{port}'"
        for host in remotes:
            rc, _ = runner.run_command(_execution_target(host), check, component_name="package-bridge")
            if rc != 0:
                blocked.append(host.get("name") or host["ip"])
        if blocked:
            raise RuntimeError(
                "package_bridge is listening on {listen}:{port}, but these nodes cannot open it: {names}. "
                "The installer does not change the firewall. On the install node run: {cmd}".format(
                    listen=listen, port=port, names=", ".join(blocked),
                    cmd=firewall_open_command([port])))
    except Exception:
        bridge.stop()
        raise
    names = ", ".join(h.get("name") or h["ip"] for h in remotes)
    print(f"[*] [package-bridge] {listen}:{port} forwarding dnf/curl for {names}")
    return bridge


def _run_pipeline(cfg, inv, runner, hosts, tags=None, force=False, allow_restart=False):
    components_dir = os.path.join(current_dir, "components")
    selected = selection(cfg, tags)
    batches = execution_batches(cfg, selected)
    full_scope = is_full_scope(cfg, hosts, tags)
    enforcer = StateEnforcer(cfg)
    completed = {_execution_target(h): [] for h in hosts}
    if not runner.dry_run:
        _require_ssh_client(cfg)
    bridge = None
    pipeline_error = None
    try:
        bridge = _start_package_bridge(cfg, runner, hosts)
        try:
            # For access/backup-only tags, validate existing DB availability without
            # changing its config or evaluating unrelated PMM credentials.
            if "db_ready" not in selected and selected & {"pgbouncer", "pgbackrest", "haproxy", "keepalived"}:
                for host in hosts:
                    if "db" in host.get("roles", []):
                        enforcer.wait_database(_execution_target(host), runner)

            for step_idx, batch in enumerate(batches, 1):
                print(f">>> [Stage {step_idx}/{len(batches)}] Components: {', '.join(c['name'] for c in batch)}")
                for comp in batch:
                    comp_hosts = _hosts_for_component(cfg, comp, hosts)
                    if comp["name"] == "db_ready":
                        _prepare_database(cfg, enforcer, runner, comp_hosts, allow_restart)
                        for host in comp_hosts:
                            completed[_execution_target(host)].append("db_ready")
                        continue
                    script = os.path.join(components_dir, comp["name"], "run.sh")
                    if not os.path.exists(script):
                        raise FileNotFoundError("Enabled component script missing: " + comp["name"])
                    expectations = {}
                    if comp["name"] == "etcd" and len(comp_hosts) > 1:
                        for phase in ("configure", "verify"):
                            print(f">>> [etcd] phase={phase} on {len(comp_hosts)} node(s)")
                            for host in comp_hosts:
                                target = _execution_target(host)
                                env = _build_host_env(cfg, inv, host, force=force)
                                env["VDE_ETCD_PHASE"] = phase
                                rc = runner.dispatch_script(target, script, env_vars=env, component_name=comp["name"])
                                if rc and not runner.dry_run:
                                    print(f"[FAILED] etcd {phase} failed on {host['ip']} rc={rc}")
                                    raise SystemExit(rc)
                                if phase == "verify":
                                    completed[target].append("etcd")
                        continue
                    if comp["name"] == "patroni":
                        comp_hosts = _order_patroni_hosts(cfg, comp_hosts, runner)
                        expectations = _patroni_expectations(cfg, comp_hosts, runner)
                    if comp["name"] == "pgbackrest":
                        comp_hosts = _order_primary_first(cfg, comp_hosts, runner)
                    for host in comp_hosts:
                        target = _execution_target(host)
                        env = _build_host_env(cfg, inv, host, force=force)
                        if comp["name"] == "etcd":
                            env["VDE_ETCD_PHASE"] = "all"
                        if comp["name"] == "patroni":
                            env["VDE_PATRONI_EXPECT_PRIMARY"] = expectations.get(host["ip"], "0")
                        rc = runner.dispatch_script(target, script, env_vars=env, component_name=comp["name"])
                        if rc and not runner.dry_run:
                            print(f"[FAILED] {comp['name']} failed on {host['ip']} rc={rc}")
                            raise SystemExit(rc)
                        if comp["name"] == "pgbouncer":
                            enforcer.sync_pgbouncer_userlist(target, runner)
                        completed[target].append(comp["name"])

            # Config-only operations can update an existing PgBouncer userlist after
            # SQL roles have been synchronized. They do not provision a missing pooler.
            if "db_ready" in selected and "pgbouncer" not in selected and cfg["settings_normalized"]["pgbouncer"].get("enabled", True):
                for host in hosts:
                    if "db" not in host.get("roles", []): continue
                    target = _execution_target(host)
                    rc, _ = runner.run_command(target, "systemctl is-active --quiet pgbouncer", component_name="pgbouncer-present")
                    if not rc: enforcer.sync_pgbouncer_userlist(target, runner)
        except BaseException as exc:
            pipeline_error = exc
        if not runner.dry_run:
            try:
                _persist_component_baselines(cfg, inv, enforcer, runner, hosts, completed)
            except BaseException as persist_error:
                if pipeline_error is None:
                    raise
                print(f"[!] [SSOT] Baseline update failed after a pipeline error: {persist_error}")
        if pipeline_error is not None:
            raise pipeline_error
        return {"scope": "full" if full_scope else "partial", "completed": completed,
                "baseline_written": not runner.dry_run}
    finally:
        if bridge is not None:
            bridge.stop()
            print(f"[*] [package-bridge] stopped {bridge.bind_ip}:{bridge.port}")


def _load_with_preflight(args):
    inv = InventoryManager(args.config)
    cfg = inv.load()
    runner = _runner(args, cfg)
    selected = selection(cfg, getattr(args, "tags", None))
    if getattr(args, "command", None) in ("apply", "enforce"):
        if getattr(args, "dry_run", False):
            print("[*] [preflight] dry-run: skip OS/hardware minimum checks (no target commands).")
        else:
            if os.geteuid() != 0 and not runner.use_sudo:
                raise RuntimeError("Local installation requires root or --sudo (passwordless sudo).")
            if selected & {"pmm", "pmm_server"}:
                validate_live_pmm(cfg["settings_normalized"]["pmm"], cfg["settings_normalized"]["auth"])
            # Bat buoc do duoc phan cung that tren tung host; loi la dung ngay,
            # tuyet doi khong fallback sang 16GB/4vCPU gia dinh de render config.
            facts = _discover_hardware(cfg, runner, strict=True)
            cfg = inv.apply_hardware_facts(facts)
    return inv, cfg, runner


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_validate(args):
    _print_banner("validate - Validate YAML manifest & topology", args)
    try:
        cfg = InventoryManager(args.config).load()
        c = cfg["cluster"]
        print(f"[+] Cluster : {c['name']}  profile={c['profile']}  PG={c.get('version','16')}  source={c.get('package_source')}")
        print(f"[+] Hosts   : {len(cfg['topology']['hosts'])}")
        for h in cfg["topology"]["hosts"]:
            print(f"    - {h.get('name')} {h['ip']} execution={'LOCAL' if h['local'] else 'SSH'} roles={','.join(h.get('roles', []))}")
        print(f"[+] DAG     : {' -> '.join(x['name'] for x in cfg['components'])}")
        print("\nManifest syntax & topology structure are VALID.")
    except Exception as e:
        print(f"[VALIDATE ERROR]: {e}"); sys.exit(1)


def cmd_plan(args):
    _print_banner("plan - Execution plan & DAG topology", args)
    try:
        inv = InventoryManager(args.config); cfg = inv.load()
        offline = getattr(args, "offline", False) or getattr(args, "dry_run", False)
        if not offline:
            # Plan reads local/remote hardware; discovery failure is fatal.
            # bao loi dung ngay, khong in so lieu fallback gay hieu nham.
            runner = _runner(args, cfg)
            facts = _discover_hardware(cfg, runner, strict=True)
            cfg = inv.apply_hardware_facts(facts)
        print(f"\nTarget Profile: {cfg['cluster']['profile'].upper()} (unified Patroni+etcd cohort)")
        print(DAGEngine(cfg["components"]).render_ascii_dag())
        eff, sd = cfg["effective_config"], cfg["smart_defaults"]
        if not offline and sd.get("memory_budget"):
            budget = sd["memory_budget"]
            print(f"RAM budget MiB: total={budget['total_ram_mb']} PMM={budget['pmm_reserved_mb']} OS={budget['os_reserved_mb']} DB={budget['database_ram_mb']}")
        if offline:
            print(f"\n=== SMART DEFAULTS [{sd.get('workload')}] === ")
            print("  ! Che do --offline: KHONG do phan cung, KHONG hien thi so lieu GUC tinh theo phan cung.")
            print("    Bo --offline/--dry-run de doc phan cung local va SSH cac node remote.")
        else:
            print(f"\n=== SMART DEFAULTS [{sd.get('workload')}] RAM={sd.get('ram_mb')}MB vCPU={sd.get('vcpu')} "
                  f"enabled={sd.get('enabled')} ===")
            print("  * Hardware source: local discovery + SSH for remote cluster nodes")
            for k in ("shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem",
                      "max_connections", "max_worker_processes", "max_parallel_workers", "wal_level"):
                print(f"  * {k:22}: {eff['guc'].get(k)}")
        if cfg.get("user_overrides", {}).get("guc"):
            print(f"  * user overrides      : {cfg['user_overrides']['guc']}")
        print(f"  * sysctl              : {eff['sysctl']}")
        print(f"  * ports               : {eff['ports']}")
        br = cfg["settings_normalized"]["pgbackrest"]
        if (cfg["cluster"]["profile"] == "ha" and br.get("enabled", True)
                and (br.get("destination") or "local") == "local"):
            print("[!] [plan] pgBackRest destination=local tren HA: moi node co kho rieng, "
                  "failover khong ke thua stanza. Khuyen nghi destination: s3.")
        print(f"  * users/databases     : {[u.get('name') for u in eff.get('users',[])]} / "
              f"{[d.get('name') for d in eff.get('databases',[])]}")
        enforcer = StateEnforcer(cfg)
        print(f"\n=== SSOT Desired Hash (SHA256): {enforcer.get_desired_hash()} ===")
        if not offline:
            print("=== Component hashes (schema 3, per host) ===")
            for h in cfg["topology"]["hosts"]:
                hashes = enforcer.component_hashes(h, _build_host_env(cfg, inv, h))
                print(f"  {h.get('name', h['ip'])}:")
                for name, digest in hashes.items():
                    print(f"    {name}: {digest}")
        print("Day-2 examples: apply -t guc | -t hba,users -l 10.10.20.11 | apply --force --restart")
    except Exception as e:
        print(f"[PLAN ERROR]: {e}"); sys.exit(1)


def cmd_apply(args):
    _print_banner("apply - Deploy components (DAG) / fast config-sync", args)
    try:
        inv, cfg, runner = _load_with_preflight(args)
        hosts = _select_hosts(cfg, getattr(args, "limit", None))
        c = cfg["cluster"]
        print(f"[*] Cluster: {c['name']}  profile={c['profile']}  PG={c.get('version', '16')}  "
              f"source={c.get('package_source', 'vnpt')}  hosts={len(cfg['topology']['hosts'])}")
        print(f"[*] Hosts: {', '.join(h['name'] + '(' + ('LOCAL' if h['local'] else 'SSH') + ')' for h in hosts)}  mode={'DRY-RUN' if args.dry_run else 'LIVE'}"
              f"  force={args.force} tags={args.tags or '-'} restart={args.restart}")
        result = _run_pipeline(cfg, inv, runner, hosts, tags=args.tags, force=args.force,
                               allow_restart=args.restart or args.force)
        if args.dry_run:
            label = "DRY-RUN COMPLETE (NO CHANGES)"
        else:
            names = sorted({name for comps in result["completed"].values() for name in comps})
            label = "APPLY COMPLETED & BASELINE UPDATED: " + (", ".join(names) if names else "(no components)")
        _print_footer(label)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[APPLY ERROR]: {e}"); sys.exit(1)


def _probe_local_for_init():
    """Best-effort read-only local probe for init sanity check (non-blocking)."""
    try:
        res = subprocess.run(
            ["/bin/bash", "-c", _HARDWARE_PROBE_CMD],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, check=False,
        )
        return res.returncode, res.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def _init_sanity_check(cfg, local_ip, offline=False):
    profile = cfg["cluster"]["profile"]
    pmm = cfg["settings_normalized"]["pmm"]
    req = minimum_requirements(pmm)
    print(f"[+] [sanity-check] YAML hop le: profile={profile}  DAG={len(cfg['components'])} components.")
    print(f"[*] [sanity-check] Yeu cau toi thieu ({profile}, pmm_local={pmm['enabled'] and pmm['mode'] == 'local'}): "
          f"OS={req['os']} | vCPU>={req['min_vcpu']} | RAM>={req['min_ram_mb']} MiB")
    if profile == "ha":
        print(f"[*] [sanity-check] HA: toi thieu {len(cfg['topology']['hosts'])} nodes da khai bao; can SSH + sudo toi cac node remote.")
    if offline:
        print("[*] [sanity-check] --offline: bo qua kiem tra phan cung local.")
        return
    rc, out = _probe_local_for_init()
    if rc != 0:
        print("[!] [sanity-check] Khong doc duoc thong so may local; se kiem tra lai khi chay plan/apply.")
        return
    snapshot, ifaces, _ = _parse_probe_output(out)
    if local_ip not in ("127.0.0.1", "localhost") and local_ip not in ifaces:
        print(f"[!] [sanity-check] May hien tai khong giu IP local '{local_ip}' (interfaces: {', '.join(ifaces) or '?'}); "
              "bo qua check phan cung local. Hay chay validate/plan tren node target sau khi chinh YAML.")
        return
    reasons = requirement_failures(snapshot, pmm)
    if reasons:
        print(f"[WARN] [sanity-check] May local chua dat cau hinh toi thieu cho profile '{profile}':")
        for r in reasons:
            print(f"    - {r}")
        if pmm.get("enabled") and pmm.get("mode") == "local" and any(r.startswith("RAM:") for r in reasons):
            print("    -> Goi y: Neu may test RAM < 6 GiB, hay dat settings.pmm.enabled: false trong YAML truoc khi apply.")
    else:
        pretty = snapshot["os_pretty"] or f"{snapshot['os_id']} {snapshot['os_version_id']}"
        print(f"[+] [sanity-check] May local dat yeu cau toi thieu (OS={pretty}, vCPU={snapshot['vcpu']}, RAM={snapshot['ram_mb']} MiB).")


def cmd_init(args):
    _print_banner(f"init - Generate cluster config ({args.profile})", args)
    ip_list = _parse_csv(args.ips) or (["127.0.0.1"] if args.profile == "standalone" else ["10.10.20.11", "10.10.20.12", "10.10.20.13"])
    local_ip = ip_list[0]
    if (len(set(ip_list)) != len(ip_list)
            or (args.profile == "standalone" and len(ip_list) != 1)
            or (args.profile == "ha" and len(ip_list) < 3)):
        print("[INIT ERROR] Use unique IPs, exactly one standalone host or >=3 HA hosts (first IP is local)."); sys.exit(1)
    if os.path.exists(args.output) and not args.force:
        if sys.stdin.isatty():
            try:
                ans = input(f"File '{args.output}' da ton tai. Ban co muon ghi de khong? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(f"\nCancelled; kept existing file: {args.output}")
                sys.exit(130)
            if ans not in ("y", "yes"):
                print(f"Cancelled; kept existing file: {args.output}")
                return
        else:
            print(f"Output already exists: {args.output} (use -f/--force)"); sys.exit(1)
    hosts_yaml = ""
    for idx, ip in enumerate(ip_list, 1):
        hosts_yaml += f'    - ip: "{ip}"\n      name: "dbe-node-{idx:02d}"\n      local: {"true" if ip == local_ip else "false"}\n      roles: ["db", "etcd", "balancer"]\n'
    sync = "true" if args.profile == "ha" else "false"
    if args.profile == "ha":
        ssh_yaml = 'ssh:\n  user: "root"\n  port: 22\n  use_sudo: false\n  # key_file: "~/.ssh/id_ed25519"\n\n'
        br_note = '  # HA production nen dung destination: "s3" de WAL archive song sot failover.\n'
        pmm_yaml = """  pmm:
    enabled: false
    mode: "external"
    version: 3
    query_source: "pgstatements"
    insecure_tls: false
    # server: "https://pmm.example:443"
    # user: "service_token"
    # password_env: "PMM_SERVER_TOKEN"
"""
    else:
        ssh_yaml = ""
        br_note = ""
        pmm_yaml = """  pmm:
    enabled: true
    mode: "local"
    version: 3
    os_reserve_mb: 1024
    server: ""
    query_source: "pgstatements"
    server_install: {runtime: "podman", image: "docker.io/percona/pmm-server:3.10.0", https_port: 8443}
"""
    template = f"""# Generated by {PROJECT_NAME} ({TOOL_NAME} {TOOL_VERSION}) - SSOT (Pigsty-style: standalone = 1-node Patroni+etcd cohort)
cluster:
  name: "{args.name}"
  profile: "{args.profile}"
  version: "16"
  package_source: "vnpt"
  release_rpm_url: "http://dbengine-repo.ttpmhcm.com/vnpt-db-engine-release.rpm"
  repo_url: "http://dbengine-repo.ttpmhcm.com/vnpt-db-engine/el$releasever/$basearch"
  gpg_key_url: "http://dbengine-repo.ttpmhcm.com/RPM-GPG-KEYS/RPM-GPG-KEY-VNPT-DB-ENGINE"
  safeguard: true
  package_bridge: false
topology:
  hosts:
{hosts_yaml}{ssh_yaml}settings:
  auto_tune: true
  workload: "oltp"
  storage: {{data_dir: "/data/postgres/16/data", wal_dir: "/data/postgres/16/wal", backup_dir: "/data/backup"}}
  auth: {{postgres_password: "postgres_password", replicator_password: "replicator_password", monitor_password: "monitor_password"}}
  parameters: {{}}  # smart defaults use the DB RAM budget after reserving local PMM/OS
  users: []
  databases: []
  etcd: {{enabled: true, token: "vde-etcd-cluster"}}
  patroni: {{enabled: true, synchronous_mode: {sync}, ttl: 30, loop_wait: 10, retry_timeout: 10}}
  pgbouncer: {{enabled: true, pool_mode: "transaction", max_client_conn: 1000, default_pool_size: 50, max_prepared_statements: 200}}
  haproxy: {{enabled: true}}
  # keepalived: {{enabled: true, virtual_ip: "10.10.20.20/24", auth_pass: "vdeVip01", virtual_router_id: 51}}
  pgbackrest: {{enabled: true, stanza: "vde_cluster", destination: "local", retention_full: 2, retention_diff: 7}}
{br_note}{pmm_yaml}"""
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(template)
    print(f"Created: {args.output}")
    cfg = InventoryManager(args.output).load()
    _init_sanity_check(cfg, local_ip, offline=getattr(args, "offline", False))
    print(f"Note: Hay doi mat khau trong settings.auth va ra soat parameters/storage truoc khi chay.\n"
          f"Run this tool on {local_ip}: local execution there, SSH to other nodes.\n"
          f"Next: edit {args.output} -> validate -> plan -> apply --dry-run -c {args.output}")


def _etcd_table_rows(output):
    """Read etcdctl table rows by column name, ignoring diagnostics/borders."""
    headers = None
    rows = []
    for line in output.splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [cell.strip() for cell in line[1:-1].split("|")]
        if headers is None:
            headers = cells
        elif len(cells) == len(headers) and cells != headers:
            rows.append(dict(zip(headers, cells)))
    return rows


def _status_units(cfg, host):
    """systemd units that this host's roles are supposed to run."""
    ns = cfg["settings_normalized"]
    roles = set(host.get("roles") or [])
    units = []
    if "etcd" in roles and ns["etcd"].get("enabled", True):
        units.append("etcd")
    if "db" in roles:
        units.append("patroni")
        if ns["pgbouncer"].get("enabled", True):
            units.append("pgbouncer")
        if ns["pmm"].get("enabled"):
            units.append("pmm-agent")
            if ns["pmm"].get("mode") == "local":
                units.append("vde-pmm-server")
    if "balancer" in roles and ns["haproxy"].get("enabled", True):
        units.append("haproxy")
    if "balancer" in roles and (ns.get("keepalived") or {}).get("enabled"):
        units.append("keepalived")
    return units


# One probe per component. etcd --cluster and Patroni /cluster already describe
# every member, so repeating them on each node only repeats the same report.
_STATUS_ROLE = {
    "etcd": "etcd",
    "patroni": "db",
    "pgbouncer": "db",
    "haproxy": "balancer",
    "keepalived": "balancer",
    "pgbackrest": "db",
    "pmm": "db",
}


def _host_keys(host):
    return {value for value in (
        host.get("name"), host.get("ip"), host.get("ssh_host"), host.get("node_ip")) if value}


def _status_dedicated_nodes(args):
    """--node pins probes. Empty means one random eligible node per component."""
    raw = getattr(args, "node", None) or []
    if isinstance(raw, str):
        raw = [raw]
    tokens = []
    for item in raw:
        tokens.extend(_parse_csv(item) or [])
    if tokens and getattr(args, "random", False):
        raise ValueError("status accepts either --random or --node, not both")
    return tokens


def _status_components(cfg):
    ns = cfg["settings_normalized"]
    names = []
    if ns["etcd"].get("enabled", True):
        names.append("etcd")
    names.append("patroni")
    if ns["pgbouncer"].get("enabled", True):
        names.append("pgbouncer")
    if ns["haproxy"].get("enabled", True):
        names.append("haproxy")
    if (ns.get("keepalived") or {}).get("enabled"):
        names.append("keepalived")
    if ns["pgbackrest"].get("enabled", True):
        names.append("pgbackrest")
    if ns["pmm"].get("enabled"):
        names.append("pmm")
    return names


def _eligible_status_hosts(hosts, component):
    role = _STATUS_ROLE[component]
    return [host for host in hosts if role in (host.get("roles") or [])]


def _choose_status_hosts(eligible, dedicated):
    if not eligible:
        return []
    if not dedicated:
        return [random.choice(list(eligible))]
    wanted = set(dedicated)
    return [host for host in eligible if wanted & _host_keys(host)]


def _probe_banner(component, host, mode, eligible_count):
    label = host.get("name") or host["ip"]
    print(f"\n=== {component} ===")
    print(f"probe: {label}  execution={'LOCAL' if host.get('local') else 'SSH'}  "
          f"target={_execution_target(host)}  mode={mode}  eligible={eligible_count}")


def _service_status(runner, host, units):
    if not units:
        return ""
    service_cmd = ("for s in " + " ".join(shlex.quote(unit) for unit in units) +
                   "; do st=$(systemctl is-active $s 2>/dev/null); echo \"$s=${st:-unknown}\"; done")
    _rc, out = runner.run_command(_execution_target(host), service_cmd, component_name="service-status")
    print(f"Services: {out.replace(chr(10), ' | ')}")
    return out


def _port_status(runner, host, port_numbers):
    watched = "|".join(str(port) for port in dict.fromkeys(port_numbers))
    if not watched:
        return
    _rc, out = runner.run_command(
        _execution_target(host),
        f"ss -tlpn 2>/dev/null | grep -E ':({watched})' | awk '{{print $4}}' | tr '\\n' ' ' || true",
        component_name="port-status")
    print(f"Ports   : {out.strip() or '(none)'}")


def _probe_etcd(cfg, runner, host):
    ports = cfg["effective_config"]["ports"]
    out = _service_status(runner, host, ["etcd"])
    if "etcd=failed" in out or "etcd=inactive" in out:
        print("  ! etcd khong chay - journal gan nhat:")
        runner.run_command(
            _execution_target(host),
            f"journalctl -u etcd -n 25 --no-pager 2>/dev/null | tail -25; "
            f"etcdctl endpoint health --endpoints=http://127.0.0.1:{ports['etcd_client']} 2>&1 | tail -3 || true",
            component_name="etcd-journal")
    # Loopback plus --cluster lists every member, including after NAT (ssh_host != node_ip).
    ep_cmd = (
        "command -v etcdctl >/dev/null 2>&1 || { echo 'etcdctl: not installed'; exit 0; }; "
        f"EP='http://127.0.0.1:{ports['etcd_client']}'; "
        "echo '--- endpoint health (cluster) ---'; "
        "etcdctl --dial-timeout=3s --endpoints=$EP endpoint health --cluster 2>&1; "
        "echo '--- members ---'; "
        "etcdctl --dial-timeout=3s --endpoints=$EP member list -w table 2>&1; "
        "echo 'IS LEARNER: false = voting member; true = non-voting learner (not a health flag).'; "
        "echo '--- endpoint status ---'; "
        "etcdctl --dial-timeout=3s --endpoints=$EP endpoint status --cluster -w table 2>&1"
    )
    _rc, eout = runner.run_command(_execution_target(host), ep_cmd, component_name="etcd-cluster")
    if eout:
        healthy = eout.count(" is healthy:")
        unhealthy = eout.count(" is unhealthy")
        member_output = eout.partition("--- members ---")[2].partition("--- endpoint status ---")[0]
        status_output = eout.partition("--- endpoint status ---")[2]
        members = len(_etcd_table_rows(member_output))
        leaders = sum(row.get("IS LEADER") == "true" for row in _etcd_table_rows(status_output))
        verdict = "OK" if healthy and not unhealthy else "DEGRADED/UNHEALTHY"
        print(f"etcd    : {verdict} - {healthy} healthy / {unhealthy} unhealthy endpoint(s), "
              f"{members} member(s), leader(s)={leaders} (chi tiet o cac dong [etcd-cluster] phia tren)")
    _port_status(runner, host, (ports["etcd_client"], ports["etcd_peer"]))


def _probe_patroni(cfg, runner, host):
    ports = cfg["effective_config"]["ports"]
    out = _service_status(runner, host, ["patroni"])
    if "patroni=failed" in out or "patroni=inactive" in out:
        print("  ! Patroni khong chay - journal gan nhat:")
        runner.run_command(
            _execution_target(host),
            "journalctl -u patroni -n 25 --no-pager 2>/dev/null | tail -25",
            component_name="patroni-journal")
    _rc, payload_text = runner.run_command(
        _execution_target(host),
        f"curl -s --connect-timeout 2 --max-time 5 http://127.0.0.1:{ports['patroni_rest']}/cluster || true",
        component_name="cluster-summary")
    members = []
    if payload_text and payload_text != "[DRY-RUN SUCCESS]":
        try:
            payload = json.loads(payload_text)
            if isinstance(payload, dict):
                members = payload.get("members") or []
        except json.JSONDecodeError:
            members = []
    if members:
        print(f"{'NODE':<20}{'ROLE':<18}{'STATE':<22}LAG")
        for member in members:
            if not isinstance(member, dict):
                continue
            print(f"{str(member.get('name', '')):<20}{str(member.get('role', '')):<18}"
                  f"{str(member.get('state', '')):<22}{member.get('lag', '')}")
    else:
        print(f"Patroni: no cluster membership payload from {_execution_target(host)}")
    _port_status(runner, host, (ports["postgresql"], ports["patroni_rest"]))


def _probe_keepalived(cfg, runner, host):
    ka = cfg["settings_normalized"]["keepalived"]
    out = _service_status(runner, host, ["keepalived"])
    if "keepalived=failed" in out or "keepalived=inactive" in out:
        print("  ! keepalived khong chay - journal gan nhat:")
        runner.run_command(
            _execution_target(host),
            "journalctl -u keepalived -n 25 --no-pager 2>/dev/null | tail -25",
            component_name="keepalived-journal")
    address = ka.get("address") or ""
    _rc, held = runner.run_command(
        _execution_target(host),
        f"ip -4 -o addr show | awk '{{print $4}}' | cut -d/ -f1 | grep -qx {shlex.quote(address)} "
        "&& echo held || echo away",
        component_name="keepalived-vip")
    where = "held on this node" if held.strip() == "held" else "not on this node"
    print(f"VIP     : {address} {where}")


def _probe_pgbackrest(cfg, runner, host):
    br = cfg["settings_normalized"]["pgbackrest"]
    stanza = br.get("stanza") or "vde_cluster"
    info_cmd = f"pgbackrest --stanza={shlex.quote(stanza)} info"
    _rc, info = runner.run_command(
        _execution_target(host),
        f"su - postgres -c {shlex.quote(info_cmd)} | head -n 20 || true",
        component_name="pgbackrest-info")
    if "status: ok" in info:
        state = "status ok"
    elif not info or info == "[DRY-RUN SUCCESS]":
        state = "no info"
    else:
        state = "see pgbackrest-info above"
    print(f"pgBackRest: stanza={stanza} destination={br.get('destination', 'local')} {state}")


def _probe_pmm(cfg, runner, host):
    pmm = cfg["settings_normalized"]["pmm"]
    units = ["pmm-agent"]
    if pmm.get("mode") == "local":
        units.append("vde-pmm-server")
    _service_status(runner, host, units)
    print(f"PMM: enabled mode={pmm.get('mode')} server={pmm.get('server') or '(unset)'}")


_STATUS_PROBES = {
    "etcd": _probe_etcd,
    "patroni": _probe_patroni,
    "pgbouncer": lambda cfg, runner, host: (
        _service_status(runner, host, ["pgbouncer"]),
        _port_status(runner, host, (cfg["effective_config"]["ports"]["pgbouncer"],)),
    ),
    "keepalived": _probe_keepalived,
    "haproxy": lambda cfg, runner, host: (
        _service_status(runner, host, ["haproxy"]),
        _port_status(runner, host, (
            cfg["effective_config"]["ports"]["haproxy_write"],
            cfg["effective_config"]["ports"]["haproxy_read"],
            cfg["effective_config"]["ports"]["haproxy_stats"],
        )),
    ),
    "pgbackrest": _probe_pgbackrest,
    "pmm": _probe_pmm,
}


def cmd_status(args):
    _print_banner("status - Cluster health & service status", args)
    try:
        inv = InventoryManager(args.config); cfg = inv.load(); runner = _runner(args, cfg)
        if not getattr(args, "dry_run", False):
            _require_ssh_client(cfg)
        selected = _select_hosts(cfg, getattr(args, "limit", None))
        dedicated = _status_dedicated_nodes(args)
        if dedicated:
            known = set()
            for host in selected:
                known |= _host_keys(host)
            missing = [token for token in dedicated if token not in known]
            if missing:
                raise ValueError(f"--node matched no host: {', '.join(missing)}")
        mode = "dedicated" if dedicated else "random"
        if dedicated:
            print(f"Probe mode: dedicated ({', '.join(dedicated)})")
        else:
            print("Probe mode: random (one eligible node per component). Use --node HOST to pin.")
        print("etcd and Patroni report every member from the probed node. "
              "PgBouncer, HAProxy, pgBackRest, and Keepalived report that node only.")
        for component in _status_components(cfg):
            eligible = _eligible_status_hosts(selected, component)
            chosen = _choose_status_hosts(eligible, dedicated)
            if not chosen:
                print(f"\n=== {component} ===")
                if not eligible:
                    print(f"skip: no selected node has role {_STATUS_ROLE[component]}")
                else:
                    print("skip: none of the --node hosts run this component")
                continue
            for host in chosen:
                _probe_banner(component, host, mode, len(eligible))
                _STATUS_PROBES[component](cfg, runner, host)
        if not cfg["settings_normalized"]["pmm"].get("enabled"):
            print("\n=== pmm ===")
            print("PMM: disabled")
    except Exception as e:
        print(f"[STATUS ERROR]: {e}"); sys.exit(1)


def cmd_enforce(args):
    _print_banner("enforce - Detect SSOT drift & reconcile", args)
    try:
        inv, cfg, runner = _load_with_preflight(args)
        hosts = _select_hosts(cfg, getattr(args, "limit", None))
        enforcer = StateEnforcer(cfg)
        drifted_by_host = {}
        for h in hosts:
            target = _execution_target(h)
            st = enforcer.inspect_state(target, runner, _build_host_env(cfg, inv, h))
            if st["drifted"]:
                drifted_by_host[target] = st["drifted"]
                for name in st["drifted"]:
                    observed = st["observed"].get(name, "NONE")
                    print(f"[DRIFT] {h['ip']} {name} observed={observed[:12]} desired={st['desired'][name][:12]}")
            else:
                print(f"[OK] {h['ip']} matches recorded component hashes (not a live audit).")
        allow_restart = getattr(args, "restart", False) or args.force
        if getattr(args, "tags", None) or args.force:
            if drifted_by_host or args.force:
                print("==> Reconciling requested scope with FORCE_UPDATE=1 ...")
                _run_pipeline(cfg, inv, runner, hosts, tags=getattr(args, "tags", None), force=True,
                              allow_restart=allow_restart)
                print("Requested scope reconciled; component hashes updated for completed components.")
            else:
                print("\nAll selected nodes match recorded component hashes (not a live-state audit).")
        elif drifted_by_host:
            print("==> Reconciling drifted components with FORCE_UPDATE=1 ...")
            groups = {}
            for h in hosts:
                names = drifted_by_host.get(_execution_target(h))
                if names:
                    groups.setdefault(tuple(names), []).append(h)
            for names, group in groups.items():
                # db_ready SQL sync needs a writable primary. A replica-only drift
                # group would stop with "No writable primary" and never reconcile.
                if "db_ready" in names:
                    others = [name for name in names if name != "db_ready"]
                    if others:
                        _run_pipeline(cfg, inv, runner, group, tags=",".join(others), force=True,
                                      allow_restart=allow_restart)
                    db_group = list(group)
                    selected_targets = {_execution_target(h) for h in db_group}
                    for host in cfg["topology"]["hosts"]:
                        if "db" not in host.get("roles", []):
                            continue
                        target = _execution_target(host)
                        if target in selected_targets:
                            continue
                        if enforcer.is_leader(target, runner):
                            db_group.append(host)
                            selected_targets.add(target)
                    _run_pipeline(cfg, inv, runner, db_group, tags="db_ready", force=True,
                                  allow_restart=allow_restart)
                else:
                    _run_pipeline(cfg, inv, runner, group, tags=",".join(names), force=True,
                                  allow_restart=allow_restart)
            print("Drifted components reconciled; their hashes were updated.")
        else:
            print("\nAll selected nodes match recorded component hashes (not a live-state audit).")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ENFORCE ERROR]: {e}"); sys.exit(1)


def cmd_cleanup(args):
    """Local-only cleanup by default; pass --cluster -c <ha.yml> to orchestrate across an HA cluster."""
    if getattr(args, "cluster", False):
        if not getattr(args, "config", None):
            print("[CLEANUP ERROR]: --cluster requires -c <cluster.yml> (no implicit sample inventory).")
            sys.exit(1)
        try:
            inv = InventoryManager(args.config)
            cfg = inv.load()
            selected_hosts = _select_hosts(cfg, getattr(args, "limit", None))
            if any(not h.get("local") for h in selected_hosts):
                _require_ssh_client(cfg)
            runner = _runner(args, cfg)
            # Cleanup discovery runs read-only probes even in --dry-run;
            # interactive_cluster_cleanup guarantees zero mutations when args.dry_run is True.
            runner.dry_run = False
            _print_banner("cleanup - Interactive cluster software removal", args, cfg)
            interactive_cluster_cleanup(args, cfg, runner, selected_hosts, _execution_target)
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled. If execution had started, inspect status before retrying.")
            sys.exit(130)
        except Exception as exc:
            print(f"[CLEANUP ERROR]: {exc}")
            sys.exit(1)
        return

    if getattr(args, "limit", None):
        print("[CLEANUP ERROR]: --limit requires --cluster -c <cluster.yml>.")
        sys.exit(1)
    if args.sudo and os.geteuid() != 0:
        argv = [arg for arg in sys.argv[1:] if arg != "--sudo"]
        result = subprocess.run(["sudo", "-n", sys.executable, os.path.abspath(__file__), *argv], check=False)
        if result.returncode:
            sys.exit(result.returncode)
        return
    _print_banner("cleanup - Interactive local software removal", args)
    try:
        interactive_cleanup(args)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. If execution had started, inspect status before retrying.")
        sys.exit(130)
    except Exception as exc:
        print(f"[CLEANUP ERROR]: {exc}")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(prog="vde-ins", description=f"{PROJECT_NAME} ({TOOL_NAME} {TOOL_VERSION}): PostgreSQL (Patroni+etcd) Lifecycle & DAG Engine (Python+SSH, Pigsty-style ops)")
    p.add_argument("--version", action="version", version=f"{PROJECT_NAME} {TOOL_NAME} {TOOL_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, with_exec=False):
        sp.add_argument("-c", "--config", default="src/config/vde-cluster.example.yml")
        if with_exec:
            sp.add_argument("-u", "--user", help="SSH user for remote nodes (default: YAML ssh.user or root)"); sp.add_argument("-p", "--port", type=int, help="SSH port for remote nodes (default: YAML ssh.port or 22)")
            sp.add_argument("-k", "--key", help="SSH private key for remote nodes"); sp.add_argument("--sudo", action="store_true", help="Use passwordless sudo locally and on remote nodes")
            sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--timeout", type=int, default=3600, help="Per command/component deadline in seconds (default: 3600)")
            sp.add_argument("-l", "--limit", help="Comma-separated host IPs/names to limit execution to")

    pi = sub.add_parser("init", help="Bootstrap a new cluster config (single YAML SSOT)")
    pi.add_argument("-p", "--profile", choices=["standalone", "ha"], default="standalone")
    pi.add_argument("-n", "--name", default="dbe-core-prod"); pi.add_argument("--ips")
    pi.add_argument("-o", "--output", default="cluster.yml"); pi.add_argument("-f", "--force", action="store_true")
    pi.add_argument("--offline", action="store_true", help="Bo qua kiem tra nhanh phan cung local sau khi tao file")
    pi.set_defaults(func=cmd_init)

    pv = sub.add_parser("validate", help="Validate YAML manifest & topology"); common(pv); pv.set_defaults(func=cmd_validate)
    pp = sub.add_parser("plan", help="Show DAG, smart defaults & SSOT hash"); common(pp)
    pp.add_argument("--discover", action="store_true", help="(Enabled by default) Read hardware locally and via SSH on remote nodes")
    pp.add_argument("--offline", action="store_true", help="Chi xem DAG, bo qua do phan cung va khong hien thi GUC theo phan cung")
    pp.add_argument("-u", "--user", help="Remote SSH user"); pp.add_argument("-p", "--port", type=int, help="Remote SSH port")
    pp.add_argument("-k", "--key", help="SSH private key"); pp.add_argument("--sudo", action="store_true")
    pp.add_argument("--timeout", type=int, default=3600, help="Per command deadline in seconds (default: 3600)"); pp.add_argument("--dry-run", action="store_true")
    pp.set_defaults(func=cmd_plan)

    pa = sub.add_parser("apply", help="Execute DAG / fast config-sync (-t) and deploy cluster")
    common(pa, with_exec=True)
    pa.add_argument("-f", "--force", action="store_true", help="Force overwrite / allow standalone->ha scale-out")
    pa.add_argument("-t", "--tags", help="Only run components/tags, e.g. guc,hba,users,pgbouncer,haproxy,keepalived,pgbackrest")
    pa.add_argument("--restart", action="store_true", help="Rolling restart (replicas first) if pending_restart")
    pa.set_defaults(func=cmd_apply)

    ps = sub.add_parser("status", help="Probe each component on a node that runs it")
    common(ps, with_exec=True)
    status_pick = ps.add_mutually_exclusive_group()
    status_pick.add_argument("--random", action="store_true",
                             help="Probe each component on one randomly chosen node that has its role (default)")
    status_pick.add_argument("--node", action="append", metavar="HOST",
                             help="Probe on this node (name, IP, or SSH host). Repeat, or pass a comma-separated list, to pin more than one. A component runs only when the node has its role.")
    ps.set_defaults(func=cmd_status)

    pe = sub.add_parser("enforce", help="Detect drift and reconcile nodes back to SSOT")
    common(pe, with_exec=True)
    pe.add_argument("-f", "--force", action="store_true"); pe.add_argument("-t", "--tags")
    pe.add_argument("--restart", action="store_true")
    pe.set_defaults(func=cmd_enforce)

    pc = sub.add_parser("cleanup", aliases=["uninstall"], help="Inspect/remove local or HA cluster RPM components interactively")
    pc.add_argument("-c", "--config", help="Optional YAML for storage paths/HA detection (required with --cluster)")
    pc.add_argument("--cluster", action="store_true", help="Orchestrate cleanup across hosts in -c YAML (local + remote SSH)")
    pc.add_argument("--interactive", action="store_true", help="Interactive menu (default unless --check)")
    pc.add_argument("--check", action="store_true", help="Read-only inventory; no terminal required")
    pc.add_argument("--dry-run", action="store_true", help="Discover and review an interactive plan without mutations")
    pc.add_argument("-u", "--user", help="SSH user for remote nodes when using --cluster (default: YAML ssh.user or root)")
    pc.add_argument("-p", "--port", type=int, help="SSH port for remote nodes when using --cluster (default: YAML ssh.port or 22)")
    pc.add_argument("-k", "--key", help="SSH private key for remote nodes when using --cluster")
    pc.add_argument("--sudo", action="store_true", help="Use passwordless sudo locally (and on remote nodes with --cluster)")
    pc.add_argument("--timeout", type=int, default=3600, help="Per command deadline in seconds when using --cluster (default: 3600)")
    pc.add_argument("-l", "--limit", help="Comma-separated host IPs/names to limit --cluster cleanup to (partial HA requires --decommissioned)")
    pc.add_argument("--decommissioned", action="store_true", help="Assert node(s) already detached or cluster retired; allow local or partial HA cleanup")
    pc.add_argument("--data-dir", help="Exact PostgreSQL data directory, overrides detected/YAML value")
    pc.add_argument("--wal-dir", help="Exact external WAL directory, overrides YAML value")
    pc.add_argument("--etcd-data-dir", help="Exact etcd data directory, overrides detected value")
    pc.add_argument("--backup-dir", help="Local backup base directory (only its pgbackrest child is eligible for deletion)")
    pc.set_defaults(func=cmd_cleanup)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
