# Data sources — Phase 1

Every number in this document was measured on **2026-08-17** by the scripts in
`scripts/`, against live endpoints, from this machine. Nothing here is quoted from a
provider's marketing page.

Reproduce with:

```bash
.venv/Scripts/python scripts/probe_data_sources.py
.venv/Scripts/python scripts/probe_pumpportal_ws.py
```

Phase 1 exists because Hades V1 shipped adapters for `jupiter` and `meteora` that
returned **404** in production. So the rule for this document is: an endpoint is
usable when it has been called and answered, not when it is documented.

---

## Selection

| Role | Source | Why |
|---|---|---|
| **PRIMARY** | **pump.fun `frontend-api-v3`** | The only source with data from **t=0**, and the only one exposing bonding-curve reserves — from which price, liquidity and curve progress are *derived exactly and reproducibly* rather than taken on trust. |
| **DISCOVERY** | **PumpPortal WebSocket**, `subscribeNewToken` | Push, sub-second, free, keyless. Removes polling latency from the one measurement where latency is the whole point. |
| **FALLBACK** | **DexScreener** | Documented, 300 req/min, fastest measured (p50 10 ms), zero pushback under burst. Blind for the first ~1–2 minutes, so it is a *cross-check and continuity* source, not a substitute. |

Per spec §6, exactly one primary and one fallback are wired. Everything else below is
documented and deliberately **not** integrated.

---

## Measured results

Latency: 5 sequential samples per endpoint. Burst: 20 concurrent requests.

| Source | Status | p50 | worst | burst ok/429/err | wanted metrics |
|---|---|---|---|---|---|
| pump.fun `/coins/{mint}` | 200 | 91 ms | 471 ms | 20/0/0 | see below |
| pump.fun `/coins?sort=created_timestamp` | 200 | 92 ms | 158 ms | 20/0/0 | discovery |
| DexScreener `token-pairs/v1` | 200 | 10 ms | 176 ms | 20/0/0 | 6/13 |
| DexScreener `latest/dex/tokens` | 200 | 10 ms | 86 ms | 20/0/0 | 6/13 |
| GeckoTerminal `new_pools` | 200 | 12 ms | 13 ms | **0/20/0** | 9/13 |
| GeckoTerminal `tokens/{m}/pools` | **429** | 9 ms | — | — | — |
| Solana public RPC `/health` | 200 | 95 ms | 526 ms | 20/0/0 | none (liveness only) |

**Sustained rate, pump.fun:** 60 requests at ~1.64 req/s over 37 s → **60/60 HTTP 200,
zero 429**, p50 90 ms, p95 119 ms, max 537 ms.

---

## The finding that decided it: coverage vs. token age

60 unique mints sampled from pump.fun across five offsets, then queried on DexScreener:

| Age bucket | Has DexScreener data | Sampled | Coverage |
|---|---|---|---|
| **0–1 min** | **0** | 12 | **0 %** |
| 1–5 min | 12 | 12 | 100 % |
| 5–30 min | 24 | 24 | 100 % |
| 30–120 min | 12 | 12 | 100 % |

Twelve of twelve tokens under 40 seconds old returned **zero pairs**. The youngest
token with data was 138 s old.

This is disqualifying for DexScreener as primary, and it is not a reliability problem —
it is an indexing lag, and it lands precisely on the window this whole system exists to
study. Spec §8 asks for 10-second snapshots for the first 300 seconds. A source that
has nothing to say for the first ~2 minutes cannot serve that.

GeckoTerminal, checked on the five newest mints: **404 at 7 s, then 200 with 1 pool at
20, 20, 25 and 26 s.** Materially better early coverage than DexScreener — and still
ruled out, for the reason in the next section.

---

## PRIMARY — pump.fun `frontend-api-v3`

```
Base            https://frontend-api-v3.pump.fun
Discovery       GET /coins?offset=0&limit=N&sort=created_timestamp&order=DESC&includeNsfw=true
Snapshot        GET /coins/{mint}
Auth            none
Rate limit      unpublished; measured 1.64 req/s sustained and 20 concurrent, both clean
Latency         p50 90 ms, p95 119 ms
Cost            free
```

### What it returns

Confirmed against a live 60-second-old mint. The full field set:

`mint · name · symbol · creator · created_timestamp · last_trade_timestamp ·
market_cap · market_cap_quote · market_cap_usd · usd_market_cap · ath_market_cap ·
ath_market_cap_timestamp · virtual_sol_reserves · virtual_token_reserves ·
real_sol_reserves · real_token_reserves · virtual_quote_reserves · real_quote_reserves ·
total_supply · base_decimals · quote_decimals · quote_mint · complete ·
bonding_curve · associated_bonding_curve · pool_address · program · protocol ·
token_program · chain_id · reply_count · nsfw · is_banned · verified · initialized ·
is_currently_live · description · image_uri · metadata_uri · twitter · updated_at`

Timestamps are **milliseconds**; `updated_at` is **seconds**. That inconsistency is in
the provider, and the normalizer must handle it explicitly rather than guess by
magnitude.

### Why the reserves matter more than any provider's price

`virtual_sol_reserves` and `virtual_token_reserves` are the bonding curve's actual
state. From them, price, market cap, liquidity and curve progress are **computed by us,
from a documented formula, identically every time**. A provider-supplied `priceUsd` is
a number we would have to trust and could never reproduce from stored data.

This matters for spec §11 (immutable T0 snapshot): storing raw reserves means a feature
recomputed a month later gives the same answer. Storing someone's derived price does not.

It also resolves liquidity, which for a pre-graduation token has no AMM pool at all:
`real_sol_reserves` **is** the liquidity, and it is the correct definition here.

### Risks, stated plainly

- **Undocumented and unofficial.** No contract, no changelog, no deprecation notice.
  It can change shape or start refusing us tomorrow.
- **Cloudflare-fronted** (`server: cloudflare` confirmed in response headers). It did
  not challenge us at the rates tested, and that is not a guarantee at higher rates or
  from a datacenter IP — the homelab is residential, which likely helps.
- **This is the single largest technical risk in the project.** The mitigation is that
  the fallback is already wired and the schema validated, so a break degrades coverage
  instead of stopping collection. It is not a mitigation that survives the primary
  disappearing permanently; if that happens, Phase 1 reopens.

---

## DISCOVERY — PumpPortal WebSocket

```
Endpoint        wss://pumpportal.fun/api/data
Subscribe       {"method": "subscribeNewToken"}
Auth            none for this method
Cost            free
```

Measured: **11 token-creation events in 45 s (~0.24/s)**, one non-token frame. A pushed
mint was cross-checked against pump.fun `/coins/{mint}` and confirmed to exist with a
matching `created_timestamp`.

Event fields: `mint · name · symbol · traderPublicKey · signature · txType · pool ·
marketCapSol · initialBuy · solAmount · vSolInBondingCurve · vTokensInBondingCurve ·
bondingCurveKey · uri · is_mayhem_mode`

**~0.24 creations/s is ~21,000 tokens/day.** For context, V1's scanner recorded 2,122
pumpfun tokens in 24 h — after a `≥ $5,000` liquidity filter. The order of magnitude is
consistent, and it sets the real scale of the problem: **the constraint is not how fast
we poll, it is how many tokens are worth tracking.** V1 learned this the hard way when
raising its poll interval 12× changed nothing, because its scanner deduplicated by mint.

### 🔴 `subscribeTokenTrade` is gated, and this costs us real features

Tested and refused:

> `'subscribeTokenTrade' and 'subscribeAccountTrade' methods are only available when
> connecting with an API key funded with at least 0.02 SOL.`

A per-trade stream carries `traderPublicKey`, which is what would let us compute
`unique_buyers`, `unique_sellers`, `buy_volume` and `sell_volume` **ourselves, exactly**.
Without it those four metrics have no free source at the cadence we need. See
"Metrics with no source" below — this is the open decision coming out of Phase 1.

Funding a wallet is a financial action and is not something this project does on its
own; it is the operator's call.

---

## FALLBACK — DexScreener

```
Base            https://api.dexscreener.com
Primary path    GET /token-pairs/v1/solana/{mint}
Legacy path     GET /latest/dex/tokens/{mint}      (still answers 200)
Auth            none
Rate limit      300 req/min on pairs endpoints (documented); 20 concurrent clean
Latency         p50 10 ms
Cost            free
```

Full payload for a live 259-second-old pump.fun pair (`dexId: "pumpfun"`):

`chainId · dexId · url · pairAddress · baseToken{address,name,symbol} ·
quoteToken{...} · priceNative · priceUsd · txns{m5,h1,h6,h24}{buys,sells} ·
volume{m5,h1,h6,h24} · priceChange{m5,h1,h6,h24} · fdv · marketCap · pairCreatedAt`

### 🔴 There is no `liquidity` field

Not null — **absent**. Confirmed on the complete payload above. Spec §13 makes
MIN_LIQUIDITY a mandatory gate on every signal, so a fallback that cannot supply it
would leave that gate unevaluable.

It is not fatal, because the primary supplies liquidity properly via
`real_sol_reserves`. But it fixes the division of labour: **DexScreener cannot be
promoted to primary without losing a risk gate**, regardless of how fast and reliable
it is. Its schema is AMM-shaped; a bonding curve has no pool for it to describe.

### What it is genuinely good for

`txns.m5.buys` / `txns.m5.sells` and `volume.m5` — trade-flow signal the primary does
not expose at all, from ~2 minutes onward. Plus an independent `priceUsd` to cross-check
our derived price, which is how a silent formula error gets caught. And post-migration
continuity, once a token graduates to PumpSwap and leaves the curve.

---

## Evaluated and deliberately not wired

| Source | Verdict |
|---|---|
| **GeckoTerminal / CoinGecko onchain** | **The only source measured to supply `unique_buyers` and `unique_sellers`** (`transactions.m5.buyers`/`.sellers`), plus buys/sells and volume at m5 — 9/13 wanted metrics, the best of any source tested, with data from ~20 s. **Rejected on rate limit only:** the burst was **0/20 successful, 20 × 429**, and `tokens/{mint}/pools` returned 429 during the plain 5-sample latency pass. Keyless is ~10–30 calls/min. Tracking hundreds of tokens at 10-second intervals needs orders of magnitude more. **Reconsider immediately if a paid CoinGecko key appears** — it is the direct answer to our missing metrics. |
| **Moralis** (`solana-gateway.moralis.io/token/mainnet/exchange/pumpfun/new`) | Documented pump.fun endpoints, requires a key. Not tested — no key. Plausible future primary alternative. |
| **Solana Tracker** (`/tokens/latest`) | Advertises sub-second new mints with pools, curve state, liquidity and a risk score. Paid. Not tested. The most complete-sounding paid option if the free primary breaks. |
| **Bitquery** | Pump.fun REST/WebSocket/gRPC/Kafka, OHLCV, curve progress, top traders. Paid. Not tested. |
| **bloXroute** `GetPumpFunNewTokensStream` | Creation stream with mint, creator, curve address. Paid/trader-oriented. Not tested. |
| **Codex.io** | Pump.fun data API. Paid. Not tested. |
| **Solana public RPC** (`api.mainnet-beta.solana.com`) | `/health` answers 200 at p50 95 ms, but it is not a data source for us: V1 measured public backups returning **429** on `getTokenLargestAccounts`, and no free provider serves that call. Reading the curve account directly would be the most authoritative path and needs a paid RPC. Not wired. |

Per spec §6, none of these get integrated "just in case". Adding a third provider needs
a named problem it solves.

---

## Metrics with no source, which stay NULL

Spec §9: *if a metric is not available, NULL. Never invent data.*

| Metric | Status |
|---|---|
| `unique_buyers`, `unique_sellers` | **No usable source.** GeckoTerminal has them but is rate-limited out; PumpPortal's trade stream needs a funded key. |
| `buy_volume`, `sell_volume` | **No usable source.** Same two blocked paths. |
| `holder_count` | **No free source found at all.** |
| `buy_count`, `sell_count`, `volume` | DexScreener, **from ~2 min onward only.** NULL before that. |

### 🔴 What this does to the EARLY MOMENTUM hypothesis

Spec §12 sketches the first hypothesis as *"buying activity accelerating + volume
increasing + sell pressure below threshold"*, and §10 lists `buyer_velocity`,
`buyer_acceleration` and `buy_volume_ratio` among the features. **Those specific
features are not computable from any free source in the 0–2 minute window** — which is
exactly the window the hypothesis is about.

What *is* computable from t=0, at 10-second cadence, from the primary alone:

- `token_age_seconds`
- `price` and `market_cap` (derived from virtual reserves)
- `liquidity` (`real_sol_reserves`)
- `bonding_curve_progress` — computable from the reserves, but it is two quantities and
  not one (tokens sold and SOL raised read 65% and 33% at the same instant), and a third
  of live tokens are not on the classic curve at all. See `DECISIONS.md` D14 and
  `scripts/probe_bonding_curve.py`.
- **net SOL flow** between consecutive snapshots — signed, so direction is known, but
  gross buys and gross sells are not separable from it
- `price_velocity`, `market_cap_velocity`, `liquidity_change`, and the second
  derivatives of each
- `time_since_last_trade` (from `last_trade_timestamp`)
- `reply_count` and its velocity — social activity, free, and V1 never used it

That is a real momentum measure and a legitimate Phase 5 hypothesis. It is **not** the
one written in §12, and pretending otherwise would be the kind of quiet substitution
this project exists to avoid. The decision below is the operator's.

---

## Open decision for Phase 2

Collection can start today on the primary + fallback above, with the four metrics above
recorded as NULL. Three ways to close the gap, in increasing cost:

1. **Start collecting now, accept NULLs.** Phase 5's hypothesis is built on
   price/mcap/liquidity/curve velocity and acceleration. Costs nothing, starts the data
   clock immediately, and the missing columns can be backfilled for *future* tokens
   later — never for past ones.
2. **Fund a PumpPortal API key (0.02 SOL).** Unlocks `subscribeTokenTrade`, which makes
   unique buyers/sellers and the volume split computable exactly, by us, from raw
   trades. Cheapest real fix. Requires the operator to move funds.
3. **Paid data plan** — CoinGecko/GeckoTerminal (direct answer, already validated as
   having the fields) or Solana Tracker (most complete). Recurring cost, and it buys
   provider aggregates rather than raw trades.

**Recommendation: (1) now, (2) alongside it.** The expensive thing is not the data, it
is the *elapsed time* — no amount of money later recovers tokens that were born while we
were still deciding. Option 2 can be added at any point without discarding anything
already collected.

### Resolved 2026-08-20 — option 3 is the wrong purchase, at any price

Measured against the live GeckoTerminal API, and the reason is granularity rather than
cost:

**Its finest aggregation bucket is five minutes.** A pool's `transactions` object offers
`m5`, `m15`, `m30`, `h1`, `h24`. The `signal_window` this system evaluates on is **30
seconds**, and the hypothesis is about tokens between 30 and 300 seconds old. A 5-minute
unique-buyer count cannot produce a 30-second feature — at t=120s the only bucket
available *is* most of the token's life. Paying more raises the request ceiling; it does
not subdivide the bucket.

**It is also blind exactly where the hypothesis lives.** Sampling tokens by age, the
youngest with a GeckoTerminal pool at all was **50 seconds old**; every token checked at
11–44 s returned no pool. So the first ~50 s of a 30–300 s window is missing outright.

**And the free tier's ceiling is real.** Requests spaced 2.2 s apart began returning
HTTP 429 after eight of them — roughly the documented ~30/min. Tracking 40 concurrent
tokens at the early-tier 10 s cadence needs **240 req/min**, an 8x gap. Any paid tier
would have to be checked against that figure, not against a nominal one.

That last point is the only one money fixes. The first two it does not.

**So: option 2, not option 3.** A funded PumpPortal key delivers *raw trades*, from which
unique buyers, unique sellers and the gross volume split are computable by us at whatever
granularity we choose — including 30 seconds — for a one-time 0.02 SOL rather than a
recurring bill. It is both cheaper and strictly more capable for this specific gap.
Reconsider option 3 only if the research question changes to one that lives on a 5-minute
scale, where the trade-off reverses.

---

## Phase 2 addendum — what running it against live sources taught us

Measured on 2026-08-17 by `scripts/run_discovery_smoke.py`, two runs of 75 s and
100 s against the real WebSocket and the real primary.

### The primary 404s on a mint the socket just announced

**49 of 51 immediate `/coins/{mint}` fetches returned 404.** PumpPortal delivers a
creation before pump.fun's own API has indexed it. The mint is real — the same address
resolves fine a little later.

This killed the original design, which fetched the authoritative `created_timestamp`
inline on every pushed mint. Deferring it to a periodic pass over rows where
`created_at IS NULL` took the same work from ~0.65 wasted req/s down to **3 attempts in
100 s**, because by then most rows have already had `created_at` filled by the poller's
own listing.

### An HTTP call inside the write path corrupted its own measurement

The inline fetch sat between a frame arriving and the row being written, so its latency
was charged to `discovered_at`. Fixed by stamping `observed_at` in the provider, at parse
time, and using that for `discovered_at`.

**Honest accounting of the size of that error:** the median moved from **2594.9 ms** to
**2404.3 ms**. So the inline fetch inflated the reading by roughly 190 ms — the mechanism
was real, the magnitude was small, and most of the 2.4 s is genuine.

### Measured discovery latency: ~2.4 s

Median of `discovered_at - created_at` over rows where both are known: **2404.3 ms**.

Read it precisely: it is the gap between *the creation timestamp pump.fun records* and
*our receipt of the PumpPortal frame*. Whether pump.fun's `created_timestamp` is the
on-chain block time or its own indexing time is **not established**, so this is not yet
"how far behind the chain we are". Establishing that needs a comparison against the
creation transaction's block time, which the `signature` we store makes possible later.

For a push feed 2.4 s is slower than one might hope. It is still far better than any
poll interval could give, and it is now measured rather than assumed.

### 🔴 Some mints never appear in pump.fun's API

One token in the 100 s run returned 404 on three consecutive backfill passes, minutes
apart. The likely explanation, unconfirmed: mints created through external launchpads —
one creation event carried `uri: https://m.rapidlaunch.io/...` — route through the pump
program and reach the PumpPortal stream, but are absent from pump.fun's own frontend
index.

Consequence for Phase 3: those tokens have **no `created_at`, so no `token_age`**, and
every early-window feature is computed from token age. They cannot be scheduled for
adaptive tracking. Options, none chosen yet:

1. Leave them with `created_at` NULL and exclude them from tracking. Honest, and loses
   whatever fraction of the universe they represent — **which has not been measured**.
2. Derive creation time from the stored creation `signature` via an RPC lookup of the
   transaction's block time. Authoritative, and needs a paid RPC.
3. Give the backfill a bounded retry budget, so the pass does not re-request an unindexed
   mint forever.

**(3) is implemented** (`docs/DECISIONS.md` D12): `tokens.backfill_attempts` is persisted,
the queue excludes tokens past the budget, and `/status` reports
`tokens_backfill_exhausted`. It was needed regardless of the others — without it the queue
accumulates permanent failures and starves the tokens whose race simply has not resolved.

**(1) and (2) remain open**, and the number that would decide between them —
what fraction of the universe these tokens are — becomes measurable as soon as collection
runs for a day: `tokens_backfill_exhausted` over `tokens_total` is exactly that fraction.

### Measured 2026-08-20, after 29h of live collection on CT202

```
tokens_total              53,253
tokens_with_created_at    53,180   (99.86%)
tokens_backfill_exhausted     54   (0.101%)
```

**0.10% of the universe permanently lacks a `created_at`**, and 0.14% lacks one at any
given moment (the difference being tokens whose retry budget is not yet spent). Over 29
hours and 53k tokens the retry budget cost the dataset one token in a thousand.

That settles it: **neither (1) nor (2) is worth building.** Both exist to rescue tokens the
primary never indexes, and the population they would rescue is a rounding error next to the
sampling this system already does deliberately — tracking admits 40 concurrent tokens out
of ~21k created per day, so roughly 98% of the universe is declined at the tracking stage
on purpose. Engineering a recovery path for 0.1% while 98% goes untracked by design would
be optimising the wrong end by three orders of magnitude.

Revisit only if the ratio moves. `tokens_backfill_exhausted` is on `/status` precisely so a
change is visible without re-deriving it, and a rise would mean pump.fun's indexing
behaviour had changed rather than that the budget was set wrong.

---

## Phase 3 addendum — what tracking measured

Measured on 2026-08-17 by `scripts/run_tracking_smoke.py`, a 150-second run of discovery
and tracking together against the live sources. 126 snapshots, 0 failures, 0 rate limits.

### Tracking the whole universe is impossible by two orders of magnitude

The primary sustains 1.64 req/s. Pump.fun creates 0.24–0.55 tokens/s. The spec's §8
schedule costs **434 snapshots per token per 24 hours**, so tracking everything needs
**104–239 req/s — 64x to 146x over capacity.**

What a 1 req/s budget actually buys:

| Horizon | Snapshots/token | Tokens/day |
|---|---|---|
| Full 24h schedule | 434 | ~199 |
| **First hour (default)** | **110** | **~785** |
| Early window only | 30 | ~2,880 |

The default takes the middle row: it covers every outcome horizon §15 asks for
(return_1m through return_1h) and yields ~24k observations a month.

### The honest consequence: this is a sample, and a biased one

In the 150-second run, at a capacity of 10: **`tracking_now` 10, `eligible_waiting` 85.**
We declined 89% of eligible tokens.

Admission is newest-first and one-way — once admitted, a token keeps its slot until it
ages out, because churning slots would produce many truncated series and a truncated
series cannot answer a question about the first minutes. The bias that introduces:
**admitted tokens are those created when a slot happened to be free.** That is close to
random in time, which is the best available, but it is **not a uniform sample of the
universe** and research must not treat it as one. `eligible_waiting` is in `/status`
because no later analysis recovers what we declined to observe.

### The achieved interval is not the configured one

Configured 10 s in the EARLY tier; observed deltas on a real series:

```
5.4 -> 17.8 -> 28.6 -> 39.0 -> 49.3 -> 61.5 -> 73.8 -> 85.9 -> 98.1 s
        12.4    10.8    10.4    10.3    12.2    12.3    12.1    12.2
```

**~12.2 s against a 10 s target, ~20% slow.** The interval is a lower bound: the next
snapshot is scheduled at `observed_at + interval`, and the pass that picks it up runs
every 2 s, so lateness of roughly half a pass plus a request is structural.

This is harmless *because* `observed_at` is stored on every row, so velocity features
computed from actual deltas are correct. It would be harmful to anyone who assumed a
uniform 10-second grid — so: **do not assume one.**

### 🔴 `is_stale` measures trade inactivity, not provider lag

15 of 126 snapshots (12%) came back flagged stale against a 60 s threshold. That number
does not mean what the column name suggests.

pump.fun's `updated_at` tracks when the record last *changed*, which is essentially the
last trade — measured 1 second apart from `last_trade_timestamp` on a live token. So for
a token nobody is trading, the reported data age grows while the data itself stays
perfectly accurate. Visible directly in the run: one token reported an identical price at
73.8 s, 85.9 s and 98.1 s. Nothing was stale; nothing had traded.

So on this provider these fields are an **activity** signal, not a **freshness** one.
Consequences, recorded rather than papered over:

* `seconds_since_last_trade`, derivable from the stored `last_trade_at`, is the
  unambiguous way to express what is actually being measured. No schema change needed.
* Real freshness gating — §13's "reject a decision made on stale data" — requires a
  decision with its own timestamp compared against the snapshot it used. That belongs to
  Phase 6, not here.
* Our own latency *is* measurable and is not the problem: `received_at - observed_at` was
  effectively zero throughout.

The column and threshold are kept, with their meaning documented at the model. Renaming
them is deferred rather than done blind, because the right name depends on whether Phase 4
wants inactivity as a feature — and it probably does.

---

## Rules every provider adapter must follow (spec §6)

Timeout · limited retry · exponential backoff · rate-limit handling · schema validation ·
structured error logging · simple circuit breaker if needed.

Never `except Exception: pass`. Never mark a provider healthy without a real check —
`Database.check_health()` already sets the pattern: measure, do not assume.

And one addition drawn from V1's four sessions of misattributed timeouts: **every
outbound client gets an explicit `httpx.Limits`.** V1 had none anywhere, which let each
client open 100 concurrent connections; the probe scripts here set it explicitly, and
the adapters must too.
