#!/bin/bash
set -euo pipefail
# Patroni: PG supervisor for BOTH standalone (1-node) and HA.
# Scale-out: existing PGDATA/leader is kept untouched; fresh nodes have an
# empty PGDATA so Patroni clones them from the leader via pg_basebackup.
NODE_IP="${VDE_NODE_IP:-127.0.0.1}"
NODE_NAME="${VDE_NODE_NAME:-dbe-node-01}"
CLUSTER_NAME="${VDE_CLUSTER_NAME:-dbe-core-prod}"
ETCD_HOSTS="${VDE_ETCD_HOSTS:-127.0.0.1:2379}"
PGDATA="${VDE_PGDATA:-/data/postgres/16/data}"
PGWAL="${VDE_PGWAL:-/data/postgres/16/wal}"
PG_VERSION="${VDE_PG_VERSION:-16}"
REST_PORT="${VDE_PATRONI_REST_PORT:-8008}"

echo "==> [Patroni] Installing/configuring Patroni (${NODE_NAME} @ ${NODE_IP})..."
if ! command -v patroni >/dev/null 2>&1; then
    if [ "${VDE_PACKAGE_SOURCE:-vnpt}" = "percona" ]; then
        dnf install -y percona-patroni >/dev/null 2>&1 || dnf install -y patroni
    else
        dnf install -y patroni >/dev/null 2>&1 || dnf install -y percona-patroni
    fi
else
    echo "--> patroni already installed."
fi
# Use the interpreter named by the Patroni package. On this repo that is
# /usr/bin/python3.12; python3 stays the OS 3.9 runtime and does not see python3.12-etcd.
patroni_py="python3"
if command -v patroni >/dev/null 2>&1; then
    shebang="$(head -n 1 "$(command -v patroni)" 2>/dev/null || true)"
    case "$shebang" in
        \#!*)
            rest="${shebang#\#!}"
            rest="${rest#"${rest%%[![:space:]]*}"}"
            if [[ "$rest" == *"/env "* ]]; then
                interp="${rest#*env }"
                interp="${interp%% *}"
            else
                interp="${rest%% *}"
            fi
            if [ -n "$interp" ] && [ -x "$interp" ]; then
                patroni_py="$interp"
            elif [ -n "$interp" ] && command -v "${interp##*/}" >/dev/null 2>&1; then
                patroni_py="$(command -v "${interp##*/}")"
            fi
            ;;
    esac
fi
echo "--> Patroni Python: ${patroni_py}"
py_rpm="$(basename "$patroni_py")"
if [ "$py_rpm" = "python3" ]; then
    dnf install -y python3-psycopg2 >/dev/null 2>&1 || true
else
    dnf install -y "${py_rpm}" "${py_rpm}-psycopg2" >/dev/null 2>&1 || dnf install -y python3-psycopg2 >/dev/null 2>&1 || true
fi
etcd_client_ready() {
    "$patroni_py" -c 'import etcd3' >/dev/null 2>&1 || "$patroni_py" -c 'import etcd' >/dev/null 2>&1
}
if ! etcd_client_ready; then
    if [ "$py_rpm" != "python3" ]; then
        dnf install -y "${py_rpm}-etcd" >/dev/null 2>&1 || true
    fi
    dnf install -y python3-etcd3 >/dev/null 2>&1 || dnf install -y python3-python-etcd >/dev/null 2>&1 \
        || dnf install -y python3-etcd >/dev/null 2>&1 || dnf install -y percona-patroni-etcd >/dev/null 2>&1 \
        || dnf install -y patroni-etcd >/dev/null 2>&1 || true
fi
if ! etcd_client_ready; then
    echo "[ERROR] Patroni cannot import an etcd client library (etcd3 or etcd) with ${patroni_py}." >&2
    exit 1
fi

mkdir -p /etc/patroni /var/run/postgresql
chown postgres:postgres /etc/patroni /var/run/postgresql 2>/dev/null || true
chmod 2775 /var/run/postgresql 2>/dev/null || true
# Fresh replica must start from EMPTY data and WAL dirs so pg_basebackup --waldir
# can recreate the symlink. Existing PGDATA is adopted as-is.
if [ ! -f "${PGDATA}/PG_VERSION" ]; then
    find "${PGDATA}" -mindepth 1 -delete 2>/dev/null || true
    if [ -d "${PGWAL}" ]; then
        find "${PGWAL}" -mindepth 1 -delete 2>/dev/null || true
    fi
fi
systemctl disable postgresql-${PG_VERSION} >/dev/null 2>&1 || true
systemctl stop postgresql-${PG_VERSION} >/dev/null 2>&1 || true

HBA_BOOT=""
HBA_DCS=""
while IFS= read -r line; do
    if [ -n "${line}" ]; then
        HBA_BOOT="${HBA_BOOT}    - ${line}
"
        HBA_DCS="${HBA_DCS}        - ${line}
"
    fi
done <<< "${VDE_HBA_LINES:-local all all peer
host all all 127.0.0.1/32 scram-sha-256
host replication replicator 0.0.0.0/0 scram-sha-256
host all all 0.0.0.0/0 scram-sha-256}"
ETCD_YAML=""
for h in $(echo "${ETCD_HOSTS}" | tr ',' ' '); do ETCD_YAML="${ETCD_YAML}    - ${h}
"; done

WAS_ACTIVE=0
systemctl is-active patroni >/dev/null 2>&1 && WAS_ACTIVE=1

cat << EOF > /etc/patroni/patroni.yml
scope: ${CLUSTER_NAME}
namespace: /service/
name: ${NODE_NAME}
restapi:
  listen: 0.0.0.0:${REST_PORT}
  connect_address: ${NODE_IP}:${REST_PORT}
etcd3:
  hosts:
${ETCD_YAML}bootstrap:
  dcs:
    ttl: ${VDE_PATRONI_TTL:-30}
    loop_wait: ${VDE_PATRONI_LOOP_WAIT:-10}
    retry_timeout: ${VDE_PATRONI_RETRY_TIMEOUT:-10}
    maximum_lag_on_failover: 1048576
    synchronous_mode: ${VDE_PATRONI_SYNC_MODE:-false}
    synchronous_mode_strict: ${VDE_PATRONI_SYNC_STRICT:-false}
    postgresql:
      use_pg_rewind: true
      use_slots: true
      parameters:
${VDE_PATRONI_GUC_YAML:-        shared_buffers: 4096MB}
      pg_hba:
${HBA_DCS}  pg_hba:
${HBA_BOOT}  initdb:
    - encoding: UTF8
    - locale: en_US.UTF-8
    - data-checksums
    - waldir: ${PGWAL}
  users:
    postgres:
      password: "${VDE_PG_PASS:-postgres_password}"
      options: [createrole, createdb]
    replicator:
      password: "${VDE_REPL_PASS:-replicator_password}"
      options: [replication]
postgresql:
  listen: 0.0.0.0:${VDE_PG_PORT:-5432}
  connect_address: ${NODE_IP}:${VDE_PG_PORT:-5432}
  data_dir: ${PGDATA}
  bin_dir: /usr/pgsql-${PG_VERSION}/bin
  config_dir: ${PGDATA}
  pgpass: /tmp/pgpass0
  authentication:
    superuser: {username: postgres, password: "${VDE_PG_PASS:-postgres_password}"}
    replication: {username: replicator, password: "${VDE_REPL_PASS:-replicator_password}"}
    rewind: {username: postgres, password: "${VDE_PG_PASS:-postgres_password}"}
  create_replica_methods: [basebackup]
  basebackup:
    max-rate: "100M"
    checkpoint: fast
    waldir: ${PGWAL}
  parameters:
    unix_socket_directories: '/var/run/postgresql'
watchdog: {mode: automatic, device: /dev/watchdog, safety_margin: 5}
tags: {nofailover: false, noloadbalance: false, clonefrom: false, nosync: false}
EOF
chown postgres:postgres /etc/patroni/patroni.yml 2>/dev/null || true; chmod 600 /etc/patroni/patroni.yml

# systemd unit cua ban dong goi co the tro config toi path khac
# (patroni.yaml thay vi patroni.yml): tu phat hien va symlink ve SSOT.
UNIT_CFG="$(systemctl cat patroni 2>/dev/null | grep -oE '/[A-Za-z0-9._/-]*patroni\.(yml|yaml)' | head -n1 || true)"
if [ -n "${UNIT_CFG}" ] && [ "${UNIT_CFG}" != "/etc/patroni/patroni.yml" ]; then
    echo "--> systemd unit expects config at ${UNIT_CFG}; linking to SSOT file."
    mkdir -p "$(dirname "${UNIT_CFG}")"
    ln -sf /etc/patroni/patroni.yml "${UNIT_CFG}"
fi
ln -sf /etc/patroni/patroni.yml /etc/patroni/patroni.yaml 2>/dev/null || true

# Unit dong goi dung ExecStart=/usr/bin/patroni ${PATRONI_CONFIG_LOCATION}.
# Neu bien nay khong duoc dinh nghia (hoac tro sai), Patroni bao "Config is
# empty." du file co noi dung. Ep cung bang systemd drop-in ve file SSOT.
mkdir -p /etc/systemd/system/patroni.service.d
cat << 'EOF' > /etc/systemd/system/patroni.service.d/10-vde.conf
[Service]
Environment=PATRONI_CONFIG_LOCATION=/etc/patroni/patroni.yml
EOF
echo "--> Forced PATRONI_CONFIG_LOCATION=/etc/patroni/patroni.yml via systemd drop-in."

# Kiem chung: config khong duoc rong va phai hop le truoc khi start
if [ ! -s /etc/patroni/patroni.yml ]; then
    echo "[ERROR] /etc/patroni/patroni.yml is EMPTY after render! Aborting."; exit 1
fi
echo "--> patroni.yml rendered: $(wc -l < /etc/patroni/patroni.yml) lines"
patroni --validate-config /etc/patroni/patroni.yml 2>&1 | tail -3 || true

systemctl daemon-reload
systemctl enable patroni >/dev/null 2>&1 || true
if [ "${WAS_ACTIVE}" = "1" ]; then
    echo "--> Patroni running: live reload (DCS PATCH is done by StateEnforcer after the DAG)."
    curl -sf -X POST "http://127.0.0.1:${REST_PORT}/reload" >/dev/null 2>&1 || systemctl restart patroni || true
else
    systemctl restart patroni >/dev/null 2>&1 || systemctl start patroni || true
fi

# Xac minh that: service phai active va REST phai tra loi, khong bao gio
# in thanh cong gia. Loi thi in journal de thay nguyen nhan ngay tai day.
sleep 3
if ! systemctl is-active patroni >/dev/null 2>&1; then
    echo "[ERROR] patroni.service failed to start. Last journal lines:"
    journalctl -u patroni -n 30 --no-pager 2>/dev/null | tail -30
    exit 1
fi
echo "--> patroni.service is active."

# db_ready owns SQL/extension sync. This gate only proves Patroni stayed up.
# EXPECT_PRIMARY=1: the bootstrap node, or a node that already has PGDATA.
# EXPECT_PRIMARY=0: a fresh replica. Accept "creating replica"; do not wait for /primary.
REST="http://127.0.0.1:${REST_PORT}"
if [ "${VDE_PATRONI_EXPECT_PRIMARY:-1}" = "1" ]; then
    READY=0
    for i in $(seq 1 90); do
        if curl -sf "${REST}/primary" >/dev/null 2>&1 || curl -sf "${REST}/health" >/dev/null 2>&1; then
            READY=1
            break
        fi
        sleep 1
    done
    if [ "${READY}" != "1" ]; then
        echo "[ERROR] Patroni did not become primary or healthy on ${NODE_IP}:${REST_PORT} within 90s."
        journalctl -u patroni -n 40 --no-pager 2>/dev/null | tail -40
        exit 1
    fi
else
    READY=0
    BODY=""
    for i in $(seq 1 90); do
        BODY="$(curl -sf "${REST}/patroni" 2>/dev/null || true)"
        if echo "${BODY}" | grep -Eq '"state"[[:space:]]*:[[:space:]]*"(running|streaming|creating replica|in archive recovery)"'; then
            READY=1
            break
        fi
        sleep 1
    done
    if [ "${READY}" != "1" ]; then
        echo "[ERROR] Fresh replica Patroni on ${NODE_IP}:${REST_PORT} is not running, streaming or creating a replica."
        echo "${BODY}"
        journalctl -u patroni -n 40 --no-pager 2>/dev/null | tail -40
        exit 1
    fi
fi
echo "[+] [Patroni] Active on ${NODE_IP}:${REST_PORT} (sync_mode=${VDE_PATRONI_SYNC_MODE:-false}, expect_primary=${VDE_PATRONI_EXPECT_PRIMARY:-1})."
