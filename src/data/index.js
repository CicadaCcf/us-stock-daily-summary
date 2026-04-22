// Data loader — picks the latest date's JSON bundle.
//
// For now this is a static import of today's (2026-04-20) data. Vite will
// inline these JSON blobs at build time. When we add a multi-date picker,
// swap these to dynamic imports keyed on the selected date.
//
// Fallback: if any file is missing or malformed, consumers should treat
// arrays as empty. We normalize shape here so App.jsx can assume fields.

import snapshot from './2026-04-20/snapshot.json';
import market from './2026-04-20/market.json';
import screener from './2026-04-20/screener.json';
import breadth from './2026-04-20/breadth.json';
import alpha from './2026-04-20/alpha.json';
import events from './2026-04-20/events.json';
import macro from './2026-04-20/macro.json';

// --- Shape normalizers ---------------------------------------------------

const toArray = (v) => (Array.isArray(v) ? v : []);

export const CURRENT_DATE = '2026-04-20';

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
      value = (r.close / 10).toFixed(2) + '%'; // TNX is yield × 10
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
    name: r.name,
    d1: r.d1_pct ?? 0,
    d5: r.d5_pct ?? 0,
    m1: r.m1_pct ?? 0,
  }));
}

export const INDICES = marketIndicesToDisplay(market.indices) || toArray(snapshot.indices);
export const SECTORS = marketSectorsToDisplay(market.sectors) || toArray(snapshot.sectors);
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

export const ALPHA = toArray(alpha.rows);
export const EVENTS = toArray(events.events);
export const MACRO_TOPICS = toArray(macro.topics);
