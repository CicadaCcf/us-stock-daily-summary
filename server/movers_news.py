#!/usr/bin/env python3
"""
Fetch news for Top Movers via Polygon's /v2/reference/news endpoint.

Replaces the earlier Futu scraper — Polygon serves clean JSON (no anti-bot,
no Playwright, no rate-limit "访问频繁" messages) and we already have the key.

Reads:   src/data/{date}/screener.json
Writes:  src/data/{date}/movers_news.json
         {
           "generated_at": "...",
           "date":         "2026-04-22",
           "by_ticker":    {
             "POET": [
               { "headline": "...", "url": "...", "source": "...",
                 "published_at": "2026-04-22T18:30:00Z",
                 "image": "...",
                 "description": "..." },
               ...
             ],
             ...
           }
         }

Window: default last 24h ending at the trading day's 20:00 UTC (~16:00 ET
market close). Most Top Movers news is same-day or late-prior-day anyway.

Cache: per-ticker JSON under server/cache/movers_news/{date}/{tk}.json so
reruns are near-free. `--force` ignores the cache.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT       = Path(__file__).resolve().parent.parent
DATA_DIR   = ROOT / 'src' / 'data'
CACHE_DIR  = ROOT / 'server' / 'cache' / 'movers_news'
BASE       = 'https://api.polygon.io'

# Walk up from this file and pull POLYGON_API_KEY out of .env.local if it's
# not already in the environment (matches polygon_snapshot.py behaviour).
ENV_FILE = ROOT / '.env.local'
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        if not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")

API_KEY = os.environ.get('POLYGON_API_KEY')
if not API_KEY:
    print('ERROR: POLYGON_API_KEY not set in env or .env.local', file=sys.stderr)
    sys.exit(2)
PROXY = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')


def _opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY})
        )
    return urllib.request.build_opener()


def _get(path_and_query: str, timeout: int = 30) -> dict:
    sep = '&' if '?' in path_and_query else '?'
    url = f'{BASE}{path_and_query}{sep}apiKey={API_KEY}'
    req = urllib.request.Request(url)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8')
            msg = json.loads(body).get('message', body[:200])
        except Exception:
            msg = str(e)
        raise RuntimeError(f'Polygon HTTP {e.code}: {msg}')


def fetch_news(ticker: str, since_iso: str, limit: int) -> list:
    """One Polygon call per ticker — paginated once is plenty at limit≤50."""
    q = urllib.parse.urlencode({
        'ticker':            ticker,
        'published_utc.gte': since_iso,
        'order':             'desc',
        'sort':              'published_utc',
        'limit':             max(1, min(limit, 50)),
    })
    data = _get(f'/v2/reference/news?{q}')
    rows = data.get('results') or []
    out = []
    for r in rows:
        url = r.get('article_url') or ''
        headline = r.get('title') or ''
        if not url or not headline:
            continue
        pub = r.get('publisher') or {}
        out.append({
            'headline':     headline,
            'url':          url,
            'source':       pub.get('name') or '',
            'published_at': r.get('published_utc') or None,
            'image':        r.get('image_url') or '',
            'description':  (r.get('description') or '')[:500],
        })
    return out


def load_screener(date_str: str) -> list:
    p = DATA_DIR / date_str / 'screener.json'
    if not p.exists():
        print(f'ERROR: {p} not found', file=sys.stderr)
        sys.exit(2)
    data = json.loads(p.read_text())
    return data.get('rows', []) or []


def latest_date_with_screener():
    if not DATA_DIR.exists():
        return None
    dates = sorted(
        d.name for d in DATA_DIR.iterdir()
        if d.is_dir() and (d / 'screener.json').exists()
        and len(d.name) == 10 and d.name[4] == '-'
    )
    return dates[-1] if dates else None


def main():
    parser = argparse.ArgumentParser(description='Fetch Polygon news for Top Movers')
    parser.add_argument('--date', default=None, help='YYYY-MM-DD (default: latest with screener.json)')
    parser.add_argument('--limit', type=int, default=10, help='Max news items per ticker (default 10)')
    parser.add_argument('--hours', type=int, default=48, help='Lookback window in hours (default 48 — covers after-close two days prior through today)')
    parser.add_argument('--force', action='store_true', help='Ignore per-ticker cache')
    args = parser.parse_args()

    date_str = args.date or latest_date_with_screener()
    if not date_str:
        print('ERROR: no screener.json found and no --date given', file=sys.stderr)
        sys.exit(2)

    rows = load_screener(date_str)
    tickers = [r['tk'] for r in rows if r.get('tk')]
    # Window anchored at the trading-day's 20:00 UTC (≈ 16:00 ET market close),
    # looking back `--hours`. Slightly lenient on the upper bound so after-close
    # press releases still qualify.
    try:
        ref_anchor = datetime.fromisoformat(f'{date_str}T20:00:00+00:00')
    except ValueError:
        ref_anchor = datetime.now(timezone.utc)
    since_iso = (ref_anchor - timedelta(hours=args.hours)).isoformat(timespec='seconds')
    print(f'[info] fetching news for {len(tickers)} tickers since {since_iso}')

    cache_dir = CACHE_DIR / date_str
    cache_dir.mkdir(parents=True, exist_ok=True)
    by_ticker = {}
    t0 = time.time()
    for i, tk in enumerate(tickers, 1):
        cache_path = cache_dir / f'{tk}.json'
        if cache_path.exists() and not args.force:
            by_ticker[tk] = json.loads(cache_path.read_text())
            print(f'  [{i:2d}/{len(tickers)}] {tk}: cached ({len(by_ticker[tk])})')
            continue
        try:
            items = fetch_news(tk, since_iso, args.limit)
        except Exception as e:
            print(f'  [{i:2d}/{len(tickers)}] {tk}: ERROR {e}', file=sys.stderr)
            items = []
        cache_path.write_text(json.dumps(items, indent=2, ensure_ascii=False) + '\n')
        by_ticker[tk] = items
        print(f'  [{i:2d}/{len(tickers)}] {tk}: {len(items)} items')
        time.sleep(0.15)  # polite pacing — Polygon allows 5 req/s on paid tier

    out_path = DATA_DIR / date_str / 'movers_news.json'
    out_path.write_text(json.dumps({
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'date':         date_str,
        'since':        since_iso,
        'by_ticker':    by_ticker,
    }, indent=2, ensure_ascii=False) + '\n')
    kb = out_path.stat().st_size / 1024
    print(f'[done] wrote {out_path} ({kb:.1f} KB) in {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
