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
  ALPHA,
  THEMES,
  EVENTS,
  BREADTH,
  MACRO_TOPICS,
  HEADER_DATE,
  HEADER_SUB,
  FOOTER_TIMESTAMP,
  CURRENT_DATE,
} from './data/index.js';
import { useState, useEffect, createContext, useContext } from 'react';
import AdminDrawer from './components/AdminDrawer.jsx';

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
          {Array.isArray(topic.tickers) && topic.tickers.length > 0 && (
            <div className="event-tickers">
              关注:{' '}
              {topic.tickers.map((tk) => (
                <span key={tk}>{tk}</span>
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
  return (
    <LightboxProvider>
      <AppBody />
    </LightboxProvider>
  );
}

function AppBody() {
  // Macro topics render in ingest order (no sort).
  const macroTopics = MACRO_TOPICS;

  return (
    <>
      {/* ========== SECTION 1: HEADER ========== */}
      <div className="header section">
        <div className="header-title-row">
          <div>
            <div className="header-date">{HEADER_DATE}</div>
            <div className="header-sub">{HEADER_SUB}</div>
          </div>
          {import.meta.env.DEV && (
            <div className="header-admin">
              <AdminDrawer currentDate={CURRENT_DATE} />
            </div>
          )}
        </div>
        <div className="idx-cards">
          {INDICES.map(ix => (
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
      </div>

      {/* ========== SECTION 2: SECTOR PERFORMANCE ========== */}
      <div className="section">
        <div className="section-title">板块表现 Sector Performance</div>
        <div className="sector-chart">
          {SECTORS.map(s => {
            const widthPct = Math.min(Math.abs(s.d1) / SECTOR_MAX_ABS * 100, 100);
            const cls = s.d1 >= 0 ? 'pos' : 'neg';
            const color = s.d1 >= 0 ? 'var(--green)' : 'var(--red)';
            return (
              <div className="sector-row" key={s.name}>
                <div className="sector-name">{s.name}</div>
                <div className="sector-bar-wrap">
                  <div className="sector-bar-bg" />
                  <div className={`sector-bar ${cls}`} style={{ width: `${widthPct}%` }} />
                </div>
                <div className="sector-pct" style={{ color }}>{fmtPct(s.d1)}</div>
                <div className="sector-extra">
                  5D {fmtPct(s.d5)} &nbsp; 1M {fmtPct(s.m1)}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ========== SECTION 2.5: FINVIZ BUBBLE MAP ========== */}
      <div className="section">
        <div className="section-title">全市场气泡图 Finviz Bubble Map</div>
        <div
          style={{
            background: 'var(--card)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            overflow: 'hidden',
            position: 'relative',
            cursor: 'pointer',
          }}
          onClick={() => window.open('https://finviz.com/bubbles.ashx', '_blank')}
        >
          <div
            style={{
              display: 'flex',
              height: 200,
              alignItems: 'center',
              justifyContent: 'center',
              flexDirection: 'column',
              gap: 8,
              background: 'var(--card)',
            }}
          >
            <span style={{ fontSize: 14, color: 'var(--text-dim)' }}>
              气泡图占位 — 将 Finviz 截图保存为{' '}
              <code style={{ color: 'var(--gold)' }}>public/finviz_bubble.png</code>{' '}
              即可显示
            </span>
          </div>
          <div
            style={{
              position: 'absolute',
              bottom: 0,
              left: 0,
              right: 0,
              padding: '6px 12px',
              background: 'linear-gradient(transparent, rgba(22,33,62,.95))',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>
              数据来源: Finviz.com | 点击图片在新窗口打开实时版
            </span>
            <a
              href="https://finviz.com/bubbles.ashx"
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              style={{ fontSize: 10, color: 'var(--blue)' }}
            >
              打开 Finviz ↗
            </a>
          </div>
        </div>
      </div>

      {/* ========== SECTION 3: TOP MOVERS ========== */}
      <div className="section">
        <div className="section-title">异动筛选 Top Movers</div>
        <div style={{ fontSize: 10, color: 'var(--text-dim)', marginBottom: 8 }}>
          筛选条件: Mid Cap | Trading Volume ≥ $100M | 1D Change &gt; 5% | 1Y Change &gt; 40% | 按 Trading Volume ↓ 排序
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table className="screener-tbl">
            <thead>
              <tr>
                <th style={{ width: 55 }}>Ticker</th>
                <th style={{ width: 170 }}>Name</th>
                <th style={{ width: 55, textAlign: 'right' }}>1D %</th>
                <th style={{ width: 55, textAlign: 'right' }}>5D %</th>
                <th style={{ width: 55, textAlign: 'right' }}>1M %</th>
                <th style={{ width: 55, textAlign: 'right' }}>3M %</th>
                <th style={{ width: 55, textAlign: 'right' }}>6M %</th>
                <th style={{ width: 55, textAlign: 'right' }}>1Y %</th>
                <th style={{ width: 85, textAlign: 'right' }}>Volume ($Bn)</th>
                <th style={{ width: 75, textAlign: 'right' }}>MktCap ($Bn)</th>
                <th style={{ width: 90 }}>多周期趋势</th>
                <th style={{ minWidth: 180 }}>主营业务</th>
              </tr>
            </thead>
            <tbody>
              {SCREENER.map(s => {
                const periods = [s.d1, s.d5, s.m1, s.m3, s.m6, s.y1];
                return (
                  <tr key={s.tk}>
                    <td className="tk">{s.tk}</td>
                    <td className="nm">{s.nm}</td>
                    {periods.map((v, i) => (
                      <td
                        className={`pct ${v == null ? '' : pnClass(v)}`}
                        style={v == null ? { color: 'var(--text-dim)' } : undefined}
                        key={i}
                      >
                        {v == null ? 'N/A' : fmtPct(v)}
                      </td>
                    ))}
                    <td className="vol" style={s.vol >= 1 ? { color: 'var(--gold)' } : undefined}>
                      {s.vol.toFixed(1)}
                    </td>
                    <td className="cap">{s.cap.toFixed(1)}</td>
                    <td>
                      <div className="mini-bars">
                        {periods.map((v, i) => {
                          if (v == null) return <div className="mini-bar n" style={{ height: 2 }} key={i} />;
                          let h = Math.min(Math.abs(v) / SCREENER_MAX_ABS * 14, 14);
                          h = Math.max(h, 2);
                          return <div className={`mini-bar ${v >= 0 ? 'g' : 'r'}`} style={{ height: h }} key={i} />;
                        })}
                      </div>
                    </td>
                    <td className="biz">{s.biz}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* ========== SECTION 4: MARKET BREADTH ========== */}
      <div className="section">
        <div className="section-title">板块资金流与市场宽度 Market Breadth</div>
        <div className="breadth-grid">
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
              <div className="metric"><div className="mv" style={{ color: 'var(--blue)' }}>{BREADTH.putCall.toFixed(2)}</div><div className="ml">Put/Call Ratio</div></div>
              <div className="metric"><div className="mv down">{BREADTH.vix.toFixed(2)}</div><div className="ml">VIX 恐慌指数</div></div>
              <div className="metric"><div className="mv up">{BREADTH.new52wHigh}</div><div className="ml">52周新高</div></div>
              <div className="metric"><div className="mv down">{BREADTH.new52wLow}</div><div className="ml">52周新低</div></div>
              <div className="metric"><div className="mv" style={{ color: 'var(--gold)' }}>{BREADTH.pctAbove50dma.toFixed(1)}%</div><div className="ml">股价 &gt; 50日均线</div></div>
              <div className="metric"><div className="mv" style={{ color: 'var(--gold)' }}>{BREADTH.pctAbove200dma.toFixed(1)}%</div><div className="ml">股价 &gt; 200日均线</div></div>
            </div>
          </div>
        </div>
      </div>

      {/* ========== SECTION 5: ALPHA SIGNALS ========== */}
      <div className="section">
        <div className="section-title">Alpha信号 Alpha Signals</div>
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>名称</th>
              <th>Alpha Score</th>
              <th>板块</th>
              <th>1D%</th>
              <th>5D Alpha vs SPX</th>
              <th>1M Alpha vs SPX</th>
              <th>信号强度</th>
            </tr>
          </thead>
          <tbody>
            {ALPHA.map(a => (
              <tr key={a.tk} className={a.hi ? 'alpha-hl' : undefined}>
                <td style={{ color: 'var(--blue)', fontWeight: 600 }}>{a.tk}</td>
                <td>{a.nm}</td>
                <td style={{ color: a.scoreGold ? 'var(--gold)' : undefined, fontWeight: 700 }}>
                  {a.score.toFixed(1)}
                </td>
                <td><span className="tag">{a.tag}</span></td>
                <td className="up">{a.d1}</td>
                <td className="up">{a.a5}</td>
                <td className={a.amDown ? 'down' : 'up'}>{a.am}</td>
                <td>
                  <span className="dots">
                    {Array.from({ length: 5 }).map((_, i) => (
                      <span className={i < a.dots ? 'dot-on' : 'dot-off'} key={i}>●</span>
                    ))}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* ========== SECTION 6: THEME TRACKING ========== */}
      <div className="section">
        <div className="section-title">主题追踪 Theme Tracking</div>
        <div className="theme-grid">
          {THEMES.map(t => (
            <div className="theme-card" key={t.name}>
              <div className="tc-head">
                <span className="tc-name">{t.name}</span>
                <div className="heat-dots">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div className={`heat-dot ${i < t.dots ? 'on' : 'off'}`} key={i} />
                  ))}
                </div>
              </div>
              <div className="tc-etf">
                {t.etfs.map((e, i) => (
                  <span key={e.sym}>
                    {e.sym} <span className={e.dir}>{e.chg}</span>
                    {i < t.etfs.length - 1 && ' \u00A0|\u00A0 '}
                  </span>
                ))}
              </div>
              <div className="tc-stocks">
                {t.stocks.map(s => (
                  <div className="tc-stock" key={s.tk}>
                    <span className="tk">{s.tk}</span>
                    <span className={s.dir}>{s.chg}</span>
                  </div>
                ))}
              </div>
            </div>
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
                <div className={`event-tag ${e.tagCls}`}>{e.tag}</div>
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
