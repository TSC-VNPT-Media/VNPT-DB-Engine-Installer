#!/bin/bash
set -euo pipefail
# One virtual IP in front of HAProxy. Clients use this address; VRRP moves it
# when the node that holds it can no longer open the HAProxy write port.
VIP="${VDE_KEEPALIVED_VIP:?}"
PREFIX="${VDE_KEEPALIVED_PREFIX:-}"
ROUTER_ID="${VDE_KEEPALIVED_ROUTER_ID:?}"
AUTH_PASS="${VDE_KEEPALIVED_AUTH_PASS:?}"
IFACE="${VDE_KEEPALIVED_INTERFACE:-}"
PRIORITY="${VDE_KEEPALIVED_PRIORITY:?}"
STATE="${VDE_KEEPALIVED_STATE:?}"
PEERS="${VDE_KEEPALIVED_PEERS:-}"
NODE_IP="${VDE_NODE_IP:?}"
WRITE_PORT="${VDE_HAPROXY_WRITE_PORT:?}"

echo "==> [Keepalived] Virtual IP ${VIP} via ${NODE_IP} (state ${STATE}, priority ${PRIORITY})..."
if ! command -v keepalived >/dev/null 2>&1; then
    dnf install -y keepalived
else
    echo "--> keepalived already installed."
fi

if [ -z "${IFACE}" ]; then
    IFACE=$(ip -o -4 addr show | awk -v ip="${NODE_IP}" '$4 ~ "^" ip "/" {print $2; exit}')
fi
if [ -z "${IFACE}" ]; then
    echo "[ERROR] No interface owns ${NODE_IP}. Set settings.keepalived.interface."
    exit 1
fi
if [ -z "${PREFIX}" ]; then
    PREFIX=$(ip -o -4 addr show dev "${IFACE}" | awk -v ip="${NODE_IP}" '$4 ~ "^" ip "/" {split($4, a, "/"); print a[2]; exit}')
fi
PREFIX="${PREFIX:-24}"

mkdir -p /etc/keepalived
cat > /etc/keepalived/vde-check.sh << EOF
#!/bin/bash
timeout 2 bash -c 'echo >/dev/tcp/127.0.0.1/${WRITE_PORT}' >/dev/null 2>&1
EOF
chown root:root /etc/keepalived/vde-check.sh
chmod 755 /etc/keepalived/vde-check.sh

PEER_BLOCK=""
if [ -n "${PEERS}" ]; then
    PEER_BLOCK=$'    unicast_src_ip '"${NODE_IP}"$'\n    unicast_peer {\n'
    for peer in $(echo "${PEERS}" | tr ',' ' '); do
        PEER_BLOCK="${PEER_BLOCK}        ${peer}"$'\n'
    done
    PEER_BLOCK="${PEER_BLOCK}    }"$'\n'
fi

cat > /etc/keepalived/keepalived.conf << EOF
# Managed by vde-install. Clients use ${VIP} and the HAProxy write/read ports.
global_defs {
    router_id vde-${NODE_IP}
    script_user root
    enable_script_security
}
vrrp_script chk_haproxy {
    script "/etc/keepalived/vde-check.sh"
    interval 2
    timeout 2
    fall 2
    rise 2
    weight 0
}
vrrp_instance CLUSTER_VIP {
    state ${STATE}
    interface ${IFACE}
    virtual_router_id ${ROUTER_ID}
    priority ${PRIORITY}
    advert_int 1
    authentication {
        auth_type PASS
        auth_pass ${AUTH_PASS}
    }
    virtual_ipaddress {
        ${VIP}/${PREFIX}
    }
${PEER_BLOCK}    track_script {
        chk_haproxy
    }
}
EOF
chown root:root /etc/keepalived/keepalived.conf
chmod 600 /etc/keepalived/keepalived.conf

keepalived -t -f /etc/keepalived/keepalived.conf
systemctl daemon-reload
systemctl enable keepalived >/dev/null 2>&1 || true
systemctl restart keepalived
systemctl is-active --quiet keepalived
echo "[+] [Keepalived] ${VIP}/${PREFIX} on ${IFACE}. Clients connect to ${VIP}:${WRITE_PORT}."
