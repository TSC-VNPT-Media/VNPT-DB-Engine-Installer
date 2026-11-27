#!/bin/bash
set -euo pipefail
# HAProxy: write -> Patroni /primary, read -> Patroni /replica.
# Backends use PgBouncer when it is enabled, otherwise PostgreSQL itself.
DB_NODES="${VDE_DB_NODES:-127.0.0.1}"
BACKEND_PORT="${VDE_HAPROXY_BACKEND_PORT:-6432}"
WRITE_PORT="${VDE_HAPROXY_WRITE_PORT:-5433}"
READ_PORT="${VDE_HAPROXY_READ_PORT:-5434}"
STATS_PORT="${VDE_HAPROXY_STATS_PORT:-7000}"
REST_PORT="${VDE_PATRONI_REST_PORT:-8008}"
echo "==> [HAProxy] Installing/configuring balancer (nodes: ${DB_NODES})..."
if ! command -v haproxy >/dev/null 2>&1; then
    if [ "${VDE_PACKAGE_SOURCE:-vnpt}" = "percona" ]; then
        dnf install -y percona-haproxy >/dev/null 2>&1 || dnf install -y haproxy
    else
        dnf install -y haproxy >/dev/null 2>&1 || dnf install -y percona-haproxy
    fi
else
    echo "--> haproxy already installed."
fi
setsebool -P haproxy_connect_any=1 2>/dev/null || true
mkdir -p /etc/haproxy /var/lib/haproxy
cp -a /etc/haproxy/haproxy.cfg "/etc/haproxy/haproxy.cfg.bak.$(date +%s)" 2>/dev/null || true

PRIMARY_SERVERS=""; REPLICA_SERVERS=""; i=0
for node in $(echo "${DB_NODES}" | tr ',' ' '); do
    i=$((i+1))
    PRIMARY_SERVERS="${PRIMARY_SERVERS}    server pg${i} ${node}:${BACKEND_PORT} maxconn 1000 check port ${REST_PORT} inter 3s fall 3 rise 2
"
    REPLICA_SERVERS="${REPLICA_SERVERS}    server pg${i} ${node}:${BACKEND_PORT} maxconn 1000 check port ${REST_PORT} inter 3s fall 3 rise 2
"
done

cat << EOF > /etc/haproxy/haproxy.cfg
global
    maxconn 4096
    log 127.0.0.1 local2
    chroot /var/lib/haproxy
    pidfile /var/run/haproxy.pid
    user haproxy
    group haproxy
    daemon
    stats socket /var/lib/haproxy/stats
defaults
    mode tcp
    log global
    retries 3
    timeout queue 1m
    timeout connect 10s
    timeout client 30m
    timeout server 30m
    timeout check 5s
    maxconn 3000
listen stats
    mode http
    bind *:${STATS_PORT}
    stats enable
    stats uri /
    stats refresh 10s
    stats show-legends
listen pg_primary
    bind *:${WRITE_PORT}
    mode tcp
    option httpchk GET /primary
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
${PRIMARY_SERVERS}
listen pg_replica
    bind *:${READ_PORT}
    mode tcp
    balance roundrobin
    option httpchk GET /replica
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
${REPLICA_SERVERS}
EOF

haproxy -c -f /etc/haproxy/haproxy.cfg
systemctl daemon-reload
systemctl enable haproxy >/dev/null 2>&1 || true
if systemctl is-active haproxy >/dev/null 2>&1; then
    systemctl reload haproxy 2>/dev/null || systemctl restart haproxy
else
    systemctl restart haproxy >/dev/null 2>&1 || systemctl start haproxy
fi
echo "[+] [HAProxy] Write :${WRITE_PORT} | Read :${READ_PORT} | Stats :${STATS_PORT} | Backend :${BACKEND_PORT}."
