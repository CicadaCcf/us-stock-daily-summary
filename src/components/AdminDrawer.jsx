// AdminDrawer — Stage-1 stub. Dev-only ingestion UI.
//
// Renders a gear button in the header. Clicking it opens a side drawer with
// three tabs: Events / Macro / Settings.
//
// Ingest tabs accept:
//   * pasted text (optional)
//   * multiple image uploads (optional)
//   * both simultaneously — text + N images are sent together
//
// POST /api/ingest → Claude classifies → preview JSON → POST /api/save writes
// to src/data/<date>/<kind>.json.
//
// Robustness:
//   * elapsed timer during classification
//   * 240s hard timeout (AbortController)
//   * Cancel button while in-flight
//
// NOTE: this file only renders in dev (`import.meta.env.DEV === true`).

import { useState, useEffect, useRef } from 'react';

const TABS = [
  { id: 'events',   label: 'Events'   },
  { id: 'macro',    label: 'Macro'    },
  { id: 'publish',  label: 'Publish'  },
  { id: 'settings', label: 'Settings' },
];

const MODEL_OPTIONS = [
  { value: '', label: 'Default (.env.local)' },
  { value: 'claude-opus-4-7',    label: 'Opus 4.7 · 最强 (slow ~60-180s)' },
  { value: 'claude-sonnet-4-6',  label: 'Sonnet 4.6 · 快 (~20-40s)' },
  { value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5 · 最快便宜' },
  { value: 'claude-opus-4-5',    label: 'Opus 4.5 (旧)' },
  { value: 'claude-sonnet-4-5',  label: 'Sonnet 4.5 (旧)' },
];

const MODEL_LS_KEY = '__ingest_model_override';
// Frontend timeout for /api/ingest. Pushed to 10 min to accommodate macro
// classifies with many images (Opus 4.5 vision can take 4-6 min for 8+
// images, plus prompt-cache misses on first call of the day add ~30s).
// The Anthropic SDK's own default timeout is also 10 min, so this matches.
// Per user 2026-04-29: classify timing out on image-heavy macro paste.
const HARD_TIMEOUT_MS = 600000; // 10 min

async function fileToBase64(file) {
  const buf = await file.arrayBuffer();
  let binary = '';
  const bytes = new Uint8Array(buf);
  // Chunked to avoid stack overflow on large images
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function getModelOverride() {
  try { return localStorage.getItem(MODEL_LS_KEY) || ''; } catch { return ''; }
}

function IngestTab({ kind, date }) {
  const [text, setText] = useState('');
  const [images, setImages] = useState([]); // [{ name, b64, previewUrl }]
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [saveMsg, setSaveMsg] = useState('');
  const [err, setErr] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const abortRef = useRef(null);
  const timerRef = useRef(null);

  useEffect(() => () => {
    // Cleanup on unmount
    abortRef.current?.abort();
    if (timerRef.current) clearInterval(timerRef.current);
    images.forEach((img) => img.previewUrl && URL.revokeObjectURL(img.previewUrl));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onAddFiles(e) {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const newOnes = await Promise.all(files.map(async (f) => ({
      name: f.name,
      b64: await fileToBase64(f),
      previewUrl: URL.createObjectURL(f),
      url: null, // persistent URL from /api/upload; filled async below
      uploading: true,
    })));
    setImages((prev) => [...prev, ...newOnes]);
    e.target.value = '';
    // Upload each in parallel; backfill persistent URL.
    for (const img of newOnes) {
      fetch('/api/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date, b64: img.b64, filename: img.name }),
      })
        .then((r) => r.json())
        .then((json) => {
          if (!json?.ok) throw new Error(json?.error || 'upload failed');
          setImages((prev) => prev.map((x) => (x.b64 === img.b64 ? { ...x, url: json.url, uploading: false } : x)));
        })
        .catch((err) => {
          setImages((prev) => prev.map((x) => (x.b64 === img.b64 ? { ...x, uploading: false, uploadError: err.message } : x)));
        });
    }
  }

  function removeImage(idx) {
    setImages((prev) => {
      const next = prev.filter((_, i) => i !== idx);
      const removed = prev[idx];
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      return next;
    });
  }

  async function onClassify() {
    if (!text.trim() && images.length === 0) {
      setErr('请粘贴文本或上传图片（可同时）');
      return;
    }
    setLoading(true);
    setErr('');
    setResult(null);
    setSaveMsg('');
    setElapsed(0);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const t0 = Date.now();
    timerRef.current = setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000);
    const timeoutId = setTimeout(() => ctrl.abort('timeout'), HARD_TIMEOUT_MS);

    try {
      // Wait for all uploads to settle so image_urls line up with images[].
      const anyUploading = images.some((i) => i.uploading);
      if (anyUploading) {
        setErr('图片还在上传中，请稍候再点 Classify');
        setLoading(false);
        abortRef.current = null;
        clearInterval(timerRef.current);
        clearTimeout(timeoutId);
        return;
      }
      const resp = await fetch('/api/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind,
          date,
          text: text.trim() || undefined,
          images: images.length > 0 ? images.map((i) => i.b64) : undefined,
          image_urls: images.length > 0 ? images.map((i) => i.url).filter(Boolean) : undefined,
          model: getModelOverride() || undefined,
        }),
        signal: ctrl.signal,
      });
      const json = await resp.json();
      if (!resp.ok || !json.ok) throw new Error(json.error || 'ingest failed');
      setResult(json);
    } catch (e) {
      if (e.name === 'AbortError') {
        setErr(ctrl.signal.reason === 'timeout'
          ? `超时 (> ${HARD_TIMEOUT_MS / 1000}s) — 模型还在生成，可重试或切 Sonnet`
          : '已取消');
      } else {
        setErr(e.message || String(e));
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
      clearInterval(timerRef.current);
      timerRef.current = null;
      clearTimeout(timeoutId);
    }
  }

  function onCancel() {
    abortRef.current?.abort('user');
  }

  async function onSave() {
    if (!result?.data) return;
    setSaveMsg(''); setErr('');
    try {
      const resp = await fetch('/api/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, date, data: result.data }),
      });
      const json = await resp.json();
      if (!resp.ok || !json.ok) throw new Error(json.error || 'save failed');
      setSaveMsg(`✓ 已保存 ${json.path} — 刷新页面可见`);
    } catch (e) {
      setErr(e.message || String(e));
    }
  }

  const canSubmit = (text.trim().length > 0 || images.length > 0) && !loading;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <label style={{ fontSize: 11, color: 'var(--text-dim)' }}>
        粘贴文本 (中文 OK；可与图片同时)
      </label>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={8}
        style={{
          background: 'var(--bg)', color: 'var(--text)',
          border: '1px solid var(--border)', borderRadius: 4,
          padding: 8, fontSize: 12, fontFamily: 'inherit', resize: 'vertical',
        }}
        placeholder={kind === 'macro'
          ? '例：<Macro发言 4/21>\nUS Fed Warsh: ...\nTrump: ...\nGS: ...'
          : '例：Anthropic 获 800B 估值要约；AMZN 向 Anthropic 追加 5B 投资 ...'}
      />

      <label style={{ fontSize: 11, color: 'var(--text-dim)' }}>
        图片（可多选，文字+图片会一起送给模型）
      </label>
      <input type="file" accept="image/*" multiple onChange={onAddFiles} />

      {images.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
          {images.map((img, i) => (
            <div
              key={i}
              style={{
                position: 'relative', width: 72, height: 72,
                border: '1px solid var(--border)', borderRadius: 4,
                overflow: 'hidden', background: 'var(--bg)',
              }}
              title={img.name}
            >
              <img src={img.previewUrl} alt={img.name}
                   style={{ width: '100%', height: '100%', objectFit: 'cover',
                            opacity: img.uploading ? 0.4 : 1 }} />
              {img.uploading && (
                <div style={{
                  position: 'absolute', inset: 0, display: 'flex',
                  alignItems: 'center', justifyContent: 'center',
                  fontSize: 9, color: 'var(--gold)',
                }}>上传中…</div>
              )}
              {img.uploadError && (
                <div style={{
                  position: 'absolute', bottom: 0, left: 0, right: 0,
                  background: 'rgba(192,57,43,.85)', color: '#fff',
                  fontSize: 8, padding: '2px 3px', textAlign: 'center',
                }}>失败</div>
              )}
              <button
                onClick={() => removeImage(i)}
                style={{
                  position: 'absolute', top: 2, right: 2, width: 18, height: 18,
                  background: 'rgba(0,0,0,.65)', color: '#fff', border: 0,
                  borderRadius: '50%', fontSize: 11, lineHeight: 1, cursor: 'pointer',
                  padding: 0,
                }}
              >×</button>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, marginTop: 4, alignItems: 'center' }}>
        <button
          onClick={onClassify}
          disabled={!canSubmit}
          style={{
            background: canSubmit ? 'var(--blue)' : '#334',
            color: '#fff', border: 0,
            padding: '6px 14px', borderRadius: 4, fontSize: 12,
            cursor: canSubmit ? 'pointer' : 'not-allowed',
            minWidth: 140,
          }}
        >
          {loading ? `Classifying… ${elapsed}s` : 'Classify →'}
        </button>
        {loading && (
          <button
            onClick={onCancel}
            style={{
              background: 'var(--card-alt)', color: 'var(--text)',
              border: '1px solid var(--border)',
              padding: '6px 10px', borderRadius: 4, fontSize: 12, cursor: 'pointer',
            }}
          >
            Cancel
          </button>
        )}
        <button
          onClick={onSave}
          disabled={!result?.data}
          style={{
            background: result?.data ? 'var(--green)' : '#334',
            color: '#fff', border: 0,
            padding: '6px 14px', borderRadius: 4, fontSize: 12,
            cursor: result?.data ? 'pointer' : 'not-allowed',
          }}
        >
          Save to JSON
        </button>
      </div>

      {err && <div style={{ color: 'var(--red)', fontSize: 11 }}>{err}</div>}
      {saveMsg && <div style={{ color: 'var(--green)', fontSize: 11 }}>{saveMsg}</div>}

      {result && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4 }}>
            {(result.data?.topics?.length ?? result.data?.events?.length ?? '?')} items
            {' • '}
            {result.model || 'claude'}
            {result.usage && ` • ${result.usage.input_tokens}in / ${result.usage.output_tokens}out`}
            {result.usage?.cache_read_input_tokens ? ` • cache: ${result.usage.cache_read_input_tokens}` : ''}
            {result._visionFallback && (
              <span style={{ marginLeft: 8, color: 'var(--gold)' }}>
                · 视觉转录失败，已直接送 Opus
              </span>
            )}
          </div>
          <pre
            style={{
              background: 'var(--bg)', border: '1px solid var(--border)',
              borderRadius: 4, padding: 8, fontSize: 11,
              maxHeight: 320, overflow: 'auto', whiteSpace: 'pre-wrap',
            }}
          >
            {JSON.stringify(result.data, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

function SettingsTab() {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState('');
  const [model, setModel] = useState(getModelOverride());

  async function check() {
    setErr('');
    try {
      const r = await fetch('/api/status');
      setStatus(await r.json());
    } catch (e) { setErr(e.message); }
  }

  function saveModel(m) {
    setModel(m);
    try {
      if (m) localStorage.setItem(MODEL_LS_KEY, m);
      else localStorage.removeItem(MODEL_LS_KEY);
    } catch { /* ignore */ }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: 12 }}>
      <div>
        <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4 }}>
          模型（留空用 .env.local 默认值）
        </div>
        <select
          value={model}
          onChange={(e) => saveModel(e.target.value)}
          style={{
            background: 'var(--bg)', color: 'var(--text)',
            border: '1px solid var(--border)', borderRadius: 4,
            padding: '6px 8px', fontSize: 12, width: '100%',
          }}
        >
          {MODEL_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 4 }}>
          选择存本地 localStorage，每次 Classify 用此模型。
        </div>
      </div>

      <div style={{ borderTop: '1px solid var(--border)', paddingTop: 10 }}>
        <div>API key from .env.local: <span style={{ color: 'var(--gold)' }}>hidden</span></div>
        <button
          onClick={check}
          style={{
            background: 'var(--card-alt)', color: 'var(--text)',
            border: '1px solid var(--border)', padding: '6px 12px',
            borderRadius: 4, fontSize: 12, cursor: 'pointer', alignSelf: 'flex-start',
            marginTop: 6,
          }}
        >
          Check status
        </button>
        {err && <div style={{ color: 'var(--red)' }}>{err}</div>}
        {status && (
          <pre style={{
            background: 'var(--bg)', border: '1px solid var(--border)',
            borderRadius: 4, padding: 8, fontSize: 11, marginTop: 6,
          }}>{JSON.stringify(status, null, 2)}</pre>
        )}
      </div>

      <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 4 }}>
        Publish to GitHub: 留待 Stage 2 (/api/publish)
      </div>
    </div>
  );
}

// --- Publish tab: Update (snapshot + finviz, local only) + Publish (git push) ---
//
// Two buttons, two endpoints. The split lets you verify Finviz and the new
// numbers LOCALLY before anything hits GitHub / Vercel. Both endpoints stream
// chunked text/plain; we read via fetch body.getReader() and append to a
// log box. Server writes a trailing `__STATUS__ ok=... date=... commit=...`
// line we parse to show success/failure banners.
function PublishTab({ currentDate, availableDates }) {
  const [log, setLog] = useState('');
  const [busy, setBusy] = useState(null); // 'update' | 'publish' | null
  const [status, setStatus] = useState(null); // {ok, date, commit, error}
  // Target date for Update: empty string means "auto" — the script picks the
  // latest trading day (ref_date). Overriding lets us re-run a specific past
  // day (e.g. backfill a folder after schema changes).
  const [targetDate, setTargetDate] = useState('');
  const abortRef = useRef(null);

  async function runAction(action) {
    if (busy) return;
    setBusy(action);
    setLog('');
    setStatus(null);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      // Only /api/update accepts a target date — /api/publish always commits
      // whatever is on disk at the latest folder.
      const url = action === 'update' && targetDate
        ? `/api/update?date=${encodeURIComponent(targetDate)}`
        : `/api/${action}`;
      // Publish also carries the user's localStorage Day Remaining edits so
      // the server can fold them into screener.json (and drop any row whose
      // final days_remaining is 0) before git commit.
      let body;
      let headers;
      if (action === 'publish') {
        let overrides = {};
        try {
          const raw = window.localStorage.getItem(`days_remaining_${currentDate}`);
          if (raw) overrides = JSON.parse(raw) || {};
        } catch {}
        body = JSON.stringify({ days_remaining_overrides: overrides });
        headers = { 'Content-Type': 'application/json' };
      }
      const resp = await fetch(url, {
        method: 'POST',
        signal: ctrl.signal,
        body,
        headers,
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        setLog(buf);
      }
      // Parse the final __STATUS__ line the server wrote
      const m = buf.match(/__STATUS__\s+(.+)$/m);
      if (m) {
        const parts = Object.fromEntries(
          m[1].trim().split(/\s+/).map((kv) => {
            const i = kv.indexOf('=');
            return i < 0 ? [kv, true] : [kv.slice(0, i), kv.slice(i + 1)];
          })
        );
        setStatus({
          ok: parts.ok === 'true',
          date: parts.date,
          commit: parts.commit,
          error: parts.error,
        });
        // Publish succeeded: baseline screener.json now reflects the user's
        // Day Remaining edits, so the localStorage overrides are stale. Clear
        // them so the inputs fall back to the JSON values (and tomorrow's
        // pipeline carry-forward sees the right numbers).
        if (action === 'publish' && parts.ok === 'true') {
          try { window.localStorage.removeItem(`days_remaining_${currentDate}`); } catch {}
        }
      } else {
        setStatus({ ok: false, error: 'no __STATUS__ line from server' });
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        setLog((l) => l + `\n[client] ${e.message}\n`);
        setStatus({ ok: false, error: e.message });
      } else {
        setLog((l) => l + `\n[client] aborted\n`);
      }
    } finally {
      setBusy(null);
      abortRef.current = null;
    }
  }

  const dates = Array.isArray(availableDates) ? availableDates : [];

  return (
    <div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.6, marginBottom: 12 }}>
        <div><b style={{ color: 'var(--text)' }}>① 更新 Update</b> — 跑 polygon_snapshot.py + finviz_screenshot.py + movers_news.py。本地文件会被覆盖，刷页面看效果。<b>不碰 git</b>。</div>
        <div style={{ marginTop: 4 }}><b style={{ color: 'var(--text)' }}>② 发布 Publish</b> — git add + commit + push → Vercel 自动 deploy。建议先 Update 看对了再 Publish。</div>
      </div>

      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '8px 10px', marginBottom: 10,
        background: 'var(--card-alt)', border: '1px solid var(--border)',
        borderRadius: 4, fontSize: 12,
      }}>
        <span style={{ color: 'var(--text-dim)' }}>目标日期</span>
        <select
          value={targetDate}
          disabled={!!busy}
          onChange={(e) => setTargetDate(e.target.value)}
          style={{
            flex: 1, background: 'var(--card)', color: 'var(--text)',
            border: '1px solid var(--border)', borderRadius: 3,
            padding: '3px 6px', fontSize: 12, fontFamily: 'inherit',
          }}
        >
          <option value="">自动（最新交易日）</option>
          {dates.map((d) => (
            <option key={d} value={d}>{d}{d === currentDate ? ' · 当前' : ''}</option>
          ))}
        </select>
        {targetDate && (
          <button
            onClick={() => setTargetDate('')}
            style={{
              background: 'transparent', color: 'var(--text-dim)',
              border: '1px solid var(--border)', borderRadius: 3,
              padding: '2px 7px', fontSize: 11, cursor: 'pointer',
            }}
          >
            清除
          </button>
        )}
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
        <button
          onClick={() => runAction('update')}
          disabled={!!busy}
          style={{
            flex: 1, padding: '10px 14px', fontSize: 13, fontWeight: 700,
            background: busy === 'update' ? 'var(--card-alt)' : 'var(--green)',
            color: busy === 'update' ? 'var(--text-dim)' : '#0d1117',
            border: 0, borderRadius: 4,
            cursor: busy ? 'wait' : 'pointer',
            opacity: busy && busy !== 'update' ? 0.4 : 1,
          }}
        >
          {busy === 'update' ? '更新中…' : '🔄 ① 更新 Update'}
        </button>
        <button
          onClick={() => runAction('publish')}
          disabled={!!busy}
          style={{
            flex: 1, padding: '10px 14px', fontSize: 13, fontWeight: 700,
            background: busy === 'publish' ? 'var(--card-alt)' : 'var(--gold)',
            color: busy === 'publish' ? 'var(--text-dim)' : '#0d1117',
            border: 0, borderRadius: 4,
            cursor: busy ? 'wait' : 'pointer',
            opacity: busy && busy !== 'publish' ? 0.4 : 1,
          }}
        >
          {busy === 'publish' ? '发布中…' : '🚀 ② 发布 Publish'}
        </button>
      </div>

      {busy && (
        <button
          onClick={() => abortRef.current?.abort()}
          style={{
            padding: '4px 10px', fontSize: 11, marginBottom: 10,
            background: 'transparent', border: '1px solid var(--border)',
            borderRadius: 3, color: 'var(--text-dim)', cursor: 'pointer',
          }}
        >
          取消
        </button>
      )}

      {status && (
        <div style={{
          padding: '6px 10px', marginBottom: 10, borderRadius: 4, fontSize: 12,
          background: status.ok ? 'rgba(70,180,110,.12)' : 'rgba(220,90,90,.12)',
          border: `1px solid ${status.ok ? 'var(--green)' : 'var(--red)'}`,
          color: status.ok ? 'var(--green)' : 'var(--red)',
        }}>
          {status.ok
            ? `✓ 完成${status.date ? ` · ${status.date}` : ''}${status.commit && status.commit !== 'noop' ? ` · commit ${status.commit}` : status.commit === 'noop' ? ' · 无改动' : ''}`
            : `✗ ${status.error || '失败'}`}
        </div>
      )}

      <pre style={{
        background: '#0b0e14', color: 'var(--text)',
        padding: 10, borderRadius: 4, fontSize: 10.5, lineHeight: 1.45,
        maxHeight: 460, overflowY: 'auto',
        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        fontFamily: 'ui-monospace, SFMono-Regular, monospace',
        minHeight: 200, margin: 0,
      }}>
        {log || '(log 会实时显示在这里)'}
      </pre>
    </div>
  );
}

export default function AdminDrawer({ currentDate, availableDates }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState('events');

  return (
    <>
      <button
        onClick={() => setOpen((v) => !v)}
        title="Admin"
        style={{
          background: 'var(--card-alt)', color: 'var(--text)',
          border: '1px solid var(--border)', borderRadius: 4,
          width: 28, height: 28, fontSize: 14, cursor: 'pointer',
          padding: 0, lineHeight: '26px',
        }}
      >
        ⚙
      </button>

      {open && (
        <div
          style={{
            position: 'fixed', top: 0, right: 0, bottom: 0, width: 520,
            background: 'var(--card)', borderLeft: '1px solid var(--border)',
            boxShadow: '-6px 0 20px rgba(0,0,0,.4)',
            zIndex: 100, display: 'flex', flexDirection: 'column',
          }}
        >
          <div
            style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '10px 14px', borderBottom: '1px solid var(--border)',
            }}
          >
            <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-bright)' }}>
              Admin · {currentDate}
            </div>
            <button
              onClick={() => setOpen(false)}
              style={{ background: 'transparent', border: 0, color: 'var(--text-dim)', fontSize: 18, cursor: 'pointer' }}
            >
              ×
            </button>
          </div>

          <div style={{ display: 'flex', borderBottom: '1px solid var(--border)' }}>
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                style={{
                  background: tab === t.id ? 'var(--card-alt)' : 'transparent',
                  color: tab === t.id ? 'var(--text-bright)' : 'var(--text-dim)',
                  border: 0, padding: '8px 16px', fontSize: 12, cursor: 'pointer',
                  borderBottom: tab === t.id ? '2px solid var(--blue)' : '2px solid transparent',
                }}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div style={{ padding: 14, overflowY: 'auto', flex: 1 }}>
            {tab === 'events' && <IngestTab kind="events" date={currentDate} />}
            {tab === 'macro' && <IngestTab kind="macro" date={currentDate} />}
            {tab === 'publish' && <PublishTab currentDate={currentDate} availableDates={availableDates} />}
            {tab === 'settings' && <SettingsTab />}
          </div>
        </div>
      )}
    </>
  );
}
