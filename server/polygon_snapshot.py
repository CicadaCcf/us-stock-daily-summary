#!/usr/bin/env python3
"""
Polygon.io daily snapshot + screener job.

ONE run generates:
  src/data/{date}/market.json    — indices (ETF proxies) + sector ETFs
                                    + theme ETFs + theme stocks
  src/data/{date}/screener.json  — Top Movers (full-market filtered)

Strategy: 6 grouped-daily calls (one per period point: today, -5d, -22d,
-66d, -126d, -252d trading days). Each call returns all ~12k US stocks
for that day. We merge client-side and compute pct returns.

================================================================
   READ-ONLY — PANOBOARD SHARES THIS API KEY
================================================================
Rules from memory (feedback_polygon_panoboard.md):
  - Only read-only endpoints (snapshots, aggregates, reference).
  - No account/billing/webhook/alerts mutations.
  - Throttle: end-of-day batch only, never intraday polling.
  - On error log status+message only, never echo the URL with apiKey=.
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

# --- Env / config --------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
_env_file = ROOT / '.env.local'
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

API_KEY = os.environ.get('POLYGON_API_KEY')
if not API_KEY:
    print('ERROR: POLYGON_API_KEY not set in .env.local', file=sys.stderr)
    sys.exit(2)
PROXY = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')

BASE = 'https://api.polygon.io'

# Period lookback in business days.
# Keys match src/App.jsx SCREENER field names (d1/d5/m1/m3/m6/y1).
PERIODS = {
    'd1': 1,    # 1 day   = previous close → today
    'd5': 5,    # 1 week
    'm1': 22,   # 1 month (~22 trading days)
    'm3': 66,
    'm6': 126,
    'y1': 252,
}

# Indices: fetched from Yahoo Finance chart API (no key needed) because
# Polygon's /v3/snapshot/indices requires a higher tier than our shared
# panoboard key has. One Yahoo call per index → 6 calls, cheap.
# Tuples: (Yahoo symbol, display name, value kind)
#   price       — show the price directly (e.g. S&P 500 7064)
#   percent     — close is a yield × 10, show as X.XX% (e.g. TNX)
INDICES_YAHOO = [
    ('^GSPC', 'S&P 500',      'price'),
    ('^NDX',  'Nasdaq 100',   'price'),
    ('^DJI',  'Dow Jones',    'price'),
    ('^RUT',  'Russell 2000', 'price'),
    ('^VIX',  'VIX',          'price'),
    ('^TNX',  '10Y Treasury', 'yield10'),  # TNX is yield × 10 (42.8 = 4.28%)
]
SECTORS = [
    ('XLK',  '信息技术 Technology'),
    ('XLC',  '通信服务 Comm.'),
    ('XLY',  '非必需消费 Disc.'),
    ('XLI',  '工业 Industrials'),
    ('XLF',  '金融 Financials'),
    ('XLB',  '材料 Materials'),
    ('XLRE', '房地产 Real Estate'),
    ('XLU',  '公用事业 Utilities'),
    ('XLV',  '医疗健康 Healthcare'),
    ('XLE',  '能源 Energy'),
    ('XLP',  '必需消费 Staples'),
]
THEME_ETFS   = ['SMH','SOXX','ARKK','IGV','SNDK','XLK','QTUM','ICLN','URA','KWEB','FXI']
THEME_STOCKS = ['NVDA','SMCI','AVGO','PLTR','APP','AI','NTNX','PSTG','SNDK',
                'QBTS','IONQ','RGTI','VST','CEG','SMR','BABA','PDD','JD']

# Top Movers filter (see src/App.jsx screener criteria line).
SCREENER_FILTERS = {
    'min_dollar_volume': 100_000_000,  # Trading Volume ≥ $100M
    'min_d1_pct':         5.0,         # 1D Change > 5%
    'min_y1_pct':        40.0,         # 1Y Change > 40%
}
SCREENER_TOP_N = 15

# --- HTTP ---------------------------------------------------------------

def _opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY})
        )
    return urllib.request.build_opener()

def _get(path_and_query: str, timeout: int = 60) -> dict:
    """GET to the Polygon base with apiKey injected. Returns parsed JSON.

    On error raises; caller logs only status/message (never the URL with key).
    """
    sep = '&' if '?' in path_and_query else '?'
    url = f'{BASE}{path_and_query}{sep}apiKey={API_KEY}'
    opener = _opener()
    req = urllib.request.Request(url)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8')
            msg = json.loads(body).get('message', body[:200])
        except Exception:
            msg = str(e)
        raise RuntimeError(f'Polygon HTTP {e.code}: {msg}')

def _get_yahoo_chart(symbol: str, range_: str = '1y', timeout: int = 30) -> dict:
    """Yahoo Finance chart endpoint (no auth). Must spoof User-Agent."""
    q = urllib.parse.quote(symbol, safe='')
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{q}?interval=1d&range={range_}'
    opener = _opener()
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

# --- Business day helpers -----------------------------------------------

def biz_days_back(d: date, n: int) -> date:
    cur = d
    while n > 0:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            n -= 1
    return cur

def daily_series(symbol: str, from_dt: date, to_dt: date) -> 'tuple[list, list]':
    """Fetch daily OHLC bars for a single symbol. Returns (closes, volumes)."""
    path = f'/v2/aggs/ticker/{symbol}/range/1/day/{from_dt.isoformat()}/{to_dt.isoformat()}?adjusted=true&sort=asc&limit=5000'
    data = _get(path)
    results = data.get('results') or []
    closes  = [r.get('c') for r in results if r.get('c') is not None]
    volumes = [int(r.get('v', 0)) for r in results if r.get('c') is not None]
    return closes, volumes

def grouped_daily(d: date) -> dict:
    """All-stocks bars for `d`. Empty dict on holiday or weekend."""
    data = _get(f'/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}?adjusted=true')
    # status OK with empty results = holiday/weekend
    out = {}
    for r in data.get('results') or []:
        ticker = r.get('T')
        if ticker:
            out[ticker] = r
    return out

def grouped_with_fallback(target: date, label: str, max_steps: int = 5) -> tuple[date, dict]:
    """If target is a non-trading day, step backwards up to max_steps until we get bars."""
    cur = target
    for attempt in range(max_steps + 1):
        bars = grouped_daily(cur)
        if bars:
            return cur, bars
        cur -= timedelta(days=1)
    return cur, {}

# --- Main ---------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Polygon daily snapshot + screener')
    parser.add_argument('--date', help='target date for the output folder (YYYY-MM-DD). Defaults to today.')
    parser.add_argument('--ref-date', help='trading date to use as "today" for computations. Defaults to latest available.')
    args = parser.parse_args()

    out_date = args.date or date.today().isoformat()
    out_dir = ROOT / 'src' / 'data' / out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    # Reference date = most recent trading day at/before `ref-date` (or today)
    ref_anchor = date.fromisoformat(args.ref_date) if args.ref_date else date.today()
    ref_date, bars_today = grouped_with_fallback(ref_anchor, 'today')
    if not bars_today:
        print('ERROR: no trading bars found in last 5 days', file=sys.stderr)
        sys.exit(1)
    print(f'[info] reference trading day: {ref_date}  ({len(bars_today):,} tickers)')

    # Historical anchors
    period_bars = {}
    for key, ndays in PERIODS.items():
        target = biz_days_back(ref_date, ndays)
        actual, bars = grouped_with_fallback(target, key)
        period_bars[key] = bars
        print(f'[info] {key:4s} -{ndays:3d}d → {actual} ({len(bars):,} tickers)')
        time.sleep(0.15)  # gentle pacing — panoboard shares this key

    # Build unified table
    table = {}
    for ticker, bar in bars_today.items():
        close = bar.get('c')
        volume = bar.get('v')
        if close is None or close <= 0 or volume is None:
            continue
        row = {
            'symbol': ticker,
            'close':  close,
            'volume': int(volume),
            'dollar_volume': close * volume,
        }
        for key in PERIODS:
            hist = period_bars[key].get(ticker)
            if hist and hist.get('c') and hist['c'] > 0:
                row[f'{key}_pct'] = (close / hist['c'] - 1) * 100
            else:
                row[f'{key}_pct'] = None
        table[ticker] = row
    print(f'[info] built unified table: {len(table):,} tickers with valid bars')

    # ----- indices via Yahoo Finance ---------------------------------
    def fetch_yahoo_index(sym: str, kind: str) -> 'dict | None':
        try:
            d = _get_yahoo_chart(sym)
            r = d.get('chart', {}).get('result', [])
            if not r:
                return None
            meta = r[0].get('meta', {})
            closes = r[0].get('indicators', {}).get('quote', [{}])[0].get('close', []) or []
            # Last non-null close is "today"; previous non-null is "prev day"
            closes_filtered = [(i, c) for i, c in enumerate(closes) if c is not None]
            if not closes_filtered:
                return None
            price = closes_filtered[-1][1]
            # Pct move for each period: count back N trading bars (each bar is a trading day)
            def pct_back(n: int) -> 'float | None':
                if len(closes_filtered) <= n:
                    return None
                prev = closes_filtered[-1 - n][1]
                if not prev:
                    return None
                return (price / prev - 1) * 100
            # Keep last 252 non-null closes for the combined trend chart
            closes_1y = [c for _, c in closes_filtered][-252:]
            return {
                'symbol': sym.lstrip('^'),
                'kind': kind,
                'price': price,
                'meta_price': meta.get('regularMarketPrice'),
                'd1_pct': pct_back(1),
                'd5_pct': pct_back(5),
                'm1_pct': pct_back(22),
                'm3_pct': pct_back(66),
                'high_52w': meta.get('fiftyTwoWeekHigh'),
                'low_52w': meta.get('fiftyTwoWeekLow'),
                'closes_1y': closes_1y,
            }
        except Exception as e:
            print(f'[warn] yahoo {sym} failed: {e}')
            return None

    print(f'[info] fetching indices from Yahoo Finance...')
    indices_out = []
    for sym, name, kind in INDICES_YAHOO:
        y = fetch_yahoo_index(sym, kind)
        if not y:
            print(f'[warn]   {sym} missing, skipping')
            continue
        price = y['price']
        # Display value. Yahoo already returns TNX as yield directly (4.28),
        # not yield × 10, so we just round either way. `kind` is kept in
        # case we later need to distinguish formatting in the loader.
        close_display = round(price, 2)
        indices_out.append({
            'symbol': sym.lstrip('^'),
            'name': name,
            'close': close_display,
            'prev_close': None,
            'd1_pct': round(y['d1_pct'], 2) if y['d1_pct'] is not None else None,
            'd5_pct': round(y['d5_pct'], 2) if y['d5_pct'] is not None else None,
            'm1_pct': round(y['m1_pct'], 2) if y['m1_pct'] is not None else None,
            'm3_pct': round(y['m3_pct'], 2) if y['m3_pct'] is not None else None,
            'volume': None,
            'mkt_cap_bn': None,
            'last_updated': ref_date.isoformat(),
            'closes_1y': y.get('closes_1y', []),
        })
        print(f'  {sym:<7s}  {close_display:>8}  d1={y["d1_pct"]:+.2f}% d5={y["d5_pct"]:+.2f}%')
        time.sleep(0.1)

    # VIX for breadth section
    vix_level = next((x['close'] for x in indices_out if x['symbol'] == 'VIX'), None)

    # ----- market.json ------------------------------------------------
    def build_section(pairs: list) -> list:
        out = []
        for sym, name in pairs:
            r = table.get(sym)
            if not r:
                print(f'[warn]   missing bar for {sym}, skipping')
                continue
            out.append({
                'symbol': sym,
                'name': name,
                'close': round(r['close'], 2),
                'prev_close': None,
                'd1_pct': round(r['d1_pct'], 2) if r.get('d1_pct') is not None else None,
                'd5_pct': round(r['d5_pct'], 2) if r.get('d5_pct') is not None else None,
                'm1_pct': round(r['m1_pct'], 2) if r.get('m1_pct') is not None else None,
                'm3_pct': round(r['m3_pct'], 2) if r.get('m3_pct') is not None else None,
                'volume': r['volume'],
                'mkt_cap_bn': None,
                'last_updated': ref_date.isoformat(),
            })
        return out

    # Enrich each sector with 1Y daily sparkline + daily volumes for
    # the frontend period selector (1W/1M/3M/6M/1Y).
    print(f'[info] fetching 1Y daily series for {len(SECTORS)} sector ETFs...')
    from_dt = ref_date - timedelta(days=400)  # ~272 trading days; extra margin
    sectors_enriched = build_section(SECTORS)
    for s in sectors_enriched:
        try:
            closes, volumes = daily_series(s['symbol'], from_dt, ref_date)
            # Keep last 252 trading days only
            s['closes_1y']  = closes[-252:]
            s['volumes_1y'] = volumes[-252:]
            print(f'  {s["symbol"]:<5s} {len(s["closes_1y"])} bars')
            time.sleep(0.1)
        except Exception as e:
            print(f'[warn]   {s["symbol"]} series failed: {e}')
            s['closes_1y']  = []
            s['volumes_1y'] = []

    market = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'source': 'polygon.io + yahoo',
        'date': out_date,
        'ref_date': ref_date.isoformat(),
        'indices':       indices_out,
        'sectors':       sectors_enriched,
        'themes_etfs':   build_section([(s, s) for s in THEME_ETFS]),
        'themes_stocks': build_section([(s, s) for s in THEME_STOCKS]),
    }
    (out_dir / 'market.json').write_text(
        json.dumps(market, indent=2, ensure_ascii=False) + '\n'
    )
    print(f'[info] wrote {out_dir / "market.json"}')

    # ----- screener.json ---------------------------------------------
    flt = SCREENER_FILTERS
    candidates = []
    for sym, r in table.items():
        if r['dollar_volume'] < flt['min_dollar_volume']:
            continue
        if r.get('d1_pct') is None or r['d1_pct'] <= flt['min_d1_pct']:
            continue
        if r.get('y1_pct') is None or r['y1_pct'] <= flt['min_y1_pct']:
            continue
        candidates.append(r)
    # Rank by dollar volume descending (matches legacy App.jsx sort)
    candidates.sort(key=lambda x: -x['dollar_volume'])
    top = candidates[:SCREENER_TOP_N]

    screener_rows = []
    for r in top:
        screener_rows.append({
            'tk': r['symbol'],
            'nm': r['symbol'],         # placeholder — can enrich via AI later
            'd1': round(r['d1_pct'], 1),
            'd5': round(r['d5_pct'], 1) if r['d5_pct'] is not None else None,
            'm1': round(r['m1_pct'], 1) if r['m1_pct'] is not None else None,
            'm3': round(r['m3_pct'], 1) if r['m3_pct'] is not None else None,
            'm6': round(r['m6_pct'], 1) if r['m6_pct'] is not None else None,
            'y1': round(r['y1_pct'], 1) if r['y1_pct'] is not None else None,
            'vol': round(r['dollar_volume'] / 1e9, 2),  # $Bn (matches existing schema)
            'cap': None,               # mkt cap — skip for MVP
            'biz': '',                 # business desc — can enrich via AI later
        })
    screener = {
        'maxAbs': 500,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'source': 'polygon.io',
        'ref_date': ref_date.isoformat(),
        'filter': {
            'min_dollar_volume_usd': flt['min_dollar_volume'],
            'min_d1_pct': flt['min_d1_pct'],
            'min_y1_pct': flt['min_y1_pct'],
            'universe_size': len(table),
            'candidates': len(candidates),
        },
        'rows': screener_rows,
    }
    (out_dir / 'screener.json').write_text(
        json.dumps(screener, indent=2, ensure_ascii=False) + '\n'
    )
    print(f'[info] wrote {out_dir / "screener.json"} with {len(screener_rows)}/{len(candidates)} candidates')

    # ----- breadth.json ----------------------------------------------
    up = down = flat = 0
    vol_up = vol_down = 0.0
    # Approx 52w high/low: today's close > max/min of the 6 anchor period
    # closes for that ticker. Undercounts (misses peaks between anchors) but
    # directionally correct and uses data we already have. Full 252-day rolling
    # precision requires a bootstrap cache — see TODO in README.
    n_52w_high = n_52w_low = 0
    for sym, r in table.items():
        d1 = r.get('d1_pct')
        if d1 is None:
            continue
        if d1 > 0.01:
            up += 1
            vol_up += r['dollar_volume']
        elif d1 < -0.01:
            down += 1
            vol_down += r['dollar_volume']
        else:
            flat += 1
        # 52w estimate via period anchor comparison
        today_close = r['close']
        prior_closes = []
        for key in ('d5', 'm1', 'm3', 'm6', 'y1'):
            pb = period_bars[key].get(sym)
            if pb and pb.get('c') and pb['c'] > 0:
                prior_closes.append(pb['c'])
        if prior_closes:
            if today_close > max(prior_closes) * 1.001:
                n_52w_high += 1
            if today_close < min(prior_closes) * 0.999:
                n_52w_low += 1
    total_ad = up + down + flat
    total_vol = vol_up + vol_down

    def pct(n, d):
        return round(n / d * 100, 1) if d else 0

    breadth = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'source': 'polygon.io + yahoo',
        'ref_date': ref_date.isoformat(),
        'universe_size': total_ad,
        # Advance/Decline stats (real from 12k universe)
        'advancers': up,
        'decliners': down,
        'unchanged': flat,
        'adUpPct':   pct(up, total_ad),
        'adDownPct': pct(down, total_ad),
        'adUncPct':  pct(flat, total_ad),
        'volUpPct':   pct(vol_up, total_vol),
        'volDownPct': pct(vol_down, total_vol),
        # Fear/VIX (from Yahoo)
        'vix': vix_level if vix_level is not None else 0,
        # 52w stats — approximate from 6 anchor points (not strict rolling 252d).
        # Full precision requires a grouped-daily bootstrap cache; TODO.
        'new52wHigh': n_52w_high,
        'new52wLow': n_52w_low,
        'new52wApprox': True,
        # Fields still not populated (need rolling daily cache or different data source)
        'putCall': 0,
        'pctAbove50dma': 0,
        'pctAbove200dma': 0,
    }
    (out_dir / 'breadth.json').write_text(
        json.dumps(breadth, indent=2, ensure_ascii=False) + '\n'
    )
    print(f'[info] wrote {out_dir / "breadth.json"}: up={up} down={down} flat={flat} volUp={breadth["volUpPct"]}% volDown={breadth["volDownPct"]}%')

    print(f'[done] done.')

if __name__ == '__main__':
    main()
