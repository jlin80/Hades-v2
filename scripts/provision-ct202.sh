#!/usr/bin/env bash
# Provision CT 202 on pve2 and bring Hades V2 up.
#
# Run ON pve2 as root, after pushing the code tarball:
#
#   # from the workstation
#   scp hades-v2.tgz provision-ct202.sh pve2:/root/
#   ssh pve2 'bash /root/provision-ct202.sh'
#
# Idempotent-ish: refuses to touch CT 202 if it already exists, so a re-run
# after a failure does not silently destroy a half-built container. Delete it
# yourself if that is what you want.
#
# Why a tarball instead of `git clone`: the repo is private, and a clone inside
# the container would need a deploy key living in the container. Pushing the code
# means no credential ever enters it.

set -euo pipefail

CTID=202
HOSTNAME=hades-v2
IP=192.168.100.42/24
GATEWAY=192.168.100.1
NAMESERVER=192.168.100.20
TEMPLATE=local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst
TARBALL=/root/hades-v2.tgz
APP_DIR=/opt/hades-v2

say() { printf '\n=== %s\n' "$1"; }

# --- guards ------------------------------------------------------------------

if pct status "$CTID" >/dev/null 2>&1; then
  echo "CT $CTID already exists. Refusing to touch it." >&2
  echo "Destroy it deliberately first: pct stop $CTID && pct destroy $CTID" >&2
  exit 1
fi

if [[ ! -f "$TARBALL" ]]; then
  echo "Missing $TARBALL. Copy the code across first." >&2
  exit 1
fi

if ping -c 2 -W 2 "${IP%%/*}" >/dev/null 2>&1; then
  echo "WARNING: ${IP%%/*} already answers ping. Aborting to avoid an IP clash." >&2
  exit 1
fi

# --- container ---------------------------------------------------------------
# 2 GB is what the workload needs (postgres + api, ~1.5 GB measured estimate).
# pve2 has ~3.9 GB available, so this leaves real headroom rather than betting
# that Seafile stays idle.
#
# nesting=1 and keyctl=1 are required for Docker inside an unprivileged LXC.

say "creating CT $CTID"
pct create "$CTID" "$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores 2 \
  --memory 2048 \
  --swap 512 \
  --rootfs local-zfs:24 \
  --net0 "name=eth0,bridge=vmbr0,ip=$IP,gw=$GATEWAY" \
  --nameserver "$NAMESERVER" \
  --unprivileged 1 \
  --features nesting=1,keyctl=1 \
  --onboot 1 \
  --ssh-public-keys /root/.ssh/authorized_keys

pct start "$CTID"
say "waiting for network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

say "installing base packages"
pct exec "$CTID" -- bash -lc 'apt-get update -qq && apt-get install -y -qq curl git ca-certificates >/dev/null'

say "installing Docker"
pct exec "$CTID" -- bash -lc 'curl -fsSL https://get.docker.com | sh >/dev/null 2>&1'

# MANDATORY. Docker's default overlay2 driver hangs indefinitely at "exporting
# to image" when it sits on a ZFS rootfs inside an unprivileged LXC. This bit
# Hermes on its first build; vfs is slower per layer but does not hang.
say "forcing the vfs storage driver (ZFS + unprivileged LXC bug)"
pct exec "$CTID" -- bash -lc 'echo "{\"storage-driver\":\"vfs\"}" > /etc/docker/daemon.json && systemctl restart docker && sleep 5 && docker info --format "{{.Driver}}"'

say "pushing the code"
pct exec "$CTID" -- bash -lc "mkdir -p $APP_DIR"
pct push "$CTID" "$TARBALL" /tmp/hades-v2.tgz
pct exec "$CTID" -- bash -lc "tar xzf /tmp/hades-v2.tgz -C $APP_DIR && rm /tmp/hades-v2.tgz"

# The password is generated in the container and never printed. It only has to
# be known by the compose stack that uses it.
say "writing .env"
pct exec "$CTID" -- bash -lc "cd $APP_DIR && \
  PW=\$(openssl rand -hex 24) && \
  sed -e \"s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=\$PW|\" \
      -e \"s|^HADES_DATABASE_URL=.*|HADES_DATABASE_URL=postgresql+asyncpg://hades:\$PW@postgres:5432/hades|\" \
      -e 's|^HADES_ENVIRONMENT=.*|HADES_ENVIRONMENT=homelab|' \
      -e 's|^HADES_DISCOVERY_ENABLED=.*|HADES_DISCOVERY_ENABLED=true|' \
      -e 's|^HADES_TRACKING_ENABLED=.*|HADES_TRACKING_ENABLED=true|' \
      .env.example > .env && chmod 600 .env && \
  grep -c . .env"

# The Bobcat CPU (AMD G-T56N) has no AVX/AVX2/SSE4.2. Wheels built for a newer
# baseline die with SIGILL. Hades V2 installs no ML packages, but pydantic-core
# and asyncpg still ship native code -- so this is verified, not assumed.
say "anti-SIGILL smoke test"
pct exec "$CTID" -- bash -lc "cd $APP_DIR && docker compose build api >/dev/null 2>&1 && \
  docker compose run --rm api python -c 'import pydantic_core, asyncpg, fastapi, sqlalchemy, websockets; print(\"native imports OK\")'"

say "starting the stack"
pct exec "$CTID" -- bash -lc "cd $APP_DIR && docker compose up -d"

say "waiting for /health"
for _ in $(seq 1 40); do
  if pct exec "$CTID" -- bash -lc 'curl -fsS http://localhost:8000/health' >/dev/null 2>&1; then
    break
  fi
  sleep 3
done

say "result"
pct exec "$CTID" -- bash -lc 'curl -fsS http://localhost:8000/status || echo "API not answering"'
echo
echo "Discovery and tracking are ENABLED in this .env -- the container starts collecting"
echo "immediately. That is deliberate: deploying is the act that turns it on."
echo
echo "  http://${IP%%/*}:8000/status"
echo
echo "Still to do by hand:"
echo "  - add CT $CTID to the daily vzdump job (storage backups-nfs)"
echo "  - point CT103's prometheus.yml at ${IP%%/*}:9100 when metrics exist"
