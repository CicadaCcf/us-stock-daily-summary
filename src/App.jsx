// 1:1 replica of https://angran.github.io/us-stock-daily-summary/
// Stage-1 refactor: data extracted to src/data/<date>/*.json (Stage-1 A1).
// Rendering logic is unchanged — only the data source moved.
//

import {
  INDICES,
  SECTORS,
  SECTOR_MAX_ABS,
  SCREENER,
  SCREENER_MAX_ABS,
  THEMES,
  EVENTS,
  BREADTH,
  MACRO_TOPICS,
  HEADER_DATE,
  HEADER_SUB,
  FOOTER_TIMESTAMP,
  CURRENT_DATE,
  AVAILABLE_DATES,
  SPX_CLOSES_1Y,
  TRADING_DATES_1Y,
  MOVERS_NEWS,
} from './data/index.js';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend, ReferenceLine,
  ResponsiveContainer, CartesianGrid,
} from 'recharts';
import { useState, useEffect, useMemo, useRef, createContext, useContext } from 'react';
import AdminDrawer from './components/AdminDrawer.jsx';
import { fetchEditsForDate, saveEdit } from './lib/screenerEdits.js';

// Context so EditableCell / ReasonCell can trigger a refetch after save
// without prop-drilling through the whole App tree.
const ScreenerEditsCtx = createContext({ edits: {}, reload: () => {} });

// --- Lightbox: single shared overlay for all clickable images ------------
const LightboxCtx = createContext({ open: () => {} });

function LightboxProvider({ children }) {
  const [src, setSrc] = useState(null);
  useEffect(() => {
    if (!src) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') setSrc(null); };
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = '';
      window.removeEventListener('keydown', onKey);
    };
  }, [src]);
  const open = (url) => setSrc(url);
  return (
    <LightboxCtx.Provider value={{ open }}>
      {children}
      {src && (
        <div
          onClick={() => setSrc(null)}
          style={{
            position: 'fixed', inset: 0, zIndex: 200,
            background: 'rgba(0,0,0,.82)', display: 'flex',
            alignItems: 'center', justifyContent: 'center',
            padding: 20, cursor: 'zoom-out',
          }}
        >
          <img
            src={src}
            onClick={(e) => e.stopPropagation()}
            style={{
              maxWidth: '95vw', maxHeight: '95vh',
              boxShadow: '0 10px 40px rgba(0,0,0,.6)',
              borderRadius: 4,
            }}
            alt=""
          />
          <button
            onClick={() => setSrc(null)}
            style={{
              position: 'absolute', top: 16, right: 20,
              width: 36, height: 36, border: 0, borderRadius: '50%',
              background: 'rgba(255,255,255,.12)', color: '#fff',
              fontSize: 22, cursor: 'pointer', lineHeight: 1,
            }}
            title="Close (Esc)"
          >×</button>
        </div>
      )}
    </LightboxCtx.Provider>
  );
}
function useLightbox() { return useContext(LightboxCtx); }

// --- Helpers ------------------------------------------------------------
const fmtPct = (v) => v == null ? 'N/A' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
const pnClass = (v) => v == null ? '' : v >= 0 ? 'up' : 'down';

// --- Finviz bubble map section: shows public/finviz_bubble.png if it exists,
// otherwise a placeholder. The daily screenshot job is server/finviz_screenshot.py.
function FinvizBubble() {
  const { open: openLightbox } = useLightbox();
  const [imgOk, setImgOk] = useState(null); // null = untested, true/false after load
  // Cache-busting param so each reload re-tests the latest screenshot
  const src = `/finviz_bubble.png?v=${Date.now()}`;
  useEffect(() => {
    const img = new Image();
    img.onload  = () => setImgOk(true);
    img.onerror = () => setImgOk(false);
    img.src = src;
    return () => { img.onload = img.onerror = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className="section">
      <div className="section-title">全市场气泡图 Finviz Bubble Map</div>
      <div className="finviz-wrap">
        {imgOk ? (
          <button
            type="button"
            className="finviz-img-btn"
            onClick={() => openLightbox(src)}
            title="点击放大"
          >
            <img src={src} alt="Finviz bubble map" />
          </button>
        ) : (
          <div className="finviz-placeholder">
            <span>
              气泡图占位 — 跑 <code>python server/finviz_screenshot.py</code> 自动截图，
              或手工保存为 <code>public/finviz_bubble.png</code>
            </span>
            <a
              href="https://finviz.com/bubbles.ashx"
              target="_blank"
              rel="noreferrer"
              style={{ fontSize: 11, color: 'var(--blue)' }}
            >
              打开 Finviz ↗
            </a>
          </div>
        )}
      </div>
    </div>
  );
}

// --- Sparkline SVG: compact line chart from an array of closes -----------
const SPARK_W = 140;
const SPARK_H = 28;
function Sparkline({ closes, width = SPARK_W, height = SPARK_H }) {
  if (!closes || closes.length < 2) {
    return <div style={{ width, height }} />;
  }
  let min = Infinity, max = -Infinity;
  for (const c of closes) {
    if (c < min) min = c;
    if (c > max) max = c;
  }
  const range = max - min || 1;
  const step = width / (closes.length - 1);
  const pad = 1;
  const d = closes.map((p, i) => {
    const x = i * step;
    const y = height - pad - ((p - min) / range) * (height - pad * 2);
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const isUp = closes[closes.length - 1] >= closes[0];
  const stroke = isUp ? 'var(--green)' : 'var(--red)';
  const fill = isUp ? 'rgba(0,184,148,.12)' : 'rgba(225,112,85,.12)';
  const fillPath = `${d} L${width},${height} L0,${height} Z`;
  return (
    <svg width={width} height={height} style={{ display: 'block' }} aria-hidden>
      <path d={fillPath} fill={fill} stroke="none" />
      <path d={d} stroke={stroke} fill="none" strokeWidth={1.5} />
    </svg>
  );
}

// --- Sector Performance section: period selector + sparkline per sector --
const SECTOR_PERIODS = [
  { id: '1W', label: '1W', days: 5 },
  { id: '1M', label: '1M', days: 22 },
  { id: '3M', label: '3M', days: 66 },
  { id: '6M', label: '6M', days: 126 },
  { id: '1Y', label: '1Y', days: 252 },
];

// 11 distinguishable colors (one per sector). Bright enough for dark bg.
const SECTOR_COLORS = {
  'XLK':  '#00b894',  // Technology — green
  'XLC':  '#0984e3',  // Comm — blue
  'XLY':  '#fdcb6e',  // Discretionary — gold
  'XLI':  '#a29bfe',  // Industrials — lilac
  'XLF':  '#00cec9',  // Financials — teal
  'XLB':  '#fab1a0',  // Materials — peach
  'XLRE': '#81ecec',  // Real Estate — cyan
  'XLU':  '#ffeaa7',  // Utilities — cream
  'XLV':  '#e17055',  // Healthcare — red-orange
  'XLE':  '#d63031',  // Energy — red
  'XLP':  '#b2bec3',  // Staples — grey
};

function SectorPerformance() {
  const [period, setPeriod] = useState('1M');
  const p = SECTOR_PERIODS.find((x) => x.id === period) || SECTOR_PERIODS[1];

  // Build normalized chart data: each row = one day, each sector column
  // holds its % change from the start of the selected period.
  // `__spx` = SPX benchmark dashed line.
  const chartData = useMemo(() => {
    const N = p.days;
    // Gather slices per sector
    const slices = SECTORS.map((s) => ({
      s,
      closes: Array.isArray(s.closes_1y) ? s.closes_1y.slice(-N) : [],
    })).filter((x) => x.closes.length >= 2);
    const spx = Array.isArray(SPX_CLOSES_1Y) ? SPX_CLOSES_1Y.slice(-N) : [];
    const maxLen = Math.max(
      spx.length,
      ...slices.map((x) => x.closes.length),
    );
    if (maxLen < 2) return { data: [], sectors: slices };
    // Slice the trading-date list to the same period. These dates drive the
    // X-axis labels below; alignment is by index (i=0 is period start).
    const dates = Array.isArray(TRADING_DATES_1Y) ? TRADING_DATES_1Y.slice(-N) : [];
    const data = [];
    for (let i = 0; i < maxLen; i++) {
      const row = { _idx: i, date: dates[i] || '' };
      for (const { s, closes } of slices) {
        if (closes.length >= 2 && i < closes.length && closes[0] > 0) {
          row[s.symbol] = ((closes[i] / closes[0]) - 1) * 100;
        }
      }
      if (spx.length >= 2 && i < spx.length && spx[0] > 0) {
        row.__spx = ((spx[i] / spx[0]) - 1) * 100;
      }
      data.push(row);
    }
    return { data, sectors: slices };
  }, [period]);

  // End-of-period summary metrics for the legend column
  const summary = useMemo(() => {
    const out = {};
    for (const { s, closes } of chartData.sectors) {
      const pct = closes.length >= 2 && closes[0] > 0
        ? ((closes[closes.length - 1] / closes[0]) - 1) * 100
        : 0;
      // Period-average SP500 sector $-volume from sector_dollar_volume_1y cache.
      // Falls back to 0 if the bootstrap hasn't run yet.
      const sectorHist = Array.isArray(s.sector_dollar_volume_1y)
        ? s.sector_dollar_volume_1y.slice(-p.days).filter((x) => typeof x === 'number')
        : [];
      const sectorAvg = sectorHist.length > 0
        ? sectorHist.reduce((a, b) => a + b, 0) / sectorHist.length
        : null;
      out[s.symbol] = { pct, sectorAvg, name: s.name };
    }
    return out;
  }, [chartData, p.days]);

  const fmtUsd = (v) => {
    if (v == null) return '—';
    if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
    if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}M`;
    return `$${Math.round(v)}`;
  };

  // SPX overall: period return from SPX_CLOSES_1Y + period-avg SP500 $-volume
  // (sum across sector histories, per day, then averaged).
  const spxSummary = useMemo(() => {
    const closes = Array.isArray(SPX_CLOSES_1Y) ? SPX_CLOSES_1Y.slice(-p.days) : [];
    const pct = closes.length >= 2 && closes[0] > 0
      ? ((closes[closes.length - 1] / closes[0]) - 1) * 100
      : null;
    // Per-day sum across sectors, then mean over the period.
    const N = p.days;
    let maxLen = 0;
    for (const { s } of chartData.sectors) {
      const h = Array.isArray(s.sector_dollar_volume_1y) ? s.sector_dollar_volume_1y : [];
      if (h.length > maxLen) maxLen = h.length;
    }
    const window = Math.min(N, maxLen);
    let total = null;
    if (window > 0) {
      let sumOfDays = 0;
      for (let i = -window; i < 0; i++) {
        let daySum = 0;
        for (const { s } of chartData.sectors) {
          const h = s.sector_dollar_volume_1y || [];
          const v = h.length + i >= 0 ? h[h.length + i] : null;
          if (typeof v === 'number') daySum += v;
        }
        sumOfDays += daySum;
      }
      total = sumOfDays / window;
    }
    return { pct, avg: total };
  }, [chartData, p.days]);

  const tickFmt = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(0)}%`;

  return (
    <div className="section">
      <div className="section-title-row">
        <div className="section-title">板块表现 Sector Performance</div>
        <div className="period-tabs">
          {SECTOR_PERIODS.map((pp) => (
            <button
              key={pp.id}
              type="button"
              onClick={() => setPeriod(pp.id)}
              className={period === pp.id ? 'active' : ''}
            >
              {pp.label}
            </button>
          ))}
        </div>
      </div>
      <div className="sector-trend-wrap">
        <div className="sector-trend-chart">
          <ResponsiveContainer width="100%" height={320}>
            <LineChart data={chartData.data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="#253655" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="date"
                tick={{ fill: '#8899aa', fontSize: 10 }}
                axisLine={{ stroke: '#253655' }}
                tickLine={{ stroke: '#253655' }}
                minTickGap={42}
                tickFormatter={(d) => {
                  if (!d || typeof d !== 'string') return '';
                  const [, m, day] = d.split('-');
                  return `${parseInt(m, 10)}/${parseInt(day, 10)}`;
                }}
              />
              <YAxis
                tick={{ fill: '#8899aa', fontSize: 11 }}
                tickFormatter={tickFmt}
                axisLine={{ stroke: '#253655' }}
                tickLine={{ stroke: '#253655' }}
              />
              <Tooltip
                contentStyle={{ background: '#16213e', border: '1px solid #253655', fontSize: 11 }}
                labelFormatter={(d) => d || ''}
                formatter={(val, key) => {
                  if (typeof val !== 'number') return ['N/A', key];
                  return [`${val >= 0 ? '+' : ''}${val.toFixed(2)}%`, key];
                }}
              />
              <ReferenceLine y={0} stroke="#555" strokeDasharray="2 2" />
              {chartData.data.length > 0 && SPX_CLOSES_1Y.length > 1 && (
                <Line
                  type="linear"
                  dataKey="__spx"
                  name="SPX"
                  stroke="#e0e0e0"
                  strokeDasharray="5 4"
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                />
              )}
              {chartData.sectors.map(({ s }) => (
                <Line
                  key={s.symbol}
                  type="linear"
                  dataKey={s.symbol}
                  name={s.name}
                  stroke={SECTOR_COLORS[s.symbol] || '#888'}
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
          <div className="sector-trend-note">
            各<b>板块日均成交</b>是该板块 S&amp;P 500 成分股（~500 只，占美股市值 80%+）在所选周期内的日均成交额。
          </div>
        </div>
        <div className="sector-trend-legend">
          <div className="legend-row legend-head">
            <span className="swatch" />
            <span className="name" />
            <span className="pct">涨幅</span>
            <span className="vol">板块日均成交</span>
          </div>
          <div className="legend-row legend-spx">
            <span className="swatch" style={{ borderTop: '2px dashed #e0e0e0' }} />
            <span className="name">SPX 基准</span>
            <span
              className="pct"
              style={{
                color: spxSummary.pct == null ? undefined
                  : spxSummary.pct >= 0 ? 'var(--green)' : 'var(--red)',
              }}
            >
              {spxSummary.pct == null
                ? '—'
                : `${spxSummary.pct >= 0 ? '+' : ''}${spxSummary.pct.toFixed(1)}%`}
            </span>
            <span className="vol">{fmtUsd(spxSummary.avg)}</span>
          </div>
          {chartData.sectors.map(({ s }) => {
            const sum = summary[s.symbol] || {};
            const pct = sum.pct ?? 0;
            const isUp = pct >= 0;
            return (
              <div className="legend-row" key={s.symbol}>
                <span className="swatch" style={{ background: SECTOR_COLORS[s.symbol] }} />
                <span className="name">{s.name} ETF</span>
                <span className="pct" style={{ color: isUp ? 'var(--green)' : 'var(--red)' }}>
                  {isUp ? '+' : ''}{pct.toFixed(1)}%
                </span>
                <span className="vol">{fmtUsd(sum.sectorAvg)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// --- Theme card: collapsed shows name + dots only, expand → ETFs + stocks
function ThemeCard({ theme }) {
  const [open, setOpen] = useState(false);
  const etfCount   = Array.isArray(theme.etfs)   ? theme.etfs.length   : 0;
  const stockCount = Array.isArray(theme.stocks) ? theme.stocks.length : 0;
  const totalCount = etfCount + stockCount;
  const hasMore    = totalCount > 0;
  return (
    <div className={`theme-card${open ? ' open' : ''}`}>
      <button
        type="button"
        className="theme-card-head"
        onClick={() => hasMore && setOpen((v) => !v)}
        disabled={!hasMore}
        aria-expanded={open}
      >
        <div className="tc-head">
          <span className="tc-name">{theme.name}</span>
          <div className="heat-dots">
            {Array.from({ length: 5 }).map((_, i) => (
              <div className={`heat-dot ${i < theme.dots ? 'on' : 'off'}`} key={i} />
            ))}
          </div>
        </div>
        {hasMore && (
          <div className="tc-summary">
            {etfCount > 0 && <span>{etfCount} ETF</span>}
            {stockCount > 0 && <span>{stockCount} 股</span>}
            <span className="tc-chev">{open ? '▾' : '▸'}</span>
          </div>
        )}
      </button>
      {open && (
        <div className="theme-card-body">
          {etfCount > 0 && (
            <div className="tc-etf">
              {theme.etfs.map((e, i) => (
                <span key={e.sym}>
                  {e.sym} <span className={e.dir}>{e.chg}</span>
                  {i < theme.etfs.length - 1 && ' \u00A0|\u00A0 '}
                </span>
              ))}
            </div>
          )}
          {stockCount > 0 && (
            <div className="tc-stocks">
              {theme.stocks.map((s) => (
                <div className="tc-stock" key={s.tk}>
                  <span className="tk">{s.tk}</span>
                  <span className={s.dir}>{s.chg}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// --- Macro card: collapsed by default, expand to show bullets/images ----
function MacroCard({ topic }) {
  const [open, setOpen] = useState(false);
  const { open: openLightbox } = useLightbox();
  const sortedBullets = [...(topic.bullets || [])]
    .map((b, i) => ({ b, i }))
    .sort((a, b) => {
      if (!!b.b.important === !!a.b.important) return a.i - b.i;
      return b.b.important ? 1 : -1;
    });
  const hasMore =
    (Array.isArray(topic.bullets) && topic.bullets.length > 0) ||
    (Array.isArray(topic.image_urls) && topic.image_urls.length > 0) ||
    (Array.isArray(topic.tickers) && topic.tickers.length > 0);
  return (
    <div className="event-item macro-card">
      <button
        type="button"
        className="macro-card-head"
        onClick={() => hasMore && setOpen((v) => !v)}
        disabled={!hasMore}
        aria-expanded={open}
      >
        <span className="event-tag">{topic.topic || 'Macro'}</span>
        <h4>{topic.title}</h4>
        {topic.summary && <p className="macro-summary">{topic.summary}</p>}
      </button>

      {Array.isArray(topic.tickers) && topic.tickers.length > 0 && (
        <div className="event-tickers macro-tickers">
          关注:{' '}
          {topic.tickers.map((tk) => (
            <span key={tk}>{tk}</span>
          ))}
        </div>
      )}

      {open && (
        <div className="macro-card-body">
          {Array.isArray(topic.image_urls) && topic.image_urls.length > 0 && (
            <div className="macro-images">
              {topic.image_urls.map((u) => (
                <button
                  type="button"
                  key={u}
                  className="macro-image-link"
                  onClick={(e) => { e.stopPropagation(); openLightbox(u); }}
                  title="点击放大"
                >
                  <img src={u} alt="" loading="lazy" />
                </button>
              ))}
            </div>
          )}
          {sortedBullets.length > 0 && (
            <div className="macro-bullets">
              {sortedBullets.map(({ b, i }) => (
                <MacroBullet bullet={b} key={i} />
              ))}
            </div>
          )}
          {/* sources intentionally hidden per user request */}
        </div>
      )}
    </div>
  );
}

// --- Macro bullet: click to toggle expanded detail ----------------------
function MacroBullet({ bullet }) {
  const [open, setOpen] = useState(false);
  const hasDetail = !!bullet.details && bullet.details !== bullet.text;
  // If there's a dedicated `details` field, use that as the expanded body.
  // Otherwise the bullet text itself is the detail — collapsed shows a
  // short lead-in (first ~60 chars), expanded shows the full text.
  const preview = bullet.text && bullet.text.length > 70 && !hasDetail
    ? bullet.text.slice(0, 64) + '…'
    : bullet.text;
  const expanded = hasDetail ? bullet.details : bullet.text;
  return (
    <div className={`macro-bullet ${bullet.important ? 'important' : ''}`}>
      <button
        type="button"
        className="macro-bullet-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="chev">{open ? '▾' : '▸'}</span>
        {bullet.important && <span className="imp-flag">⚑</span>}
        <span className="actor">{bullet.actor}</span>
        {bullet.actor_type && <span className="actor-type">{bullet.actor_type}</span>}
        {!open && <span className="preview">{preview}</span>}
      </button>
      {open && <div className="macro-bullet-body">{expanded}</div>}
    </div>
  );
}

// --- Component ----------------------------------------------------------
export default function App() {
  const [edits, setEdits] = useState({});
  const reload = async () => {
    if (!CURRENT_DATE) return;
    const m = await fetchEditsForDate(CURRENT_DATE);
    setEdits(m);
  };
  useEffect(() => { reload(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);
  return (
    <LightboxProvider>
      <ScreenerEditsCtx.Provider value={{ edits, reload }}>
        <AppBody />
      </ScreenerEditsCtx.Provider>
    </LightboxProvider>
  );
}

// --- DateSwitcher: <select> tied to ?date=YYYY-MM-DD so dashboards can be
// shared at a specific trading day. Changing the selection sets the query
// param and reloads the page (data is imported at module top and can't be
// swapped without a reload). Latest date has no ?date param so the URL
// stays clean for the default view.
// Inline-editable cell for Top Movers (industry / reason). Saves to
// Supabase (screener_edits table) on blur — the JSON stays pristine and
// Supabase overlays the value at render time. See src/lib/screenerEdits.js.
function EditableCell({ tk, field, initial, placeholder, multiline }) {
  const [value, setValue] = useState(initial || '');
  const [status, setStatus] = useState('idle');  // idle | saving | saved | error
  // Sync when overlay arrives async after first render.
  useEffect(() => { setValue(initial || ''); }, [initial]);
  const Tag = multiline ? 'textarea' : 'input';
  const save = async () => {
    if (value === (initial || '')) return;
    setStatus('saving');
    const ok = await saveEdit(CURRENT_DATE, tk, field, value);
    setStatus(ok ? 'saved' : 'error');
  };
  return (
    <Tag
      className={`editable-cell ${status}`}
      value={value}
      placeholder={placeholder}
      rows={multiline ? 2 : undefined}
      onChange={(e) => { setValue(e.target.value); setStatus('idle'); }}
      onBlur={save}
      onKeyDown={(e) => {
        if (!multiline && e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur(); }
      }}
    />
  );
}

// Reason cell with a Futu-news picker. Shows current reason text (editable
// inline) and, if Futu news candidates exist for this ticker, a "📰 N" button
// that opens a popover listing them. Clicking a candidate fills the reason
// and saves immediately.
function ReasonCell({ tk, initial }) {
  const [value, setValue] = useState(initial || '');
  const [status, setStatus] = useState('idle');
  const [open, setOpen] = useState(false);
  const news = MOVERS_NEWS[tk] || [];
  const rootRef = useRef(null);
  useEffect(() => { setValue(initial || ''); }, [initial]);

  useEffect(() => {
    if (!open) return;
    const onClick = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    window.addEventListener('mousedown', onClick);
    return () => window.removeEventListener('mousedown', onClick);
  }, [open]);

  const save = async (next) => {
    if (next === (initial || '')) return;
    setStatus('saving');
    const ok = await saveEdit(CURRENT_DATE, tk, 'reason', next);
    setStatus(ok ? 'saved' : 'error');
  };

  const pick = (n) => {
    setValue(n.headline);
    setOpen(false);
    save(n.headline);
  };

  return (
    <div className="reason-cell" ref={rootRef}>
      <textarea
        className={`editable-cell ${status}`}
        value={value}
        placeholder="填写异动原因…"
        rows={2}
        onChange={(e) => { setValue(e.target.value); setStatus('idle'); }}
        onBlur={() => save(value)}
      />
      {news.length > 0 && (
        <button
          type="button"
          className="news-btn"
          title={`${news.length} 条 Futu 候选新闻`}
          onClick={() => setOpen((o) => !o)}
        >
          📰 {news.length}
        </button>
      )}
      {open && (
        <div className="news-popover">
          <div className="news-popover-head">
            <span>{tk} · Futu 候选新闻</span>
            <button type="button" className="news-close" onClick={() => setOpen(false)}>×</button>
          </div>
          {news.length === 0 ? (
            <div className="news-popover-empty">无候选新闻</div>
          ) : (
            <ul className="news-popover-list">
              {news.map((n, i) => (
                <li key={i} className={value === n.headline ? 'picked' : ''}>
                  <button type="button" className="news-pick" onClick={() => pick(n)}>
                    <div className="news-headline">{n.headline}</div>
                    <div className="news-meta">
                      {n.source && <span className="news-source">{n.source}</span>}
                      {n.time_txt && <span className="news-time">{n.time_txt}</span>}
                    </div>
                  </button>
                  {n.url && (
                    <a
                      className="news-ext"
                      href={n.url}
                      target="_blank"
                      rel="noreferrer"
                      title="在新标签页打开"
                    >↗</a>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function DateSwitcher() {
  if (!AVAILABLE_DATES.length) return null;
  const latest = AVAILABLE_DATES[0];
  const onPick = (e) => {
    const picked = e.target.value;
    const url = new URL(window.location.href);
    if (picked === latest) url.searchParams.delete('date');
    else url.searchParams.set('date', picked);
    window.location.href = url.toString();
  };
  return (
    <select
      value={CURRENT_DATE}
      onChange={onPick}
      className="date-switcher"
      title="切换历史日期"
    >
      {AVAILABLE_DATES.map((d) => (
        <option value={d} key={d}>
          {d}{d === latest ? ' (最新)' : ''}
        </option>
      ))}
    </select>
  );
}

function AppBody() {
  // Macro topics render in ingest order (no sort).
  const macroTopics = MACRO_TOPICS;
  // Human overrides for Top Movers industry / reason (loaded from Supabase).
  const { edits: screenerEdits } = useContext(ScreenerEditsCtx);

  return (
    <>
      {/* ========== SECTION 1: MARKET OVERVIEW (indices + breadth merged) ========== */}
      <div className="section overview">
        <div className="header-title-row">
          <div>
            <div className="header-date">
              {HEADER_DATE || CURRENT_DATE}
              <DateSwitcher />
            </div>
            <div className="header-sub">{HEADER_SUB}</div>
          </div>
          {import.meta.env.DEV && (
            <div className="header-admin">
              <AdminDrawer currentDate={CURRENT_DATE} availableDates={AVAILABLE_DATES} />
            </div>
          )}
        </div>
        <div className="section-title" style={{ marginTop: 14 }}>市场总览 Market Overview</div>
        {/* Row 1 = US (first 6 from INDICES_YAHOO), Row 2 = global + commodities */}
        {[INDICES.slice(0, 6), INDICES.slice(6)].map((row, rowIdx) => (
          <div className="idx-cards" key={rowIdx} style={rowIdx === 1 ? { marginTop: 6 } : undefined}>
            {row.map(ix => (
              <div className="idx-card" key={ix.label}>
                <div className="label">{ix.label}</div>
                <div className="value">{ix.value}</div>
                <div className={`change ${ix.dir}`}>
                  <span className="arrow">{ix.dir === 'up' ? '\u25B2' : '\u25BC'}</span>
                  {ix.change}
                </div>
              </div>
            ))}
          </div>
        ))}
        <div className="breadth-grid" style={{ marginTop: 12 }}>
          <div className="breadth-box">
            <h3>涨跌家数 &amp; 成交量分布</h3>
            <div className="metric-grid" style={{ gridTemplateColumns: '1fr 1fr 1fr', marginBottom: 12 }}>
              <div className="metric"><div className="mv up">{BREADTH.advancers.toLocaleString()}</div><div className="ml">上涨</div></div>
              <div className="metric"><div className="mv down">{BREADTH.decliners.toLocaleString()}</div><div className="ml">下跌</div></div>
              <div className="metric"><div className="mv" style={{ color: 'var(--text-dim)' }}>{BREADTH.unchanged.toLocaleString()}</div><div className="ml">持平</div></div>
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4 }}>涨跌家数比 A/D Ratio</div>
            <div className="ad-bar">
              <div className="adv" style={{ width: `${BREADTH.adUpPct}%` }} />
              <div className="unc" style={{ width: `${BREADTH.adUncPct}%` }} />
              <div className="dec" style={{ width: `${BREADTH.adDownPct}%` }} />
            </div>
            <div className="ad-legend">
              <span><span style={{ color: 'var(--green)' }}>■</span> 上涨 {BREADTH.adUpPct}%</span>
              <span><span style={{ color: '#555' }}>■</span> 持平 {BREADTH.adUncPct}%</span>
              <span><span style={{ color: 'var(--red)' }}>■</span> 下跌 {BREADTH.adDownPct}%</span>
            </div>
            <div style={{ marginTop: 12, fontSize: 11, color: 'var(--text-dim)' }}>成交量分布 Up/Down Volume</div>
            <div className="ad-bar">
              <div className="adv" style={{ width: `${BREADTH.volUpPct}%` }} />
              <div className="dec" style={{ width: `${BREADTH.volDownPct}%` }} />
            </div>
            <div className="ad-legend">
              <span><span style={{ color: 'var(--green)' }}>■</span> 上涨量 {BREADTH.volUpPct}%</span>
              <span><span style={{ color: 'var(--red)' }}>■</span> 下跌量 {BREADTH.volDownPct}%</span>
            </div>
          </div>
          <div className="breadth-box">
            <h3>关键市场指标</h3>
            <div className="metric-grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
              <div className="metric"><div className="mv">{BREADTH.putCall ? BREADTH.putCall.toFixed(2) : '—'}</div><div className="ml">Put/Call Ratio</div></div>
              <div className="metric"><div className="mv">{BREADTH.vix.toFixed(2)}</div><div className="ml">VIX 恐慌指数</div></div>
              <div className="metric"><div className="mv">{BREADTH.new52wHigh || '—'}</div><div className="ml">52周新高</div></div>
              <div className="metric"><div className="mv">{BREADTH.new52wLow || '—'}</div><div className="ml">52周新低</div></div>
              <div className="metric"><div className="mv">{BREADTH.pctAbove50dma ? BREADTH.pctAbove50dma.toFixed(1) + '%' : '—'}</div><div className="ml">股价 &gt; 50日均线</div></div>
              <div className="metric"><div className="mv">{BREADTH.pctAbove200dma ? BREADTH.pctAbove200dma.toFixed(1) + '%' : '—'}</div><div className="ml">股价 &gt; 200日均线</div></div>
            </div>
          </div>
        </div>
      </div>

      {/* ========== SECTION 2: SECTOR PERFORMANCE ========== */}
      <SectorPerformance />

      {/* ========== SECTION 2.5: FINVIZ BUBBLE MAP ========== */}
      <FinvizBubble />

      {/* ========== SECTION 3: TOP MOVERS ========== */}
      <div className="section">
        <div className="section-title">异动筛选 Top Movers</div>
        <div style={{ fontSize: 10, color: 'var(--text-dim)', marginBottom: 8 }}>
          筛选条件: Mkt Cap ≥ $1Bn | $Volume ≥ $300M | 1D ≥ 15% 或 1W ≥ 40%（任一） · 按 1D Change ↓ 排序
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table className="screener-tbl">
            <thead>
              <tr>
                <th style={{ width: 90 }}>行业分类</th>
                <th style={{ width: 60 }}>Ticker</th>
                <th style={{ minWidth: 140 }}>Name</th>
                <th style={{ width: 60, textAlign: 'center' }}>Day<br/>Remaining</th>
                <th style={{ width: 65, textAlign: 'right' }}>Change_%</th>
                <th style={{ width: 65, textAlign: 'right' }}>1w<br/>Change_%</th>
                <th style={{ width: 75, textAlign: 'right' }}>Market_Cap<br/>($ Bn)</th>
                <th style={{ width: 70, textAlign: 'right' }}>$Volume<br/>($ Bn)</th>
                <th style={{ width: 70, textAlign: 'right' }}>1w avg<br/>$Volume<br/>($ Bn)</th>
                <th style={{ minWidth: 180 }}>Reason</th>
                <th style={{ minWidth: 200 }}>Main Business</th>
              </tr>
            </thead>
            <tbody>
              {SCREENER.length === 0 && (
                <tr>
                  <td colSpan={11} style={{ textAlign: 'center', color: 'var(--text-dim)', padding: 16 }}>
                    今日无符合筛选条件的个股
                  </td>
                </tr>
              )}
              {SCREENER.map(s => {
                const d1 = s.d1;
                const w1 = s.w1;
                const isDown = (d1 ?? 0) < 0;
                const ov = screenerEdits[s.tk] || {};
                const industry = ov.industry ?? s.industry;
                const reason   = ov.reason   ?? s.reason;
                return (
                  <tr key={s.tk}>
                    <td style={{ padding: 2 }}>
                      <EditableCell tk={s.tk} field="industry" initial={industry} placeholder="填写…" />
                    </td>
                    <td className="tk">{s.tk}</td>
                    <td className="nm">{s.nm}</td>
                    <td style={{ textAlign: 'center', fontWeight: 600 }}>{s.days_remaining ?? '—'}</td>
                    <td className={`pct ${isDown ? 'down' : 'up'}`}>{d1 == null ? '—' : fmtPct(d1)}</td>
                    <td className={`pct ${(w1 ?? 0) < 0 ? 'down' : 'up'}`}>{w1 == null ? '—' : fmtPct(w1)}</td>
                    <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {s.market_cap_bn != null ? s.market_cap_bn.toFixed(1) : '—'}
                    </td>
                    <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {s.dollar_vol_bn != null ? s.dollar_vol_bn.toFixed(2) : '—'}
                    </td>
                    <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: 'var(--text-dim)' }}>
                      {s.w1_avg_dollar_vol_bn != null ? s.w1_avg_dollar_vol_bn.toFixed(2) : '—'}
                    </td>
                    <td style={{ padding: 2 }}>
                      <ReasonCell tk={s.tk} initial={reason} />
                    </td>
                    <td className="biz">{s.main_business || <span style={{ color: 'var(--text-dim)' }}>—</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* ========== SECTION 6: THEME TRACKING ========== */}
      <div className="section">
        <div className="section-title">主题追踪 Theme Tracking</div>
        <div className="theme-grid">
          {THEMES.map(t => (
            <ThemeCard theme={t} key={t.name} />
          ))}
        </div>
      </div>

      {/* ========== SECTION 6.5: MACRO BRIEF — moved here (before Recap) ========== */}
      <div className="section">
        <div className="section-title">宏观日览 Macro Brief</div>
        {macroTopics.length === 0 ? (
          <div className="macro-empty">
            暂无宏观日报 — 到 <code style={{ color: 'var(--gold)' }}>/admin</code> 添加
          </div>
        ) : (
          <div className="highlights">
            <div className="event-grid">
              {macroTopics.map(t => (
                <MacroCard topic={t} key={t.id || t.title} />
              ))}
            </div>
          </div>
        )}
      </div>

      {/* ========== SECTION 8: GLOBAL EVENTS ========== */}
      <div className="section">
        <div className="section-title">全球重点事件 Global Events</div>
        <div className="highlights">
          <div className="event-grid">
            {EVENTS.map(e => (
              <div className="event-item" key={e.title}>
                <div className="event-tag">{e.tag}</div>
                <h4>{e.title}</h4>
                <p>{e.body}</p>
                <div className="event-tickers">
                  关注:{' '}
                  {e.tickers.map(t => (
                    <span key={t}>{t}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div style={{ textAlign: 'center', padding: '16px 0', fontSize: 11, color: 'var(--text-dim)', borderTop: '1px solid var(--border)' }}>
        Generated by Investment Research Team &nbsp;|&nbsp; Data for illustration only &nbsp;|&nbsp; {FOOTER_TIMESTAMP}
      </div>
    </>
  );
}
