#!/usr/bin/env bash
#
# Provision a Debian 13 LXC container to run the Hades V2 compose stack.
# Idempotent: safe to re-run.
#
# Usage (from the Proxmox host):
#   pct push <ctid> provision-ct.sh /root/provision-ct.sh
#   pct exec <ctid> -- bash /root/provision-ct.sh

set -euo pipefail

log() { printf '\n=== %s ===\n' "$1"; }

# -----------------------------------------------------------------------------
# Docker storage driver
#
# This is the single most important line in this script.
#
# Docker inside an LXC container on ZFS does not get a usable zfs or overlay2
# graph driver by default and silently falls back to `vfs`, which copies the
# entire filesystem for every layer instead of sharing them. In Hades v1 that
# turned ~3.7GB of logical images into 32GB on disk and filled the container's
# root filesystem completely; PostgreSQL shut itself down and recovery required
# pruning 70 orphaned layers to reclaim 16GB.
#
# daemon.json is written BEFORE docker is installed so the daemon never starts
# with vfs even once, and the driver is asserted at the end of this script.
# -----------------------------------------------------------------------------
configure_docker_daemon() {
    log "Configuring Docker daemon (overlay2 + log rotation)"
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json <<'JSON'
{
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
JSON
}

install_docker() {
    log "Installing Docker from the official Debian repository"
    export DEBIAN_FRONTEND=noninteractive
    . /etc/os-release

    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg git >/dev/null

    install -m 0755 -d /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.asc ]; then
        curl -fsSL https://download.docker.com/linux/debian/gpg \
            -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
    fi

    printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] %s %s stable\n' \
        "https://download.docker.com/linux/debian" "${VERSION_CODENAME}" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin >/dev/null

    systemctl enable --now docker
}

verify() {
    log "Verifying"
    docker --version
    docker compose version

    local driver
    driver="$(docker info --format '{{.Driver}}')"
    echo "storage driver: ${driver}"

    if [ "${driver}" != "overlay2" ]; then
        cat >&2 <<EOF

FATAL: Docker is using the '${driver}' storage driver, not overlay2.

'vfs' copies every image layer in full instead of sharing them. This is what
exhausted the disk on the Hades v1 container. Do not deploy on top of this:
fix the driver first, for example by giving /var/lib/docker its own ext4
volume.
EOF
        exit 1
    fi

    log "OK"
}

configure_docker_daemon
install_docker
verify
