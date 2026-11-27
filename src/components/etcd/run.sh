#!/bin/bash
set -euo pipefail
# The etcd systemd service runs as etcd; its non-secret config must be readable
# regardless of the umask inherited from the caller.
umask 022
# etcd: 1-node (standalone, phase=all) or 3-node HA (phase=configure then verify).
# HA must not wait for quorum on the first node: the other members are not up yet.
# Scale-out 1->3: the existing member runs `etcdctl member add` for new peers
# BEFORE those peers start; new peers then boot with state=existing.
NODE_IP="${VDE_NODE_IP:-127.0.0.1}"
ETCD_NAME="${VDE_ETCD_NAME:-etcd1}"
INITIAL_CLUSTER="${VDE_ETCD_INITIAL_CLUSTER:-etcd1=http://127.0.0.1:2380}"
TOKEN="${VDE_ETCD_TOKEN:-vde-etcd-cluster}"
CLIENT_PORT="${VDE_ETCD_CLIENT_PORT:-2379}"
PEER_PORT="${VDE_ETCD_PEER_PORT:-2380}"
PHASE="${VDE_ETCD_PHASE:-all}"
DATA_DIR="/var/lib/etcd/default.etcd"
PGDATA_DIR="${VDE_PGDATA:-/data/postgres/16/data}"

check_health() {
    local tries="${1:-15}"
    local i
    for i in $(seq 1 "${tries}"); do
        echo "--> [etcd] Kiem tra health lan ${i}/${tries} (127.0.0.1 + ${NODE_IP})..."
        if etcdctl endpoint health --dial-timeout=3s --endpoints="http://127.0.0.1:${CLIENT_PORT}" >/dev/null 2>&1 \
           && etcdctl endpoint health --dial-timeout=3s --endpoints="http://${NODE_IP}:${CLIENT_PORT}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# Confirm one leader and a voting majority. Used only after every HA member has started.
verify_quorum() {
    local status_json member_json
    status_json="$(etcdctl --dial-timeout=3s --endpoints="http://127.0.0.1:${CLIENT_PORT}" endpoint status --cluster -w json 2>/dev/null || true)"
    member_json="$(etcdctl --dial-timeout=3s --endpoints="http://127.0.0.1:${CLIENT_PORT}" member list -w json 2>/dev/null || true)"
    STATUS_JSON="${status_json}" MEMBER_JSON="${member_json}" python3 - << 'PY'
import json, os, sys
try:
    status = json.loads(os.environ.get("STATUS_JSON") or "[]")
    members = json.loads(os.environ.get("MEMBER_JSON") or "{}")
except json.JSONDecodeError:
    sys.exit("etcd status/member JSON is unreadable")
if isinstance(members, dict):
    members = members.get("members") or []
voting = [m for m in members if not m.get("isLearner")]
if not voting:
    sys.exit("etcd has no voting members")
majority = len(voting) // 2 + 1
healthy = []
leaders = set()
for item in status if isinstance(status, list) else []:
    body = item.get("Status") if isinstance(item, dict) else None
    if not isinstance(body, dict) or item.get("error") or body.get("errors"):
        continue
    header = body.get("header") or {}
    healthy.append(item.get("Endpoint"))
    member_id = header.get("member_id")
    leader = body.get("leader")
    if member_id is not None and member_id == leader:
        leaders.add(member_id)
if len(healthy) < majority:
    sys.exit(f"etcd healthy endpoints {len(healthy)} < majority {majority}")
if len(leaders) != 1:
    sys.exit(f"etcd leader count is {len(leaders)}, expected 1")
print(f"[+] [etcd] Quorum OK: {len(healthy)} healthy endpoint(s), {len(voting)} voting member(s), 1 leader.")
PY
}

rescue_unhealthy() {
    # Controlled rescue: only with --force AND before PostgreSQL has been bootstrapped
    # (no PG_VERSION => the DCS does not yet hold real cluster data).
    if [ "${VDE_FORCE_UPDATE:-0}" = "1" ] && [ ! -f "${PGDATA_DIR}/PG_VERSION" ]; then
        local bak
        bak="${DATA_DIR}.bak.$(date +%Y%m%d%H%M%S)"
        echo "[!] [etcd] Cluster khong healthy va PG chua bootstrap. --force: backup data dir -> ${bak} va bootstrap lai."
        systemctl stop etcd >/dev/null 2>&1 || true
        [ -d "${DATA_DIR}" ] && mv "${DATA_DIR}" "${bak}"
        mkdir -p "${DATA_DIR}"
        id etcd >/dev/null 2>&1 && chown -R etcd:etcd /var/lib/etcd || true
        sed -i 's/^initial-cluster-state:.*/initial-cluster-state: new/' /etc/etcd/etcd.yaml
        sed -i 's/^ETCD_INITIAL_CLUSTER_STATE=.*/ETCD_INITIAL_CLUSTER_STATE="new"/' /etc/etcd/etcd.conf
        systemctl start etcd || true
        local tries=15
        [ "${PHASE}" = "verify" ] && tries=30
        if check_health "${tries}"; then
            echo "[+] [etcd] Re-bootstrapped healthy. Old data kept at ${bak}."
            if [ "${PHASE}" = "verify" ]; then
                verify_quorum
            fi
            exit 0
        fi
    fi
    echo "[ERROR] [etcd] Not healthy after start/restart. Last journal lines:"
    journalctl -u etcd -n 30 --no-pager 2>/dev/null | tail -30
    exit 1
}

if [ "${PHASE}" = "verify" ]; then
    echo "==> [etcd-verify] ${ETCD_NAME} @ ${NODE_IP}:${CLIENT_PORT}..."
    if check_health 30 && verify_quorum; then
        echo "[+] [etcd] Healthy on 127.0.0.1:${CLIENT_PORT} and ${NODE_IP}:${CLIENT_PORT}."
        exit 0
    fi
    rescue_unhealthy
fi

echo "==> [etcd] Installing etcd (${ETCD_NAME} @ ${NODE_IP}, phase=${PHASE})..."
if ! command -v etcd >/dev/null 2>&1; then
    dnf install -y etcd >/dev/null 2>&1 || yum install -y etcd
else
    echo "--> etcd already installed."
fi
dnf install -y python3-etcd3 >/dev/null 2>&1 || dnf install -y python3-python-etcd >/dev/null 2>&1 || true
mkdir -p /etc/etcd "${DATA_DIR}"
id etcd >/dev/null 2>&1 && chown -R etcd:etcd /var/lib/etcd || chown -R etcd /var/lib/etcd 2>/dev/null || true

# --- decide cluster state ---------------------------------------------------
STATE="new"
if [ -d "${DATA_DIR}/member" ]; then
    STATE="existing"
elif [ "${VDE_FORCE_UPDATE:-0}" = "1" ]; then
    # Fresh peer joining a scale-out: an existing member is already reachable
    for peer in $(echo "${INITIAL_CLUSTER}" | tr ',' ' '); do
        peer_ip="$(echo "${peer}" | sed -E 's|.*http://([^:]+):.*|\1|')"
        if [ "${peer_ip}" != "${NODE_IP}" ] && curl -sf "http://${peer_ip}:${CLIENT_PORT}/health" >/dev/null 2>&1; then
            STATE="existing"; break
        fi
    done
fi

# --- Reconcile membership TRUOC khi ghi config ------------------------------
# QUY TAC BAT BUOC: member da co TEN thi chi duoc UPDATE peer URL, TUYET DOI
# khong member add trung ten (se tao member 'ma' khong bao gio start -> mat quorum).
PEER_CHANGED=0
member_field() { # $1=name $2=field-index(1=id,4=peerURL) -> first match
    echo "${CURRENT:-}" | awk -F',' -v n="$1" -v f="$2" '{for(i=1;i<=NF;i++) gsub(/^ +| +$/,"",$i); if ($3==n) {print $f; exit}}'
}
if systemctl is-active etcd >/dev/null 2>&1; then
    CURRENT="$(etcdctl member list 2>/dev/null || true)"
    IFS=',' read -ra MEMBERS <<< "${INITIAL_CLUSTER}"
    for m in "${MEMBERS[@]}"; do
        m_name="${m%%=*}"; m_url="${m#*=}"
        m_id="$(member_field "${m_name}" 1)"
        m_cur_url="$(member_field "${m_name}" 4)"
        if [ -n "${m_id}" ]; then
            if [ "${m_cur_url}" != "${m_url}" ]; then
                echo "--> [etcd] member ${m_name} (${m_id}) peer URL cu ${m_cur_url} -> update ${m_url}"
                etcdctl member update "${m_id}" --peer-urls="${m_url}" >/dev/null 2>&1 && PEER_CHANGED=1 || \
                    echo "[!] [etcd] member update ${m_name} that bai (cluster co the dang mat quorum)."
            fi
        elif ! echo "${CURRENT}" | grep -q "${m_url}"; then
            echo "--> [etcd scale-out] member add ${m_name} ${m_url}"
            etcdctl member add "${m_name}" --peer-urls="${m_url}" >/dev/null 2>&1 || true
            PEER_CHANGED=1
            CURRENT="$(etcdctl member list 2>/dev/null || true)"
        fi
    done
fi

# Hash config truoc khi ghi de biet co can restart hay khong
OLD_CFG_HASH="$( { cat /etc/etcd/etcd.yaml /etc/etcd/etcd.conf /etc/systemd/system/etcd.service.d/10-vde.conf 2>/dev/null || true; } | sha256sum | awk '{print $1}')"

cat << EOF > /etc/etcd/etcd.conf
ETCD_NAME="${ETCD_NAME}"
ETCD_DATA_DIR="${DATA_DIR}"
ETCD_LISTEN_PEER_URLS="http://${NODE_IP}:${PEER_PORT}"
ETCD_LISTEN_CLIENT_URLS="http://${NODE_IP}:${CLIENT_PORT},http://127.0.0.1:${CLIENT_PORT}"
ETCD_INITIAL_ADVERTISE_PEER_URLS="http://${NODE_IP}:${PEER_PORT}"
ETCD_ADVERTISE_CLIENT_URLS="http://${NODE_IP}:${CLIENT_PORT}"
ETCD_INITIAL_CLUSTER="${INITIAL_CLUSTER}"
ETCD_INITIAL_CLUSTER_TOKEN="${TOKEN}"
ETCD_INITIAL_CLUSTER_STATE="${STATE}"
ETCD_HEARTBEAT_INTERVAL=250
ETCD_ELECTION_TIMEOUT=1250
ETCD_ENABLE_V2=true
EOF
chmod 644 /etc/etcd/etcd.conf

# Mot so ban dong goi (Rocky/Fedora) doc config YAML thay vi env-file:
# ghi ca 2 dinh dang de unit nao cung nhan dung listen/advertise URLs.
cat << EOF > /etc/etcd/etcd.yaml
name: '${ETCD_NAME}'
data-dir: '${DATA_DIR}'
listen-peer-urls: 'http://${NODE_IP}:${PEER_PORT}'
listen-client-urls: 'http://${NODE_IP}:${CLIENT_PORT},http://127.0.0.1:${CLIENT_PORT}'
initial-advertise-peer-urls: 'http://${NODE_IP}:${PEER_PORT}'
advertise-client-urls: 'http://${NODE_IP}:${CLIENT_PORT}'
initial-cluster: '${INITIAL_CLUSTER}'
initial-cluster-token: '${TOKEN}'
initial-cluster-state: '${STATE}'
heartbeat-interval: 250
election-timeout: 1250
enable-v2: true
EOF
chmod 644 /etc/etcd/etcd.yaml
if id etcd >/dev/null 2>&1 && ! runuser -u etcd -- test -r /etc/etcd/etcd.yaml; then
    echo "[ERROR] etcd cannot read /etc/etcd/etcd.yaml; check its parent directory permissions (namei -l)." >&2
    exit 1
fi

# Ep unit doc dung file YAML nay bang systemd drop-in (giong cach da xu ly
# PATRONI_CONFIG_LOCATION): tranh phu thuoc ban dong goi doc conf o dau.
ETCD_BIN="$(command -v etcd || echo /usr/bin/etcd)"
mkdir -p /etc/systemd/system/etcd.service.d
cat << EOF > /etc/systemd/system/etcd.service.d/10-vde.conf
[Service]
ExecStart=
ExecStart=${ETCD_BIN} --config-file /etc/etcd/etcd.yaml
EOF
chmod 644 /etc/systemd/system/etcd.service.d/10-vde.conf
echo "--> Forced etcd config-file=/etc/etcd/etcd.yaml via systemd drop-in (${ETCD_BIN})."

NEW_CFG_HASH="$(cat /etc/etcd/etcd.yaml /etc/etcd/etcd.conf /etc/systemd/system/etcd.service.d/10-vde.conf 2>/dev/null | sha256sum | awk '{print $1}')"
CONFIG_CHANGED=0; [ "${OLD_CFG_HASH}" != "${NEW_CFG_HASH}" ] && CONFIG_CHANGED=1

systemctl daemon-reload
systemctl enable etcd >/dev/null 2>&1 || true
KEEP_RUNNING=0
if systemctl is-active etcd >/dev/null 2>&1 && [ "${STATE}" = "existing" ] && [ -d "${DATA_DIR}/member" ] \
   && [ "${PEER_CHANGED}" != "1" ] && [ "${CONFIG_CHANGED}" != "1" ]; then
    KEEP_RUNNING=1
    echo "--> etcd already running, config unchanged; keeping process."
fi

if [ "${PHASE}" = "configure" ]; then
    # Fresh HA members cannot be healthy until the rest of the cohort has started.
    if [ "${KEEP_RUNNING}" != "1" ]; then
        if [ "${STATE}" = "new" ]; then
            systemctl start --no-block etcd >/dev/null 2>&1 || systemctl start --no-block etcd || true
        else
            systemctl restart etcd >/dev/null 2>&1 || systemctl start etcd || true
        fi
    fi
    echo "[+] [etcd] Phase configure done on ${NODE_IP} (state=${STATE}). Health is checked after every member starts."
    exit 0
fi

if [ "${KEEP_RUNNING}" != "1" ]; then
    systemctl restart etcd >/dev/null 2>&1 || systemctl start etcd || true
fi

if check_health 15; then
    echo "[+] [etcd] Healthy on 127.0.0.1:${CLIENT_PORT} and ${NODE_IP}:${CLIENT_PORT} (state=${STATE})."
    exit 0
fi
rescue_unhealthy
