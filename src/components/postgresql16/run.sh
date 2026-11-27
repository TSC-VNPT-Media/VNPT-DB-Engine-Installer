#!/bin/bash
set -euo pipefail
# postgresql16: ONLY binaries/extensions (Pigsty-style). Patroni owns the
# postgres process in BOTH standalone and HA -- systemd postgresql-16 is
# always disabled so there is exactly one supervisor.
PG_VERSION="${VDE_PG_VERSION:-16}"
PACKAGE_SOURCE="${VDE_PACKAGE_SOURCE:-vnpt}"
PGDATA="${VDE_PGDATA:-/data/postgres/16/data}"

echo "==> [PostgreSQL ${PG_VERSION}] Installing PostgreSQL binaries (source: ${PACKAGE_SOURCE})..."
if [ -x "/usr/pgsql-${PG_VERSION}/bin/postgres" ] && { rpm -q "postgresql${PG_VERSION}-server" >/dev/null 2>&1 || rpm -q "percona-postgresql${PG_VERSION}-server" >/dev/null 2>&1; }; then
    echo "--> postgresql${PG_VERSION}-server already installed, skipping core dnf."
else
    if [ "${PACKAGE_SOURCE}" = "percona" ]; then
        # Core la bat buoc: loi la dung ngay (khong nuot loi)
        dnf install -y percona-postgresql${PG_VERSION}-server percona-postgresql${PG_VERSION}-contrib
    else
        dnf install -y postgresql${PG_VERSION}-server postgresql${PG_VERSION}-contrib || \
        dnf install -y percona-postgresql${PG_VERSION}-server percona-postgresql${PG_VERSION}-contrib
    fi
fi
# Extensions/tools la tuy chon: cai tung goi, goi nao khong co trong repo thi
# canh bao bo qua, khong lam rot ca cum nhu truoc.
EXT_PKGS=()
if [ "${PACKAGE_SOURCE}" = "percona" ]; then
    EXT_PKGS=("percona-pg_repack${PG_VERSION}" "percona-pgaudit${PG_VERSION}" "percona-pgaudit${PG_VERSION}_set_user" "percona-wal2json${PG_VERSION}" "percona-pgbadger" "percona-pg_gather")
else
    EXT_PKGS=("pg_repack_${PG_VERSION}" "pg_repack${PG_VERSION}" "pgaudit${PG_VERSION}" "pgaudit_${PG_VERSION}" "pgaudit" "pgaudit${PG_VERSION}_set_user" "wal2json_${PG_VERSION}" "wal2json${PG_VERSION}" "pgbadger" "pg_gather" "percona-pg_repack${PG_VERSION}" "percona-pgaudit${PG_VERSION}" "percona-wal2json${PG_VERSION}")
fi
for pkg in "${EXT_PKGS[@]}"; do

        if rpm -q "${pkg}" >/dev/null 2>&1; then
            echo "--> optional ${pkg}: already installed."
        elif dnf install -y "${pkg}" >/dev/null 2>&1; then
            echo "--> optional ${pkg}: installed."
        else
            echo "[!] optional package unavailable in repo, skipped: ${pkg}"
        fi
    done
"/usr/pgsql-${PG_VERSION}/bin/postgres" --version || true

cat << EOF > /etc/profile.d/postgresql.sh
# PostgreSQL ${PG_VERSION} environment (VNPT DB Engine)
export PATH=/usr/pgsql-${PG_VERSION}/bin:\$PATH
export PGDATA=${PGDATA}
EOF

# Hand process ownership to Patroni (also migrates legacy systemd standalone)
if systemctl is-active postgresql-${PG_VERSION} >/dev/null 2>&1; then
    echo "--> Stopping legacy systemd postgresql-${PG_VERSION} (Patroni will adopt PGDATA)..."
    systemctl stop postgresql-${PG_VERSION} || true
fi
systemctl disable postgresql-${PG_VERSION} >/dev/null 2>&1 || true
echo "[+] [PostgreSQL ${PG_VERSION}] Binaries ready. Lifecycle governed by Patroni."
