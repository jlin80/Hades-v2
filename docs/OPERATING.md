# Operating

## Environments

| Environment | Location | Purpose |
|---|---|---|
| Workstation | `C:\Users\jinlh\OneDrive\Documents\Projects\Hadesv2` | Editing, lint, type check, unit tests |
| CT 204 `hades-v2` | `192.168.100.44`, Proxmox host `pve2` | Docker, compose stack, integration tests |

Docker is not installed on the workstation, so the compose stack is built and
validated on CT 204. `ssh hades-v2` is configured in `~/.ssh/config`.

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

The quality gate. All four must pass before a phase is considered done:

```bash
ruff check .
ruff format --check .
mypy src
pytest
```

Integration tests are skipped unless a database is reachable:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://hades:PASSWORD@127.0.0.1:5432/hades pytest -m integration
```

## Running the stack

```bash
cp .env.example .env      # then set a real POSTGRES_PASSWORD
docker compose up -d --build
```

Start order is enforced, not hoped for: PostgreSQL must pass its healthcheck
before `migrate` runs, and `migrate` must exit successfully before `api` starts.
The API can therefore never serve traffic against an un-migrated schema.

Verify it is genuinely working — not merely running:

```bash
curl -s localhost:8000/status | python -m json.tool
```

`"database": {"connected": true, "migration_revision": "0001"}` means the whole
chain works. A process that starts but cannot reach its database reports
`unhealthy` and returns HTTP 503; it does not pretend to be fine.

## Provisioning a container from scratch

On the Proxmox host:

```bash
pct create 204 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname hades-v2 \
  --cores 2 --memory 2048 --swap 512 \
  --rootfs local-zfs:32 \
  --net0 name=eth0,bridge=vmbr0,firewall=1,gw=192.168.100.1,ip=192.168.100.44/24,type=veth \
  --nameserver '192.168.100.20 1.1.1.1' \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 --onboot 1 \
  --ssh-public-keys /tmp/hades-v2.pub \
  --start 1
```

`nesting=1` and `keyctl=1` are required for Docker inside an unprivileged LXC.

Then install Docker:

```bash
pct push 204 scripts/provision-ct.sh /root/provision-ct.sh
pct exec 204 -- bash /root/provision-ct.sh
```

The script writes `/etc/docker/daemon.json` before installing Docker and then
asserts the storage driver is `overlay2`, failing loudly otherwise. This is not
optional bookkeeping: Docker in LXC on ZFS silently falls back to `vfs`, which
copies every image layer in full instead of sharing it. On the v1 container that
turned ~3.7 GB of logical images into 32 GB on disk, filled the root filesystem,
and shut PostgreSQL down. Recovery meant pruning 70 orphaned layers.

## Deploying

```bash
ssh hades-v2 "cd /opt/hades-v2 && git pull --ff-only && docker compose up -d --build"
```

After deploying, confirm the deployed revision is the one you expect:

```bash
ssh hades-v2 "cd /opt/hades-v2 && git rev-parse --short HEAD && docker compose ps"
curl -s http://192.168.100.44:8000/status | python -m json.tool
```

Check the deployed commit every time. Several v1 fixes were committed, believed
to be live, and were never actually deployed — including the final one.

## Debugging

```bash
# Logs. One JSON object per line, one format for the whole process.
ssh hades-v2 "cd /opt/hades-v2 && docker compose logs -f api"

# Filter structured logs by event
ssh hades-v2 "cd /opt/hades-v2 && docker compose logs api | grep database_probe_failed"

# Database shell
ssh hades-v2 "cd /opt/hades-v2 && docker compose exec postgres psql -U hades -d hades"

# Migration state
ssh hades-v2 "cd /opt/hades-v2 && docker compose run --rm migrate alembic current"

# Disk. Watch this; it is what killed the v1 container.
ssh hades-v2 "df -h / && docker system df"
```

PostgreSQL is published on `127.0.0.1` only. To reach it with a local client:

```bash
ssh -L 5432:127.0.0.1:5432 hades-v2
```

## Migrations

```bash
# Create a revision after adding or changing a model
alembic revision --autogenerate -m "add tokens table"

# Review the generated file before applying it. Always.
alembic upgrade head
alembic downgrade -1
```

New model modules must be imported in `migrations/env.py`, otherwise
autogenerate silently produces an empty migration.

## Things not to do

**Do not create `docker-compose.override.yml` on a deployed host.** Compose
loads it automatically. On v1 an override quietly ran production with
`--reload`, mounted source, `HADES_ENV=development` and published database
ports, and went unnoticed. The file is gitignored to keep it a deliberate,
local-only act.

**Do not let a credential reach a log line.** v1 leaked a Helius API key into
container logs because httpx logs full URLs including query parameters, and that
log stream was rendered in the dashboard. `Settings.database_url_safe` exists
for this reason; when Phase 1 adds an HTTP client, its request logging must
redact query strings.

**Do not add a service to `docker-compose.yml` without a demonstrated need.**
No Redis, no Kafka, no Prometheus, no Grafana until something concrete requires
it.
