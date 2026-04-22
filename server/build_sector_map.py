#!/usr/bin/env python3
"""
One-time / periodic build of an SP500 sector-constituent map.

Pulls the S&P 500 companies table from Wikipedia, extracts each row's
Symbol + GICS Sector, and maps GICS → SPDR select-sector ETF (XL*). The
result lives at server/cache/sector_map.json and is used by
polygon_snapshot.py to compute a "per-sector total dollar volume"
across ~500 real constituents, a better heat proxy than the ETF's own
volume. Regenerate every few months (constituents churn slowly).

    python3 server/build_sector_map.py
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# GICS sector name (Wikipedia column) → SPDR select-sector ETF symbol.
GICS_TO_ETF = {
    "Information Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Industrials":            "XLI",
    "Financials":             "XLF",
    "Materials":              "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Health Care":            "XLV",
    "Energy":                 "XLE",
    "Consumer Staples":       "XLP",
}

ROOT = Path(__file__).resolve().parent.parent
OUT  = ROOT / "server" / "cache" / "sector_map.json"


def fetch_html() -> str:
    proxy = __import__('os').environ.get('HTTPS_PROXY') or __import__('os').environ.get('HTTP_PROXY')
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
        )
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(URL, headers={'User-Agent': 'Mozilla/5.0'})
    with opener.open(req, timeout=30) as resp:
        return resp.read().decode('utf-8')


def parse(html: str) -> dict:
    m = re.search(r'<table[^>]*id="constituents"[^>]*>(.+?)</table>', html, re.DOTALL)
    if not m:
        raise RuntimeError('constituents table not found in HTML')
    table_html = m.group(1)

    by_sector: dict[str, list[str]] = {etf: [] for etf in GICS_TO_ETF.values()}
    rows = re.finditer(r'<tr[^>]*>(.+?)</tr>', table_html, re.DOTALL)
    for row in rows:
        cells = re.findall(r'<t[dh][^>]*>(.+?)</t[dh]>', row.group(1), re.DOTALL)
        if len(cells) < 4:
            continue
        # Column 0 = Symbol, column 2 = GICS Sector (Wikipedia's S&P 500 layout).
        ticker = re.sub(r'<[^>]+>', '', cells[0]).strip()
        sector = re.sub(r'<[^>]+>', '', cells[2]).strip()
        # Wikipedia uses BRK.B / BF.B — Polygon grouped-daily returns the same
        # format as-is, so no transform needed.
        etf = GICS_TO_ETF.get(sector)
        if etf and ticker and re.match(r'^[A-Z][A-Z.\-]*$', ticker):
            by_sector[etf].append(ticker)
    return by_sector


def main():
    print(f'[info] fetching {URL}')
    html = fetch_html()
    by_sector = parse(html)
    total = sum(len(v) for v in by_sector.values())
    if total < 450:
        print(f'[warn] only {total} tickers parsed — Wikipedia layout may have changed', file=sys.stderr)
    for etf in sorted(by_sector):
        print(f'  {etf}: {len(by_sector[etf])} tickers')
    out = {
        'generated_at':      datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source':            URL,
        'constituent_count': total,
        'tickers_by_sector': by_sector,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    print(f'[done] wrote {OUT} ({total} SP500 tickers)')


if __name__ == '__main__':
    main()
