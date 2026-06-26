#!/bin/bash
# Daily alphapai 全球版 -> Notion push wrapper.
#
# Invoked by launchd via:
#   ~/Library/LaunchAgents/com.panoramichills.alphapai-to-notion.plist
#
# Schedule: Tue-Sat at 08:01 Beijing (= the 4 minutes after alphapai's 08:00
# publish window for the global edition). On Sat we still run because Friday's
# NY trading produces a Saturday-morning publication; on Sun/Mon we skip
# (no new NY trading content overnight).
#
# Properties:
#   - Idempotent: skips if data_archive/alphapai/.published_{BJT-date} marker
#     exists (one publish per BJT calendar day, matching alphapai's cadence).
#     Use a BJT-date marker rather than the NY-date archive name because
#     python derives NY date from alphapai's updateTime (which can disagree
#     with `date now` if the wrapper runs off-schedule).
#   - Self-healing: starts the dedicated Chrome on :9222 if not running.
#     This shares the same user-data-dir as daily_finviz.sh — both rely on
#     the Notion + alphapai cookies stored in ~/ChromeDebug.
#   - Logs to ~/Library/Logs/alphapai-to-notion.out.log via launchd
#
# Manual run:
#   bash scripts/daily_alphapai.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

# Beijing date is what we use to decide "is this a publish day" — alphapai
# publishes the global edition Tue-Sat BJT.
BJT_DOW=$(TZ=Asia/Shanghai date +%u)   # 1=Mon, 7=Sun
BJT_DATE=$(TZ=Asia/Shanghai date +%Y-%m-%d)

# Per-BJT-day marker (idempotency). The wrapper used to look at the
# {NY_DATE}.json archive directly, but python derives NY date from the
# alphapai report's updateTime (more accurate than `date now` at off-schedule
# times). Using a BJT-date marker decouples the two and matches alphapai's
# publish cadence: ~once per BJT calendar day.
MARKER="data_archive/alphapai/.published_${BJT_DATE}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

log "=== daily_alphapai.sh start (BJT ${BJT_DATE} DOW=${BJT_DOW}) ==="

# 1. Skip Sun (DOW=7) and Mon (DOW=1) BJT — alphapai global edition is only
#    published Tue-Sat (covering Mon-Fri NY trading). Running on those days
#    would either hit yesterday's stale report or fail to find a new card.
if [ "$BJT_DOW" -eq 7 ] || [ "$BJT_DOW" -eq 1 ]; then
  log "BJT DOW=${BJT_DOW} (Sun/Mon), no new global edition expected, skipping"
  exit 0
fi

# 2. Idempotent: skip if we've already pushed today's report.
if [ -f "$MARKER" ]; then
  log "marker ${MARKER} already exists, skipping push"
  exit 0
fi

# 3. Ensure dedicated Chrome is running on :9222 (same profile as Finviz).
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_DATA_DIR="$HOME/ChromeDebug"

if ! curl -sf --max-time 3 http://localhost:9222/json/version >/dev/null 2>&1; then
  log "starting dedicated Chrome on :9222 (profile=$USER_DATA_DIR)"
  if [ ! -x "$CHROME_BIN" ]; then
    log "ERROR: Chrome not found at $CHROME_BIN"
    exit 1
  fi
  "$CHROME_BIN" \
    --remote-debugging-port=9222 \
    --user-data-dir="$USER_DATA_DIR" \
    >/dev/null 2>&1 &

  for i in $(seq 1 30); do
    sleep 1
    if curl -sf --max-time 2 http://localhost:9222/json/version >/dev/null 2>&1; then
      log "Chrome ready after ${i}s"
      break
    fi
    if [ "$i" -eq 30 ]; then
      log "ERROR: Chrome did not respond on :9222 after 30s"
      exit 1
    fi
  done
else
  log "Chrome already on :9222, reusing"
fi

# 4. Run the push.
# Exit code semantics (the python script uses --skeleton-no-marker):
#   0 = full content pushed → mark day done
#   2 = skeleton-only (alphapai 全球版 not yet published, but Notion framework
#       was created) → DON'T mark, so a later run today can still attempt to
#       fetch real content if launchd re-fires (or user manually re-runs)
#   1/other = real failure → don't mark
log "running alphapai_to_notion.py"
set +e
/usr/bin/python3 server/alphapai_to_notion.py --skeleton-no-marker
RC=$?
set -e

case "$RC" in
  0)
    mkdir -p "$(dirname "$MARKER")"
    touch "$MARKER"
    log "=== done (full content pushed, marker written) ==="
    ;;
  2)
    log "=== done (skeleton-only — Notion framework created, NO marker so we retry later) ==="
    # exit 0 so launchd doesn't treat this as a hard failure
    exit 0
    ;;
  *)
    log "ERROR: alphapai_to_notion.py failed (rc=$RC); not writing marker"
    exit "$RC"
    ;;
esac
