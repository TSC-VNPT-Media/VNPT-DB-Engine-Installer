#!/bin/bash
set -euo pipefail
# Storage parents created by this component must be traversable by postgres,
# even when the invoking shell uses a restrictive umask. Existing directories
# retain their permissions; the access check below reports unsafe layouts.
umask 022
# os_baseline: OS prep shared by standalone (1-node cohort) and HA.
echo "==> [OS Baseline] sysctl, limits, softdog, postgres user, /data layout..."
PGDATA="${VDE_PGDATA:-/data/postgres/16/data}"
PGWAL="${VDE_PGWAL:-/data/postgres/16/wal}"
BACKUP_DIR="${VDE_BACKUP_DIR:-/data/backup}"

dnf install -y numactl sysstat acl cronie >/dev/null 2>&1 || yum install -y numactl sysstat acl cronie >/dev/null 2>&1 || true

if ! getent group postgres >/dev/null; then groupadd -g 26 postgres; fi
if ! getent passwd postgres >/dev/null; then useradd -m -u 26 -g postgres -s /bin/bash postgres; fi

# Patroni watchdog fencing (HA + standalone alike)
modprobe softdog 2>/dev/null || true
echo "softdog" > /etc/modules-load.d/softdog.conf 2>/dev/null || true

# Kernel tuning: SSOT lines from YAML if provided, else safe defaults
if [ -n "${VDE_SYSCTL_CONF_LINES:-}" ]; then
    printf '%s\n' "${VDE_SYSCTL_CONF_LINES}" > /etc/sysctl.d/99-vde-postgres.conf
else
    cat << 'EOF' > /etc/sysctl.d/99-vde-postgres.conf
vm.overcommit_memory = 2
vm.overcommit_ratio = 80
vm.swappiness = 1
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
EOF
fi
sysctl -p /etc/sysctl.d/99-vde-postgres.conf >/dev/null || true

cat << 'EOF' > /etc/security/limits.d/99-vde-postgres.conf
postgres soft nofile 65536
postgres hard nofile 65536
postgres soft nproc  32768
postgres hard nproc  32768
postgres soft memlock unlimited
postgres hard memlock unlimited
EOF

mkdir -p "${PGDATA}" "${PGWAL}" "${BACKUP_DIR}" /etc/vde
PG_ROOT="$(dirname "$(dirname "${PGDATA}")")"
# Recursive chown only before the cluster exists: on a multi-TB PGDATA it walks
# every file on each apply.
if [ -f "${PGDATA}/PG_VERSION" ]; then
    chown postgres:postgres "${PG_ROOT}" "$(dirname "${PGDATA}")" "${PGDATA}" "${PGWAL}"
else
    chown -R postgres:postgres "${PG_ROOT}" "${PGWAL}"
fi
chown -R postgres:postgres "${BACKUP_DIR}"
chmod 700 "${PGDATA}" "${PGWAL}" 2>/dev/null || true
for dir in "${PGDATA}" "${PGWAL}" "${BACKUP_DIR}"; do
    if ! runuser -u postgres -- test -x "${dir}"; then
        echo "[ERROR] postgres cannot traverse ${dir}; check permissions on the directory and its parents (namei -l)." >&2
        exit 1
    fi
done
systemctl enable cronie >/dev/null 2>&1 || systemctl enable crond >/dev/null 2>&1 || true
echo "[+] [OS Baseline] Completed."
