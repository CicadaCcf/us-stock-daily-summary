// Data loader — picks the latest date folder under src/data/ automatically.
//
// At build time Vite's import.meta.glob eager-imports every `./YYYY-MM-DD/*.json`
// file and inlines the JSON. At runtime we pick the lexicographically-largest
// date (ISO YYYY-MM-DD sorts correctly) and expose that day's fields.
//
// Daily cron just writes a new `src/data/{trading_date}/` folder + commits —
// nothing else has to change for the frontend to display the new day.
//
// Fallback: any missing file for the latest date falls back to an empty object
// so App.jsx never crashes on a half-populated folder (e.g. cron ran but macro
// hasn't been pasted yet).

const pickLatestDate = (glob) => {
  const dates = Object.keys(glob)
    .map((p) => p.match(/\.\/([\d-]+)\//)?.[1])
    .filter(Boolean)
    .sort();
  return dates[dates.length - 1] || null;
};

const pickFor = (glob, date, filename) =>
  (date && glob[`./${date}/${filename}`]) || {};

// Eager-glob every per-date file type. Vite inlines the JSON at build time.
const snapshotMap = import.meta.glob('./*/snapshot.json', { eager: true, import: 'default' });
const marketMap   = import.meta.glob('./*/market.json',   { eager: true, import: 'default' });
const screenerMap = import.meta.glob('./*/screener.json', { eager: true, import: 'default' });
const breadthMap  = import.meta.glob('./*/breadth.json',  { eager: true, import: 'default' });
const eventsMap   = import.meta.glob('./*/events.json',   { eager: true, import: 'default' });
const macroMap    = import.meta.glob('./*/macro.json',    { eager: true, import: 'default' });

// market.json is the canonical per-day marker — it's written by the daily
// snapshot cron, so its presence is the authoritative signal that a date
// folder is "ready" to be shown. macro/events may lag a day.
const LATEST_DATE = pickLatestDate(marketMap);

const snapshot = pickFor(snapshotMap, LATEST_DATE, 'snapshot.json');
const market   = pickFor(marketMap,   LATEST_DATE, 'market.json');
const screener = pickFor(screenerMap, LATEST_DATE, 'screener.json');
const breadth  = pickFor(breadthMap,  LATEST_DATE, 'breadth.json');
const events   = pickFor(eventsMap,   LATEST_DATE, 'events.json');
const macro    = pickFor(macroMap,    LATEST_DATE, 'macro.json');

// --- Shape normalizers ---------------------------------------------------

const toArray = (v) => (Array.isArray(v) ? v : []);

export const CURRENT_DATE = LATEST_DATE || '';

// Transform market.json (IB raw shape {symbol, name, close, d1_pct, ...})
// into the UI display shape {label, value, change, dir} that App.jsx expects.
// Returns `null` if market has no data — caller falls back to snapshot.
function marketIndicesToDisplay(rows) {
  if (!rows || rows.length === 0) return null;
  return rows.map((r) => {
    const pct = r.d1_pct;
    // 10Y yield: show as "4.28%" (close is the yield itself for TNX index)
    // VIX: show as 16.82
    // Others: formatted price
    let value;
    if (r.symbol === 'TNX') {
      // Yahoo returns TNX already as yield (e.g. 4.28) — show with % suffix
      value = r.close.toFixed(2) + '%';
    } else if (typeof r.close === 'number') {
      value = r.close.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    } else {
      value = 'N/A';
    }
    const change =
      pct == null
        ? 'N/A'
        : (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
    return { label: r.name, value, change, dir: (pct ?? 0) >= 0 ? 'up' : 'down' };
  });
}

function marketSectorsToDisplay(rows) {
  if (!rows || rows.length === 0) return null;
  return rows.map((r) => ({
    symbol: r.symbol,
    name: r.name,
    close: r.close,
    d1: r.d1_pct ?? 0,
    d5: r.d5_pct ?? 0,
    m1: r.m1_pct ?? 0,
    m3: r.m3_pct ?? 0,
    closes_1y: Array.isArray(r.closes_1y) ? r.closes_1y : [],
    volumes_1y: Array.isArray(r.volumes_1y) ? r.volumes_1y : [],
  }));
}

export const INDICES = marketIndicesToDisplay(market.indices) || toArray(snapshot.indices);
export const SECTORS = marketSectorsToDisplay(market.sectors) || toArray(snapshot.sectors);

// Raw indices with sparklines (for benchmark line in combined sector chart)
export const INDICES_RAW = toArray(market.indices);
export const SPX_CLOSES_1Y = (INDICES_RAW.find((r) => r.symbol === 'GSPC')?.closes_1y) || [];
export const SECTOR_MAX_ABS = typeof snapshot.sectorMaxAbs === 'number' ? snapshot.sectorMaxAbs : 2.5;
export const THEMES = toArray(snapshot.themes);
export const MARKET_GENERATED_AT = market.generated_at || null;

export const HEADER_DATE = snapshot.headerDate || '';
export const HEADER_SUB = snapshot.headerSub || '';
export const FOOTER_TIMESTAMP = snapshot.footerTimestamp || '';
export const RECAP_SECTIONS = toArray(snapshot.recap?.sections);

export const SCREENER = toArray(screener.rows);
export const SCREENER_MAX_ABS = typeof screener.maxAbs === 'number' ? screener.maxAbs : 500;

// Breadth — provide safe defaults for every field so the JSX never errors.
export const BREADTH = {
  advancers: 0,
  decliners: 0,
  unchanged: 0,
  adUpPct: 0,
  adUncPct: 0,
  adDownPct: 0,
  volUpPct: 0,
  volDownPct: 0,
  putCall: 0,
  vix: 0,
  new52wHigh: 0,
  new52wLow: 0,
  pctAbove50dma: 0,
  pctAbove200dma: 0,
  ...(breadth || {}),
};

export const EVENTS = toArray(events.events);
export const MACRO_TOPICS = toArray(macro.topics);
