# `server/` — IB Gateway daily-snapshot bridge

Stage-1 B1. A once-per-day Python script that connects to Interactive
Brokers Gateway (or TWS), pulls one year of daily bars for every
symbol in `universe.py`, computes 1D/5D/1M percent moves, and writes
a single JSON file that the React frontend reads at build/load time.

```
IB Gateway :7497 ──ib_insync──▶ server/ibkr_bridge.py ──▶ src/data/{YYYY-MM-DD}/snapshot.json
```

No persistent server, no WebSocket — run it, it writes a file, it exits.

---

## Data-only (no trading)

This bridge is **never** allowed to place trades. It:
- does not import `MarketOrder` / `LimitOrder` / `Order` / etc.
- connects with `readonly=True` so the IB server rejects any order request;
- monkey-patches `placeOrder` / `cancelOrder` / `modifyOrder` /
  `reqGlobalCancel` / `exerciseOptions` to raise `RuntimeError` on
  invocation.

If you ever need trading, put it in a separate file and leave this one
alone.

---

## Install

From the repository root:

```bash
cd server
pip install -r requirements.txt
```

If you're behind the mainland-China VPN:

```bash
HTTPS_PROXY=http://127.0.0.1:1235 HTTP_PROXY=http://127.0.0.1:1235 \
  pip install -r requirements.txt
```

Dependencies: `ib_insync`, `python-dotenv`. Python 3.11+ (uses `zoneinfo`
and PEP 604 type unions).

---

## Configure

The script reads `../.env.local` (same file the Vite frontend uses):

```bash
IB_HOST=127.0.0.1
IB_PORT=7497          # TWS paper. Gateway paper=4002, Gateway live=4001, TWS live=7496
IB_CLIENT_ID=11       # any int 1-999 unique across concurrent clients
```

On the IB side, before running:

1. Launch IB Gateway (or TWS) and log in.
2. **Configure → API → Settings**:
   - tick **Enable ActiveX and Socket Clients**
   - set **Socket port** to the value in `IB_PORT`
   - add `127.0.0.1` to **Trusted IPs**
   - leave **Read-Only API** enabled (the bridge expects this)

---

## Run

```bash
cd server
python ibkr_bridge.py                    # today in America/New_York
python ibkr_bridge.py --date 2026-04-20  # a specific date
python ibkr_bridge.py --dry-run          # no IB required; writes mock data
```

`--dry-run` is the fastest way to smoke-test the plumbing and give the
frontend team a `snapshot.json` in the right shape without waiting on
Gateway credentials. Values are deterministic per symbol (stable across
runs) but obviously synthetic.

Runtime with ~40 symbols and `REQUEST_SPACING_S=0.5` pacing is roughly
25 - 45 s end-to-end (dominated by IB's historical-data latency).

Exit codes:
- `0` — every symbol succeeded
- `1` — at least one symbol failed; `snapshot.json` still written with
  whatever did succeed
- `130` — interrupted by Ctrl-C

---

## Output

```
src/data/
  2026-04-20/
    snapshot.json     ← this script
    events.json       ← other agent (untouched)
    macro.json        ← other agent (untouched)
```

`snapshot.json` shape:

```json
{
  "generated_at": "2026-04-20T16:31:04-04:00",
  "date": "2026-04-20",
  "indices":       [ { "symbol": "SPX", "name": "...", "close": 5842.3, ... } ],
  "sectors":       [ { "symbol": "XLK", ... } ],
  "themes_etfs":   [ { "symbol": "SMH", ... } ],
  "themes_stocks": [ { "symbol": "NVDA", ... } ]
}
```

Per-symbol record:

```json
{
  "symbol": "XLK",
  "name": "Technology Select Sector SPDR",
  "close": 245.12,
  "prev_close": 240.08,
  "d1_pct": 2.10,
  "d5_pct": 4.30,
  "m1_pct": 8.20,
  "m3_pct": null,
  "volume": 18234500,
  "mkt_cap_bn": null,
  "last_updated": "2026-04-20T16:31:04-04:00"
}
```

Fields that are `null` mean "not available in this snapshot", not
"zero" — the frontend renders them as "N/A".

---

## 1Y close-price cache (DMA + strict 52w)

Two breadth metrics need a full rolling year of closes per ticker:
`pctAbove50dma` / `pctAbove200dma` and strict `new52wHigh` / `new52wLow`.
The 6-anchor approach in `polygon_snapshot.py` can't produce these, so we
keep a local cache at `server/cache/closes_1y.json` (gitignored, ~20 MB).

**One-time bootstrap** (takes ~10 min, hits Polygon ~252 times):

```bash
python3 server/bootstrap_cache.py                  # last 252 trading days
python3 server/bootstrap_cache.py --days 200       # shorter, for smoke tests
python3 server/bootstrap_cache.py --ref-date 2026-04-17
```

After that, `polygon_snapshot.py` incrementally appends each new trading
day on every run (reusing its own `bars_today` so typically **zero extra
API calls**), trims to the last 260 days, and writes stats into
`breadth.json`. If `server/cache/closes_1y.json` is missing, the snapshot
script still runs — it falls back to the 6-anchor 52w approximation and
leaves DMA at 0, with `new52wApprox: true` so the frontend can label it.

## Scheduling (cron, optional)

Run once per trading day around 16:30 ET, after the US close:

```cron
# crontab -e  (server's timezone must be sane; otherwise wrap with TZ=)
30 16 * * 1-5  cd /Users/future/us-stock-daily-summary/server && /usr/bin/env python ibkr_bridge.py >> ../logs/snapshot.log 2>&1
```

Keep IB Gateway auto-restart configured so the socket is up when cron
fires.

---

## Gotchas

- **Index permissions**: paper accounts often lack direct index data for
  SPX / NDX etc. The script falls back to the matching ETF proxy
  (SPY / QQQ / DIA / IWM / VIXY / TLT) and tags the `name` field with
  `(via SPY)` so it's obvious in the JSON. Check stderr for the
  `WARNING:` line.
- **clientId collisions**: if a previous run crashed without
  `disconnect()`, Gateway may still hold the clientId open for ~30 s.
  Either wait or bump `IB_CLIENT_ID` in `.env.local`.
- **Rate limiting**: if you see `pacing violation` in the logs, raise
  `REQUEST_SPACING_S` in `ibkr_bridge.py`. IB's rule of thumb is
  60 historical requests per 10 min per client.
- **mkt_cap_bn** is `null` in Stage 1 — fetching it requires a
  separate `reqFundamentalData` call, which is entitlement-gated. Add
  later if needed.
