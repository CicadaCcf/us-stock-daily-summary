#!/usr/bin/env python3
"""
One-time bootstrap for the 1Y per-sector dollar-volume history.

Iterates 252 trading days of Polygon grouped-daily, applies sector_map.json,
sums close*volume per SPDR sector ETF. Writes:

    server/cache/sector_volumes_1y.json

    {
      "generated_at": "...",
      "last_date":    "2026-04-22",
      "days":         252,
      "dates":        ["2025-04-23", ..., "2026-04-22"],
      "volumes":      { "XLK": [...252 floats...], "XLF": [...], ... }
    }

polygon_snapshot.py appends one new row per trading day on subsequent runs.

Prereq:
    python3 server/build_sector_map.py   # creates sector_map.json

Runtime: ~11 min (same call pattern as bootstrap_cache.py — 252 grouped-daily
calls at 0.15s pacing + API latency). Sized at ~20 KB on disk — tiny since
we only store 11 numbers per day, not the full 12k ticker table.
"""

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from polygon_snapshot import grouped_daily, grouped_with_fallback, CACHE_DIR

SECTOR_MAP_FILE = CACHE_DIR / 'sector_map.json'
OUT             = CACHE_DIR / 'sector_volumes_1y.json'
LOOKBACK        = 252
PACING_SECONDS  = 0.15


def main():
    if not SECTOR_MAP_FILE.exists():
        print('ERROR: run `python3 server/build_sector_map.py` first', file=sys.stderr)
        sys.exit(2)
    by_sector = json.loads(SECTOR_MAP_FILE.read_text())['tickers_by_sector']
    # Invert: ticker → ETF (sector)
    ticker_to_etf = {}
    for etf, tks in by_sector.items():
        for t in tks:
            ticker_to_etf[t] = etf

    ref, _ = grouped_with_fallback(date.today(), 'today')
    if not ref:
        print('ERROR: no trading bars in last 5 days', file=sys.stderr)
        sys.exit(1)
    print(f'[info] reference trading day: {ref}')
    print(f'[info] mapping {len(ticker_to_etf)} SP500 tickers across {len(by_sector)} sectors')
    print(f'[info] walking back {LOOKBACK} trading days...')
    t0 = time.time()

    dates: list[date] = []
    volumes: dict[str, list[float]] = {etf: [] for etf in by_sector}

    cur = ref
    calendar_cap = LOOKBACK * 2 + 30
    steps = 0
    while len(dates) < LOOKBACK and steps < calendar_cap:
        steps += 1
        if cur.weekday() >= 5:
            cur -= timedelta(days=1)
            continue
        try:
            bars = grouped_daily(cur)
        except Exception as e:
            print(f'  [warn] {cur}: {e}')
            cur -= timedelta(days=1)
            time.sleep(PACING_SECONDS)
            continue
        if bars:
            dates.append(cur)
            per = {etf: 0.0 for etf in by_sector}
            for tk, bar in bars.items():
                etf = ticker_to_etf.get(tk)
                if etf is None:
                    continue
                c = bar.get('c')
                v = bar.get('v')
                if c and v and c > 0 and v > 0:
                    per[etf] += float(c) * float(v)
            for etf, total in per.items():
                volumes[etf].append(round(total))
            idx = len(dates)
            print(f'  [{idx:3d}/{LOOKBACK}] {cur}: Σ sector $-vol')
        else:
            print(f'  [skip] {cur}: no bars')
        cur -= timedelta(days=1)
        time.sleep(PACING_SECONDS)

    dates.reverse()
    for etf in volumes:
        volumes[etf].reverse()

    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'last_date':    dates[-1].isoformat(),
        'days':         len(dates),
        'dates':        [d.isoformat() for d in dates],
        'volumes':      volumes,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(',', ':')))
    dt = time.time() - t0
    kb = OUT.stat().st_size / 1024
    print(f'[done] wrote {OUT} ({kb:.1f} KB) in {dt:.0f}s')


if __name__ == '__main__':
    main()
