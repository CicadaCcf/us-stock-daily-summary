#!/bin/bash
# Daily Notion -> dashboard ingest wrapper.
#
# Runs at 08:10 BJT (5 minutes after the 08:05 alphapai -> Notion push), so
# the '全球重点事件' toggle is fresh and the user has had ~10 minutes since
# alphapai's 08:00-08:03 publish to paste anything they want into '宏观日览'.
#
# Reads both toggles, classifies via Anthropic API (same tools/prompts as the
# /api/ingest dev endpoint), writes:
#     src/data/{NY-date}/macro.json
#     src/data/{NY-date}/events.json
# Both files include an `_archive` field with the verbatim Notion text used.
#
# Properties:
#   - Idempotent: per-BJT-day marker file (.published_ingest_{BJT_DATE})
#   - Skips Sun/Mon BJT (no NY trading content overnight)
#   - DOES NOT need Chrome — talks to Notion + Anthropic over HTTPS only.
#     This decouples the ingest from the alphapai push (Chrome may be down
#     for unrelated reasons; ingest can still run if Notion was already
#     populated by an earlier alphapai run that day).
#
# Manual run:
#   bash scripts/daily_notion_ingest.sh
#   /usr/bin/python3 server/notion_to_dashboard.py --date 2026-04-27 --kind macro

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

BJT_DOW=$(TZ=Asia/Shanghai date +%u)
BJT_DATE=$(TZ=Asia/Shanghai date +%Y-%m-%d)
MARKER="data_archive/notion_ingest/.published_${BJT_DATE}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

log "=== daily_notion_ingest.sh start (BJT ${BJT_DATE} DOW=${BJT_DOW}) ==="

# 1. Skip Sun/Mon BJT — no NY trading content was generated overnight.
if [ "$BJT_DOW" -eq 7 ] || [ "$BJT_DOW" -eq 1 ]; then
  log "BJT DOW=${BJT_DOW} (Sun/Mon), skipping"
  exit 0
fi

# 2. Idempotent: per-BJT-day marker.
if [ -f "$MARKER" ]; then
  log "marker ${MARKER} already exists, skipping ingest"
  exit 0
fi

# 3. Run ingest.
log "running notion_to_dashboard.py"
if /usr/bin/python3 server/notion_to_dashboard.py; then
  mkdir -p "$(dirname "$MARKER")"
  touch "$MARKER"
  log "=== done (marker written) ==="
else
  log "ERROR: notion_to_dashboard.py failed; not writing marker"
  exit 1
fi
