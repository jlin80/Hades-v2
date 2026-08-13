# Deployment — homelab node 2 (pve2)

> **Status: planned, not created.** Phase 0 has been validated natively on Windows
> with Python 3.12. Nothing has been deployed.

## CT 202 — reclaimed from Hermes

**Decided 2026-08-13:** Hades V2 takes **CT 202 / `192.168.100.42`**. Hermes (the
Polymarket 5-min paper copilot, `/opt/poly`) is **deliberately decommissioned** to
free the slot. Hades V1 stays on CT 203 during the overlap.

### 🔴 Back Hermes up before destroying anything

Hermes was **never added to the daily vzdump job** — that gap has been open since
July. There is no automatic restore point. `pct destroy 202` without the steps below
is unrecoverable.

```bash
# On pve2, BEFORE touching CT 202.
pct exec 202 -- docker compose -f /opt/poly/docker-compose.yml down
pct exec 202 -- tar czf /root/hermes-final-$(date +%F).tgz /opt/poly

# The SQLite data lives in the `poly_data` docker volume, not in /opt/poly —
# a tarball of the project directory alone does NOT contain the trade history.
pct exec 202 -- docker run --rm -v poly_data:/data -v /root:/backup alpine \
  tar czf /backup/hermes-poly_data-$(date +%F).tgz -C /data .

# Pull both off the CT and onto the NFS backup dataset before it is destroyed.
pct pull 202 /root/hermes-final-$(date +%F).tgz /rpool/backups/hermes-final.tgz
pct pull 202 /root/hermes-poly_data-$(date +%F).tgz /rpool/backups/hermes-poly_data.tgz

# Verify both archives are non-empty and listable, THEN:
pct stop 202 && pct destroy 202
```

Also: remove the Hermes cron entry (it lives in the CT's root crontab, so it goes
with the container) and drop the Discord webhook target if it is no longer wanted.

## Node reality

pve2 is an HP t610 with 8 GB and an AMD G-T56N (Bobcat, dual-core 1.65 GHz).

Retiring Hermes returns its 3 GB cap to the node. After the swap: ZFS ARC 2 GB +
CT201 Seafile 2 GB + CT202 Hades V2 2 GB + CT203 Hades V1 3 GB + host — still over
committed on paper, but LXC caps are not reservations and V1 will eventually be
retired too. Watch for OOM during the overlap.

V2 is much lighter than V1 by design: **two services** (postgres + api), no Redis, no
ClickHouse, no worker, no scheduler, no dashboard. Estimated ~1.5 GB in use.
**2 GB allocated** is a realistic cap — half what V1 was budgeted.

### Two hardware constraints that are not optional

1. **SIGILL on Bobcat.** No AVX/AVX2/SSE4.2. Wheels built for a newer baseline
   crash with `Illegal instruction` (`exitCode 132`) — this killed Hermes'
   `@libsql/client` and the QuantEngine bot's `numpy2/pyarrow`. V2's dependency list
   has no ML packages at all (see `docs/DECISIONS.md` D8), but `pydantic-core` and
   `asyncpg` still ship native code. **The smoke test below is mandatory before
   trusting a deploy.**

2. **Docker `overlay2` hangs on ZFS inside an unprivileged LXC.** Hermes' first build
   hung indefinitely at "exporting to image". `vfs` is required.

## Steps

```bash
# On pve2 — only after the Hermes backup above is verified.
pct create 202 local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst \
  --hostname hades-v2 --cores 2 --memory 2048 \
  --rootfs local-zfs:24 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.100.42/24,gw=192.168.100.1 \
  --nameserver 192.168.100.20 \
  --unprivileged 1 --features nesting=1,keyctl=1
pct start 202
```

```bash
# Inside the CT
apt update && apt install -y curl git ca-certificates
curl -fsSL https://get.docker.com | sh

# Mandatory: overlay2 hangs on ZFS-backed unprivileged LXC
echo '{"storage-driver":"vfs"}' > /etc/docker/daemon.json
systemctl restart docker

git clone <repo> /opt/hades-v2 && cd /opt/hades-v2
cp .env.example .env    # set POSTGRES_PASSWORD and HADES_DATABASE_URL
```

```bash
# Mandatory anti-SIGILL smoke test — before trusting anything
docker compose build api
docker compose run --rm api python -c \
  "import pydantic_core, asyncpg, fastapi, sqlalchemy; print('OK')"
```

```bash
docker compose up -d          # migrate runs to head, then api starts
curl -s http://192.168.100.42:8000/health
```

Expected: `status: healthy`, `database.connected: true`, `trading_mode: paper`.

## Do not repeat V1's gaps

- **Add the CT to the daily vzdump job** (`backups-nfs`) — first thing, not later.
  Hermes' missing backup is the reason its decommission needed a manual archival
  dance. Do not inherit that.
- **Do not deploy Prometheus/Grafana here.** They already run on CT103 — point that
  Prometheus at this CT when metrics exist. V1's observability profile was never
  brought up, which is why nobody read the histograms that eventually explained the
  36-second dashboard query.
- Use `overlay2` if the ZFS/LXC bug is ever fixed; `vfs` copies every layer and it is
  what filled Hermes' rootfs and took down `pvestatd` on pve2.
