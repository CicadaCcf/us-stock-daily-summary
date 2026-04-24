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
from datetime import date, datetime, timedelta, timezone
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
        k, v = k.strip(), v.strip()
        # Treat .env.local as source of truth when the shell value is unset or
        # empty (shells sometimes export empty placeholders that mask real secrets).
        if not os.environ.get(k):
            os.environ[k] = v

API_KEY = os.environ.get('POLYGON_API_KEY')
if not API_KEY:
    print('ERROR: POLYGON_API_KEY not set in .env.local', file=sys.stderr)
    sys.exit(2)
PROXY = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')

BASE = 'https://api.polygon.io'

# 1Y close-price cache (populated by server/bootstrap_cache.py, then
# incrementally updated here). Powers pctAbove50dma, pctAbove200dma, and
# strict new52wHigh / new52wLow.
CACHE_DIR        = ROOT / 'server' / 'cache'
CACHE_FILE       = CACHE_DIR / 'closes_1y.json'
CACHE_KEEP_DAYS  = 260   # ~1Y + buffer; trim older entries on each run

# S&P 500 sector map (built by server/build_sector_map.py). Used to sum per-
# sector dollar volume on ref_date across real constituents — a better
# "sector heat" proxy than the ETF's own trading volume.
SECTOR_MAP_FILE      = CACHE_DIR / 'sector_map.json'
# 1Y rolling per-sector $-volume history (built by server/build_sector_volumes.py,
# then incrementally updated here). Lets the frontend show period-averaged
# sector volume that tracks the 1W/1M/3M/6M/1Y tab selection.
SECTOR_VOLUMES_FILE  = CACHE_DIR / 'sector_volumes_1y.json'

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
    # Row 1 — US macro + commodities. Frontend picks by symbol (not slice
    # position) so adding or reordering is safe without a client change.
    ('^GSPC',     'S&P 500',       'price'),
    ('^NDX',      'Nasdaq 100',    'price'),
    ('^DJI',      'Dow Jones',     'price'),
    ('^RUT',      'Russell 2000',  'price'),
    ('^VIX',      'VIX',           'price'),
    ('^TNX',      '10Y Treasury',  'yield10'),  # TNX is yield × 10 (42.8 = 4.28%)
    ('DX-Y.NYB',  '美元指数 DXY',  'price'),
    ('GC=F',      '黄金 Gold',     'price'),
    ('CL=F',      'WTI 原油',      'price'),
    ('HG=F',      '铜期货 Copper', 'price'),
    # Row 2 — global. Order per user request: 韩国, 日经, 沪深300, 新加坡, 德国, 澳洲.
    ('^KS11',     '韩国 KOSPI',    'price'),
    ('^N225',     '日经 225',      'price'),
    ('000300.SS', '沪深 300',      'price'),
    ('^STI',      '新加坡 STI',    'price'),
    ('^GDAXI',    '德国 DAX',      'price'),
    ('^AXJO',     '澳洲 ASX 200',  'price'),
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

# Top Movers filter — match the user's Futu-style criteria.
SCREENER_FILTERS = {
    'min_dollar_volume':  300_000_000,    # $Vol ≥ $300M
    'min_d1_pct':         15.0,           # 1D ≥ 15%
    'min_w1_pct':         40.0,           # 1W ≥ 40% (uses d5_pct from anchors)
    'min_market_cap':     1_000_000_000,  # Mkt Cap ≥ $1B
}
SCREENER_TOP_N = 20

# Ticker reference cache: name + description + shares outstanding. Fetched
# lazily from Polygon /v3/reference/tickers/{t} and reused across runs so
# we don't hammer the API for stable attributes. description_cn is added
# once by the Claude translation pass (see translate_descriptions).
TICKER_INFO_FILE = CACHE_DIR / 'ticker_info.json'
# Refresh reference data if older than this many days (shares outstanding
# rarely change; description effectively never does).
TICKER_INFO_TTL_DAYS = 30

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

def _fetch_cnn_pcr(timeout: int = 20) -> 'float | None':
    """CNN Fear & Greed endpoint exposes the latest CBOE equity Put/Call Ratio
    as a sub-indicator. CBOE's own CDN blocks non-browser requests, but CNN's
    API accepts us with realistic headers. Returns None on any failure so the
    snapshot never fails just because of this one field.
    """
    url = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://edition.cnn.com/',
        'Origin':  'https://edition.cnn.com',
    })
    try:
        opener = _opener()
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        pts = (data.get('put_call_options') or {}).get('data') or []
        if not pts:
            return None
        y = pts[-1].get('y')
        return float(y) if y is not None else None
    except Exception as e:
        print(f'[warn] CNN PCR fetch failed: {e}')
        return None

def _get_yahoo_chart(symbol: str, range_: str = '1y', timeout: int = 30) -> dict:
    """Yahoo Finance chart endpoint (no auth). Must spoof User-Agent."""
    q = urllib.parse.quote(symbol, safe='')
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{q}?interval=1d&range={range_}'
    opener = _opener()
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

# --- Ticker reference + Claude translation (Top Movers enrichment) ------

def load_ticker_info() -> dict:
    if TICKER_INFO_FILE.exists():
        try:
            return json.loads(TICKER_INFO_FILE.read_text())
        except Exception:
            pass
    return {}

def save_ticker_info(info: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TICKER_INFO_FILE.write_text(json.dumps(info, indent=2, ensure_ascii=False))

def fetch_ticker_reference(ticker: str) -> dict:
    """Polygon /v3/reference/tickers/{t}. Returns the `results` object
    (name, description, market_cap, weighted_shares_outstanding, etc.)."""
    try:
        data = _get(f'/v3/reference/tickers/{urllib.parse.quote(ticker, safe="")}')
        return data.get('results') or {}
    except Exception as e:
        print(f'[warn] reference {ticker}: {e}')
        return {}

def fetch_w1_avg_dollar_volume(ticker: str, ref_date: date) -> 'float | None':
    """Mean of close*volume over the last ~5 trading days ending at ref_date."""
    from_dt = biz_days_back(ref_date, 6)  # a small buffer
    try:
        data = _get(
            f'/v2/aggs/ticker/{urllib.parse.quote(ticker, safe="")}'
            f'/range/1/day/{from_dt.isoformat()}/{ref_date.isoformat()}'
            f'?adjusted=true&sort=asc&limit=50'
        )
        results = (data.get('results') or [])[-5:]
        dvs = [r['c'] * r['v'] for r in results
               if r.get('c') and r.get('v') and r['c'] > 0 and r['v'] > 0]
        return sum(dvs) / len(dvs) if dvs else None
    except Exception as e:
        print(f'[warn] w1_avg_vol {ticker}: {e}')
        return None

def translate_descriptions(items: dict) -> dict:
    """{ticker: english_description} → {ticker: chinese_one_liner}.

    One batched Claude call, so a 20-ticker Top Movers list = 1 API call
    instead of 20. Returns empty dict on any error (caller keeps going).
    Cached in ticker_info so repeat tickers don't re-translate.
    """
    if not items:
        return {}
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print('[warn] ANTHROPIC_API_KEY not set — skipping main_business translation')
        return {}
    lines = [f'{tk}: {desc[:400]}' for tk, desc in items.items() if desc]
    if not lines:
        return {}
    prompt = (
        '为每家公司生成一个简洁的中文主营业务描述（1 句，15-30 汉字），突出核心业务。'
        '**严格只输出 JSON**，形如 {"AAPL": "设计制造 iPhone / Mac 等智能硬件", ...}，不要任何解释。\n\n'
        + '\n'.join(lines)
    )
    body = json.dumps({
        'model': os.environ.get('ANTHROPIC_MODEL', 'claude-opus-4-7'),
        'max_tokens': 2000,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        headers={
            'content-type': 'application/json',
            'anthropic-version': '2023-06-01',
            'x-api-key': api_key,
        },
        method='POST',
    )
    try:
        opener = _opener()
        with opener.open(req, timeout=120) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
        # First try strict parse
        try:
            return json.loads(text.strip())
        except Exception:
            pass
        # Extract first {...} block
        import re
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        print(f'[warn] Claude translate failed: {e}')
    return {}

def _rows_by_tk(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {r['tk']: r for r in (data.get('rows') or []) if r.get('tk')}
    except Exception as e:
        print(f'[warn] screener load {path}: {e}')
        return {}


def load_yesterday_screener(out_date: str) -> dict:
    """Return {ticker: row_dict} from the most recent screener.json
    STRICTLY BEFORE out_date. Used to apply the Day Remaining
    reset/decrement rules across trading days. Empty on first-day run."""
    data_dir = ROOT / 'src' / 'data'
    if not data_dir.exists():
        return {}
    prior = [
        d.name for d in data_dir.iterdir()
        if d.is_dir() and len(d.name) == 10 and d.name < out_date
    ]
    if not prior:
        return {}
    latest = sorted(prior)[-1]
    return _rows_by_tk(data_dir / latest / 'screener.json')


def load_existing_screener(out_date: str) -> dict:
    """Return {ticker: row_dict} from the CURRENT out_date folder, if any.
    Used to preserve state across same-day reruns — industry, reason, and
    days_remaining stay put so pressing Update twice on the same trading
    day is idempotent (no second decrement, no lost manual edits)."""
    path = ROOT / 'src' / 'data' / out_date / 'screener.json'
    return _rows_by_tk(path)

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

# --- 1Y close cache (bootstrap once, then incremental) ------------------

def load_cache() -> 'dict | None':
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception as e:
        print(f'[warn] cache load failed: {e}')
        return None

def update_cache(cache: dict, ref_date: date, bars_today: dict) -> dict:
    """Append every missing trading day through ref_date. Reuses bars_today
    for ref_date itself so the common case costs zero extra API calls.
    Trims to last CACHE_KEEP_DAYS and returns the mutated cache."""
    cache_dates = [date.fromisoformat(s) for s in cache['dates']]
    closes = cache['closes']
    if cache_dates[-1] >= ref_date:
        return cache
    missing = []
    cur = cache_dates[-1] + timedelta(days=1)
    while cur <= ref_date:
        if cur.weekday() < 5:
            missing.append(cur)
        cur += timedelta(days=1)
    print(f'[info] cache incremental: {len(missing)} potential trading day(s) since {cache_dates[-1]}')
    for d in missing:
        if d == ref_date:
            bars = bars_today  # reuse — saves one API call
        else:
            try:
                bars = grouped_daily(d)
            except Exception as e:
                print(f'[warn] cache incr {d}: {e}')
                time.sleep(0.15)
                continue
            time.sleep(0.15)
        if not bars:
            continue  # holiday
        cache_dates.append(d)
        new_idx = len(cache_dates) - 1
        for tk in closes:
            closes[tk].append(None)
        for tk, bar in bars.items():
            c = bar.get('c')
            if c is None or c <= 0:
                continue
            c = round(float(c), 4)
            if tk in closes:
                closes[tk][new_idx] = c
            else:
                closes[tk] = [None] * new_idx + [c]
        print(f'  [cache] +{d}: {len(bars):,} tickers')
    if len(cache_dates) > CACHE_KEEP_DAYS:
        trim = len(cache_dates) - CACHE_KEEP_DAYS
        cache_dates = cache_dates[trim:]
        for tk in list(closes.keys()):
            closes[tk] = closes[tk][trim:]
            if all(x is None for x in closes[tk]):
                del closes[tk]
    cache['dates']        = [d.isoformat() for d in cache_dates]
    cache['last_date']    = cache_dates[-1].isoformat()
    cache['days']         = len(cache_dates)
    cache['closes']       = closes
    cache['generated_at'] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return cache

def save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, separators=(',', ':')))

def compute_cache_stats(cache: dict, today_table: dict) -> dict:
    """Strict DMA + 52w stats against the full 1Y cache.

    DMA window: last 50 / 200 non-null closes including today (Finviz /
    Yahoo convention). 52w high/low: today's close vs the 252 PRIOR closes
    (today excluded). Tickers with < 100 prior closes are skipped for 52w
    (too young to have a meaningful 52w anchor). Denominators reflect
    tickers with enough history, not the full 12k universe.
    """
    closes = cache['closes']
    above50 = total50 = 0
    above200 = total200 = 0
    n_high = n_low = total_52w = 0
    for tk, row in today_table.items():
        today_close = row['close']
        hist = closes.get(tk)
        if not hist:
            continue
        vals = [c for c in hist if c is not None]
        if not vals:
            continue
        if len(vals) >= 50:
            ma50 = sum(vals[-50:]) / 50
            total50 += 1
            if today_close > ma50:
                above50 += 1
        if len(vals) >= 200:
            ma200 = sum(vals[-200:]) / 200
            total200 += 1
            if today_close > ma200:
                above200 += 1
        window = vals[-253:-1]  # prior ≤252 closes, exclude today
        if len(window) >= 100:
            total_52w += 1
            if today_close > max(window):
                n_high += 1
            if today_close < min(window):
                n_low += 1
    return {
        'pct_above_50':  round(above50 / total50 * 100, 1) if total50 else 0,
        'pct_above_200': round(above200 / total200 * 100, 1) if total200 else 0,
        'n_52w_high':    n_high,
        'n_52w_low':     n_low,
        'dma_universe':  total50,
        'n200_universe': total200,
        '52w_universe':  total_52w,
    }

# --- Main ---------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Polygon daily snapshot + screener')
    parser.add_argument('--date', help='target date for the output folder (YYYY-MM-DD). Defaults to today.')
    parser.add_argument('--ref-date', help='trading date to use as "today" for computations. Defaults to latest available.')
    args = parser.parse_args()

    # Reference date = most recent trading day at/before `ref-date` (or today)
    ref_anchor = date.fromisoformat(args.ref_date) if args.ref_date else date.today()
    ref_date, bars_today = grouped_with_fallback(ref_anchor, 'today')
    if not bars_today:
        print('ERROR: no trading bars found in last 5 days', file=sys.stderr)
        sys.exit(1)
    print(f'[info] reference trading day: {ref_date}  ({len(bars_today):,} tickers)')

    # The output folder is named after the TRADING day, not the calendar day.
    # If the user runs the snapshot on 4/23 before US market close, ref_date is
    # 4/22 and we write to src/data/2026-04-22/ (overwriting in place). This
    # avoids creating a half-populated 4/23 folder that the frontend would pick
    # up as LATEST_DATE while events/macro/movers_news are still missing.
    out_date = args.date or ref_date.isoformat()
    out_dir = ROOT / 'src' / 'data' / out_date
    out_dir.mkdir(parents=True, exist_ok=True)

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
            timestamps = r[0].get('timestamp', []) or []
            closes = r[0].get('indicators', {}).get('quote', [{}])[0].get('close', []) or []
            # Align to ref_date: a 4/22 snapshot must show each market's 4/22
            # close by its own local calendar. Dropping bars dated after
            # ref_date also guards against Asia markets' next-day close
            # leaking in when the snapshot runs later the same day.
            ref_iso = ref_date.isoformat()
            closes_filtered = [
                (i, c) for i, (t, c) in enumerate(zip(timestamps, closes))
                if c is not None
                   and datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat() <= ref_iso
            ]
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

    # CBOE equity Put/Call Ratio — via CNN F&G endpoint (CBOE blocks direct).
    pcr = _fetch_cnn_pcr()
    if pcr is not None:
        print(f'[info] Put/Call Ratio: {pcr:.2f}')

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

    # Per-sector dollar-volume across SP500 constituents. We compute today's
    # totals, append them to sector_volumes_1y.json (if bootstrapped), and embed
    # the trailing 252-day series into each sector row so the UI can compute
    # period averages (1W/1M/3M/6M/1Y) client-side.
    sector_map = None
    if SECTOR_MAP_FILE.exists():
        try:
            sector_map = json.loads(SECTOR_MAP_FILE.read_text()).get('tickers_by_sector', {})
        except Exception as e:
            print(f'[warn] sector_map load failed: {e}')
    sector_vol_cache = None
    if SECTOR_VOLUMES_FILE.exists():
        try:
            sector_vol_cache = json.loads(SECTOR_VOLUMES_FILE.read_text())
        except Exception as e:
            print(f'[warn] sector_volumes cache load failed: {e}')

    today_totals = {}
    if sector_map:
        for s in sectors_enriched:
            tickers = sector_map.get(s['symbol']) or []
            total = matched = 0
            for tk in tickers:
                row = table.get(tk)
                if row:
                    total += row['dollar_volume']
                    matched += 1
            today_totals[s['symbol']]       = round(total) if tickers else None
            s['sector_constituents_count']  = matched
        print(f'[info] today\'s sector $-volume totals: {sum(len(v) for v in sector_map.values())} SP500 tickers')

        # Update the rolling history cache with today's row
        if sector_vol_cache is not None:
            cache_dates = [date.fromisoformat(s) for s in sector_vol_cache['dates']]
            if cache_dates and cache_dates[-1] < ref_date:
                cache_dates.append(ref_date)
                for etf in sector_vol_cache['volumes']:
                    sector_vol_cache['volumes'][etf].append(today_totals.get(etf) or 0)
                if len(cache_dates) > CACHE_KEEP_DAYS:
                    trim = len(cache_dates) - CACHE_KEEP_DAYS
                    cache_dates = cache_dates[trim:]
                    for etf in sector_vol_cache['volumes']:
                        sector_vol_cache['volumes'][etf] = sector_vol_cache['volumes'][etf][trim:]
                sector_vol_cache['dates']        = [d.isoformat() for d in cache_dates]
                sector_vol_cache['last_date']    = cache_dates[-1].isoformat()
                sector_vol_cache['days']         = len(cache_dates)
                sector_vol_cache['generated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
                SECTOR_VOLUMES_FILE.write_text(json.dumps(sector_vol_cache, separators=(',', ':')))
                print(f'[info] sector_volumes cache extended to {cache_dates[-1]} ({len(cache_dates)} days)')
            elif cache_dates and cache_dates[-1] == ref_date:
                # Already up to date — overwrite today's row in case it was incomplete
                for etf in sector_vol_cache['volumes']:
                    sector_vol_cache['volumes'][etf][-1] = today_totals.get(etf) or 0
                sector_vol_cache['generated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
                SECTOR_VOLUMES_FILE.write_text(json.dumps(sector_vol_cache, separators=(',', ':')))

            # Embed last 252 days per sector so the frontend gets a stable window
            for s in sectors_enriched:
                hist = sector_vol_cache['volumes'].get(s['symbol'], [])
                s['sector_dollar_volume_1y'] = hist[-252:]
        else:
            # No bootstrapped history — emit a 1-entry array so the UI still
            # has something to reduce over. Period tabs will all show the
            # same single-day number until bootstrap runs.
            print('[warn] no sector_volumes_1y.json — emitting single-day total')
            print('       (run `python3 server/build_sector_volumes.py` once to enable period averages)')
            for s in sectors_enriched:
                t = today_totals.get(s['symbol'])
                s['sector_dollar_volume_1y'] = [t] if t is not None else []
    else:
        print('[warn] no sector_map.json — skipping per-sector totals (run server/build_sector_map.py)')
        for s in sectors_enriched:
            s['sector_dollar_volume_1y']    = []
            s['sector_constituents_count']  = 0

    # Trading dates aligned to sector_dollar_volume_1y arrays (and sectors'
    # closes_1y). Frontend uses these for the X-axis labels on the sector
    # performance chart.
    trading_dates_1y = (sector_vol_cache or {}).get('dates', [])[-252:]

    market = {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec="seconds"),
        'source': 'polygon.io + yahoo',
        'date': out_date,
        'ref_date': ref_date.isoformat(),
        'indices':       indices_out,
        'sectors':       sectors_enriched,
        'themes_etfs':   build_section([(s, s) for s in THEME_ETFS]),
        'themes_stocks': build_section([(s, s) for s in THEME_STOCKS]),
        'trading_dates_1y': trading_dates_1y,
    }
    (out_dir / 'market.json').write_text(
        json.dumps(market, indent=2, ensure_ascii=False) + '\n'
    )
    print(f'[info] wrote {out_dir / "market.json"}')

    # ----- screener.json (Top Movers — Futu-style with Day Remaining) -----
    flt = SCREENER_FILTERS

    # Stage 1: first cut by $-vol AND (1D OR 1W). Market-cap check needs a
    # per-ticker reference call; defer until we've narrowed the set.
    # Note: momentum condition is OR — 1D ≥ 15% OR 1W ≥ 40%, not both.
    def passes_partial(r):
        d1 = r.get('d1_pct')
        w1 = r.get('d5_pct')
        momentum_ok = (d1 is not None and d1 > flt['min_d1_pct']) \
                      or (w1 is not None and w1 > flt['min_w1_pct'])
        return r['dollar_volume'] >= flt['min_dollar_volume'] and momentum_ok
    partial_pass = {sym for sym, r in table.items() if passes_partial(r)}

    # Stage 2: include yesterday's tickers (for cross-day countdown) AND any
    # rows already in the current out_date folder (so a same-day rerun is
    # idempotent — manual edits + days_remaining are preserved).
    yesterday_rows = load_yesterday_screener(out_date)
    existing_rows  = load_existing_screener(out_date)
    candidate_tks = (
        partial_pass
        | set(yesterday_rows.keys())
        | set(existing_rows.keys())
    ) & set(table.keys())
    print(f'[info] Top Movers: {len(partial_pass)} pass first cut, '
          f'{len(yesterday_rows)} from prior trading day, '
          f'{len(existing_rows)} already in {out_date}/, '
          f'{len(candidate_tks)} total to evaluate')

    # Stage 3: fetch ticker reference (name, description, market_cap, type) for
    # each candidate — lazy + 30-day TTL so we only hit Polygon for new or stale
    # rows. `type` is what lets us filter out ETFs / ETNs / funds later.
    ticker_info = load_ticker_info()
    today_iso = date.today().isoformat()
    fetched = 0
    for tk in sorted(candidate_tks):
        entry = ticker_info.get(tk) or {}
        last = entry.get('last_ref_fetched') or '1970-01-01'
        age_days = (date.fromisoformat(today_iso) - date.fromisoformat(last)).days \
            if last != '1970-01-01' else 9999
        # Force a refetch when `type` is missing — older cache entries from before
        # the ETF-filter change don't have it, so we can't rule them in/out.
        needs_refetch = (
            age_days >= TICKER_INFO_TTL_DAYS
            or 'name' not in entry
            or 'type' not in entry
        )
        if needs_refetch:
            ref = fetch_ticker_reference(tk)
            if ref:
                entry.update({
                    'name':                     ref.get('name', tk),
                    'type':                     ref.get('type'),   # CS / ETF / ETN / FUND / PFD / ...
                    'description':              ref.get('description') or entry.get('description', ''),
                    'market_cap':               ref.get('market_cap'),
                    'weighted_shares_outstanding': ref.get('weighted_shares_outstanding'),
                    'share_class_shares_outstanding': ref.get('share_class_shares_outstanding'),
                    'sic_description':          ref.get('sic_description', ''),
                    'last_ref_fetched':         today_iso,
                })
                ticker_info[tk] = entry
                fetched += 1
            time.sleep(0.1)
    if fetched:
        print(f'[info] fetched fresh reference data for {fetched} tickers')

    # Types we accept as "individual stocks" per the user's focus. Everything
    # else (ETF / ETN / FUND / preferred / warrant / unit / SP / etc.) is filtered out.
    INDIVIDUAL_STOCK_TYPES = {'CS', 'ADRC', 'ADRP', 'ADRW', 'ADRR'}

    def market_cap_for(tk, today_close):
        info = ticker_info.get(tk) or {}
        # Prefer a shares-based calc since Polygon's market_cap is static daily.
        shares = info.get('weighted_shares_outstanding') or info.get('share_class_shares_outstanding')
        if shares and today_close:
            return shares * today_close
        return info.get('market_cap')

    # Stage 4: evaluate "passes ALL today" (including mkt cap now that we have it).
    # Also drops non-individual-stock types (ETFs, funds, etc.) per user's focus.
    def passes_full(tk):
        r = table[tk]
        if not passes_partial(r):
            return False
        info = ticker_info.get(tk) or {}
        typ = info.get('type')
        if typ and typ not in INDIVIDUAL_STOCK_TYPES:
            return False
        mc = market_cap_for(tk, r['close']) or 0
        return mc >= flt['min_market_cap']

    # Stage 5: Day Remaining bookkeeping
    new_rows_by_tk = {}
    for tk in candidate_tks:
        info = ticker_info.get(tk) or {}
        typ = info.get('type')
        # Drop ETFs / funds / etc. immediately — even yesterday's carryovers —
        # so they never linger on the Top Movers list via the decrement path.
        if typ and typ not in INDIVIDUAL_STOCK_TYPES:
            continue
        r = table[tk]
        existing = existing_rows.get(tk)
        yest     = yesterday_rows.get(tk)
        mc = market_cap_for(tk, r['close']) or 0

        if existing:
            # Same-day rerun: preserve state verbatim. No countdown applied —
            # the row was already resolved for today, and re-decrementing
            # would erase it after 3 reruns on a flat day.
            initial        = int(existing.get('initial_days') or 3)
            dr_raw         = existing.get('days_remaining')
            days_remaining = int(dr_raw) if dr_raw is not None else initial
            if days_remaining <= 0:
                continue
            reason   = existing.get('reason', '')
            industry = existing.get('industry', '')
        elif yest:
            initial = int(yest.get('initial_days') or 3)
            # Day Remaining is now manually managed via the Supabase overlay
            # (screener_edits.days_remaining) — the pipeline just carries
            # yesterday's value forward. The editor decides when to drop a row
            # by setting days_remaining to 0.
            yest_dr = yest.get('days_remaining')
            days_remaining = int(yest_dr) if yest_dr is not None else initial
            if days_remaining <= 0:
                continue  # off the list
            reason       = yest.get('reason', '')
            industry     = yest.get('industry', '')
        else:
            if not passes_full(tk):
                continue  # new entrants must pass all
            initial        = 3
            days_remaining = 3
            reason         = ''
            industry       = ''

        new_rows_by_tk[tk] = {
            'tk':                    tk,
            'nm':                    info.get('name') or tk,
            'industry':              industry,
            'days_remaining':        days_remaining,
            'initial_days':          initial,
            'd1':                    round(r['d1_pct'], 1) if r.get('d1_pct') is not None else None,
            'w1':                    round(r['d5_pct'], 1) if r.get('d5_pct') is not None else None,
            'market_cap_bn':         round(mc / 1e9, 2) if mc else None,
            'dollar_vol_bn':         round(r['dollar_volume'] / 1e9, 2),
            'w1_avg_dollar_vol_bn':  None,   # filled in stage 6
            'reason':                reason,
            'main_business':         info.get('description_cn') or '',
        }

    # Stage 6: fetch 1W avg $-volume per surviving row
    for tk, row in new_rows_by_tk.items():
        avg = fetch_w1_avg_dollar_volume(tk, ref_date)
        if avg is not None:
            row['w1_avg_dollar_vol_bn'] = round(avg / 1e9, 2)
        time.sleep(0.1)

    # Stage 7: translate any English descriptions we haven't translated yet
    need_cn = {}
    for tk in new_rows_by_tk:
        info = ticker_info.get(tk) or {}
        if not info.get('description_cn') and info.get('description'):
            need_cn[tk] = info['description']
    if need_cn:
        print(f'[info] translating {len(need_cn)} main_business descriptions via Claude...')
        cn_map = translate_descriptions(need_cn)
        for tk, cn in cn_map.items():
            if isinstance(cn, str) and cn.strip():
                ticker_info.setdefault(tk, {})['description_cn'] = cn.strip()
                if tk in new_rows_by_tk:
                    new_rows_by_tk[tk]['main_business'] = cn.strip()

    save_ticker_info(ticker_info)

    # Stage 8: sort by 1D change descending, cap at SCREENER_TOP_N
    sorted_rows = sorted(
        new_rows_by_tk.values(),
        key=lambda r: -(r['d1'] if r.get('d1') is not None else -9999),
    )
    top_rows = sorted_rows[:SCREENER_TOP_N]

    screener = {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec="seconds"),
        'source': 'polygon.io + claude',
        'ref_date': ref_date.isoformat(),
        'filter': {
            'min_dollar_volume_usd': flt['min_dollar_volume'],
            'min_d1_pct':  flt['min_d1_pct'],
            'min_w1_pct':  flt['min_w1_pct'],
            'min_market_cap_usd': flt['min_market_cap'],
            'universe_size':    len(table),
            'partial_pass':     len(partial_pass),
            'with_carryover':   len(candidate_tks),
            'after_full_check': len(new_rows_by_tk),
        },
        'rows': top_rows,
    }
    (out_dir / 'screener.json').write_text(
        json.dumps(screener, indent=2, ensure_ascii=False) + '\n'
    )
    print(f'[info] wrote {out_dir / "screener.json"}: {len(top_rows)} rows')

    # ----- breadth.json ----------------------------------------------
    # Advance/decline counts (always real from 12k universe).
    up = down = flat = 0
    vol_up = vol_down = 0.0
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
    total_ad = up + down + flat
    total_vol = vol_up + vol_down

    # DMA + strict 52w from 1Y cache if available; otherwise fall back to
    # 6-anchor approximation for 52w and leave DMA at 0.
    cache = load_cache()
    if cache is not None:
        cache = update_cache(cache, ref_date, bars_today)
        save_cache(cache)
        stats = compute_cache_stats(cache, table)
        n_52w_high = stats['n_52w_high']
        n_52w_low  = stats['n_52w_low']
        pct_above_50  = stats['pct_above_50']
        pct_above_200 = stats['pct_above_200']
        approx_52w = False
        print(f'[info] cache stats: {pct_above_50}% >50dma ({stats["dma_universe"]:,}), '
              f'{pct_above_200}% >200dma ({stats["n200_universe"]:,}), '
              f'52wH={n_52w_high} 52wL={n_52w_low} of {stats["52w_universe"]:,}')
    else:
        print('[warn] cache missing — run server/bootstrap_cache.py to enable DMA + strict 52w')
        # 6-anchor 52w approximation (directionally correct, undercounts peaks
        # between anchors). Skipped entirely when cache is present.
        n_52w_high = n_52w_low = 0
        for sym, r in table.items():
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
        pct_above_50 = pct_above_200 = 0
        approx_52w = True

    def pct(n, d):
        return round(n / d * 100, 1) if d else 0

    breadth = {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
        # 52w stats — strict rolling 252d when cache present, else approx
        # from 6 anchor points (undercounts peaks between anchors).
        'new52wHigh': n_52w_high,
        'new52wLow': n_52w_low,
        'new52wApprox': approx_52w,
        # DMA — 0 until cache is bootstrapped (server/bootstrap_cache.py).
        'pctAbove50dma': pct_above_50,
        'pctAbove200dma': pct_above_200,
        # Put/Call Ratio — CBOE equity PCR via CNN F&G endpoint (CBOE CDN 403s
        # outside their allowed surfaces). None if CNN is unreachable.
        'putCall': round(pcr, 2) if pcr is not None else 0,
    }
    (out_dir / 'breadth.json').write_text(
        json.dumps(breadth, indent=2, ensure_ascii=False) + '\n'
    )
    print(f'[info] wrote {out_dir / "breadth.json"}: up={up} down={down} flat={flat} volUp={breadth["volUpPct"]}% volDown={breadth["volDownPct"]}%')

    print(f'[done] done.')

if __name__ == '__main__':
    main()
