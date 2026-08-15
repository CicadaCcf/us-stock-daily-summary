#!/usr/bin/env python3
"""Auto-fill `industry` (catalyst tag) + `reason` for newly-highlighted Top Movers.

Runs as a step in the daily Update, AFTER polygon_snapshot.py + movers_news.py.

Target: the "highlighted" rows = today's screener rows with d1 >= filter.min_d1_pct
(the same signal the frontend uses to shade a row white). Among those, we only
fill tickers that don't already carry an industry/reason from a prior day
(Supabase carry-forward), so re-qualified names keep their existing tag.

Writing rules were learned from the user's own history (Supabase screener_edits):
  * industry = a short catalyst / theme tag (2-8 chars), NOT a GICS sector.
    e.g. 业绩 / 被收购 / 合作 / 数据中心 / 大订单 / 积极临床数据 / 存储 / 量子计算.
    Combine with "+" when several apply (业绩+上调指引). Prefer the existing
    vocabulary; only coin a new tag in the same terse style when nothing fits.
  * reason = a terse factual one-liner (~10-35 chars). For earnings:
    `EPS 1.98，beat 4.2%` / `Revenue 137m，beat 1.48%`. For M&A / partnership /
    clinical / orders: one sentence stating the specific fact + number.

Data sources per ticker:
  * Polygon news (src/data/{date}/movers_news.json) + Longbridge `news`.
  * main_business (Chinese company desc) already in screener.json.
  * For earnings tags: Longbridge `consensus` — actual vs estimate → real beat%.

Outputs:
  * industry + reason written as SUGGESTIONS into Supabase screener_edits
    (date, tk), updated_by='auto'. These pre-fill the editable cells for the
    user to review/tweak. No visual "unreviewed" flag (per user's choice).
  * days_remaining / initial_days set to 1 in screener.json for one-shot
    catalysts (tag contains 业绩 or 临床, or explicit FDA批准) — a single-day
    event doesn't warrant the default 3-day carryover. Other tags untouched;
    the pipeline's day-remaining rule otherwise stands.

Usage:
  python3 server/movers_autofill.py [--date YYYY-MM-DD] [--dry-run] [--force]
  --dry-run : classify + print, write NOTHING (no Supabase, no screener.json)
  --force   : re-fill even tickers that already have a carried-forward tag
  --model   : override Anthropic model (default: env ANTHROPIC_MODEL)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'src' / 'data'
ENV_FILE = ROOT / '.env.local'

# --- env -----------------------------------------------------------------
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        if not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
ANTHROPIC_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-opus-4-8')
PROXY = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
SUPA_URL = (os.environ.get('VITE_SUPABASE_URL') or '').rstrip('/')
SUPA_KEY = os.environ.get('VITE_SUPABASE_ANON_KEY') or ''

TABLE = 'screener_edits'

# One-shot catalysts → days_remaining = 1 (single-day event, no 3-day carry).
# Substring match so combos (业绩+软件, 被收购传闻, 被收购提案) are covered too.
ONESHOT_SUBSTR = ('业绩', '临床', '收购')
ONESHOT_EXACT = {'FDA批准'}


def is_oneshot(tag: str) -> bool:
    tag = (tag or '').strip()
    return any(s in tag for s in ONESHOT_SUBSTR) or tag in ONESHOT_EXACT


# --- http ----------------------------------------------------------------
def _opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY})
        )
    return urllib.request.build_opener()


def _http(url, headers, data=None, method='GET', timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _opener().open(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8')
    return raw


# --- Supabase (past examples + upsert) -----------------------------------
def supa_headers(extra=None):
    h = {
        'apikey': SUPA_KEY,
        'Authorization': f'Bearer {SUPA_KEY}',
        'Content-Type': 'application/json',
    }
    if extra:
        h.update(extra)
    return h


def supa_fetch_history():
    """Returns (taxonomy, examples, tags_by_tk).

    taxonomy   : [(tag, count)] desc — the vocabulary to prefer.
    examples   : [(industry, reason)] — style few-shot (deduped, diverse).
    tags_by_tk : {tk: {'industry':.., 'reason':..}} latest-wins carry-forward.
    """
    if not (SUPA_URL and SUPA_KEY):
        print('[warn] Supabase not configured — no history to learn from')
        return [], [], {}
    q = urllib.parse.urlencode({
        'select': 'date,tk,industry,reason',
        'order': 'date.asc',
    })
    try:
        raw = _http(f'{SUPA_URL}/rest/v1/{TABLE}?{q}', supa_headers(), timeout=60)
        rows = json.loads(raw)
    except Exception as e:
        print(f'[warn] Supabase history fetch failed: {e}')
        return [], [], {}

    tags_by_tk = {}
    tax = {}
    ex_by_tag = {}
    for r in sorted(rows, key=lambda x: x.get('date') or ''):
        tk = r.get('tk')
        ind = (r.get('industry') or '').strip()
        rea = (r.get('reason') or '').strip()
        cur = tags_by_tk.get(tk, {})
        tags_by_tk[tk] = {
            'industry': ind or cur.get('industry'),
            'reason': rea or cur.get('reason'),
        }
        if ind:
            tax[ind] = tax.get(ind, 0) + 1
        if ind and rea:
            ex_by_tag.setdefault(ind, [])
            if rea not in ex_by_tag[ind]:
                ex_by_tag[ind].append(rea)

    taxonomy = sorted(tax.items(), key=lambda kv: -kv[1])
    # Diverse examples: up to 3 reasons per tag, tags by frequency.
    examples = []
    for tag, _ in taxonomy:
        for rea in ex_by_tag.get(tag, [])[:3]:
            examples.append((tag, rea))
    return taxonomy, examples, tags_by_tk


def supa_upsert(date, tk, industry, reason):
    payload = [{
        'date': date,
        'tk': tk,
        'industry': industry if industry else None,
        'reason': reason if reason else None,
        'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'updated_by': 'auto',
    }]
    body = json.dumps(payload).encode('utf-8')
    headers = supa_headers({'Prefer': 'resolution=merge-duplicates,return=minimal'})
    _http(f'{SUPA_URL}/rest/v1/{TABLE}?on_conflict=date,tk', headers,
          data=body, method='POST', timeout=60)


# --- Longbridge (read-only market data only) -----------------------------
def lb(*args):
    """Run a read-only longbridge CLI command → parsed JSON, or None."""
    try:
        p = subprocess.run(
            ['longbridge', *args, '--format', 'json'],
            capture_output=True, text=True, timeout=40,
        )
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return json.loads(p.stdout)
    except Exception:
        return None


def _num(s):
    try:
        return float(str(s).strip())
    except Exception:
        return None


def _fmt_eps(v):
    return ('%.2f' % v).rstrip('0').rstrip('.') if v is not None else ''


def _fmt_rev(v):
    if v is None:
        return ''
    if abs(v) >= 1e9:
        return ('%.1f' % (v / 1e9)).rstrip('0').rstrip('.') + 'b'
    return '%.0f' % (v / 1e6) + 'm'


def earnings_beat(tk: str):
    """Latest RELEASED quarter's EPS/Revenue actual vs estimate → beat%.
    Returns a dict or None."""
    d = lb('consensus', f'{tk}.US')
    if not isinstance(d, dict):
        return None
    lst = d.get('list') or []
    if not lst:
        return None
    ci = d.get('current_index')
    order = ([ci] if isinstance(ci, int) and 0 <= ci < len(lst) else []) + list(range(len(lst)))
    for i in order:
        period = lst[i] or {}
        details = {x.get('key'): x for x in period.get('details', [])}
        eps = details.get('eps') or {}
        if not eps.get('is_released'):
            continue
        out = {}
        ea, ee = _num(eps.get('actual')), _num(eps.get('estimate'))
        if ea is not None and ee not in (None, 0):
            out['eps_actual'] = ea
            out['eps_est'] = ee
            out['eps_beat'] = (ea - ee) / abs(ee) * 100
        rev = details.get('revenue') or {}
        ra, re_ = _num(rev.get('actual')), _num(rev.get('estimate'))
        if ra is not None and re_ not in (None, 0):
            out['rev_actual'] = ra
            out['rev_est'] = re_
            out['rev_beat'] = (ra - re_) / abs(re_) * 100
        return out or None
    return None


def format_earnings_reason(eb: dict) -> str:
    """Match the user's style: prefer EPS; beat% when positive, else 预期 X."""
    if 'eps_actual' in eb:
        a = _fmt_eps(eb['eps_actual'])
        if eb['eps_beat'] >= 0:
            return f'EPS {a}，beat {eb["eps_beat"]:.1f}%'
        return f'EPS {a}，预期 {_fmt_eps(eb["eps_est"])}'
    if 'rev_actual' in eb:
        a = _fmt_rev(eb['rev_actual'])
        if eb['rev_beat'] >= 0:
            return f'Revenue {a}，beat {eb["rev_beat"]:.2f}%'
        return f'Revenue {a}，预期 {_fmt_rev(eb["rev_est"])}'
    return ''


def lb_news_titles(tk: str, limit=6):
    d = lb('news', f'{tk}.US')
    items = []
    if isinstance(d, dict):
        items = d.get('items') or d.get('list') or d.get('news') or []
    elif isinstance(d, list):
        items = d
    out = []
    for it in items[:limit]:
        if isinstance(it, dict):
            t = it.get('title') or it.get('headline') or ''
            if t:
                out.append(t.strip())
    return out


# --- Claude classification (batched) -------------------------------------
def build_prompt(targets, taxonomy, examples):
    tax_line = '、'.join(t for t, _ in taxonomy[:60]) or '业绩、被收购、合作、大订单'
    ex_lines = '\n'.join(f'  [{tag}] {rea}' for tag, rea in examples[:45])
    blocks = []
    for t in targets:
        news = '\n'.join(f'    - {h}' for h in t['news'][:8]) or '    （无相关新闻）'
        blocks.append(
            f"{t['tk']}（{t['nm']}，当日 +{t['d1']}%）\n"
            f"  主营：{t['main_business'] or '（未知）'}\n"
            f"  新闻：\n{news}"
        )
    body = '\n\n'.join(blocks)
    return f"""你是美股异动归因助手。为每只当天大涨的股票，判断"催化剂标签(industry)"并写一句极简"理由(reason)"。

**industry = 催化剂/主题标签**（2-8 字，不是 GICS 行业）。**优先复用**以下已有标签，实在没有再按同样极简风格造新词：
{tax_line}

**reason = 一句事实性短句**（约 10-35 字），只陈述具体催化剂 + 关键数字，不加修饰。风格样例（标签 → 理由）：
{ex_lines}

**catalyst_kind** 从 {{earnings, clinical, ma, partnership, order, guidance, rating, product, other}} 中选一个（earnings=财报业绩，clinical=临床数据，ma=并购收购）。

规则：
- reason 务必**极简（≤25 字）、只写事实与数字**，禁止"升温/提振/情绪/概念/持续/带动"等主观修饰词。
- 财报类(earnings)的精确 EPS/beat 数字由系统另行填入，你的 reason 可先写事件概括。
- 有确切新闻/公告就写具体事实；没有确切依据时写得保守简短，拿不准就用 other + 一句主营概括。
- **只输出 JSON**，形如：
{{"AAPL": {{"industry": "业绩", "reason": "EPS beat，上调指引", "catalyst_kind": "earnings"}}, ...}}

待分析：

{body}"""


def classify(targets, taxonomy, examples, model):
    if not targets:
        return {}
    if not ANTHROPIC_API_KEY:
        print('[warn] ANTHROPIC_API_KEY not set — cannot classify')
        return {}
    prompt = build_prompt(targets, taxonomy, examples)
    body = json.dumps({
        'model': model,
        'max_tokens': 3000,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode('utf-8')
    headers = {
        'content-type': 'application/json',
        'anthropic-version': '2023-06-01',
        'x-api-key': ANTHROPIC_API_KEY,
    }
    try:
        raw = _http('https://api.anthropic.com/v1/messages', headers,
                    data=body, method='POST', timeout=180)
        data = json.loads(raw)
        text = ''.join(b.get('text', '') for b in data.get('content', [])
                       if b.get('type') == 'text')
        try:
            return json.loads(text.strip())
        except Exception:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
    except Exception as e:
        print(f'[warn] Claude classify failed: {e}')
    return {}


# --- main ----------------------------------------------------------------
def latest_date():
    dates = sorted(
        d.name for d in DATA_DIR.iterdir()
        if d.is_dir() and (d / 'screener.json').exists()
        and len(d.name) == 10 and d.name[4] == '-'
    ) if DATA_DIR.exists() else []
    return dates[-1] if dates else None


def main():
    ap = argparse.ArgumentParser(description='Auto-fill Top Movers industry/reason')
    ap.add_argument('--date', default=None)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--model', default=ANTHROPIC_MODEL)
    args = ap.parse_args()

    date = args.date or latest_date()
    if not date:
        print('ERROR: no screener.json found', file=sys.stderr)
        sys.exit(2)

    scr_path = DATA_DIR / date / 'screener.json'
    scr = json.loads(scr_path.read_text())
    rows = scr.get('rows', []) or []
    thr = (scr.get('filter') or {}).get('min_d1_pct', 15.0)

    try:
        news_by_tk = json.loads((DATA_DIR / date / 'movers_news.json').read_text()).get('by_ticker', {})
    except Exception:
        news_by_tk = {}

    taxonomy, examples, tags_by_tk = supa_fetch_history()

    highlighted = [r for r in rows if (r.get('d1') or 0) >= thr]
    targets = []
    for r in highlighted:
        tk = r['tk']
        have = tags_by_tk.get(tk, {})
        if not args.force and have.get('industry') and have.get('reason'):
            continue  # already carried forward — skip
        poly = [n.get('headline', '') for n in news_by_tk.get(tk, []) if n.get('headline')]
        lbn = lb_news_titles(tk)
        news = list(dict.fromkeys(poly + lbn))  # dedupe, keep order
        targets.append({
            'tk': tk, 'nm': r.get('nm', tk), 'd1': r.get('d1'),
            'main_business': r.get('main_business', ''), 'news': news,
        })

    print(f'[info] {date}: {len(highlighted)} highlighted (d1>={thr}), '
          f'{len(targets)} need fill{" (force)" if args.force else ""}')

    # --- Fill pass: classify + write suggestions for untagged highlights ---
    verdicts = classify(targets, taxonomy, examples, args.model) if targets else {}
    filled = []       # (tk, industry, reason)
    eff_kind = {}     # tk -> catalyst_kind for this run's verdicts
    for t in targets:
        tk = t['tk']
        v = verdicts.get(tk) or {}
        industry = (v.get('industry') or '').strip()
        reason = (v.get('reason') or '').strip()
        kind = (v.get('catalyst_kind') or '').strip()
        eff_kind[tk] = kind

        # Earnings: replace reason with real Longbridge beat numbers.
        if kind == 'earnings' or '业绩' in industry:
            eb = earnings_beat(tk)
            if eb:
                r2 = format_earnings_reason(eb)
                if r2:
                    reason = r2

        filled.append((tk, industry, reason))
        if not args.dry_run:
            try:
                supa_upsert(date, tk, industry, reason)
            except Exception as e:
                print(f'  [warn] supabase upsert {tk} failed: {e}')

    fresh_ind = {tk: ind for tk, ind, _ in filled}

    # --- Days-remaining pass: applies to ALL highlighted rows by their
    # EFFECTIVE tag (this run's fill wins, else the carried-forward tag), so
    # one-shot names get days=1 even when they were tagged on a prior day. ---
    row_by_tk = {r['tk']: r for r in rows}
    changed_screener = False
    day_rows = []     # (tk, industry, days_after)
    for r in highlighted:
        tk = r['tk']
        ind = fresh_ind.get(tk) or (tags_by_tk.get(tk) or {}).get('industry') or ''
        oneshot = is_oneshot(ind) or eff_kind.get(tk) in ('earnings', 'clinical')
        row = row_by_tk.get(tk)
        if oneshot and row and (row.get('days_remaining') != 1 or row.get('initial_days') != 1):
            if not args.dry_run:
                row['days_remaining'] = 1
                row['initial_days'] = 1
            changed_screener = True
        day_rows.append((tk, ind, 1 if oneshot else (row.get('days_remaining') if row else '?')))

    if changed_screener and not args.dry_run:
        scr_path.write_text(json.dumps(scr, ensure_ascii=False, indent=2) + '\n')

    pre = 'DRY-RUN — ' if args.dry_run else ''
    print(f'\n{pre}filled {len(filled)} ticker(s):')
    if filled:
        print(f'  {"TK":6s} {"industry":16s} reason')
        for tk, ind, rea in filled:
            print(f'  {tk:6s} {ind:16s} {rea}')
    print(f'\n{pre}days_remaining (all highlighted, effective tag):')
    print(f'  {"TK":6s} {"days":4s} industry')
    for tk, ind, days in day_rows:
        print(f'  {tk:6s} {str(days):4s} {ind}')
    print(f'\n[done] {"(dry-run, nothing written)" if args.dry_run else "wrote Supabase suggestions"}'
          f'{"" if args.dry_run else "; screener.json days updated" if changed_screener else ""}')


if __name__ == '__main__':
    main()
