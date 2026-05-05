#!/usr/bin/env python3
"""Daily 08:10 Beijing job: read today's Notion '宏观日览' + '全球重点事件'
toggles, classify via the same Claude tools as the dev-server /api/ingest
endpoint, and write src/data/{ny_date}/macro.json + events.json.

The raw Notion text for each toggle is preserved into a `_archive` field on
the written JSON so the source paste is recoverable without round-tripping
back to Notion.

Auth: ANTHROPIC_API_KEY + NOTION_TOKEN from .env.local. Honors HTTPS_PROXY /
HTTP_PROXY for mainland-China connectivity (same pattern as ingestApi.js).

NOTE on prompt drift: the EVENT_TOOL / MACRO_TOOL schemas and the SYS_EVENTS /
SYS_MACRO system prompts below are copied byte-for-byte from
vite-plugins/ingestApi.js. If you edit prompts in one file, edit the other
too — otherwise the daily ingest and the manual /api/ingest path will
diverge and prompt-cache hits will be lost.

Usage:
    python3 server/notion_to_dashboard.py            # production
    python3 server/notion_to_dashboard.py --dry-run  # fetch + classify, no write
    python3 server/notion_to_dashboard.py --kind macro    # only macro
    python3 server/notion_to_dashboard.py --kind events   # only events
    python3 server/notion_to_dashboard.py --date 2026-04-27  # override NY date
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

REPO = Path(__file__).resolve().parents[1]
ENV_FILE = REPO / ".env.local"
DATA_DIR = REPO / "src" / "data"

NOTION_PAGE_ID = "350cd422-cbff-80b3-b74a-c7b81f80b9cc"  # "Daily Summary"
NOTION_VERSION = "2022-06-28"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


# ---------- env / http ----------

def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        raise RuntimeError(f"{ENV_FILE} missing")
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def make_opener(env: dict[str, str]) -> urllib.request.OpenerDirector:
    proxy_url = env.get("HTTPS_PROXY") or env.get("HTTP_PROXY")
    if proxy_url:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    return urllib.request.build_opener()


def notion_req(method: str, path: str, token: str, body=None):
    url = f"https://api.notion.com/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Notion-Version", NOTION_VERSION)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        print(f"  Notion {method} {path} -> {e.code}: {e.read().decode()[:500]}",
              file=sys.stderr)
        raise


# ---------- date helpers ----------

def ny_trading_date_iso() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def fmt_date_label(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{y}/{int(m)}/{int(d)}"


# ---------- notion read ----------

def list_children(block_id: str, token: str) -> list[dict]:
    out = []
    cursor = None
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        resp = notion_req("GET", path, token)
        out.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return out


def text_of(block: dict) -> str:
    t = block.get("type")
    if not t:
        return ""
    rich = block.get(t, {}).get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rich).strip()


# Block-types we know how to render. The set is intentionally narrow — we
# walk children for everything else but emit no leading marker.
_PREFIX = {
    "heading_1": "# ",
    "heading_2": "## ",
    "heading_3": "### ",
    "bulleted_list_item": "- ",
    "numbered_list_item": "1. ",
    "quote": "> ",
    "toggle": "▸ ",
}


def block_to_text(block: dict, token: str, depth: int = 0) -> str:
    """Recursively render a Notion block (and its descendants) to markdown-ish
    text. Preserves nesting via 2-space indent. Image blocks become
    `![image](url)` so Claude can see references even though we don't pass
    actual image bytes in this v1 (text-only ingest)."""
    t = block.get("type")
    indent = "  " * depth
    lines: list[str] = []

    text = text_of(block)
    if t in _PREFIX and text:
        lines.append(f"{indent}{_PREFIX[t]}{text}")
    elif t == "paragraph" and text:
        lines.append(f"{indent}{text}")
    elif t == "to_do":
        checked = block.get("to_do", {}).get("checked", False)
        marker = "- [x] " if checked else "- [ ] "
        if text:
            lines.append(f"{indent}{marker}{text}")
    elif t == "code":
        code_lang = block.get("code", {}).get("language", "")
        lines.append(f"{indent}```{code_lang}")
        if text:
            for ln in text.split("\n"):
                lines.append(f"{indent}{ln}")
        lines.append(f"{indent}```")
    elif t == "image":
        img = block.get("image", {})
        url = (img.get("file") or {}).get("url") or (img.get("external") or {}).get("url") or ""
        cap_rich = img.get("caption", [])
        cap = "".join(r.get("plain_text", "") for r in cap_rich).strip() or "image"
        if url:
            lines.append(f"{indent}![{cap}]({url})")
    elif t == "divider":
        lines.append(f"{indent}---")
    elif t == "callout" and text:
        lines.append(f"{indent}> 💡 {text}")
    # ignore: child_page, child_database, table, embed, etc.

    if block.get("has_children"):
        for child in list_children(block["id"], token):
            sub = block_to_text(child, token, depth + 1)
            if sub:
                lines.append(sub)
    return "\n".join(lines)


def find_date_toggle(page_id: str, date_iso: str, token: str) -> dict | None:
    label = fmt_date_label(date_iso)
    for b in list_children(page_id, token):
        if b.get("type") != "toggle":
            continue
        text = text_of(b)
        if text in (label, date_iso):
            return b
    return None


def find_subtoggle(parent_id: str, name: str, token: str) -> dict | None:
    for b in list_children(parent_id, token):
        if b.get("type") == "toggle" and name in text_of(b):
            return b
    return None


# ---------- claude tools / prompts (copied from vite-plugins/ingestApi.js) ----------

# IMPORTANT: keep these byte-for-byte in sync with ingestApi.js. The prompt
# cache is keyed off literal text, so any drift = cold cache = slower + costlier.

EVENT_TOOL = {
    "name": "submit_events",
    "description": "Submit a list of industry/market events extracted from the input.",
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string", "description": "Short Chinese category label shown as colored pill (e.g. \"AI估值\", \"监管\", \"反垄断\")."},
                        "tagCls": {
                            "type": "string",
                            "enum": ["", "hot", "warn"],
                            "description": "CSS variant: \"\" (blue/default), \"hot\" (red/orange, top stories), \"warn\" (yellow, risk-related).",
                        },
                        "title": {"type": "string", "description": "Chinese headline, ~15-30 chars."},
                        "body": {"type": "string", "description": "Chinese summary, 2-4 sentences with key numbers preserved."},
                        "tickers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Relevant US stock tickers (symbols only, uppercase). Empty array if none.",
                        },
                    },
                    "required": ["tag", "tagCls", "title", "body", "tickers"],
                },
            },
        },
        "required": ["events"],
    },
}

MACRO_TOOL = {
    "name": "submit_macro",
    "description": "Submit macro/geopolitical/policy topics extracted from the input.",
    "input_schema": {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Stable kebab-case slug, e.g. \"hormuz-strait-apr20\"."},
                        "topic": {"type": "string", "description": "Short topic label, e.g. \"霍尔木兹海峡\" or \"日本央行\"."},
                        "topic_tag": {
                            "type": "string",
                            "enum": ["geo", "policy", "rates", "oil", "ai", "fed", "earnings", "macro-other"],
                            "description": "CSS class: geo=geopolitics, policy=legislation/regulation, rates=yields/currencies, oil=energy, ai=AI sector macro, fed=central bank, earnings=earnings season macro, macro-other=catchall.",
                        },
                        "title": {"type": "string", "description": "Chinese headline, ~15-30 chars."},
                        "summary": {"type": "string", "description": "2-3 sentence Chinese AI summary."},
                        "bullets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "actor": {"type": "string", "description": "Named actor, e.g. \"Trump\", \"Powell\", \"日本央行\"."},
                                    "actor_type": {
                                        "type": "string",
                                        "description": "Role category: \"US-Executive\" / \"US-Congress\" / \"Central-Bank\" / \"Foreign-Gov\" / \"Corporate\" / \"Analyst\" / \"KOL\" / \"新闻\" / \"Unknown\". Use \"新闻\" for media reports (WSJ/FT/Axios/Bloomberg/AP/Reuters/路透/ABC). Use \"KOL\" for Twitter/X personalities and named-but-independent commentators. \"Unknown\" is a last resort only when truly unattributable.",
                                    },
                                    "text": {
                                        "type": "string",
                                        "description": "One-sentence summary (collapsed view). Keep under ~35 Chinese chars — this is the line user scans.",
                                    },
                                    "details": {
                                        "type": "string",
                                        "description": "Verbatim / near-verbatim preservation of the input sentence(s) for this bullet (expanded view). MUST retain all numbers, amounts, percentages, named sources, anonymous sources, nested conditions, alternative proposals. Do NOT summarize away facts here — this is the ground-truth cache. If input is already one short line, details may equal text.",
                                    },
                                    "important": {"type": "boolean", "description": "True if this bullet is the most market-moving within its topic — UI highlights with red left border AND puts it first within the card."},
                                },
                                "required": ["actor", "actor_type", "text", "details", "important"],
                            },
                        },
                        "tickers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Relevant US stock tickers. Empty array if none.",
                        },
                        "image_indexes": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "If the user provided images along with text, list the 0-based indices (in the order they appeared) of images that belong to THIS topic. Use chart/table visuals to decide. Empty array if none of the images fit this topic. Do NOT put the same index in multiple topics unless the chart genuinely spans both.",
                        },
                        "importance": {
                            "type": "integer",
                            "enum": [1, 2, 3],
                            "description": "3=top-of-page, 2=mid, 1=minor. UI sorts desc.",
                        },
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Source names, e.g. [\"WSJ\", \"Bloomberg\"]. Empty if unknown.",
                        },
                        "date": {"type": "string", "description": "YYYY-MM-DD, typically the ingest date."},
                    },
                    "required": ["id", "topic", "topic_tag", "title", "summary", "bullets", "tickers", "image_indexes", "importance", "sources", "date"],
                },
            },
        },
        "required": ["topics"],
    },
}

SYS_EVENTS = """你是一个美股市场事件分类器。输入是中文财经摘要或资讯截图。
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

不要输出解释性文字，直接调用工具。"""

SYS_MACRO = """你是一个宏观/地缘事件分类器。输入是中文财经摘要或截图，通常跨多个独立主题（地缘 / 央行 / 商品 / 财政 / 监管 / 卖方观点 / KOL）。
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

不要输出解释性文字，直接调用工具。"""


# ---------- claude call ----------

def build_macro_dedup_context(date: str) -> str:
    """For events ingest: append the just-published macro topics so Claude
    can drop events already covered by the macro section. Mirrors
    buildMacroDedupContext() in ingestApi.js."""
    if not date:
        return ""
    p = DATA_DIR / date / "macro.json"
    if not p.exists():
        return ""
    try:
        topics = json.loads(p.read_text()).get("topics") or []
    except Exception:
        return ""
    if not topics:
        return ""
    lines = []
    for t in topics:
        if not t or not t.get("topic"):
            continue
        tag = f" ({t['topic_tag']})" if t.get("topic_tag") else ""
        bullets = (t.get("bullets") or [])[:4]
        inner = " / ".join(
            (b.get("text") or b.get("details") or "")[:60]
            for b in bullets if b
        ).strip(" /")
        suffix = f" — {inner}" if inner else ""
        lines.append(f"• {t['topic']}{tag}{suffix}")
    if not lines:
        return ""
    return f"""

---
[今日宏观日览已覆盖的主题 · src/data/{date}/macro.json]
{chr(10).join(lines)}

**去重要求**：以上主题已在"宏观日览"区块展示。请从 events 结果中**剔除任何被以上主题完全覆盖的内容**（如地缘表态、央行政策、宏观商品走向等）。只保留真正独立的**行业/个股事件**（财报、并购、监管细项、产品发布、股东行动等）。如不确定某条是否独立，倾向于**剔除**。"""


def call_claude(env: dict, kind: str, text: str, date: str, opener) -> dict:
    api_key = env.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing in .env.local")
    model = env.get("ANTHROPIC_MODEL") or "claude-opus-4-5"

    is_events = kind == "events"
    tool = EVENT_TOOL if is_events else MACRO_TOOL
    sys_text = SYS_EVENTS if is_events else SYS_MACRO

    user_text = text
    if is_events:
        dedup = build_macro_dedup_context(date)
        if dedup:
            user_text = text + dedup
            print(f"  [events] appended macro dedup context ({len(dedup)} chars)")

    body = {
        "model": model,
        "max_tokens": 16000,
        "system": [{"type": "text", "text": sys_text,
                    "cache_control": {"type": "ephemeral"}}],
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
    }

    body_bytes = json.dumps(body).encode()
    # DEBUG: dump request body for diagnostics
    debug_path = REPO / f"/tmp/anthropic_req_{kind}.json"
    try:
        debug_path.write_bytes(body_bytes)
        print(f"  [{kind}] body: {len(body_bytes):,} bytes -> {debug_path}")
    except Exception:
        pass

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body_bytes,
        method="POST",
    )
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", ANTHROPIC_VERSION)
    req.add_header("content-type", "application/json")

    try:
        with opener.open(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        snippet = e.read().decode()[:500]
        raise RuntimeError(f"Anthropic {e.code}: {snippet}") from None


def extract_tool_payload(api_resp: dict, kind: str) -> dict:
    """Pull the forced tool_use block out of Claude's response. Defensive
    re-parse if the LLM stringified the inner array (mirrors ingestApi.js)."""
    is_events = kind == "events"
    key = "events" if is_events else "topics"
    for blk in api_resp.get("content", []):
        if blk.get("type") != "tool_use":
            continue
        inp = blk.get("input") or {}
        val = inp.get(key)
        if isinstance(val, str):
            try:
                val = json.loads(val)
                print(f"  [{kind}] unstringified {key} -> array of {len(val)}")
            except Exception as e:
                print(f"  [{kind}] WARNING: could not parse stringified {key}: {e}",
                      file=sys.stderr)
        if val is None:
            raise RuntimeError(f"tool_use block missing '{key}' field")
        return {key: val, "_usage": api_resp.get("usage", {}), "_model": api_resp.get("model")}
    raise RuntimeError("no tool_use block in Claude response")


# ---------- main pipeline ----------

def run_pipeline(args, env):
    token = env.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN missing in .env.local")

    ny_date = args.date or ny_trading_date_iso()
    label = fmt_date_label(ny_date)
    print(f"  NY trading date: {ny_date} (toggle '{label}')")

    date_toggle = find_date_toggle(NOTION_PAGE_ID, ny_date, token)
    if date_toggle is None:
        raise RuntimeError(f"no toggle '{label}' on Notion page — has it been "
                           f"created yet? (alphapai_to_notion.py creates it "
                           f"automatically on first run for that date)")
    print(f"  date toggle: {date_toggle['id']}")

    macro_toggle = find_subtoggle(date_toggle["id"], "宏观日览", token)
    events_toggle = find_subtoggle(date_toggle["id"], "全球重点事件", token)
    if not macro_toggle:
        raise RuntimeError("no '宏观日览' sub-toggle found")
    if not events_toggle:
        raise RuntimeError("no '全球重点事件' sub-toggle found")

    # Render to text
    macro_text = "\n".join(
        block_to_text(b, token, 0)
        for b in list_children(macro_toggle["id"], token)
    ).strip()
    events_text = "\n".join(
        block_to_text(b, token, 0)
        for b in list_children(events_toggle["id"], token)
    ).strip()

    print(f"  宏观日览  text: {len(macro_text):,} chars")
    print(f"  全球重点事件 text: {len(events_text):,} chars")

    if args.dry_run:
        print("\n=== DRY RUN — macro_text preview (first 500) ===")
        print(macro_text[:500] or "(empty)")
        print("\n=== DRY RUN — events_text preview (first 500) ===")
        print(events_text[:500] or "(empty)")
        return

    opener = make_opener(env)
    out_dir = DATA_DIR / ny_date
    out_dir.mkdir(parents=True, exist_ok=True)

    # Run macro FIRST so events ingest can dedup against it. Skip if user
    # passed --kind events and macro doesn't exist (warn but don't fail).
    if args.kind in ("both", "macro"):
        if not macro_text:
            print("  WARNING: 宏观日览 toggle is empty — skipping macro ingest")
        else:
            print("\n  >>> calling Claude (macro)")
            api = call_claude(env, "macro", macro_text, ny_date, opener)
            payload = extract_tool_payload(api, "macro")
            # Inject canonical date on each topic (defensive — same as ingestApi.js)
            topics = payload.get("topics") or []
            for t in topics:
                if isinstance(t, dict):
                    t["date"] = ny_date
            output = {
                "topics": topics,
                "_archive": {
                    "source": "notion",
                    "fetched_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                    "raw_text": macro_text,
                    "model": payload.get("_model"),
                    "usage": payload.get("_usage"),
                },
            }
            macro_path = out_dir / "macro.json"
            macro_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
            print(f"  wrote {macro_path.relative_to(REPO)} ({len(topics)} topic(s))")

    if args.kind in ("both", "events"):
        if not events_text:
            print("  WARNING: 全球重点事件 toggle is empty — skipping events ingest")
        else:
            print("\n  >>> calling Claude (events)")
            api = call_claude(env, "events", events_text, ny_date, opener)
            payload = extract_tool_payload(api, "events")
            events = payload.get("events") or []
            output = {
                "events": events,
                "_archive": {
                    "source": "notion",
                    "fetched_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                    "raw_text": events_text,
                    "model": payload.get("_model"),
                    "usage": payload.get("_usage"),
                },
            }
            events_path = out_dir / "events.json"
            events_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
            print(f"  wrote {events_path.relative_to(REPO)} ({len(events)} event(s))")

    print("\n  done.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + render text but don't call Claude or write files")
    parser.add_argument("--kind", choices=["both", "macro", "events"], default="both",
                        help="which side to ingest (default: both)")
    parser.add_argument("--date", default=None,
                        help="override NY trading date (YYYY-MM-DD); default = today NY")
    args = parser.parse_args()

    env = load_env()
    # Also export proxy env vars so urllib's default opener picks them up too
    # if our explicit opener path is missed somewhere.
    for k in ("HTTPS_PROXY", "HTTP_PROXY"):
        if env.get(k):
            os.environ.setdefault(k, env[k])
    run_pipeline(args, env)


if __name__ == "__main__":
    main()
