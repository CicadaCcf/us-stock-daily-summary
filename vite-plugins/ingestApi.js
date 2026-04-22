// Vite dev middleware plugin: /api/ingest, /api/save
//
// - POST /api/ingest  { kind, mode, content, date? }
//     Calls Claude (official @anthropic-ai/sdk) with tool-forced JSON output.
//     `kind`: "events" | "macro"
//     `mode`: "text" | "image"
//     `content`: string (text) or base64 (image, no data: prefix)
//     Returns: { ok, data } where data matches the event/macro schema.
//
// - POST /api/save    { kind, date, data }
//     Writes src/data/{date}/{kind}.json. Creates the date directory
//     if missing. Returns { ok, path }.
//
// - TODO (Stage 2): POST /api/publish — commit JSON to GitHub main branch.
//
// Frontend always calls /api/ingest and /api/save; in prod these will be
// served by a Vercel function with identical contract. The frontend does
// not need to know dev vs prod.

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');
const DATA_DIR = path.join(REPO_ROOT, 'src', 'data');

// --- Tool schemas (forced structured output) -----------------------------

// Matches existing EVENTS shape in src/data/<date>/events.json
const EVENT_TOOL = {
  name: 'submit_events',
  description: 'Submit a list of industry/market events extracted from the input.',
  input_schema: {
    type: 'object',
    properties: {
      events: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            tag: { type: 'string', description: 'Short Chinese category label shown as colored pill (e.g. "AI估值", "监管", "反垄断").' },
            tagCls: {
              type: 'string',
              enum: ['', 'hot', 'warn'],
              description: 'CSS variant: "" (blue/default), "hot" (red/orange, top stories), "warn" (yellow, risk-related).',
            },
            title: { type: 'string', description: 'Chinese headline, ~15-30 chars.' },
            body: { type: 'string', description: 'Chinese summary, 2-4 sentences with key numbers preserved.' },
            tickers: {
              type: 'array',
              items: { type: 'string' },
              description: 'Relevant US stock tickers (symbols only, uppercase). Empty array if none.',
            },
          },
          required: ['tag', 'tagCls', 'title', 'body', 'tickers'],
        },
      },
    },
    required: ['events'],
  },
};

// Matches macro topic schema (new for Stage 1)
const MACRO_TOOL = {
  name: 'submit_macro',
  description: 'Submit macro/geopolitical/policy topics extracted from the input.',
  input_schema: {
    type: 'object',
    properties: {
      topics: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            id: { type: 'string', description: 'Stable kebab-case slug, e.g. "hormuz-strait-apr20".' },
            topic: { type: 'string', description: 'Short topic label, e.g. "霍尔木兹海峡" or "日本央行".' },
            topic_tag: {
              type: 'string',
              enum: ['geo', 'policy', 'rates', 'oil', 'ai', 'fed', 'earnings', 'macro-other'],
              description: 'CSS class: geo=geopolitics, policy=legislation/regulation, rates=yields/currencies, oil=energy, ai=AI sector macro, fed=central bank, earnings=earnings season macro, macro-other=catchall.',
            },
            title: { type: 'string', description: 'Chinese headline, ~15-30 chars.' },
            summary: { type: 'string', description: '2-3 sentence Chinese AI summary.' },
            bullets: {
              type: 'array',
              items: {
                type: 'object',
                properties: {
                  actor: { type: 'string', description: 'Named actor, e.g. "Trump", "Powell", "日本央行".' },
                  actor_type: {
                    type: 'string',
                    description: 'Role category: "US-Executive" / "US-Congress" / "Central-Bank" / "Foreign-Gov" / "Corporate" / "Analyst" / "KOL" / "新闻" / "Unknown". Use "新闻" for media reports (WSJ/FT/Axios/Bloomberg/AP/Reuters/路透/ABC). Use "KOL" for Twitter/X personalities and named-but-independent commentators. "Unknown" is a last resort only when truly unattributable.',
                  },
                  text: {
                    type: 'string',
                    description: 'One-sentence summary (collapsed view). Keep under ~35 Chinese chars — this is the line user scans.',
                  },
                  details: {
                    type: 'string',
                    description: 'Verbatim / near-verbatim preservation of the input sentence(s) for this bullet (expanded view). MUST retain all numbers, amounts, percentages, named sources, anonymous sources, nested conditions, alternative proposals. Do NOT summarize away facts here — this is the ground-truth cache. If input is already one short line, details may equal text.',
                  },
                  important: { type: 'boolean', description: 'True if this bullet is the most market-moving within its topic — UI highlights with red left border AND puts it first within the card.' },
                },
                required: ['actor', 'actor_type', 'text', 'details', 'important'],
              },
            },
            tickers: {
              type: 'array',
              items: { type: 'string' },
              description: 'Relevant US stock tickers. Empty array if none.',
            },
            image_indexes: {
              type: 'array',
              items: { type: 'integer' },
              description: 'If the user provided images along with text, list the 0-based indices (in the order they appeared) of images that belong to THIS topic. Use chart/table visuals to decide. Empty array if none of the images fit this topic. Do NOT put the same index in multiple topics unless the chart genuinely spans both.',
            },
            importance: {
              type: 'integer',
              enum: [1, 2, 3],
              description: '3=top-of-page, 2=mid, 1=minor. UI sorts desc.',
            },
            sources: {
              type: 'array',
              items: { type: 'string' },
              description: 'Source names, e.g. ["WSJ", "Bloomberg"]. Empty if unknown.',
            },
            date: { type: 'string', description: 'YYYY-MM-DD, typically the ingest date.' },
          },
          required: ['id', 'topic', 'topic_tag', 'title', 'summary', 'bullets', 'tickers', 'image_indexes', 'importance', 'sources', 'date'],
        },
      },
    },
    required: ['topics'],
  },
};

// --- Stable system prompts (cached) --------------------------------------
// Kept constant byte-for-byte so Claude's prompt cache can reuse the prefix.

const SYS_EVENTS = `你是一个美股市场事件分类器。输入是中文财经摘要或资讯截图。
你的任务：提取每一条独立的事件，并调用 submit_events 工具返回结构化数据。

**关键：events 字段必须是真实的 JSON 数组 [{...}, {...}]，绝对不要把数组序列化成字符串。**

分类规则 (tagCls)：
- "hot"  — 头部故事：AI/算力/估值/大额并购融资，今日最值得关注的 2-4 条
- "warn" — 风险类：反垄断、流动性担忧、概念炒作、监管处罚
- ""     — 其他常规：常规财报、业务动态、行业数据

tag 标签用 2-6 个汉字，尽量具体 (例："AI估值"优于"科技")。
title 15-30 字，保留核心数字。
body 2-4 句，保留关键数字 (估值、金额、百分比)。
tickers 只列直接相关的美股代号，忽略 ETF。

不要输出解释性文字，直接调用工具。`;

const SYS_MACRO = `你是一个宏观/地缘事件分类器。输入是中文财经摘要或截图，通常跨多个独立主题（地缘 / 央行 / 商品 / 财政 / 监管 / 卖方观点 / KOL）。
你的任务：**按主题拆分**——每个独立主题单独一张卡片，不得合并到其他主题。然后调用 submit_macro 工具返回。

**拆分 vs 合并规则（两条都要遵守）**：

*拆分*：完全无关的议题必须独立卡 — Fed 降息 / 油价预测 / 反垄断 / AI 基建 / 海峡地缘 属于不同卡。

*合并*：**同一叙事的不同侧面必须合并到一张卡**。关于美伊-海峡危机这一个事件的：Trump 发言、伊朗官方表态、美国国会立场、美军动作、伊朗军方动作、美伊谈判 — **全部归入一张「美伊/霍尔木兹危机」卡**，不要按说话人或角色拆成多张。一个叙事就一张卡。

*最短一张卡 ≥ 2 条 bullets*：**绝对不要创建只有 1 条 bullet 的卡片**。如果一条信息值得保留但找不到合适的主卡，放入「其他」卡（topic_tag: macro-other, topic: "其他"），不要自立门户。

*卖方/KOL 特例*：多家卖方或 KOL 的观点合并到「卖方观点」「KOL 观点」两张大卡，不要每家一张。

一条典型日报输入应该产出 **4-8 张大卡 + 1 张"其他"兜底卡**，不是 15+ 张碎卡。

**完整性规则（最重要，不容违反）**：
- 输入中出现的每一条独立事实（金额、百分比、人名、机构名、提案、匿名消息人、数字、条款）**必须**至少出现在某个 bullet 的 details 字段里
- 遇到复杂多方谈判（如"美方提议 X，伊方要求 Y，最新 Z"）→ 必须把每个数字和立场都写入一条或多条 bullets
- **禁止为了简洁跳过输入中的任何一段。宁可拆出更多 bullets 也不要丢信息。**
- 如果某段引文或消息来源找不到明确归属主题，独立开一张卡片，topic_tag 用 macro-other

**text vs details 区分**：
- text = 一句话概括（≤ 35 汉字），用户扫一眼能知道发生了什么
- details = 原文尽量保真（保留全部数字、来源、对手方立场、嵌套条件、匿名消息人措辞）。这是原文的"只读缓存"。输入本身很短时，details 可等于 text

**重要性与排序**：
- 每张卡片内，**important=true 的 bullet 必须排在数组第一位**
- importance 字段是卡片本身的重要度（1-3）

**图片分配（有图片输入时）**：
- 输入图片按上传顺序编号 0, 1, 2...
- 每个 topic 判断哪张图属于它（看图表标题/数据主题/表格内容）
- 把图片的 0-based index 放入该 topic 的 image_indexes 数组
- 一张图原则上只分配给一张卡（除非图表确实跨多主题）
- 若没有图片或该卡无相关图片，image_indexes = []

**关键：topics 字段必须是真实的 JSON 数组 [{...}, {...}]，绝对不要把数组序列化成字符串。bullets 同理。**

topic_tag CSS 类：
- geo         — 地缘冲突 (红海、台海、俄乌等)
- policy      — 立法/监管/关税
- rates       — 收益率、美元、汇率
- oil         — 原油/天然气
- ai          — AI 行业宏观 (不是单一公司)
- fed         — 美联储/央行
- earnings    — 财报季宏观层面 (不是单股)
- macro-other — 其他

importance: 3=头条、2=重要、1=一般。
bullets：每条一句话、带明确 actor。把"最市场驱动"的那条标 important=true，通常每卡 ≤1 条。
actor_type 枚举: US-Executive / US-Congress / Central-Bank / Foreign-Gov / Corporate / Analyst / Unknown。
id 用英文 kebab-case，便于 diff。
date 用 ingest 日期 (YYYY-MM-DD)。

不要输出解释性文字，直接调用工具。`;

// --- Helpers -------------------------------------------------------------

async function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      try {
        const raw = Buffer.concat(chunks).toString('utf8');
        resolve(raw ? JSON.parse(raw) : {});
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, body) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.end(JSON.stringify(body));
}

// Pre-pass to fix JSON emitted by Claude where string values contain inner
// ASCII double quotes that weren't escaped (common when `details` verbatim
// preserves a Chinese quote like 『"耍花招"』). State machine walks char by
// char; inside a string, a `"` is treated as closing only if the next
// non-whitespace char is one of , } ] :
// Otherwise it's escaped to \".
function escapeInnerQuotes(s) {
  let out = '';
  let inString = false;
  let escape = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (!inString) {
      out += c;
      if (c === '"') inString = true;
      continue;
    }
    if (escape) { out += c; escape = false; continue; }
    if (c === '\\') { out += c; escape = true; continue; }
    if (c === '"') {
      // Look ahead to decide: structural close or inner quote?
      let j = i + 1;
      while (j < s.length && /\s/.test(s[j])) j++;
      const next = s[j];
      if (next === undefined || next === ',' || next === '}' || next === ']' || next === ':') {
        out += c;
        inString = false;
      } else {
        out += '\\"';
      }
    } else {
      out += c;
    }
  }
  return out;
}

function sniffImageMediaType(b64) {
  // Decode enough bytes to peek at the magic header.
  const head = Buffer.from(b64.slice(0, 24), 'base64');
  if (head[0] === 0xff && head[1] === 0xd8) return 'image/jpeg';
  if (head[0] === 0x89 && head[1] === 0x50 && head[2] === 0x4e && head[3] === 0x47) return 'image/png';
  if (head[0] === 0x47 && head[1] === 0x49 && head[2] === 0x46) return 'image/gif';
  if (head[0] === 0x52 && head[1] === 0x49 && head[2] === 0x46 && head[3] === 0x46) return 'image/webp';
  return 'image/png'; // best-effort default
}

// Lazy import so we don't crash dev if the SDK isn't installed yet.
async function getAnthropicClient(env) {
  const apiKey = env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw Object.assign(new Error('ANTHROPIC_API_KEY not set in .env.local'), { status: 500 });
  }

  const { default: Anthropic } = await import('@anthropic-ai/sdk');

  // Proxy support for mainland-China dev.
  // The @anthropic-ai/sdk v0.64+ uses Node's built-in fetch (undici), which
  // ignores the classic `httpAgent` option AND the HTTPS_PROXY env var.
  // The correct way is to install `undici` and pass a ProxyAgent via a custom
  // fetch that calls undici.fetch with that dispatcher.
  const proxyUrl = env.HTTPS_PROXY || env.HTTP_PROXY;
  let customFetch;
  if (proxyUrl) {
    try {
      const { ProxyAgent, fetch: undiciFetch } = await import('undici');
      const dispatcher = new ProxyAgent(proxyUrl);
      customFetch = (url, init) => undiciFetch(url, { ...init, dispatcher });
    } catch (e) {
      console.warn('[ingestApi] proxy requested but undici unavailable:', e.message);
    }
  }

  const client = new Anthropic({
    apiKey,
    ...(customFetch ? { fetch: customFetch } : {}),
  });
  return client;
}

// For Events ingest: summarize the already-published macro.json into a
// compact list of topics + key bullets, so Claude can skip events that
// the Macro section already covers. Returns empty string if no macro
// file exists yet. This is appended to the USER message (not system)
// to preserve the cached system prefix.
function buildMacroDedupContext(date) {
  if (!date) return '';
  const p = path.join(DATA_DIR, date, 'macro.json');
  if (!fs.existsSync(p)) return '';
  let topics;
  try { topics = JSON.parse(fs.readFileSync(p, 'utf8')).topics; } catch { return ''; }
  if (!Array.isArray(topics) || topics.length === 0) return '';

  const lines = topics
    .filter((t) => t && t.topic)
    .map((t) => {
      const tag = t.topic_tag ? ` (${t.topic_tag})` : '';
      const bullets = Array.isArray(t.bullets) ? t.bullets.slice(0, 4) : [];
      const inner = bullets
        .map((b) => b && (b.text || b.details || '').toString().slice(0, 60))
        .filter(Boolean)
        .join(' / ');
      return `• ${t.topic}${tag}${inner ? ' — ' + inner : ''}`;
    })
    .join('\n');

  return `

---
[今日宏观日览已覆盖的主题 · src/data/${date}/macro.json]
${lines}

**去重要求**：以上主题已在"宏观日览"区块展示。请从 events 结果中**剔除任何被以上主题完全覆盖的内容**（如地缘表态、央行政策、宏观商品走向等）。只保留真正独立的**行业/个股事件**（财报、并购、监管细项、产品发布、股东行动等）。如不确定某条是否独立，倾向于**剔除**。`;
}

async function callClaude({ env, kind, text, images, imageUrls, date, modelOverride }) {
  const client = await getAnthropicClient(env);

  const model = modelOverride || env.ANTHROPIC_MODEL || 'claude-opus-4-5';
  const isEvents = kind === 'events';
  const tool = isEvents ? EVENT_TOOL : MACRO_TOOL;
  const systemText = isEvents ? SYS_EVENTS : SYS_MACRO;

  // Build user content: optional images first, then text. Both can coexist.
  const userContent = [];
  if (Array.isArray(images)) {
    for (const b64 of images) {
      if (!b64) continue;
      userContent.push({
        type: 'image',
        source: { type: 'base64', media_type: sniffImageMediaType(b64), data: b64 },
      });
    }
  }
  // For events, append the already-covered macro topics so Claude can
  // filter out duplicates. No-op if macro.json doesn't exist yet.
  const dedupSuffix = isEvents ? buildMacroDedupContext(date) : '';
  if (text && String(text).trim()) {
    userContent.push({ type: 'text', text: String(text) + dedupSuffix });
  } else if (userContent.length > 0) {
    userContent.push({ type: 'text', text: '请从上图中提取全部事件/主题，结合图中图表与文字。' + dedupSuffix });
  }
  if (dedupSuffix) {
    console.log('[ingestApi] events dedup: appended macro context from', date);
  }

  const response = await client.messages.create({
    model,
    max_tokens: 16000,
    // System prompt is stable across requests — mark for prefix cache.
    system: [
      { type: 'text', text: systemText, cache_control: { type: 'ephemeral' } },
    ],
    tools: [tool],
    tool_choice: { type: 'tool', name: tool.name },
    messages: [{ role: 'user', content: userContent }],
  });

  // Extract the forced tool_use block.
  const toolUse = response.content.find((b) => b.type === 'tool_use');
  if (!toolUse) {
    throw Object.assign(new Error('Claude returned no tool_use block'), { status: 502 });
  }
  // Defensive unwrap: Sonnet sometimes stringifies nested arrays. If the
  // top-level `events` / `topics` is a string, try JSON.parse it.
  const raw = toolUse.input || {};
  const key = isEvents ? 'events' : 'topics';
  let data = raw;
  if (typeof raw[key] === 'string') {
    try {
      data = { ...raw, [key]: JSON.parse(raw[key]) };
      console.log('[ingestApi] parsed stringified', key, '→ array of', data[key].length);
    } catch (e) {
      console.log('[ingestApi] strict parse failed:', e.message, '→ trying jsonrepair');
      // Lenient: Claude sometimes emits stringified JSON with unescaped
      // ASCII quotes inside string values (e.g. 『"耍花招"』 copied verbatim).
      // jsonrepair handles this specific breakage well.
      try {
        // First try our quote-escape pre-pass (most common failure mode:
        // unescaped ASCII " inside Chinese verbatim `details` strings).
        const fixed = escapeInnerQuotes(raw[key]);
        data = { ...raw, [key]: JSON.parse(fixed) };
      } catch {
        try {
          const { jsonrepair } = await import('jsonrepair');
          const repaired = jsonrepair(raw[key]);
          data = { ...raw, [key]: JSON.parse(repaired) };
        } catch (e2) {
          console.log('[ingestApi] all repair attempts failed:', e2.message);
        }
      }
    }
  }
  // Inject the canonical ingest date server-side so it never depends on
  // whatever year the LLM hallucinates from the input text.
  if (date && Array.isArray(data[key])) {
    data[key] = data[key].map((item) => ({ ...item, date }));
  }

  // Resolve image_indexes → image_urls on each macro topic (server-side join).
  const urlList = Array.isArray(imageUrls) ? imageUrls : [];
  if (!isEvents && Array.isArray(data.topics) && urlList.length > 0) {
    data.topics = data.topics.map((t) => {
      const idxs = Array.isArray(t.image_indexes) ? t.image_indexes : [];
      const image_urls = idxs
        .filter((i) => Number.isInteger(i) && i >= 0 && i < urlList.length)
        .map((i) => urlList[i]);
      return { ...t, image_urls };
    });
  }

  // Safety net: collapse any 1-bullet topics the LLM still emitted into
  // a single "其他" catch-all card preserving each bullet's content.
  // (Only for macro — industry events are fine as singletons.)
  if (!isEvents && Array.isArray(data.topics)) {
    const multi = [];
    const singles = [];
    for (const t of data.topics) {
      const n = Array.isArray(t.bullets) ? t.bullets.length : 0;
      if (n >= 2) multi.push(t);
      else if (n === 1) singles.push(t);
    }
    if (singles.length > 0) {
      // Merge into existing "其他" card if the LLM made one, else create.
      let other = multi.find((t) => t.topic_tag === 'macro-other' && /^其他|杂项|Other/i.test(t.topic || ''));
      if (!other) {
        other = {
          id: 'other-' + (date || 'today'),
          topic: '其他',
          topic_tag: 'macro-other',
          title: '其他零散信息',
          bullets: [],
          tickers: [],
          importance: 1,
          sources: [],
          date: date || '',
        };
        multi.push(other);
      }
      for (const s of singles) {
        // Prefix the bullet text with a short topic hint so context isn't lost.
        for (const b of s.bullets || []) {
          const prefix = s.topic ? `[${s.topic}] ` : '';
          other.bullets.push({
            ...b,
            text: prefix + (b.text || ''),
            details: b.details && b.details !== b.text ? b.details : b.text,
          });
        }
        // Merge sources / tickers into other
        (s.sources || []).forEach((x) => {
          if (!other.sources.includes(x)) other.sources.push(x);
        });
        (s.tickers || []).forEach((x) => {
          if (!other.tickers.includes(x)) other.tickers.push(x);
        });
        // Carry images from merged singleton up to 其他.
        if (!Array.isArray(other.image_urls)) other.image_urls = [];
        (s.image_urls || []).forEach((u) => {
          if (!other.image_urls.includes(u)) other.image_urls.push(u);
        });
      }
    }
    data.topics = multi;
  }
  return {
    data,
    usage: response.usage,
    model: response.model,
  };
}

// --- Update + Publish (dev-only, streams chunks back to the browser) -----
//
// /api/update   — runs polygon_snapshot.py and finviz_screenshot.py so the
//                 user can review the new data + chart in the browser BEFORE
//                 anything hits GitHub. No git commands run here.
// /api/publish  — only git add / commit / push. Separate so a bad Finviz
//                 screenshot or a broken macro paste never reaches prod.
// Both endpoints stream stdout/stderr as chunked text/plain. The browser
// reads via fetch().body.getReader() and appends to a log box. On exit they
// write a final sentinel line the frontend parses:
//     __STATUS__ ok=<true|false> [commit=<sha>] [date=<YYYY-MM-DD>]
// so client code can tell success from failure without re-parsing the log.

function startStream(res) {
  res.writeHead(200, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Content-Type-Options': 'nosniff',
  });
  return (s) => res.write(s);
}

function spawnStep(bin, args, write, env = process.env) {
  return new Promise((resolve, reject) => {
    write(`\n▶ ${bin} ${args.join(' ')}\n`);
    const proc = spawn(bin, args, { cwd: REPO_ROOT, env });
    proc.stdout.on('data', (d) => write(d.toString()));
    proc.stderr.on('data', (d) => write(d.toString()));
    proc.on('error', (e) => reject(new Error(`${bin}: ${e.message}`)));
    proc.on('close', (code) =>
      code === 0 ? resolve() : reject(new Error(`${bin} exited ${code}`))
    );
  });
}

function latestDataDate() {
  try {
    const dirs = fs.readdirSync(DATA_DIR, { withFileTypes: true })
      .filter((d) => d.isDirectory() && /^\d{4}-\d{2}-\d{2}$/.test(d.name))
      .map((d) => d.name)
      .sort();
    return dirs[dirs.length - 1] || null;
  } catch { return null; }
}

async function handleUpdate(req, res) {
  const write = startStream(res);
  // Optional ?date=YYYY-MM-DD to force polygon + futu to target a specific
  // folder (e.g. re-run 2026-04-22 after a schema change). When omitted each
  // script picks its own default (polygon uses ref_date, futu uses latest).
  const url = new URL(req.url, 'http://x');
  const targetDate = url.searchParams.get('date');
  const dateOk = targetDate && /^\d{4}-\d{2}-\d{2}$/.test(targetDate);
  const pyArgs = (script) => dateOk ? [script, '--date', targetDate] : [script];
  try {
    write(`[info] step 1/3: polygon + yahoo + CNN PCR snapshot${dateOk ? ` · date=${targetDate}` : ''}\n`);
    await spawnStep('python3', ['-u', ...pyArgs('server/polygon_snapshot.py')], write);

    write(`\n[info] step 2/3: Finviz bubble screenshot\n`);
    try {
      await spawnStep('python3', ['-u', 'server/finviz_screenshot.py'], write);
    } catch (e) {
      // Finviz failure is non-fatal — the previous PNG stays usable and the
      // user can retry after checking their debug Chrome is up.
      write(`\n[warn] Finviz step failed: ${e.message}\n`);
      write(`[warn] keeping previous public/finviz_bubble.png\n`);
    }

    write(`\n[info] step 3/3: Polygon news for Top Movers\n`);
    try {
      await spawnStep('python3', ['-u', ...pyArgs('server/movers_news.py')], write);
    } catch (e) {
      // Non-fatal — if Polygon news call fails the reason picker just shows
      // empty candidate lists; manual input in the cell still works.
      write(`\n[warn] Movers news step failed: ${e.message}\n`);
      write(`[warn] keeping previous src/data/{date}/movers_news.json\n`);
    }
    const date = latestDataDate();
    write(`\n__STATUS__ ok=true date=${date}\n`);
  } catch (e) {
    write(`\n__STATUS__ ok=false error=${e.message}\n`);
  }
  res.end();
}

function gitSpawn(args, write) {
  return new Promise((resolve, reject) => {
    write(`\n▶ git ${args.join(' ')}\n`);
    const proc = spawn('git', args, { cwd: REPO_ROOT, env: process.env });
    let stdout = '';
    proc.stdout.on('data', (d) => { const s = d.toString(); stdout += s; write(s); });
    proc.stderr.on('data', (d) => write(d.toString()));
    proc.on('error', (e) => reject(new Error(`git ${args[0]}: ${e.message}`)));
    proc.on('close', (code) =>
      code === 0 ? resolve(stdout.trim()) : reject(new Error(`git ${args[0]} exited ${code}`))
    );
  });
}

async function handlePublish(req, res) {
  const write = startStream(res);
  try {
    const date = latestDataDate();
    if (!date) throw new Error('no src/data/YYYY-MM-DD folder found');

    // 1. Show what's changing so the user sees it in the log.
    await gitSpawn(['status', '--short'], write);

    // 2. Stage just the files the update flow touches — never blanket `git add .`
    //    which could pick up unrelated local edits (.env.local won't get added
    //    because of the gitignore, but be explicit anyway). public/uploads/{date}/
    //    holds attachments referenced by macro.json's image_urls; without it,
    //    the prod site renders broken <img>s.
    const addTargets = [`src/data/${date}`, 'public/finviz_bubble.png'];
    const uploadsDir = path.join(REPO_ROOT, 'public', 'uploads', date);
    if (fs.existsSync(uploadsDir)) {
      addTargets.push(`public/uploads/${date}`);
    }
    await gitSpawn(['add', ...addTargets], write);

    // 3. If nothing staged, skip the commit — not an error, common when user
    //    pressed Publish twice without re-running Update.
    const diffCached = await new Promise((resolve) => {
      const p = spawn('git', ['diff', '--cached', '--quiet'], { cwd: REPO_ROOT });
      p.on('close', (code) => resolve(code));
    });
    if (diffCached === 0) {
      write(`\n[info] nothing staged — already in sync with last publish\n`);
      write(`\n__STATUS__ ok=true date=${date} commit=noop\n`);
      return res.end();
    }

    await gitSpawn(['commit', '-m', `daily: ${date}`], write);
    const sha = await gitSpawn(['rev-parse', '--short', 'HEAD'], write);
    await gitSpawn(['push'], write);

    write(`\n__STATUS__ ok=true date=${date} commit=${sha}\n`);
  } catch (e) {
    write(`\n__STATUS__ ok=false error=${e.message}\n`);
  }
  res.end();
}

// --- Plugin factory ------------------------------------------------------

export function ingestApiPlugin(env) {
  return {
    name: 'ingest-api',
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url || !req.url.startsWith('/api/')) return next();

        try {
          if (req.method === 'POST' && req.url === '/api/ingest') {
            const body = await readJsonBody(req);
            const { kind, text, images, image_urls, model: modelOverride, mode, content } = body || {};
            if (!['events', 'macro'].includes(kind)) {
              return sendJson(res, 400, { ok: false, error: 'kind must be "events" or "macro"' });
            }
            // Normalize inputs: accept new {text, images[]} or legacy {mode, content}.
            let textIn = typeof text === 'string' ? text : '';
            let imagesIn = Array.isArray(images) ? images.filter((s) => typeof s === 'string' && s.length > 0) : [];
            if (!textIn && !imagesIn.length) {
              if (mode === 'text' && typeof content === 'string') textIn = content;
              else if (mode === 'image' && typeof content === 'string') imagesIn = [content];
            }
            if (!textIn && imagesIn.length === 0) {
              return sendJson(res, 400, { ok: false, error: 'either text or at least one image is required' });
            }
            const date = body?.date && /^\d{4}-\d{2}-\d{2}$/.test(body.date) ? body.date : null;
            const urlsIn = Array.isArray(image_urls)
              ? image_urls.filter((u) => typeof u === 'string')
              : [];
            const result = await callClaude({
              env, kind, text: textIn, images: imagesIn, imageUrls: urlsIn, date,
              modelOverride: typeof modelOverride === 'string' && modelOverride ? modelOverride : null,
            });
            return sendJson(res, 200, { ok: true, ...result });
          }

          if (req.method === 'POST' && req.url === '/api/upload') {
            const body = await readJsonBody(req);
            const { date, b64, filename } = body || {};
            if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
              return sendJson(res, 400, { ok: false, error: 'date must be YYYY-MM-DD' });
            }
            if (!b64 || typeof b64 !== 'string') {
              return sendJson(res, 400, { ok: false, error: 'b64 (string) is required' });
            }
            const mediaType = sniffImageMediaType(b64);
            const ext = (mediaType.split('/')[1] || 'bin').replace('jpeg', 'jpg');
            const buf = Buffer.from(b64, 'base64');
            const hash = crypto.createHash('sha1').update(buf).digest('hex').slice(0, 12);
            const dir = path.join(REPO_ROOT, 'public', 'uploads', date);
            fs.mkdirSync(dir, { recursive: true });
            const filePath = path.join(dir, `${hash}.${ext}`);
            if (!fs.existsSync(filePath)) fs.writeFileSync(filePath, buf);
            const url = `/uploads/${date}/${hash}.${ext}`;
            return sendJson(res, 200, { ok: true, url, hash, bytes: buf.length, filename: filename || null });
          }

          if (req.method === 'POST' && req.url === '/api/update') {
            await handleUpdate(req, res);
            return;
          }

          if (req.method === 'POST' && req.url === '/api/publish') {
            await handlePublish(req, res);
            return;
          }

          if (req.method === 'POST' && req.url === '/api/screener-edit') {
            const body = await readJsonBody(req);
            const { date, tk, field, value } = body || {};
            if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
              return sendJson(res, 400, { ok: false, error: 'date must be YYYY-MM-DD' });
            }
            if (!tk || typeof tk !== 'string') {
              return sendJson(res, 400, { ok: false, error: 'tk required' });
            }
            // Whitelist — only user-editable columns may be overwritten from the UI.
            const EDITABLE = new Set(['industry', 'reason']);
            if (!EDITABLE.has(field)) {
              return sendJson(res, 400, { ok: false, error: `field must be one of ${[...EDITABLE].join(',')}` });
            }
            const filePath = path.join(DATA_DIR, date, 'screener.json');
            if (!fs.existsSync(filePath)) {
              return sendJson(res, 404, { ok: false, error: `no screener.json for ${date}` });
            }
            const json = JSON.parse(fs.readFileSync(filePath, 'utf8'));
            const rows = Array.isArray(json.rows) ? json.rows : [];
            const row = rows.find(r => r.tk === tk);
            if (!row) {
              return sendJson(res, 404, { ok: false, error: `ticker ${tk} not found in ${date}` });
            }
            row[field] = typeof value === 'string' ? value : '';
            fs.writeFileSync(filePath, JSON.stringify(json, null, 2) + '\n', 'utf8');
            return sendJson(res, 200, { ok: true, tk, field });
          }

          if (req.method === 'POST' && req.url === '/api/save') {
            const body = await readJsonBody(req);
            const { kind, date, data } = body || {};
            if (!kind || !/^[a-z]+$/i.test(kind)) {
              return sendJson(res, 400, { ok: false, error: 'kind must be a filename-safe string' });
            }
            if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
              return sendJson(res, 400, { ok: false, error: 'date must be YYYY-MM-DD' });
            }
            if (data == null || typeof data !== 'object') {
              return sendJson(res, 400, { ok: false, error: 'data (object) is required' });
            }
            const dir = path.join(DATA_DIR, date);
            fs.mkdirSync(dir, { recursive: true });
            const filePath = path.join(dir, `${kind}.json`);
            fs.writeFileSync(filePath, JSON.stringify(data, null, 2) + '\n', 'utf8');
            return sendJson(res, 200, { ok: true, path: path.relative(REPO_ROOT, filePath) });
          }

          if (req.method === 'GET' && req.url === '/api/status') {
            return sendJson(res, 200, {
              ok: true,
              hasApiKey: !!env.ANTHROPIC_API_KEY,
              model: env.ANTHROPIC_MODEL || 'claude-opus-4-7',
              proxy: env.HTTPS_PROXY || env.HTTP_PROXY || null,
            });
          }

          return next();
        } catch (err) {
          const status = err?.status || 500;
          console.error('[ingestApi] error:', err);
          return sendJson(res, status, { ok: false, error: err?.message || String(err) });
        }
      });
    },
  };
}
