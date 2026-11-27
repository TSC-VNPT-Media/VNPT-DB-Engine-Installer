#!/bin/bash
set -euo pipefail
DEST_MODE="${VDE_BACKUP_DEST:-local}"
STANZA="${VDE_BACKUP_STANZA:-vde_cluster}"
PGDATA="${VDE_PGDATA:-/data/postgres/16/data}"
BACKUP_DIR="${VDE_BACKUP_DIR:-/data/backup}"
echo "==> [pgBackRest] Installing/configuring stanza=${STANZA} dest=${DEST_MODE}..."
if ! command -v pgbackrest >/dev/null 2>&1; then
    # Khong nuot loi dnf: in repo dang enable + goi kha dung de thay nguyen nhan that
    echo "--> Repos enabled:"; dnf repolist 2>/dev/null | grep -iE 'vnpt|percona|ppg' || true
    echo "--> Available *pgbackrest* packages:"; dnf list --available '*pgbackrest*' 2>&1 | tail -5 || true
    if [ "${VDE_PACKAGE_SOURCE:-vnpt}" = "percona" ]; then
        if ! dnf install -y percona-pgbackrest; then
            echo "[!] Cai truc tiep that bai (thuong do thieu libssh2 - goi nay nam o EPEL tren EL9)."
            echo "--> Enable EPEL de bo sung dependency, roi thu lai ban Percona truoc..."
            dnf install -y epel-release >/dev/null 2>&1 || true
            dnf makecache >/dev/null 2>&1 || true
            if ! dnf install -y percona-pgbackrest; then
                echo "[!] Van khong duoc, fallback sang pgbackrest cua EPEL..."
                dnf install -y pgbackrest
            fi
        fi
    else
        if ! dnf install -y pgbackrest; then
            echo "[!] Cai pgbackrest truc tiep that bai, thu fallback qua EPEL hoac percona-pgbackrest..."
            dnf install -y epel-release >/dev/null 2>&1 || true
            dnf makecache >/dev/null 2>&1 || true
            dnf install -y pgbackrest || dnf install -y percona-pgbackrest
        fi
    fi
else
    echo "--> pgbackrest already installed."
fi
command -v pgbackrest >/dev/null 2>&1 || { echo "[ERROR] pgbackrest van chua co sau khi cai. Xem loi dnf o tren."; exit 1; }
pgbackrest version || true
mkdir -p /etc/pgbackrest /var/log/pgbackrest "${BACKUP_DIR}/pgbackrest" /var/spool/pgbackrest
chown -R postgres:postgres /etc/pgbackrest /var/log/pgbackrest "${BACKUP_DIR}" /var/spool/pgbackrest

if [ "${DEST_MODE}" = "s3" ]; then
    cat << EOF > /etc/pgbackrest/pgbackrest.conf
[global]
repo1-type=s3
repo1-s3-endpoint=${VDE_BACKUP_S3:-}
repo1-s3-bucket=${VDE_BACKUP_S3_BUCKET:-vde-backup}
repo1-s3-key=${VDE_BACKUP_S3_KEY:-}
repo1-s3-key-secret=${VDE_BACKUP_S3_SECRET:-}
repo1-s3-region=${VDE_BACKUP_S3_REGION:-us-east-1}
repo1-s3-uri-style=${VDE_BACKUP_S3_URI_STYLE:-path}
repo1-s3-verify-tls=${VDE_BACKUP_S3_VERIFY_TLS:-y}
repo1-path=/pgbackrest
repo1-retention-full=${VDE_BACKUP_RETENTION_FULL:-2}
repo1-retention-diff=${VDE_BACKUP_RETENTION_DIFF:-7}
compress-type=zst
compress-level=3
process-max=4
log-level-console=info
log-level-file=detail
start-fast=y
[${STANZA}]
pg1-path=${PGDATA}
pg1-port=${VDE_PG_PORT:-5432}
pg1-user=postgres
EOF
else
    cat << EOF > /etc/pgbackrest/pgbackrest.conf
[global]
repo1-path=${BACKUP_DIR}/pgbackrest
repo1-retention-full=${VDE_BACKUP_RETENTION_FULL:-2}
repo1-retention-diff=${VDE_BACKUP_RETENTION_DIFF:-7}
repo1-retention-archive=2
repo1-retention-archive-type=full
compress-type=zst
compress-level=3
process-max=2
log-level-console=info
log-level-file=detail
log-path=/var/log/pgbackrest
start-fast=y
delta=y
[${STANZA}]
pg1-path=${PGDATA}
pg1-port=${VDE_PG_PORT:-5432}
pg1-user=postgres
EOF
fi
chown postgres:postgres /etc/pgbackrest/pgbackrest.conf; chmod 640 /etc/pgbackrest/pgbackrest.conf

if [ "${VDE_PROFILE:-standalone}" = "ha" ] && [ "${DEST_MODE}" = "local" ]; then
    echo "[!] [pgBackRest] HA destination=local: moi node co kho rieng, failover khong ke thua stanza. Khuyen nghi destination: s3."
fi

# stanza-create/check only when the local PG is the running primary
REST_PORT="${VDE_PATRONI_REST_PORT:-8008}"
if curl -sf "http://127.0.0.1:${REST_PORT}/primary" >/dev/null 2>&1; then
    su - postgres -c "pgbackrest --stanza=${STANZA} stanza-create" 2>/dev/null || true
    su - postgres -c "pgbackrest --stanza=${STANZA} check" 2>/dev/null || \
        echo "[!] pgbackrest check deferred (archive/WAL not flowing yet)."
fi

# On HA every DB node has this cron file, but only the current primary runs the job.
# After failover the new primary picks the job up without rewriting cron.
if [ "${VDE_PROFILE:-standalone}" = "ha" ]; then
    RUN_IF_PRIMARY="curl -sf http://127.0.0.1:${REST_PORT}/primary >/dev/null 2>&1 && "
else
    RUN_IF_PRIMARY=""
fi
cat << EOF > /etc/cron.d/pgbackrest
# pgBackRest (${STANZA}) - VDE-Install
0 2 * * 0 postgres ${RUN_IF_PRIMARY}pgbackrest --stanza=${STANZA} --type=full backup >> /var/log/pgbackrest/cron.log 2>&1
0 2 * * 1-6 postgres ${RUN_IF_PRIMARY}pgbackrest --stanza=${STANZA} --type=diff backup >> /var/log/pgbackrest/cron.log 2>&1
0 4 * * * postgres ${RUN_IF_PRIMARY}pgbackrest --stanza=${STANZA} expire >> /var/log/pgbackrest/cron.log 2>&1
EOF
chmod 644 /etc/cron.d/pgbackrest
echo "[+] [pgBackRest] Configured (${DEST_MODE}), cron installed."
