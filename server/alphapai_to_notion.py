#!/usr/bin/env python3
"""Daily 08:01 Beijing job: fetch alphapai 全球版 (蓝宝书) and push parsed
events to Notion under the correct date toggle's '全球重点事件' sub-toggle.

Auth: reuses the logged-in Chrome at localhost:9222 (same browser/profile
used for the daily Finviz screenshot). No tokens stored on disk for alphapai.

Notion: writes to the page configured by NOTION_PAGE_ID (env or constant
below). Date-toggle label uses NY trading date in YYYY/M/D format to match
the user's manual convention.

Usage:
    python3 server/alphapai_to_notion.py            # production push
    python3 server/alphapai_to_notion.py --dry-run  # parse + print, no Notion writes
"""
from __future__ import annotations

import argparse
import asyncio
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
ARCHIVE_DIR = REPO / "data_archive" / "alphapai"

NOTION_PAGE_ID = "350cd422-cbff-80b3-b74a-c7b81f80b9cc"  # "Daily Summary"
NOTION_VERSION = "2022-06-28"

ALPHAPAI_HOMEPAGE = "https://alphapai-web.rabyte.cn/reading/home/my-focus"
GLOBAL_CARD_SELECTOR = ".blue-book-card.global"


# ---------- env / notion HTTP ----------

def load_env() -> dict[str, str]:
    out = {}
    if not ENV_FILE.exists():
        raise RuntimeError(f"{ENV_FILE} missing")
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


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
        body_text = e.read().decode()[:500]
        print(f"  Notion {method} {path} -> {e.code}: {body_text}", file=sys.stderr)
        raise


# ---------- date helpers ----------

def ny_trading_date_iso() -> str:
    """ISO date in NY tz. When called at Beijing 08:01 this returns yesterday's
    NY date (which is what alphapai's overnight global edition is about).
    Used as a fallback only — prefer ny_date_from_report() which derives
    the trading date from the report's own publish timestamp."""
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def ny_date_from_report(api: dict) -> str:
    """Convert alphapai's BJT updateTime to NY tz to recover the trading day
    the overnight report is about. This is robust to running off-schedule
    (i.e. ad-hoc testing during BJT daytime).

    Example: updateTime '2026-04-28 08:03:56' BJT -> '2026-04-27' NY.
    """
    update_str = api["data"].get("updateTime") or ""
    try:
        bjt = datetime.strptime(update_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai"))
        return bjt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except ValueError:
        # malformed updateTime, fall back to current NY date
        return ny_trading_date_iso()


def fmt_date_label(iso: str) -> str:
    """2026-04-27 -> 2026/4/27 to match user's existing toggle convention."""
    y, m, d = iso.split("-")
    return f"{y}/{int(m)}/{int(d)}"


# ---------- alphapai scrape ----------

# alphapai 的首页会冒出各种 Element-UI 风格的 modal：
#   - 直播提醒（class="live-meeting-modal"）
#   - PPT 引导（class="ppt-guide-dialog__slide-grid" / "el-dialog__wrapper"）
#   - 偶尔的活动公告
# 这些 modal 的 mask 会拦截下面卡片的 click，让 Playwright 一直在
# "subtree intercepts pointer events" → "retrying click action" 里转圈，
# 最终 10s 超时，整个 job 挂掉。
#
# 策略：点击目标卡片之前先按 ESC + 主动找 .el-dialog__close /
# .close / [aria-label="Close"] 按一遍。modal 关掉后再点。
# 重试 5 次，每次 300ms，无论如何都不抛错（关 modal 失败也不该挡住主流程
# 的正常报错）。Per user 2026-06-18: "以后每天打开alphapai获取全球重点
# 事件之后记得自动关掉，不然会在浏览器里越堆越多"。
MODAL_CLOSE_SELECTORS = [
    ".el-dialog__close",
    ".live-meeting-modal__close",
    ".modal-close",
    "[aria-label='Close']",
    "[aria-label='关闭']",
]


async def dismiss_blocking_modals(page, attempts: int = 5) -> int:
    """Try to close any blocking modal/dialog on the page. Returns the number
    of close actions taken (best-effort, never raises)."""
    closed = 0
    for _ in range(attempts):
        any_visible = False
        # ESC first — handles many Element-UI dialogs without us needing to
        # find the specific close button.
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass
        # Then try each known close-button selector.
        for sel in MODAL_CLOSE_SELECTORS:
            try:
                els = await page.locator(sel).all()
            except Exception:
                continue
            for el in els:
                try:
                    if await el.is_visible():
                        any_visible = True
                        await el.click(timeout=1000)
                        closed += 1
                        await asyncio.sleep(0.2)
                except Exception:
                    pass
        if not any_visible:
            break
    return closed


async def fetch_global_report() -> dict:
    """Returns the parsed JSON body of GET /report/detail/v2 for the global
    edition (clicked from the 蓝宝书 homepage card).

    On exit: closes EVERY page we may have spawned in the connected Chrome
    (the homepage tab plus any detail tab the click action opened). Without
    this, daily runs leave alphapai tabs piling up across the user's Chrome
    session.
    """
    from playwright.async_api import async_playwright

    captured: dict = {}

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]

        # Snapshot the page set BEFORE we open anything, so the cleanup step
        # only closes pages we (or alphapai's click handler) opened during
        # this run — never a user's pre-existing tab.
        preexisting = set(ctx.pages)

        async def on_response(resp):
            if "report/detail/v2" in resp.url and "isUs=true" in resp.url:
                try:
                    captured["body"] = await resp.text()
                    captured["url"] = resp.url
                except Exception as e:
                    captured["err"] = str(e)

        ctx.on("response", lambda r: asyncio.create_task(on_response(r)))

        page = await ctx.new_page()
        try:
            await page.goto(ALPHAPAI_HOMEPAGE, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            # Dismiss any modal that's covering the homepage before we click.
            closed = await dismiss_blocking_modals(page)
            if closed:
                print(f"  dismissed {closed} blocking modal(s) before card click")
            await page.click(GLOBAL_CARD_SELECTOR, timeout=10000)
            for _ in range(30):
                if "body" in captured:
                    break
                await asyncio.sleep(0.5)
        finally:
            # Close every page WE spawned (homepage tab + the detail tab the
            # card click may have opened). Pages the user had open before our
            # run are untouched.
            new_pages = [pg for pg in ctx.pages if pg not in preexisting]
            for pg in new_pages:
                try:
                    if not pg.is_closed():
                        await pg.close()
                except Exception:
                    pass
            print(f"  closed {len(new_pages)} alphapai tab(s) after scrape")
            # We're connected over CDP — don't actually shut down the user's
            # Chrome, just disconnect.
            try:
                await browser.close()
            except Exception:
                pass

    if "body" not in captured:
        raise RuntimeError(f"failed to capture detail/v2 (err={captured.get('err')})")
    api = json.loads(captured["body"])
    if api.get("code") != 200000:
        raise RuntimeError(f"alphapai returned non-200: {api.get('code')} {api.get('message')}")
    return api


# ---------- parse ----------

TICKER_RE = re.compile(r"\*\*([^*()]+?)\(([A-Z0-9.\-]{1,8})\)\*\*\s*(?:\(([^)]+)\))?")


def parse_event(child: dict) -> dict:
    """alphapai child -> {topicName, body, tickers, raw_summary}.

    summary contains a `<br><br>美股关注：...` tail listing 2-4 tickers; we
    split that off, dewrap markdown bolds in the body, and parse each ticker.
    """
    summary: str = child["summary"]
    parts = re.split(r"<br><br>美股关注[：:]\s*", summary, maxsplit=1)
    body = parts[0].replace("<br>", "\n").strip()
    tickers_blob = parts[1] if len(parts) > 1 else ""

    tickers = []
    for m in TICKER_RE.finditer(tickers_blob):
        tickers.append({
            "name": m.group(1).strip(),
            "symbol": m.group(2),
            "note": (m.group(3) or "").strip(),
        })

    return {
        "id": child.get("id"),
        "topicName": child["topicName"],
        "body": body,
        "tickers": tickers,
        "raw_summary": summary,
    }


def extract_global_events(api: dict) -> tuple[str, list[dict]]:
    """Returns (alphapai_publish_date, list_of_events)."""
    data = api["data"]
    sections = data["contentJson"]
    section = next((s for s in sections if s["title"] == "全球重点事件梳理"), None)
    if section is None:
        titles = [s["title"] for s in sections]
        raise RuntimeError(f"'全球重点事件梳理' section not found. saw: {titles}")
    return data.get("date", ""), [parse_event(c) for c in section["children"]]


# ---------- markdown -> notion rich_text ----------

BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def md_to_rich_text(text: str) -> list[dict]:
    """Convert a single-line string with **bold** runs to Notion rich_text.
    Caller should split multi-line text on \\n into separate paragraph blocks."""
    out = []
    pos = 0
    for m in BOLD_RE.finditer(text):
        if m.start() > pos:
            out.append({"type": "text", "text": {"content": text[pos:m.start()]}})
        out.append({
            "type": "text",
            "text": {"content": m.group(1)},
            "annotations": {"bold": True},
        })
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": {"content": text[pos:]}})
    return out or [{"type": "text", "text": {"content": text}}]


def event_to_blocks(evt: dict) -> list[dict]:
    """Layout per event:
        heading_3: topicName  (bold-ish heading)
        paragraph(s): body lines, with bold markdown preserved
        bulleted_list_item per ticker: <code>SYM</code> Name — note
    """
    blocks = []
    blocks.append({
        "object": "block",
        "type": "heading_3",
        "heading_3": {
            "rich_text": [{"type": "text", "text": {"content": evt["topicName"]}}],
        },
    })
    for line in evt["body"].split("\n"):
        line = line.strip()
        if not line:
            continue
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": md_to_rich_text(line)},
        })
    for t in evt["tickers"]:
        runs: list[dict] = [
            {"type": "text", "text": {"content": t["symbol"]},
             "annotations": {"bold": True, "code": True}},
            {"type": "text", "text": {"content": f"  {t['name']}"}},
        ]
        if t["note"]:
            runs.append({"type": "text", "text": {"content": f" — {t['note']}"}})
        blocks.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": runs},
        })
    return blocks


# ---------- notion tree ops ----------

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


def find_or_create_date_toggle(page_id: str, date_iso: str, token: str) -> str:
    label = fmt_date_label(date_iso)
    for b in list_children(page_id, token):
        if b.get("type") != "toggle":
            continue
        text = text_of(b)
        if text in (label, date_iso):
            return b["id"]
    # not found -> create with the two standard sub-toggles
    print(f"  date toggle '{label}' not found; creating", file=sys.stderr)
    res = notion_req("PATCH", f"/blocks/{page_id}/children", token, {
        "children": [{
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": label}}],
                "children": [
                    {"object": "block", "type": "toggle", "toggle": {
                        "rich_text": [{"type": "text", "text": {"content": "宏观日览"}}]}},
                    {"object": "block", "type": "toggle", "toggle": {
                        "rich_text": [{"type": "text", "text": {"content": "全球重点事件"}}]}},
                ],
            },
        }],
    })
    return res["results"][0]["id"]


def find_or_create_subtoggle(parent_id: str, name: str, token: str) -> str:
    for b in list_children(parent_id, token):
        if b.get("type") == "toggle" and name in text_of(b):
            return b["id"]
    print(f"  sub-toggle '{name}' not found; creating", file=sys.stderr)
    res = notion_req("PATCH", f"/blocks/{parent_id}/children", token, {
        "children": [{
            "object": "block",
            "type": "toggle",
            "toggle": {"rich_text": [{"type": "text", "text": {"content": name}}]},
        }],
    })
    return res["results"][0]["id"]


def clear_block_children(block_id: str, token: str) -> int:
    children = list_children(block_id, token)
    for b in children:
        notion_req("DELETE", f"/blocks/{b['id']}", token)
    return len(children)


def append_blocks(parent_id: str, blocks: list[dict], token: str) -> None:
    for i in range(0, len(blocks), 100):
        chunk = blocks[i : i + 100]
        notion_req("PATCH", f"/blocks/{parent_id}/children", token, {"children": chunk})


# ---------- main ----------

async def main_async(args):
    # Per user 2026-06-18: when alphapai 全球版 hasn't been published yet
    # (early-morning run, or alphapai outage), still create the empty Notion
    # framework for today (日期 toggle + 宏观日览 + 全球重点事件 sub-toggles)
    # so the user can manually fill macro content without first having to
    # build the toggle tree by hand.
    api = None
    events: list[dict] = []
    fetch_err: str | None = None
    try:
        api = await fetch_global_report()
        _publish_date, events = extract_global_events(api)
        if not events:
            fetch_err = "zero events parsed (alphapai returned no '全球重点事件梳理' section)"
    except Exception as e:
        fetch_err = f"{type(e).__name__}: {e}"
        print(f"  [warn] alphapai fetch failed: {fetch_err}")

    # NY date: prefer the alphapai report's own updateTime when available
    # (handles off-schedule runs correctly); else fall back to "today NY".
    ny_date = ny_date_from_report(api) if api else ny_trading_date_iso()

    print(f"  NY trading date:       {ny_date}")
    print(f"  parsed events:         {len(events)}")

    # archive raw response when we did fetch one
    if api is not None:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archive_path = ARCHIVE_DIR / f"{ny_date}.json"
        archive_path.write_text(json.dumps(api, ensure_ascii=False, indent=2))
        print(f"  archived raw -> {archive_path.relative_to(REPO)}")

    blocks: list[dict] = []
    for evt in events:
        blocks.extend(event_to_blocks(evt))
    if events:
        print(f"  rendered {len(blocks)} Notion blocks")

    if args.dry_run:
        if events:
            print("\n=== DRY RUN — first 6 blocks preview ===")
            for b in blocks[:6]:
                t = b["type"]
                txt = "".join(r.get("text", {}).get("content", "") for r in b[t]["rich_text"])
                print(f"  [{t}] {txt[:90]!r}")
        else:
            print(f"  [dry-run] would create skeleton only (no events). reason: {fetch_err}")
        return

    env = load_env()
    token = env.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN missing from .env.local")

    page_id = args.page_id or NOTION_PAGE_ID
    # find_or_create_date_toggle creates the date toggle with BOTH the 宏观
    # 日览 and 全球重点事件 sub-toggles when missing — i.e. the empty
    # framework the user wants for early-morning skeleton fallback. Calling
    # it always (whether or not we have events) means the user gets the same
    # tree regardless of alphapai availability.
    date_toggle = find_or_create_date_toggle(page_id, ny_date, token)
    print(f"  date toggle id:    {date_toggle}")
    sub_id = find_or_create_subtoggle(date_toggle, "全球重点事件", token)
    print(f"  全球重点事件 id:    {sub_id}")
    # Always ensure 宏观日览 exists too — historically find_or_create_date_toggle
    # creates it on FIRST run, but if the date toggle existed already (e.g. user
    # pre-created it without 宏观日览), this is a safe idempotent top-up.
    macro_sub = find_or_create_subtoggle(date_toggle, "宏观日览", token)
    print(f"  宏观日览 id:       {macro_sub}")

    if not events:
        # Skeleton mode: framework's there, nothing else to write. Exit 0 so
        # launchd treats it as success — the day's marker WILL be touched and
        # tomorrow morning's run won't re-attempt. If you want the wrapper to
        # NOT mark the day done in this case (so a later retry can fill the
        # 全球重点事件 section once alphapai catches up), pass --skeleton-no-marker
        # in the wrapper and have the wrapper skip touching the marker on
        # exit code 2.
        print(f"  [skeleton] alphapai content unavailable ({fetch_err}) — "
              f"created empty framework only; user can fill manually")
        if args.skeleton_no_marker:
            sys.exit(2)  # signal to wrapper: skeleton-only, do not mark day done
        return

    # Full content path: clear + re-append events under 全球重点事件.
    cleared = clear_block_children(sub_id, token)
    print(f"  cleared {cleared} existing block(s)")
    append_blocks(sub_id, blocks, token)
    print(f"  appended {len(blocks)} new block(s)")
    print("  done.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="parse + render but don't touch Notion")
    parser.add_argument("--page-id", default=None,
                        help=f"override Notion page id (default: {NOTION_PAGE_ID})")
    parser.add_argument("--skeleton-no-marker", action="store_true",
                        help="exit 2 (instead of 0) when skeleton-only — signals the "
                             "wrapper to NOT touch the per-day marker, so a later run "
                             "can still attempt to fetch alphapai content once it "
                             "publishes")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
