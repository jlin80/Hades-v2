#!/usr/bin/env bash
#
# Verify a running deployment against reality rather than assumption.
#
# Checks the container states, the HTTP contract, the database contents, the
# log format and the disk. Exits non-zero on the first genuine failure so it can
# gate a deploy.
#
# Usage:
#   ./scripts/verify-deployment.sh

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# shellcheck disable=SC1091
set -a; . ./.env; set +a

base_url="http://127.0.0.1:${API_PUBLISHED_PORT:-8000}"
failures=0

section() { printf '\n=== %s ===\n' "$1"; }
pass()    { printf '  PASS  %s\n' "$1"; }
fail()    { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }

section "Containers"
docker compose ps

section "HTTP contract"
health_code="$(curl -s -o /dev/null -w '%{http_code}' "${base_url}/health")"
if [ "$health_code" = "200" ]; then
    pass "GET /health -> 200"
else
    fail "GET /health -> ${health_code} (expected 200)"
fi

status_body="$(curl -s "${base_url}/status")"
echo "$status_body" | python3 -m json.tool

if echo "$status_body" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["database"]["connected"] else 1)'; then
    pass "database reports a real connection"
else
    fail "database is not connected"
fi

revision="$(echo "$status_body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["database"]["migration_revision"])')"
if [ "$revision" != "None" ] && [ -n "$revision" ]; then
    pass "migration revision reported: ${revision}"
else
    fail "no migration revision reported"
fi

section "Database"
psql_exec() {
    docker compose exec -T postgres \
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"
}

db_revision="$(psql_exec 'SELECT version_num FROM alembic_version')"
if [ "$db_revision" = "$revision" ]; then
    pass "alembic_version in database matches /status: ${db_revision}"
else
    fail "alembic_version is '${db_revision}' but /status reports '${revision}'"
fi

echo "  tables:"
psql_exec "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename" \
    | sed 's/^/    /'

db_timezone="$(psql_exec 'SHOW timezone')"
if [ "$db_timezone" = "UTC" ]; then
    pass "database timezone is UTC"
else
    fail "database timezone is '${db_timezone}', expected UTC"
fi

section "Token discovery"
token_count="$(psql_exec 'SELECT count(*) FROM tokens')"
echo "  tokens stored: ${token_count}"

status_tokens="$(echo "$status_body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["discovery"]["tokens_discovered"])')"
if [ "$status_tokens" = "$token_count" ]; then
    pass "/status token count matches the database"
else
    fail "/status reports ${status_tokens} tokens, database holds ${token_count}"
fi

# The unique constraint is what makes discovery idempotent (task.md §13).
# Verify it exists rather than trusting the migration ran as intended.
constraint="$(psql_exec "SELECT conname FROM pg_constraint WHERE conname = 'uq_tokens_token_address'")"
if [ -n "$constraint" ]; then
    pass "unique constraint on token_address is present"
else
    fail "uq_tokens_token_address is MISSING — duplicates are possible"
fi

duplicates="$(psql_exec 'SELECT count(*) FROM (SELECT token_address FROM tokens GROUP BY token_address HAVING count(*) > 1) d')"
if [ "$duplicates" = "0" ]; then
    pass "no duplicate token addresses stored"
else
    fail "${duplicates} token addresses appear more than once"
fi

# Every stored address must be base58. A non-base58 address means validation
# was bypassed somewhere.
bad_addresses="$(psql_exec "SELECT count(*) FROM tokens WHERE token_address !~ '^[1-9A-HJ-NP-Za-km-z]{32,44}\$'")"
if [ "$bad_addresses" = "0" ]; then
    pass "every stored address is a valid base58 Solana address"
else
    fail "${bad_addresses} stored addresses are not valid base58"
fi

if [ "$token_count" != "0" ]; then
    echo "  most recent discoveries:"
    psql_exec "SELECT token_address || '  ' || coalesce(symbol,'(no symbol)') || '  age=' || coalesce((extract(epoch from (discovered_at - first_seen_at))::int)::text || 's', 'unknown') || '  via ' || discovery_provider FROM tokens ORDER BY discovered_at DESC LIMIT 5" \
        | sed 's/^/    /'
fi

section "Log format"
# Every application log line must be a single JSON object. An unparseable line
# means something is bypassing the structured pipeline.
if docker compose logs api --no-log-prefix --tail 20 \
    | grep -v '^\s*$' \
    | python3 -c '
import json, sys
for line in sys.stdin:
    json.loads(line)
' 2>/dev/null; then
    pass "all recent api log lines are valid JSON"
else
    fail "api emitted a line that is not valid JSON"
fi

if docker compose logs api --no-log-prefix | grep -q "${POSTGRES_PASSWORD}"; then
    fail "THE DATABASE PASSWORD APPEARS IN THE LOGS"
else
    pass "database password does not appear in logs"
fi

section "Storage"
driver="$(docker info --format '{{.Driver}}')"
if [ "$driver" = "overlay2" ]; then
    pass "docker storage driver is overlay2"
else
    fail "docker storage driver is '${driver}' (vfs exhausted the v1 container's disk)"
fi

df -h / | tail -1 | sed 's/^/  /'
docker system df

section "Result"
if [ "$failures" -eq 0 ]; then
    echo "All checks passed."
else
    echo "${failures} check(s) failed."
    exit 1
fi
