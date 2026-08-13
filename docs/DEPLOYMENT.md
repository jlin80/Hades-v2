# Deployment — homelab node 2 (pve2)

> **Status: planned, not created.** Phase 0 has been validated natively on Windows
> with Python 3.12. Nothing has been deployed.

## ⚠️ CT 202 is taken

The request was "put it on CT 202". **CT 202 is Hermes** (`poly`, `192.168.100.42`),
running and in active use. CT 203 is Hades V1. Taking 202 would destroy Hermes.

Free IDs on pve2: **200** (planned for a Samba share, explicitly discarded, never
created) and **204**.

**Recommendation: CT 204 / `192.168.100.44`.** It leaves V1 running on 203 during
the overlap — V2 has no data yet, and shutting down the only system currently
collecting anything to make room for one that collects nothing is a bad trade.

Using 200 also works and keeps the numbering tight. Either is fine; 202 is not.

**This is an open decision — nothing will be created until it is settled.**

## Node reality

pve2 is an HP t610 with 8 GB and an AMD G-T56N (Bobcat, dual-core 1.65 GHz).
Committed: ZFS ARC 2 GB + CT201 Seafile 2 GB + CT202 Hermes 3 GB + host. It is tight.

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
# On pve2 — CT ID pending the decision above; 204 shown.
pct create 204 local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst \
  --hostname hades-v2 --cores 2 --memory 2048 \
  --rootfs local-zfs:24 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.100.44/24,gw=192.168.100.1 \
  --nameserver 192.168.100.20 \
  --unprivileged 1 --features nesting=1,keyctl=1
pct start 204
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
curl -s http://192.168.100.44:8000/health
```

Expected: `status: healthy`, `database.connected: true`, `trading_mode: paper`.

## Do not repeat V1's gaps

- **Add the CT to the daily vzdump job** (`backups-nfs`). Hermes still has no backup;
  that gap has been open since July.
- **Do not deploy Prometheus/Grafana here.** They already run on CT103 — point that
  Prometheus at this CT when metrics exist. V1's observability profile was never
  brought up, which is why nobody read the histograms that eventually explained the
  36-second dashboard query.
- Use `overlay2` if the ZFS/LXC bug is ever fixed; `vfs` copies every layer and it is
  what filled Hermes' rootfs and took down `pvestatd` on pve2.
