#!/bin/bash
# Daily Update wrapper — replaces the manual Admin → Publish → Update click.
#
# Runs the same three Python steps as POST /api/update in vite-plugins/ingestApi.js:
#   1. polygon_snapshot.py  (mandatory — market.json + screener.json + breadth.json)
#   2. finviz_screenshot.py (best-effort — public/finviz_bubble.png + archive)
#   3. movers_news.py       (best-effort — src/data/{NY}/movers_news.json)
#   4. movers_autofill.py   (best-effort — Supabase industry/reason suggestions)
#
# Schedule (via com.panoramichills.daily-update.plist):
#   Tue-Sat 05:30 BJT — 30 min after US close.
#     EDT (Mar-Nov): close 16:00 ET = 04:00 BJT → +30min run @ 04:30 → we run @ 05:30 (60min late, fine)
#     EST (Nov-Mar): close 16:00 ET = 05:00 BJT → +30min run @ 05:30 (exact)
#   05:30 BJT works for both DST states; max delay vs "+30min" is 60 min in EDT.
#
# Idempotency: per-NY-date marker `data_archive/daily_update/.published_{NY-date}`.
# A successful polygon_snapshot.py touches it. If polygon fails the marker is
# NOT written and the next launchd fire / manual re-run will retry.
#
# Per user 2026-06-18: "我们的local host部分可以每天美股收盘后30min自动
# 更新update吗？现在我需要每次都自己点开admin的publish然后再点更新update"
#
# Manual run:
#   bash scripts/daily_update.sh
#   bash scripts/daily_update.sh --force   # re-run even if marker exists
#   bash scripts/daily_update.sh --date 2026-05-09  # specific NY date

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

FORCE=0
TARGET_DATE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force)    FORCE=1; shift;;
    --date)     TARGET_DATE="$2"; shift 2;;
    *)          echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# NY date = today's NY trading day if not overridden.
if [ -z "$TARGET_DATE" ]; then
  NY_DATE=$(TZ=America/New_York date +%Y-%m-%d)
else
  NY_DATE="$TARGET_DATE"
fi
MARKER="data_archive/daily_update/.published_${NY_DATE}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

log "=== daily_update.sh start (NY ${NY_DATE}) ==="

if [ "$FORCE" -eq 0 ] && [ -f "$MARKER" ]; then
  log "marker ${MARKER} already exists, skipping (use --force to re-run)"
  exit 0
fi

# --- Step 1: polygon snapshot (mandatory) ---
# This script already has retry/backoff for transient VPN flakes (added
# 2026-05-09). If it still fails, we abort without marking.
log "step 1/3: polygon + yahoo + CNN PCR snapshot"
if [ -n "$TARGET_DATE" ]; then
  /usr/bin/python3 -u server/polygon_snapshot.py --date "$TARGET_DATE"
else
  /usr/bin/python3 -u server/polygon_snapshot.py
fi

# --- Step 2: finviz screenshot (best-effort) ---
# Matches /api/update behavior: failure here keeps the previous PNG, not
# fatal to the overall daily run. Needs Chrome on :9222.
log "step 2/3: Finviz bubble screenshot"
if ! /usr/bin/python3 -u server/finviz_screenshot.py; then
  log "[warn] Finviz step failed; keeping previous public/finviz_bubble.png"
fi

# --- Step 3: movers news (best-effort) ---
log "step 3/4: Polygon news for Top Movers"
if [ -n "$TARGET_DATE" ]; then
  if ! /usr/bin/python3 -u server/movers_news.py --date "$TARGET_DATE"; then
    log "[warn] movers_news failed; keeping previous movers_news.json"
  fi
else
  if ! /usr/bin/python3 -u server/movers_news.py; then
    log "[warn] movers_news failed; keeping previous movers_news.json"
  fi
fi

# --- Step 4: auto-fill industry/reason for newly-highlighted movers (best-effort) ---
# Mirrors /api/update step 4. Claude drafts catalyst tag + reason (earnings
# numbers via Longbridge) for highlighted-new tickers with no carried-forward
# tag, writes them as suggestions into Supabase, and sets days_remaining=1 for
# one-shot catalysts. Non-fatal — cells just stay blank for manual entry.
log "step 4/4: auto-fill industry/reason for highlighted movers"
if [ -n "$TARGET_DATE" ]; then
  if ! /usr/bin/python3 -u server/movers_autofill.py --date "$TARGET_DATE"; then
    log "[warn] movers_autofill failed; industry/reason left blank for manual entry"
  fi
else
  if ! /usr/bin/python3 -u server/movers_autofill.py; then
    log "[warn] movers_autofill failed; industry/reason left blank for manual entry"
  fi
fi

# All mandatory steps succeeded — mark the day done.
mkdir -p "$(dirname "$MARKER")"
touch "$MARKER"
log "=== done (marker written: ${MARKER}) ==="
