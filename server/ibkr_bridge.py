"""
IBKR daily end-of-day snapshot job (Stage-1 B1, data-only).

Connects to IB Gateway / TWS via ib_insync, requests one year of daily
bars for every symbol in `universe.py`, computes 1D/5D/1M percent moves
locally, and writes a single `market.json` file into
`../src/data/{YYYY-MM-DD}/`. Then exits. Designed to be run once per
trading day ~16:30 ET, manually or via cron.

================================================================
   DATA ONLY — TRADING IS FORBIDDEN IN THIS BRIDGE
================================================================
Inherited verbatim from healthcare-dashboard/server/ibkr_bridge.py:
  1. This file must not import order-related classes from ib_insync
     (MarketOrder, LimitOrder, Order, StopOrder, Trade, etc.).
  2. This file must not call ib.placeOrder / ib.cancelOrder /
     ib.modifyOrder / anything that mutates the account.
  3. At startup we monkey-patch those methods on the IB instance to
     raise RuntimeError as a belt-and-suspenders runtime guard.
  4. `connectAsync(..., readonly=True)` tells the IB server to refuse
     any order-mutating request from this client, a third layer on top
     of (1) and (3).
  5. Read-only endpoints (reqHistoricalData, reqContractDetails,
     qualifyContracts) are allowed.

If you ever need to add trading, make a SEPARATE file; do not weaken
these guards.

Output shape (what A1's loader will consume):

    src/data/{YYYY-MM-DD}/market.json
    {
      "generated_at": "2026-04-20T16:31:04-04:00",
      "date": "2026-04-20",
      "indices":      [ {symbol, name, close, prev_close, d1_pct, ...}, ... ],
      "sectors":      [ ... ],
      "themes_etfs":  [ ... ],
      "themes_stocks":[ ... ]
    }

Per-symbol record:

    {
      "symbol": "XLK",
      "name": "Technology Select Sector SPDR",
      "close": 245.12,
      "prev_close": 240.08,
      "d1_pct": 2.10,        # 1 bar ago → today
      "d5_pct": 4.30,        # 5 bars ago
      "m1_pct": 8.20,        # ~21 bars ago
      "m3_pct": null,        # Stage-1: left null, add later
      "volume": 18234500,
      "mkt_cap_bn": null,    # Stage-1: null (needs a separate fund-data req)
      "last_updated": "2026-04-20T16:31:04-04:00"
    }

Merging: if the target directory already contains other JSON files
(events.json, macro.json from a sibling agent), they are untouched.
Only `market.json` is overwritten. If `market.json` already exists
we overwrite it wholesale — this job always produces a full snapshot,
there's nothing to merge inside it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_insync import IB, Index, Stock

import config
import universe

ET = ZoneInfo('America/New_York')

# ---- Output layout ----------------------------------------------------
# Script lives at `server/ibkr_bridge.py`; frontend data lives at
# `src/data/{date}/`. Resolve once at import so we're immune to cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = _REPO_ROOT / 'src' / 'data'

# ---- Pacing -----------------------------------------------------------
# IB throttles historical-data requests at roughly 60 per 10 minutes per
# client (and ~6 simultaneous in-flight). With ~40 unique symbols we're
# safely under the budget at 0.5s between sequential requests; that's
# ~20s of pure IB time. Daily bars are tiny, so the bottleneck is the
# rate-limit itself, not bandwidth.
REQUEST_SPACING_S = 0.5

# ---- Historical data window ------------------------------------------
# `1 Y` of daily bars gives us ~252 closes — plenty for the 1D/5D/~21D
# (1M) and ~63D (3M) calculations, with headroom for holidays and
# half-days. If you later add 6M / 1Y % you won't need to refetch.
DURATION = '1 Y'
BAR_SIZE = '1 day'
WHAT_TO_SHOW = 'TRADES'
USE_RTH = True
FORMAT_DATE = 1  # returns datetime/date objects; easier to compare


# ======================================================================
# Safety: lockdown
# ======================================================================

def _forbidden_trading(*args, **kwargs):
    raise RuntimeError(
        'Trading is disabled in ibkr_bridge.py (data-only snapshot job). '
        'If you need to place orders, do it in a separate script.'
    )


def lockdown(ib: IB) -> None:
    """Replace every order-mutating method with a raising stub.

    Applied immediately after `connectAsync()` — before any other code
    touches `ib` — so an accidental `ib.placeOrder(...)` crashes loudly
    rather than reaching the IB server. Paired with `readonly=True` in
    the connect call, which asks the IB server to refuse order requests
    regardless of what the client code does.
    """
    for name in (
        'placeOrder', 'cancelOrder', 'reqGlobalCancel',
        'modifyOrder', 'exerciseOptions',
    ):
        if hasattr(ib, name):
            setattr(ib, name, _forbidden_trading)


# ======================================================================
# Helpers
# ======================================================================

def _num(x):
    """Convert IB's sentinel-style floats to JSON-safe values.

    ib_insync inherits IB's convention of using NaN (and occasionally
    Double.MAX_VALUE-equivalents) to mean "no value". JSON has no NaN,
    so we map those to None → which serializes as `null` and is what the
    frontend checks for ('value == null ? N/A').
    """
    if x is None:
        return None
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
    except (TypeError, ValueError):
        return None
    return float(x)


def _pct_change(new: float | None, old: float | None) -> float | None:
    """Return `(new-old)/old * 100` rounded to 2 dp, or None if either
    side is missing / zero. Rounding happens here (not at JSON-dump
    time) so the file is deterministic across re-runs with the same
    underlying bars."""
    if new is None or old is None or not old:
        return None
    try:
        return round((new - old) / old * 100.0, 2)
    except ZeroDivisionError:
        return None


# ======================================================================
# IB fetch
# ======================================================================

@dataclass
class SymbolResult:
    """One per symbol. `error` is set iff we couldn't produce a usable
    record; the main loop still writes partial results for healthy
    symbols and logs the failures."""
    symbol: str
    name: str
    record: dict | None
    error: str | None = None


async def _qualify_one(ib: IB, entry: dict, kind: str):
    """Qualify a single contract for `kind` in {'index','stock'}.

    Indices may need to fall back to an ETF proxy (e.g. SPX → SPY) on
    paper / delayed accounts that lack index-data entitlements. We try
    the native index first and only fall back on explicit failure so the
    snapshot uses true index values whenever available.
    """
    if kind == 'index':
        sym = entry['symbol']
        exch = entry['exchange']
        contract = Index(sym, exch, 'USD')
        try:
            await ib.qualifyContractsAsync(contract)
            if contract.conId:
                return contract, sym, entry.get('name', sym), False  # not fallback
        except Exception as e:
            print(f'[bridge] index qualify failed for {sym} ({exch}): {e}')
        # Fallback to ETF proxy.
        fallback_sym = entry.get('etf_fallback')
        if fallback_sym:
            print(f'[bridge] WARNING: {sym} unavailable as Index; falling back to ETF {fallback_sym}')
            etf = Stock(fallback_sym, 'SMART', 'USD')
            try:
                await ib.qualifyContractsAsync(etf)
                if etf.conId:
                    # Tag name so the JSON makes it obvious we proxied.
                    return etf, sym, f"{entry.get('name', sym)} (via {fallback_sym})", True
            except Exception as e:
                print(f'[bridge] ETF fallback {fallback_sym} also failed: {e}')
        return None, sym, entry.get('name', sym), False

    # kind == 'stock' (also used for regular ETFs — same contract type)
    sym = entry['symbol']
    contract = Stock(sym, 'SMART', 'USD')
    try:
        await ib.qualifyContractsAsync(contract)
        if contract.conId:
            return contract, sym, entry.get('name', sym), False
    except Exception as e:
        print(f'[bridge] stock qualify failed for {sym}: {e}')
    return None, sym, entry.get('name', sym), False


def _record_from_bars(symbol: str, name: str, bars, as_of_iso: str) -> dict | None:
    """Build the per-symbol JSON record from a daily-bars response.

    Bars come back oldest-first. We index from the end:
      bars[-1] → today's (or most recent) close
      bars[-2] → prior close → d1
      bars[-6] → 5 sessions ago → d5 (needs ≥ 6 bars)
      bars[-22] → ~1 month ago → m1 (needs ≥ 22 bars)

    If the series is shorter than needed, the corresponding pct is left
    None rather than raising — newly-listed symbols (e.g. recent IPOs)
    simply don't have a 1M number yet, which the frontend handles.
    """
    if not bars:
        return None

    closes = [_num(b.close) for b in bars]
    if not closes or closes[-1] is None:
        return None

    close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else None
    close_5d = closes[-6] if len(closes) >= 6 else None
    close_1m = closes[-22] if len(closes) >= 22 else None  # ~21 trading days
    # m3_pct left None for Stage 1 per spec; add here if/when needed:
    # close_3m = closes[-64] if len(closes) >= 64 else None

    return {
        'symbol': symbol,
        'name': name,
        'close': close,
        'prev_close': prev_close,
        'd1_pct': _pct_change(close, prev_close),
        'd5_pct': _pct_change(close, close_5d),
        'm1_pct': _pct_change(close, close_1m),
        'm3_pct': None,
        'volume': _num(bars[-1].volume),
        'mkt_cap_bn': None,  # requires reqFundamentalData; out of scope for Stage 1
        'last_updated': as_of_iso,
    }


async def _fetch_symbol(ib: IB, contract, symbol: str, name: str, as_of_iso: str) -> SymbolResult:
    """Fetch daily bars + build the record. Any IB error short-circuits
    to a SymbolResult with `error` populated so the caller can decide
    exit code without blowing up the whole run."""
    try:
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime='',
            durationStr=DURATION,
            barSizeSetting=BAR_SIZE,
            whatToShow=WHAT_TO_SHOW,
            useRTH=USE_RTH,
            formatDate=FORMAT_DATE,
        )
    except Exception as e:
        return SymbolResult(symbol, name, None, error=f'reqHistoricalData: {e}')

    rec = _record_from_bars(symbol, name, bars, as_of_iso)
    if rec is None:
        return SymbolResult(symbol, name, None, error='empty bars series')
    return SymbolResult(symbol, name, rec)


async def fetch_group(ib: IB, entries: list[dict], kind: str, as_of_iso: str) -> list[SymbolResult]:
    """Sequentially fetch every entry in a group with pacing.

    We qualify each contract lazily (one at a time, spaced) rather than
    batching a single `qualifyContractsAsync(*many)` call because:
      - the IB-side qualify latency on a cold Gateway is already small,
      - interleaving qualify + historical makes the progress log linear
        per symbol (nicer for humans watching stdout),
      - pacing is dominated by the historical requests anyway.
    """
    results: list[SymbolResult] = []
    total = len(entries)
    for i, entry in enumerate(entries, 1):
        sym = entry['symbol']
        print(f'[bridge] [{i}/{total}] qualifying + fetching {sym} ...', flush=True)

        contract, canonical_sym, display_name, is_fallback = await _qualify_one(ib, entry, kind)
        if contract is None:
            print(f'[bridge] [{i}/{total}] FAILED to qualify {sym}')
            results.append(SymbolResult(sym, entry.get('name', sym), None, error='qualify failed'))
            await asyncio.sleep(REQUEST_SPACING_S)
            continue

        res = await _fetch_symbol(ib, contract, canonical_sym, display_name, as_of_iso)
        if res.error:
            print(f'[bridge] [{i}/{total}] {sym}: {res.error}')
        else:
            d1 = res.record.get('d1_pct')
            d1_txt = f'{d1:+.2f}%' if d1 is not None else 'n/a'
            print(f'[bridge] [{i}/{total}] {sym} close={res.record["close"]:.2f} d1={d1_txt}')
        results.append(res)

        # Pace between requests regardless of success/failure — a 162-ms
        # error reply still counts against IB's sliding 10-min window.
        await asyncio.sleep(REQUEST_SPACING_S)

    return results


# ======================================================================
# Dry-run (mock) data source
# ======================================================================

def _mock_record(symbol: str, name: str, seed: int, as_of_iso: str) -> dict:
    """Deterministic fake snapshot for `--dry-run`. No network, no IB.

    Uses `hash(symbol)` as a seed so values are stable across runs for
    a given symbol — handy when A1 is styling the UI and wants to
    eyeball consistent mock values. Numbers are plausible but obviously
    synthetic (close prices in 50-500 range, day moves -5% to +5%).
    """
    h = abs(hash(f'{symbol}:{seed}'))
    close = round(50 + (h % 45000) / 100.0, 2)         # 50.00 .. 500.00
    d1 = round(((h % 1001) - 500) / 100.0, 2)          # -5.00 .. +5.00
    d5 = round((((h >> 7) % 2001) - 1000) / 100.0, 2)  # -10.00 .. +10.00
    m1 = round((((h >> 13) % 4001) - 2000) / 100.0, 2)  # -20.00 .. +20.00
    prev_close = round(close / (1 + d1 / 100.0), 2) if d1 is not None else close
    volume = 1_000_000 + (h % 50_000_000)
    return {
        'symbol': symbol,
        'name': name,
        'close': close,
        'prev_close': prev_close,
        'd1_pct': d1,
        'd5_pct': d5,
        'm1_pct': m1,
        'm3_pct': None,
        'volume': volume,
        'mkt_cap_bn': None,
        'last_updated': as_of_iso,
    }


def build_mock_snapshot(as_of_iso: str) -> dict:
    """Build the same JSON shape as the live run, from deterministic
    mock values. Useful for: (a) frontend development without IB,
    (b) CI smoke tests, (c) proving the output schema before anyone
    has Gateway credentials."""
    return {
        'indices':       [_mock_record(x['symbol'], x.get('display', x['name']), 1, as_of_iso) for x in universe.INDICES],
        'sectors':       [_mock_record(x['symbol'], x['name'], 2, as_of_iso) for x in universe.SECTORS],
        'themes_etfs':   [_mock_record(x['symbol'], x['name'], 3, as_of_iso) for x in universe.THEME_ETFS],
        'themes_stocks': [_mock_record(x['symbol'], x['name'], 4, as_of_iso) for x in universe.THEME_STOCKS],
    }


# ======================================================================
# JSON writing
# ======================================================================

def results_to_records(results: list[SymbolResult]) -> tuple[list[dict], list[str]]:
    """Split SymbolResult list into (good records, failed symbols)."""
    good = [r.record for r in results if r.record is not None]
    bad = [r.symbol for r in results if r.record is None]
    return good, bad


def write_snapshot(date_str: str, payload: dict) -> Path:
    """Write `market.json` into `src/data/{date}/`, creating the
    directory if needed. Any sibling JSON files already in that folder
    (events.json, macro.json, ...) are left untouched — this function
    only writes its own file.

    Returns the absolute path written, so the caller can echo it."""
    out_dir = DATA_ROOT / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / 'market.json'
    with out_path.open('w', encoding='utf-8') as f:
        # indent=2 + ensure_ascii=False so Chinese sector names render
        # as-is (utf-8) rather than \uXXXX escape sequences.
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write('\n')
    return out_path


# ======================================================================
# Main
# ======================================================================

async def run_live(date_str: str, as_of_iso: str) -> tuple[dict, list[str]]:
    """Connect to IB and fetch all four groups. Returns (payload, failed_symbols)."""
    ib = IB()
    print(f'[bridge] connecting to IB at {config.IB_HOST}:{config.IB_PORT} '
          f'(clientId={config.IB_CLIENT_ID}) ...', flush=True)
    # readonly=True → IB server rejects orders from this client; paired
    # with lockdown() for belt-and-suspenders. ib_insync also skips its
    # default startup reqOpenOrders / reqCompletedOrders calls, which
    # hang forever when Gateway is in "Read-Only API" mode.
    await ib.connectAsync(config.IB_HOST, config.IB_PORT,
                          clientId=config.IB_CLIENT_ID, readonly=True)
    print('[bridge] connected (readonly)')

    lockdown(ib)
    print('[bridge] trading methods locked')

    try:
        # Delayed data works fine for end-of-day snapshots, but if the
        # account is entitled to live we use live (free upgrade). 3 =
        # delayed, 1 = live. We don't force either; whatever the account
        # defaults to is acceptable for EOD.
        #
        # Intentionally NOT calling ib.reqMarketDataType() — daily bars
        # from reqHistoricalData are independent of the streaming tick
        # type, and the default behaviour is best here.

        all_failed: list[str] = []

        print('\n[bridge] === Indices ===')
        idx_results = await fetch_group(ib, universe.INDICES, 'index', as_of_iso)
        print('\n[bridge] === Sectors ===')
        sec_results = await fetch_group(ib, universe.SECTORS, 'stock', as_of_iso)
        print('\n[bridge] === Theme ETFs ===')
        te_results = await fetch_group(ib, universe.THEME_ETFS, 'stock', as_of_iso)
        print('\n[bridge] === Theme Stocks ===')
        ts_results = await fetch_group(ib, universe.THEME_STOCKS, 'stock', as_of_iso)

        idx, bad = results_to_records(idx_results); all_failed += bad
        sec, bad = results_to_records(sec_results); all_failed += bad
        te,  bad = results_to_records(te_results);  all_failed += bad
        ts,  bad = results_to_records(ts_results);  all_failed += bad

        payload = {
            'indices': idx,
            'sectors': sec,
            'themes_etfs': te,
            'themes_stocks': ts,
        }
        return payload, all_failed
    finally:
        # Always disconnect, even on exception, so we don't leak the
        # clientId slot on IB Gateway (future runs would fail with a
        # "clientId already in use" error until Gateway restart).
        try:
            ib.disconnect()
            print('[bridge] disconnected from IB')
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Daily end-of-day snapshot to src/data/{date}/market.json')
    p.add_argument('--date', help='Target date YYYY-MM-DD (default: today in America/New_York)')
    p.add_argument('--dry-run', action='store_true',
                   help='Skip IB entirely; write deterministic mock data. Useful for frontend dev.')
    return p.parse_args()


async def main_async() -> int:
    args = parse_args()

    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now(ET).date().isoformat()
    as_of_iso = datetime.now(ET).isoformat(timespec='seconds')

    print('=' * 60)
    print(f'  US-STOCK DAILY SUMMARY — snapshot job')
    print(f'  target date: {date_str}')
    print(f'  mode: {"DRY-RUN (mock data)" if args.dry_run else "LIVE (IB Gateway)"}')
    print(f'  output: src/data/{date_str}/market.json')
    print('=' * 60)

    if args.dry_run:
        payload = build_mock_snapshot(as_of_iso)
        failed: list[str] = []
    else:
        payload, failed = await run_live(date_str, as_of_iso)

    payload['generated_at'] = as_of_iso
    payload['date'] = date_str

    out_path = write_snapshot(date_str, payload)

    counts = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
    print('\n' + '=' * 60)
    print(f'  wrote {out_path}')
    print(f'  counts: {counts}')
    if failed:
        print(f'  FAILED symbols ({len(failed)}): {", ".join(failed)}')
        print('  exit code: 1 (partial success)')
        print('=' * 60)
        return 1
    print('  all symbols succeeded')
    print('  exit code: 0')
    print('=' * 60)
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print('\n[bridge] interrupted by user')
        return 130


if __name__ == '__main__':
    sys.exit(main())
