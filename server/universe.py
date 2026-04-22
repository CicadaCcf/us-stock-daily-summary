"""Symbol universe for the daily end-of-day snapshot.

Kept as Python constants (rather than a JSON file) because these lists
are small, change rarely, and editing Python is friendlier than editing
JSON when you need to add a comment next to a new ticker. If this ever
grows past a few hundred symbols, convert to JSON/CSV loaded at startup.

Everything is grouped by section matching the frontend `App.jsx` layout:

  * INDICES     - top-bar cards (6)
  * SECTORS     - Sector Performance bars (11 SPDR ETFs, one per GICS sector)
  * THEME_ETFS  - union of ETF symbols appearing in any THEMES entry
  * THEME_STOCKS - union of stock tickers appearing in any THEMES entry

The `name` fields are short English/Chinese labels used purely for
frontend display; the bridge copies them straight into the output JSON
so A1's loader has something human-readable without maintaining a
separate lookup table.
"""

# ---- Indices ----------------------------------------------------------
# IB's Index contract type needs an exchange hint. SPX / VIX / TNX all
# live on CBOE; NDX on NASDAQ; INDU and RUT on NYSE (yes, RUT moved to
# Cboe historically but IB still routes it via NYSE for the index
# contract; the `RUT` index symbol on CBOE also works, so we try both
# in the bridge).
#
# `etf_fallback` is the ticker we swap in if qualifyContracts() fails
# for the index contract (common on paper / delayed-data accounts that
# don't have index permissions). The ETF proxies track the same thing
# closely enough for a daily % snapshot.
INDICES = [
    {'symbol': 'SPX',  'name': 'S&P 500 Index',        'exchange': 'CBOE',   'etf_fallback': 'SPY',  'display': 'S&P 500'},
    {'symbol': 'NDX',  'name': 'Nasdaq 100 Index',     'exchange': 'NASDAQ', 'etf_fallback': 'QQQ',  'display': 'Nasdaq 100'},
    {'symbol': 'INDU', 'name': 'Dow Jones Industrial', 'exchange': 'CBOE',   'etf_fallback': 'DIA',  'display': 'Dow Jones'},
    {'symbol': 'RUT',  'name': 'Russell 2000 Index',   'exchange': 'RUSSELL','etf_fallback': 'IWM',  'display': 'Russell 2000'},
    {'symbol': 'VIX',  'name': 'CBOE Volatility Index','exchange': 'CBOE',   'etf_fallback': 'VIXY', 'display': 'VIX'},
    {'symbol': 'TNX',  'name': '10Y Treasury Yield x10','exchange': 'CBOE',  'etf_fallback': 'TLT',  'display': '10Y Treasury'},
]

# ---- Sector SPDRs (11 GICS sectors) -----------------------------------
# Order & labels match the SECTORS array in src/App.jsx so the frontend
# loader can render them in the same order without re-sorting.
SECTORS = [
    {'symbol': 'XLK',  'name': '信息技术 Technology'},
    {'symbol': 'XLC',  'name': '通信服务 Comm.'},
    {'symbol': 'XLY',  'name': '非必需消费 Disc.'},
    {'symbol': 'XLI',  'name': '工业 Industrials'},
    {'symbol': 'XLF',  'name': '金融 Financials'},
    {'symbol': 'XLB',  'name': '材料 Materials'},
    {'symbol': 'XLRE', 'name': '房地产 Real Estate'},
    {'symbol': 'XLU',  'name': '公用事业 Utilities'},
    {'symbol': 'XLV',  'name': '医疗健康 Healthcare'},
    {'symbol': 'XLE',  'name': '能源 Energy'},
    {'symbol': 'XLP',  'name': '必需消费 Staples'},
]

# ---- Theme ETFs (union across all themes in App.jsx, deduped) ---------
# Deriving from App.jsx THEMES: SMH, SOXX, ARKK, IGV, SNDK, XLK, QTUM,
# ICLN, URA, KWEB, FXI. XLK is already in SECTORS but we keep it here
# too so the theme view renders identically if the loader looks it up
# from this list (cheap to duplicate, no request savings either way
# since historical data is per-conId and SMART-routed once).
THEME_ETFS = [
    {'symbol': 'SMH',  'name': 'VanEck Semiconductor ETF'},
    {'symbol': 'SOXX', 'name': 'iShares Semiconductor ETF'},
    {'symbol': 'ARKK', 'name': 'ARK Innovation ETF'},
    {'symbol': 'IGV',  'name': 'iShares Expanded Tech-Software ETF'},
    {'symbol': 'SNDK', 'name': 'SanDisk Corporation'},      # ETF slot in theme list, actually a stock; kept because App.jsx has it.
    {'symbol': 'XLK',  'name': 'Technology Select Sector SPDR'},
    {'symbol': 'QTUM', 'name': 'Defiance Quantum ETF'},
    {'symbol': 'ICLN', 'name': 'iShares Global Clean Energy ETF'},
    {'symbol': 'URA',  'name': 'Global X Uranium ETF'},
    {'symbol': 'KWEB', 'name': 'KraneShares CSI China Internet ETF'},
    {'symbol': 'FXI',  'name': 'iShares China Large-Cap ETF'},
]

# ---- Theme constituent stocks (union across all themes, deduped) ------
# SNDK also appears in THEME_ETFS above (App.jsx has it in both slots);
# that's fine — the bridge fetches each unique symbol once and emits
# under whichever section the caller asks for.
THEME_STOCKS = [
    {'symbol': 'NVDA', 'name': 'NVIDIA Corp.'},
    {'symbol': 'SMCI', 'name': 'Super Micro Computer'},
    {'symbol': 'AVGO', 'name': 'Broadcom Inc.'},
    {'symbol': 'PLTR', 'name': 'Palantir Technologies'},
    {'symbol': 'APP',  'name': 'AppLovin Corp.'},
    {'symbol': 'AI',   'name': 'C3.ai Inc.'},
    {'symbol': 'NTNX', 'name': 'Nutanix Inc.'},
    {'symbol': 'PSTG', 'name': 'Pure Storage Inc.'},
    {'symbol': 'SNDK', 'name': 'SanDisk Corporation'},
    {'symbol': 'QBTS', 'name': 'D-Wave Quantum Inc.'},
    {'symbol': 'IONQ', 'name': 'IonQ Inc.'},
    {'symbol': 'RGTI', 'name': 'Rigetti Computing'},
    {'symbol': 'VST',  'name': 'Vistra Corp.'},
    {'symbol': 'CEG',  'name': 'Constellation Energy'},
    {'symbol': 'SMR',  'name': 'NuScale Power Corp.'},
    {'symbol': 'BABA', 'name': 'Alibaba Group'},
    {'symbol': 'PDD',  'name': 'PDD Holdings Inc.'},
    {'symbol': 'JD',   'name': 'JD.com Inc.'},
]


def all_unique_symbols() -> list[str]:
    """Return the deduped set of every ticker we'll need to fetch.

    Useful when planning request pacing — total count drives how long
    the snapshot job will take at ~0.5s per symbol.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for group in (INDICES, SECTORS, THEME_ETFS, THEME_STOCKS):
        for item in group:
            # Indices also expose `etf_fallback`; not counted here because
            # we only fall back when the index fetch fails, so in the
            # happy path we never hit it. Pacing allowance is fine.
            if item['symbol'] not in seen:
                seen.add(item['symbol'])
                ordered.append(item['symbol'])
    return ordered
