#!/usr/bin/env bash
#
# Create a .env from .env.example with a freshly generated database password.
#
# Refuses to overwrite an existing .env: regenerating the password against a
# database that already has data would lock the stack out of its own volume.
#
# Usage:
#   ./scripts/bootstrap-env.sh [environment]
#
# environment defaults to "production".

set -euo pipefail

environment="${1:-production}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$root"

if [ -f .env ]; then
    echo ".env already exists; leaving it untouched." >&2
    exit 0
fi

if [ ! -f .env.example ]; then
    echo "FATAL: .env.example not found in ${root}" >&2
    exit 1
fi

password="$(openssl rand -hex 24)"

sed \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${password}|" \
    -e "s|^ENVIRONMENT=.*|ENVIRONMENT=${environment}|" \
    .env.example > .env

# The file holds a database credential; keep it readable only by its owner.
chmod 600 .env

echo "Wrote .env (environment=${environment}) with a generated POSTGRES_PASSWORD."
echo "It is gitignored. Back it up somewhere safe: losing it means losing access"
echo "to the existing database volume."
