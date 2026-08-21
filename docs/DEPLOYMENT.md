# Deployment — homelab node 2 (pve2)

> **Status: deployed and running.** CT 202 / `192.168.100.42:8000` answers `/health` with
> `status: healthy`, `database.connected: true`, `trading_mode: paper`. As of 2026-08-20 it
> had 29h uptime and 53,253 tokens discovered.
>
> The instructions below created it; the **Operations** section at the bottom is what
> applies now.

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

---

# Operations

## Redeploy

```bash
# On CT 202.
cd /opt/hades-v2
git pull
docker compose build api
docker compose run --rm api python -c \
  "import pydantic_core, asyncpg, fastapi, sqlalchemy; print('OK')"   # anti-SIGILL, still mandatory
docker compose up -d
curl -s http://192.168.100.42:8000/status | jq '.discovery.supervision'
```

The last line is the check that matters after the supervision change: `state` should read
`running`, and `restarts` climbing over time is the signal that a loop is dying repeatedly
even though it looks alive at any given instant.

## Add the CT to the daily vzdump job (`backups-nfs`)

Hermes' missing backup is the reason its decommission needed a manual archival dance. Do
not inherit that. **This is still open** — verify before assuming it was done:

```bash
# On pve2 — does the daily job include 202?
grep -n 'vmid' /etc/pve/jobs.cfg
cat /etc/vzdump.conf
```

If 202 is absent, add it to the job's `vmid` list (Datacenter → Backup in the UI, or edit
`/etc/pve/jobs.cfg`), then prove it rather than trusting it:

```bash
vzdump 202 --storage backups-nfs --mode snapshot --compress zstd
pvesm list backups-nfs | grep 202
```

A backup job that has never produced a restorable file is not a backup.

## Prometheus on CT103

**Ready.** Both blockers are fixed:

- `/metrics` serves Prometheus text exposition, hand-rolled — no `prometheus-client`
  dependency, for D8's reasons.
- The expensive aggregates moved onto a 30-second refresh loop
  (`hades.monitoring.stats`). `/status` and `/metrics` read the last snapshot, so neither
  can trigger the `COUNT(*)` scans that made `/status` take ~14 seconds. Both report
  `stats_age_seconds` / `hades_stats_age_seconds`, because a cached number is fine and a
  cached number presented as live is not.

On CT103:

```yaml
scrape_configs:
  - job_name: hades-v2
    static_configs:
      - targets: ['192.168.100.42:8000']
```

The default 15 s interval is fine — `/metrics` touches no tables. Scraping faster than the
30 s refresh just returns the same aggregate values twice, which `hades_stats_age_seconds`
makes visible.

### Alerts worth having

| Expression | Means |
|---|---|
| `hades_loop_running == 0` | A collector is down or in backoff. |
| `increase(hades_loop_restarts[1h]) > 3` | A loop is crash-looping while looking alive at any instant. |
| `hades_snapshots_last_hour == 0` | Nothing is being collected, whatever else says. |
| `hades_stats_age_seconds > 300` | The refresher is stuck; every aggregate below it is stale. |
| `hades_database_latency_ms > 500` | Usually the host, not this container. |

The first two are the 2026-08-19 outage expressed as queries: discovery sat at
`running == 0` for 7h20m while four other loops reported healthy.

Do **not** deploy Prometheus/Grafana on CT202 — they already run on CT103, and V1's
observability profile was never brought up, which is why nobody read the histograms that
eventually explained the 36-second dashboard query.

## What to watch

| Symptom | Where | Means |
|---|---|---|
| `discovery.running: false` | `/status` | The head of the pipeline is dead; everything downstream starves behind it. |
| `discovery.supervision.restarts` climbing | `/status` | A loop is crash-looping while appearing healthy at any instant. |
| `tracking.snapshots_last_hour: 0` | `/status` | No data is being collected, whatever the `running` flags say. |
| `tracking.eligible_waiting` large | `/status` | The sample being declined for lack of capacity. No later analysis recovers it. |
| `tracking.oldest_due_seconds` growing | `/status` | The tracker is not keeping up with its own schedule. |

The first three appeared together on 2026-08-19: discovery crashed at 20:16 UTC and the
system sat idle for over seven hours with four of five components still reporting
`running: true`. That outage is why the loops now restart — see `hades/supervision.py`.
