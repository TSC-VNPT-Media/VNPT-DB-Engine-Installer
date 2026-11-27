#!/bin/bash
set -euo pipefail
# local_repo: VNPT DB Engine RPM repo (default) or Percona repo. Idempotent.
PACKAGE_SOURCE="${VDE_PACKAGE_SOURCE:-vnpt}"
PG_VERSION="${VDE_PG_VERSION:-16}"

if [ "${PACKAGE_SOURCE}" = "percona" ]; then
    echo "==> [Repo] Percona PostgreSQL repository (ppg-${PG_VERSION})..."
    if ! rpm -q percona-release >/dev/null 2>&1; then
        dnf install -y https://repo.percona.com/yum/percona-release-latest.noarch.rpm >/dev/null 2>&1 || \
        yum install -y https://repo.percona.com/yum/percona-release-latest.noarch.rpm
    else
        echo "--> percona-release already installed, skipping download."
    fi
    if command -v percona-release >/dev/null 2>&1; then
        # setup/enable are idempotent; ignore 'already enabled' noise
        percona-release setup -y "ppg-${PG_VERSION}" >/dev/null 2>&1 || true
    fi
    if command -v dnf >/dev/null 2>&1; then
        dnf module disable -y postgresql >/dev/null 2>&1 || true
    fi
    dnf makecache >/dev/null 2>&1 || yum makecache >/dev/null 2>&1 || true
    echo "[+] [Repo] Percona PPG-${PG_VERSION} ready."
else
    echo "==> [Repo] VNPT DB Engine internal repository..."
    RELEASE_RPM_URL="${VDE_RELEASE_RPM_URL:-http://dbengine-repo.ttpmhcm.com/vnpt-db-engine-release.rpm}"
    if ! rpm -q vnpt-db-engine-release >/dev/null 2>&1; then
        echo "--> Installing VNPT DB Engine release RPM (${RELEASE_RPM_URL})..."
        dnf install -y "${RELEASE_RPM_URL}" >/dev/null 2>&1 || \
        yum install -y "${RELEASE_RPM_URL}" >/dev/null 2>&1 || \
        rpm -Uvh "${RELEASE_RPM_URL}" >/dev/null 2>&1 || {
            echo "[!] Fallback: creating /etc/yum.repos.d/vnpt-db-engine.repo manually..."
            REPO_URL="${VDE_REPO_URL:-http://dbengine-repo.ttpmhcm.com/vnpt-db-engine/el\$releasever/\$basearch}"
            GPG_KEY_URL="${VDE_GPG_KEY_URL:-http://dbengine-repo.ttpmhcm.com/RPM-GPG-KEYS/RPM-GPG-KEY-VNPT-DB-ENGINE}"
            cat << EOF > /etc/yum.repos.d/vnpt-db-engine.repo
[vnpt-db-engine]
name=VNPT DB Engine Repository - EL\$releasever
baseurl=${REPO_URL}
enabled=1
gpgcheck=1
gpgkey=${GPG_KEY_URL}
metadata_expire=1h
EOF
            if command -v curl >/dev/null 2>&1; then
                curl -fsSL "${GPG_KEY_URL}" -o /tmp/rpm-gpg-key-vnpt 2>/dev/null && rpm --import /tmp/rpm-gpg-key-vnpt 2>/dev/null || true
            fi
        }
    else
        echo "--> vnpt-db-engine-release already installed, skipping download."
    fi
    if command -v dnf >/dev/null 2>&1; then
        dnf module disable -y postgresql >/dev/null 2>&1 || true
    fi
    dnf clean all >/dev/null 2>&1 || yum clean all >/dev/null 2>&1 || true
    dnf makecache >/dev/null 2>&1 || yum makecache >/dev/null 2>&1 || true
    echo "[+] [Repo] VNPT DB Engine repository configured."
fi
