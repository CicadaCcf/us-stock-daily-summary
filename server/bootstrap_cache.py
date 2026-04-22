#!/usr/bin/env python3
"""
One-time bootstrap for the 1-year close-price cache.

Pulls ~252 Polygon grouped-daily responses (one per trading day) and writes
a compact JSON cache that polygon_snapshot.py incrementally updates each
day. The cache powers two metrics that the 6-anchor snapshot can't produce:

  - pctAbove50dma / pctAbove200dma   (need full rolling MA windows)
  - strict new52wHigh / new52wLow    (need every close in the past year)

================================================================
   READ-ONLY — PANOBOARD SHARES THIS API KEY
================================================================
Throttled at 0.15 s between calls. At 252 days ≈ 38 s of throttle plus
actual API latency; expect 5–15 min wall clock. Re-running replaces the
cache, it does not dedupe on disk.

Output: server/cache/closes_1y.json

Shape:
  {
    "generated_at": "2026-04-22T17:36:31",
    "last_date":    "2026-04-22",
    "days":         252,
    "dates":        ["2025-04-23", ..., "2026-04-22"],
    "closes":       { "AAPL": [180.51, 181.22, ..., 245.33],
                      "TSLA": [null, 183.0, ..., 192.17] }
  }

`closes[ticker]` is an array aligned with `dates` (same length). A null
entry means the stock didn't trade that day (delisted, halted, or not yet
listed). Tickers that appear on fewer than MIN_ACTIVE_DAYS days are
dropped to keep the file under control.
"""

import os
import sys
import json
import time
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

# Reuse HTTP + helpers from the daily snapshot script
from polygon_snapshot import (
    grouped_daily,
    grouped_with_fallback,
    ROOT,
)

CACHE_DIR  = ROOT / 'server' / 'cache'
CACHE_FILE = CACHE_DIR / 'closes_1y.json'

DEFAULT_DAYS        = 252         # 1 trading year
MIN_ACTIVE_DAYS     = 20          # drop tickers with < 20 non-null closes
CLOSE_DECIMALS      = 4           # round closes to 4dp in cache
PACING_SECONDS      = 0.15


def fetch_trading_days(ref_date: date, target_days: int) -> tuple[list, dict]:
    """Walk back one calendar day at a time from `ref_date`, fetching
    grouped-daily bars. Skip weekends/holidays (empty responses). Stop once
    we've collected `target_days` trading days.

    Returns (trading_dates ascending, closes_by_ticker).
        closes_by_ticker: {ticker: [(date, close), ...]}
    """
    trading_dates: list[date] = []
    closes: dict[str, list] = {}
    cur = ref_date
    # safety cap: 400 calendar days ≈ 260 trading days even with heavy holidays
    calendar_cap = target_days * 2 + 30
    steps = 0
    while len(trading_dates) < target_days and steps < calendar_cap:
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
            trading_dates.append(cur)
            idx = len(trading_dates)
            for tk, bar in bars.items():
                c = bar.get('c')
                if c is None or c <= 0:
                    continue
                closes.setdefault(tk, []).append((cur, round(float(c), CLOSE_DECIMALS)))
            print(f'  [{idx:3d}/{target_days}] {cur}: {len(bars):,} tickers')
        else:
            print(f'  [skip] {cur}: no bars (holiday)')
        cur -= timedelta(days=1)
        time.sleep(PACING_SECONDS)
    trading_dates.reverse()  # ascending (oldest → newest)
    return trading_dates, closes


def build_aligned(trading_dates: list, closes: dict) -> dict:
    """Turn {ticker: [(date, close), ...]} into {ticker: [close_or_null, ...]}
    aligned with trading_dates. Drops tickers with < MIN_ACTIVE_DAYS non-null."""
    date_idx = {d: i for i, d in enumerate(trading_dates)}
    n = len(trading_dates)
    out = {}
    dropped = 0
    for tk, tuples in closes.items():
        if len(tuples) < MIN_ACTIVE_DAYS:
            dropped += 1
            continue
        arr = [None] * n
        for d, c in tuples:
            arr[date_idx[d]] = c
        out[tk] = arr
    print(f'[info] aligned: kept {len(out):,} tickers, dropped {dropped:,} with <{MIN_ACTIVE_DAYS} days')
    return out


def main():
    parser = argparse.ArgumentParser(description='Bootstrap 1Y close-price cache')
    parser.add_argument('--days', type=int, default=DEFAULT_DAYS,
                        help=f'trading days to fetch (default {DEFAULT_DAYS})')
    parser.add_argument('--ref-date',
                        help='reference trading date (default: most recent available)')
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    ref_anchor = date.fromisoformat(args.ref_date) if args.ref_date else date.today()
    ref_date, _ = grouped_with_fallback(ref_anchor, 'today')
    if not ref_date:
        print('ERROR: no trading bars found in last 5 days', file=sys.stderr)
        sys.exit(1)
    print(f'[info] reference trading day: {ref_date}')
    print(f'[info] fetching {args.days} trading days of grouped-daily bars...')
    t0 = time.time()

    trading_dates, closes = fetch_trading_days(ref_date, args.days)
    if len(trading_dates) < args.days:
        print(f'[warn] only got {len(trading_dates)}/{args.days} days (hit calendar cap)')

    print(f'[info] raw collect: {len(closes):,} unique tickers across {len(trading_dates)} days')
    aligned = build_aligned(trading_dates, closes)

    cache = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'last_date':    trading_dates[-1].isoformat(),
        'days':         len(trading_dates),
        'dates':        [d.isoformat() for d in trading_dates],
        'closes':       aligned,
    }
    CACHE_FILE.write_text(json.dumps(cache, separators=(',', ':')))
    mb = CACHE_FILE.stat().st_size / 1e6
    dt = time.time() - t0
    print(f'[done] wrote {CACHE_FILE} ({mb:.1f} MB) in {dt:.0f}s')


if __name__ == '__main__':
    main()
