#!/usr/bin/env python3
"""
Finviz bubble-map screenshot job.

Connects to an already-running Chrome (with --remote-debugging-port) and
reuses its logged-in session, navigates to finviz.com/bubbles.ashx, waits
for the canvas to finish rendering, then screenshots to:

    public/finviz_bubble.png

The frontend displays this file if present (see FinvizBubble in App.jsx).

----
How to start your logged-in Chrome once (run in a Terminal):

    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
      --remote-debugging-port=9222 \\
      --user-data-dir=$HOME/ChromeDebug

On first run Chrome will be empty — log into finviz.com (and anything else
you want). On subsequent runs it reuses that profile. Leave Chrome running
when you run this script.

----
Dependencies:

    pip install playwright
    (no `playwright install chromium` needed — we reuse your Chrome.)
"""

import os
import shutil
import sys
import time
import argparse
import asyncio
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9 fallback (we require 3.11+ but be defensive)
    from backports.zoneinfo import ZoneInfo  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / 'public'
# Per-date archive — never overwritten so past daily reports keep their
# original bubble map. New each trading day. Folder is created on first run.
ARCHIVE_DIR = PUBLIC_DIR / 'archive' / 'finviz'

# Default Finviz URL — user can override with --url.
# Per user 2026-05-26: lock filters into the URL so the daily screenshot is
# stable and doesn't drift with whatever the logged-in session last looked at.
#   x=sector, y=lastChange, size=marketCap, color=sector  (axes)
#   idx=any                                               (no index filter)
#   cap=midover                                           ("+Mid (over $2bn)" =
#                                                          Mid + Large + Mega)
DEFAULT_URL = ('https://finviz.com/bubbles'
               '?x=sector&y=lastChange&size=marketCap&color=sector'
               '&idx=any&cap=midover')
# Default CDP endpoint matches the Chrome launch arg above
DEFAULT_CDP = 'http://localhost:9222'
# Output path relative to project root (latest, always overwritten)
DEFAULT_OUT = 'public/finviz_bubble.png'


def trading_date_ny() -> str:
    """Today's date in America/New_York. Caller stamps this onto the archive
    filename so each daily run preserves a permanent copy under
    public/archive/finviz/{YYYY-MM-DD}.png. Weekends still get stamped — if
    you accidentally run the script on a Saturday it'll overwrite Friday's
    archive only if you also pass --date Friday's-iso, otherwise it lands in
    a Saturday-named file you can delete."""
    return datetime.now(ZoneInfo('America/New_York')).date().isoformat()


async def run(cdp_url: str, target_url: str, out_path: Path,
              wait_seconds: float, viewport: tuple, crop: bool,
              archive_path=None):  # archive_path: Path | None — written as untyped
                                    # to keep py3.9 compat (PEP 604 needs 3.10+)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('ERROR: `pip install playwright` required (no browser download needed for CDP).', file=sys.stderr)
        sys.exit(2)

    async with async_playwright() as p:
        print(f'[info] connecting to Chrome at {cdp_url}')
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            print(f'ERROR: cannot connect to Chrome at {cdp_url}: {e}', file=sys.stderr)
            print('  Make sure Chrome is running with: '
                  '--remote-debugging-port=9222 --user-data-dir=$HOME/ChromeDebug',
                  file=sys.stderr)
            sys.exit(3)

        # Use the existing default context (has your cookies / logged-in session)
        contexts = browser.contexts
        if not contexts:
            print('ERROR: Chrome has no open contexts', file=sys.stderr)
            sys.exit(4)
        ctx = contexts[0]

        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': viewport[0], 'height': viewport[1]})
            print(f'[info] navigating to {target_url}')
            await page.goto(target_url, wait_until='domcontentloaded', timeout=30000)

            # Wait for the bubble-map canvas to finish rendering. Finviz renders
            # via JS into <canvas> — we can poll for a canvas with non-trivial
            # pixel content.
            print(f'[info] waiting {wait_seconds}s for canvas render')
            await asyncio.sleep(wait_seconds)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            # If the user wants just the map, try to locate the canvas and
            # screenshot that bounding box only.
            if crop:
                canvas = await page.query_selector('canvas')
                if canvas:
                    print('[info] screenshotting canvas only')
                    await canvas.screenshot(path=str(out_path))
                else:
                    print('[warn] canvas not found; full-page screenshot fallback')
                    await page.screenshot(path=str(out_path), full_page=False)
            else:
                await page.screenshot(path=str(out_path), full_page=False)

            size = out_path.stat().st_size
            print(f'[info] wrote {out_path} ({size:,} bytes)')

            # Mirror to per-date archive so historical daily reports keep their
            # original bubble map even after future runs overwrite the live PNG.
            if archive_path is not None:
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(out_path, archive_path)
                print(f'[info] archived to {archive_path}')
        finally:
            await page.close()
            # Don't close the browser — it's the user's Chrome.
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description='Screenshot Finviz bubble map via logged-in Chrome')
    parser.add_argument('--cdp', default=DEFAULT_CDP, help='Chrome DevTools Protocol URL')
    parser.add_argument('--url', default=DEFAULT_URL, help='Page to screenshot')
    parser.add_argument('--out', default=DEFAULT_OUT, help='Output path (relative to repo root)')
    parser.add_argument('--wait', type=float, default=5.0, help='Seconds to wait for canvas render')
    parser.add_argument('--width', type=int, default=1600, help='Viewport width')
    parser.add_argument('--height', type=int, default=1000, help='Viewport height')
    parser.add_argument('--full-page', action='store_true', help='Screenshot full page instead of just canvas')
    parser.add_argument('--date', default=None,
                        help='Trading date for archive filename (YYYY-MM-DD). '
                             'Default: today in America/New_York. Pass --no-archive to skip.')
    parser.add_argument('--no-archive', action='store_true',
                        help='Skip writing the per-date archive copy')
    args = parser.parse_args()

    out_path = ROOT / args.out if not os.path.isabs(args.out) else Path(args.out)

    archive_path = None
    if not args.no_archive:
        date_str = args.date or trading_date_ny()
        archive_path = ARCHIVE_DIR / f'{date_str}.png'

    asyncio.run(run(
        cdp_url=args.cdp,
        target_url=args.url,
        out_path=out_path,
        wait_seconds=args.wait,
        viewport=(args.width, args.height),
        crop=not args.full_page,
        archive_path=archive_path,
    ))


if __name__ == '__main__':
    main()
