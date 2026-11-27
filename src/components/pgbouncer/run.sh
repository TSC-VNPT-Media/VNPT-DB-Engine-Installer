#!/bin/bash
set -euo pipefail
echo "==> [PgBouncer] Installing/configuring pooler :${VDE_PGBOUNCER_PORT:-6432}..."
if ! command -v pgbouncer >/dev/null 2>&1; then
    if [ "${VDE_PACKAGE_SOURCE:-vnpt}" = "percona" ]; then
        dnf install -y percona-pgbouncer >/dev/null 2>&1 || dnf install -y pgbouncer
    else
        dnf install -y pgbouncer >/dev/null 2>&1 || dnf install -y percona-pgbouncer
    fi
else
    echo "--> pgbouncer already installed."
fi
mkdir -p /etc/pgbouncer /var/log/pgbouncer /var/run/pgbouncer
chown -R pgbouncer:pgbouncer /etc/pgbouncer /var/log/pgbouncer /var/run/pgbouncer 2>/dev/null || true

cat << EOF > /etc/pgbouncer/pgbouncer.ini
[databases]
* = host=127.0.0.1 port=${VDE_PG_PORT:-5432}
[pgbouncer]
logfile = /var/log/pgbouncer/pgbouncer.log
pidfile = /var/run/pgbouncer/pgbouncer.pid
listen_addr = *
listen_port = ${VDE_PGBOUNCER_PORT:-6432}
unix_socket_dir = /var/run/pgbouncer
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
admin_users = postgres
stats_users = postgres
pool_mode = ${VDE_PGBOUNCER_POOL_MODE:-transaction}
max_client_conn = ${VDE_PGBOUNCER_MAX_CLIENT_CONN:-1000}
default_pool_size = ${VDE_PGBOUNCER_DEFAULT_POOL_SIZE:-50}
min_pool_size = 10
reserve_pool_size = 10
max_prepared_statements = ${VDE_PGBOUNCER_MAX_PREPARED:-200}
server_reset_query = DISCARD ALL
server_check_query = select 1
EOF
touch /etc/pgbouncer/userlist.txt
# Sync SCRAM hashes from the local PG when it is already up (leader or replica)
if su - postgres -c "/usr/pgsql-${VDE_PG_VERSION:-16}/bin/psql -p ${VDE_PG_PORT:-5432} -Atc \"SELECT 1\"" >/dev/null 2>&1; then
    su - postgres -c "/usr/pgsql-${VDE_PG_VERSION:-16}/bin/psql -p ${VDE_PG_PORT:-5432} -Atc \"SELECT '\\\"' || usename || '\\\" \\\"' || passwd || '\\\"' FROM pg_shadow WHERE usename='postgres';\"" \
        > /etc/pgbouncer/userlist.txt 2>/dev/null || true
fi
[ -s /etc/pgbouncer/userlist.txt ] || echo "\"postgres\" \"${VDE_PG_PASS:-postgres_password}\"" > /etc/pgbouncer/userlist.txt
chown pgbouncer:pgbouncer /etc/pgbouncer/pgbouncer.ini /etc/pgbouncer/userlist.txt 2>/dev/null || true
chmod 640 /etc/pgbouncer/pgbouncer.ini; chmod 600 /etc/pgbouncer/userlist.txt

systemctl daemon-reload
systemctl enable pgbouncer >/dev/null 2>&1 || true
if systemctl is-active pgbouncer >/dev/null 2>&1; then
    systemctl reload pgbouncer 2>/dev/null || systemctl restart pgbouncer
else
    systemctl restart pgbouncer >/dev/null 2>&1 || systemctl start pgbouncer
fi
echo "[+] [PgBouncer] Active on :${VDE_PGBOUNCER_PORT:-6432} (${VDE_PGBOUNCER_POOL_MODE:-transaction})."
