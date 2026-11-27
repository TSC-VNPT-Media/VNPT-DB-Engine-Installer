"""Refuse to start when a node is outside the supported OS or hardware floor."""

MIN_VCPU = 2
MIN_RAM_MB = 2048
SUPPORTED_OS = {
    "rhel": "RHEL",
    "rocky": "Rocky Linux",
    "almalinux": "AlmaLinux",
}
SUPPORTED_MAJORS = (8, 9)
# RHEL 8/9 baseline microarchitecture (x86-64-v2).
X86_64_V2_FLAGS = ("cx16", "lahf_lm", "popcnt", "sse4_2", "ssse3")
PROBE_KEYS = (
    "OS_ID", "OS_VERSION_ID", "OS_PRETTY_NAME", "ARCH", "CPU_MODEL", "CPU_FLAGS",
    "EPOCH", "FIREWALLD", "FW_PORTS", "FW_RICH", "FW_ZONES", "FW_SERVICES", "FW_PROTOCOLS",
)


def parse_probe_meta(line):
    """Return (key, value) for a labeled platform probe line, else None."""
    key, sep, value = line.partition("=")
    if sep and key in PROBE_KEYS:
        return key, value.strip()
    return None


def os_major(version_id):
    head = str(version_id or "").split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _minimum_ram(pmm):
    """Return (minimum MiB, operator-facing reason)."""
    if pmm.get("enabled") and pmm.get("mode") == "local":
        install = pmm.get("server_install") or {}
        value = str(install.get("memory") or "4g")
        pmm_mb = int(value[:-1]) * (1024 if value[-1].lower() == "g" else 1)
        os_mb = int(pmm.get("os_reserve_mb") or 1024)
        total = pmm_mb + os_mb + 1024
        return total, (
            f"Tối thiểu {total} MiB khi PMM chạy cùng máy "
            f"({pmm_mb} PMM + {os_mb} OS + 1024 database)."
        )
    return MIN_RAM_MB, f"Tối thiểu {MIN_RAM_MB} MiB."


def minimum_requirements(pmm):
    """Return structured minimum OS/CPU/RAM requirements for the given PMM settings."""
    min_ram, ram_reason = _minimum_ram(pmm or {})
    os_names = "/".join(SUPPORTED_OS.values())
    majors = "/".join(str(m) for m in SUPPORTED_MAJORS)
    return {
        "os": f"{os_names} {majors} (x86_64, x86-64-v2)",
        "min_vcpu": MIN_VCPU,
        "min_ram_mb": min_ram,
        "ram_reason": ram_reason,
    }


def requirement_failures(facts, pmm):
    """List every unmet OS/CPU/RAM requirement. Empty means the node may start."""
    failures = []
    os_id = str(facts.get("os_id") or "").strip().lower()
    version_id = str(facts.get("os_version_id") or "").strip()
    pretty = str(facts.get("os_pretty") or "").strip()
    major = os_major(version_id)
    supported = ", ".join(SUPPORTED_OS.values())
    if not os_id or major is None:
        failures.append(
            f"Không đọc được hệ điều hành (/etc/os-release). "
            f"Yêu cầu {supported} bản {SUPPORTED_MAJORS[0]} hoặc {SUPPORTED_MAJORS[1]}.")
    elif os_id not in SUPPORTED_OS or major not in SUPPORTED_MAJORS:
        shown = pretty or f"{os_id} {version_id}".strip()
        failures.append(
            f"Hệ điều hành: {shown} ({os_id} {version_id}). "
            f"Yêu cầu {supported} bản {SUPPORTED_MAJORS[0]} hoặc {SUPPORTED_MAJORS[1]}.")

    arch = str(facts.get("arch") or "").strip().lower()
    flags = {flag.lower() for flag in str(facts.get("cpu_flags") or "").split() if flag}
    model = str(facts.get("cpu_model") or "").strip() or "không rõ model"
    if arch != "x86_64":
        failures.append(f"Kiến trúc CPU: {arch or 'không đọc được'}. Yêu cầu x86_64.")
    elif not flags:
        failures.append("Không đọc được cờ CPU (/proc/cpuinfo) để xác định đời vi xử lý x86-64-v2.")
    else:
        missing = [flag for flag in X86_64_V2_FLAGS if flag not in flags]
        if missing:
            failures.append(
                f"Đời CPU: {model} chưa đạt x86-64-v2 (mức RHEL 8/9). Thiếu cờ: {', '.join(missing)}.")

    vcpu = facts.get("vcpu")
    if not isinstance(vcpu, int) or isinstance(vcpu, bool) or vcpu < MIN_VCPU:
        failures.append(f"Số CPU: {vcpu}. Tối thiểu {MIN_VCPU}.")

    ram_mb = facts.get("ram_mb")
    minimum_ram, ram_reason = _minimum_ram(pmm or {})
    if not isinstance(ram_mb, int) or isinstance(ram_mb, bool) or ram_mb < minimum_ram:
        failures.append(f"RAM: {ram_mb} MiB. {ram_reason}")
    return failures


def required_firewall_ports(host, ports, settings):
    """TCP ports a host must accept from the cluster, based on its roles."""
    roles = set(host.get("roles") or [])
    needed = []
    if "etcd" in roles and (settings.get("etcd") or {}).get("enabled", True):
        needed.extend((ports["etcd_client"], ports["etcd_peer"]))
    if "db" in roles:
        needed.extend((ports["postgresql"], ports["patroni_rest"]))
        if (settings.get("pgbouncer") or {}).get("enabled", True):
            needed.append(ports["pgbouncer"])
    if "balancer" in roles and (settings.get("haproxy") or {}).get("enabled", True):
        needed.extend((ports["haproxy_write"], ports["haproxy_read"], ports["haproxy_stats"]))
    # Preserve declaration order while dropping duplicates (a node can hold every role).
    return list(dict.fromkeys(int(port) for port in needed))


def _firewall_allows(port, fw_ports, fw_rich):
    if f"{int(port)}/tcp" in str(fw_ports or "").split():
        return True
    rich = str(fw_rich or "")
    token = f'port="{int(port)}"'
    token_plain = f"port={int(port)}"
    return token in rich or token_plain in rich


def missing_firewall_ports(firewalld, fw_ports, fw_rich, needed):
    """Empty unless firewalld is the active filter and a required port is closed."""
    if str(firewalld or "").strip().lower() != "active":
        return []
    return [port for port in needed if not _firewall_allows(port, fw_ports, fw_rich)]


def vrrp_allowed(firewalld, services, protocols):
    """Keepalived advertisements use protocol 112, which firewalld calls vrrp."""
    if str(firewalld or "").strip().lower() != "active":
        return True
    granted = set(str(services or "").split()) | set(str(protocols or "").split())
    return "vrrp" in granted


def firewall_open_command(ports):
    """Read-only suggestion. The installer never changes firewalld itself."""
    if not ports:
        return ""
    tcp = [port for port in ports if port != "vrrp"]
    adds = " ".join(f"--add-port={int(port)}/tcp" for port in tcp)
    if "vrrp" in ports:
        adds = (adds + " --add-service=vrrp").strip()
    return f"firewall-cmd --permanent {adds} && firewall-cmd --reload"


def refusal_message(blocks):
    """blocks: sequence of (label, [reason, ...])."""
    lines = []
    for label, reasons in blocks:
        lines.append(f"Không thể start: {label} không đạt cấu hình tối thiểu.")
        lines.extend(f"  - {reason}" for reason in reasons)
    lines.append("Không tiếp tục cài đặt.")
    return "\n".join(lines)
