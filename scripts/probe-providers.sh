#!/usr/bin/env bash
#
# Probe candidate Solana data providers against their live endpoints.
#
# This exists because task.md §20 forbids assuming how an API responds, and
# because Hades v1 broke silently when Jupiter retired quote-api.jup.ag. No
# adapter is written until this script shows what a provider actually returns.
#
# Read-only: issues GET requests and prints what came back. Changes nothing.
#
# Usage:
#   ./scripts/probe-providers.sh

set -uo pipefail

USER_AGENT="hades-v2-probe/0.1 (provider evaluation)"
TIMEOUT=15

probe() {
    local label="$1" url="$2"
    printf '\n--------------------------------------------------------------\n'
    printf '%s\n%s\n' "$label" "$url"

    local body_file status elapsed size
    body_file="$(mktemp)"

    read -r status elapsed size < <(
        curl -sS -o "$body_file" \
            -w '%{http_code} %{time_total} %{size_download}\n' \
            -A "$USER_AGENT" \
            --max-time "$TIMEOUT" \
            "$url" 2>/dev/null
    ) || { printf '  REQUEST FAILED (curl error)\n'; rm -f "$body_file"; return; }

    printf '  http=%s  time=%ss  bytes=%s\n' "$status" "$elapsed" "$size"

    if [ "$status" != "200" ]; then
        printf '  body (first 200 chars): %s\n' "$(head -c 200 "$body_file")"
        rm -f "$body_file"
        return
    fi

    python3 - "$body_file" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
    raw = handle.read()

try:
    data = json.loads(raw)
except json.JSONDecodeError as exc:
    print(f"  NOT JSON: {exc}")
    print(f"  first 200 chars: {raw[:200]!r}")
    sys.exit()

def describe(value, indent="  "):
    if isinstance(value, dict):
        print(f"{indent}object, {len(value)} keys: {sorted(value)[:14]}")
    elif isinstance(value, list):
        print(f"{indent}array, {len(value)} items")
        if value and isinstance(value[0], dict):
            print(f"{indent}item keys: {sorted(value[0])[:20]}")
    else:
        print(f"{indent}scalar: {type(value).__name__}")

describe(data)

# Drill one level into the most likely payload container.
if isinstance(data, dict):
    for key in ("data", "pairs", "results", "tokens", "coins"):
        if key in data:
            print(f"  ['{key}'] ->")
            describe(data[key], indent="    ")
            inner = data[key]
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                print("  first item sample:")
                print(json.dumps(inner[0], indent=2)[:1200])
            break
elif isinstance(data, list) and data:
    print("  first item sample:")
    print(json.dumps(data[0], indent=2)[:1200])
PY

    rm -f "$body_file"
}

echo "Probing candidate providers at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

probe "GeckoTerminal — new Solana pools" \
    "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1"

probe "GeckoTerminal — trending Solana pools" \
    "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1"

probe "DexScreener — latest token profiles" \
    "https://api.dexscreener.com/token-profiles/latest/v1"

probe "DexScreener — search SOL pairs" \
    "https://api.dexscreener.com/latest/dex/search?q=SOL"

probe "Pump.fun — frontend api, newest coins" \
    "https://frontend-api.pump.fun/coins?sort=created_timestamp&order=DESC&limit=5"

probe "Pump.fun — frontend api v3, newest coins" \
    "https://frontend-api-v3.pump.fun/coins?sort=created_timestamp&order=DESC&limit=5"

probe "Jupiter — new tokens (lite-api)" \
    "https://lite-api.jup.ag/tokens/v1/new"

probe "Jupiter — token list tagged new" \
    "https://tokens.jup.ag/tokens?tags=new"

printf '\n--------------------------------------------------------------\nDone.\n'
