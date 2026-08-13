#!/usr/bin/env bash
#
# Run the full test suite, including integration tests, against the running
# compose stack's real PostgreSQL.
#
# Uses a throwaway container on the stack's network so the host needs no Python
# toolchain and nothing is installed into the deployment. The repository is
# mounted read-only and copied, so the working tree is never modified.
#
# Usage:
#   ./scripts/run-tests-against-stack.sh

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# shellcheck disable=SC1091
set -a; . ./.env; set +a

network="$(docker compose ps --format '{{.Name}}' >/dev/null 2>&1 && docker network ls --format '{{.Name}}' | grep -m1 -E '^hades-v2_default$' || true)"
if [ -z "$network" ]; then
    echo "FATAL: the hades-v2_default network is not present. Is the stack up?" >&2
    exit 1
fi

dsn="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"

docker run --rm \
    --network "$network" \
    -v "${root}:/src:ro" \
    -e "TEST_DATABASE_URL=${dsn}" \
    python:3.12-slim-bookworm \
    bash -c '
        set -euo pipefail
        mkdir -p /work
        cp -a /src/. /work/
        cd /work
        rm -rf .venv
        pip install -q ".[dev]"
        pytest -v
    '
