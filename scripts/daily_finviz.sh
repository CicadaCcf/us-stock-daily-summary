#!/bin/bash
# Daily Finviz bubble-map screenshot wrapper.
#
# Designed to be invoked by launchd via:
#   ~/Library/LaunchAgents/com.panoramichills.finviz-screenshot.plist
#
# Properties:
#   - Idempotent: skips if public/archive/finviz/{NY-date}.png already exists
#   - Time-gated: only runs after US market close (>= 16:00 ET) on weekdays
#   - Self-healing: auto-starts the dedicated Chrome (port 9222) if not running
#   - Path-independent: locates its own repo via $(dirname), so the plist
#     can keep working if you move/symlink this file elsewhere
#   - Logged: every step prints a timestamped line; launchd captures stdout
#     to ~/Library/Logs/finviz-screenshot.out.log
#
# Manual run (any time):
#   bash scripts/daily_finviz.sh
# Manual backfill of a past date (Finviz must still be showing it intraday):
#   python3 server/finviz_screenshot.py --date 2026-04-25

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

NY_DATE=$(TZ=America/New_York date +%Y-%m-%d)
NY_DOW=$(TZ=America/New_York date +%u)   # 1=Mon, 7=Sun
NY_HOUR=$(TZ=America/New_York date +%H)

ARCHIVE="public/archive/finviz/${NY_DATE}.png"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

log "=== daily_finviz.sh start (NY ${NY_DATE} ${NY_HOUR}h DOW=${NY_DOW}) ==="

# 1. Skip on weekends — US markets closed, no fresh bubble map
if [ "$NY_DOW" -gt 5 ]; then
  log "weekend (NY DOW=$NY_DOW), skipping"
  exit 0
fi

# 2. Idempotent: skip if today's archive already exists
if [ -f "$ARCHIVE" ]; then
  size=$(stat -f%z "$ARCHIVE")
  log "archive ${NY_DATE} already exists (${size} bytes), skipping"
  exit 0
fi

# 3. Time-gated: skip if market still open or pre-market.
#    The launchd job fires at 09:30 Beijing (= 21:30 EDT or 20:30 EST yesterday)
#    so this should never trigger from the scheduled run; it only matters when
#    RunAtLoad fires after a boot at the wrong time of day.
if [ "$NY_HOUR" -lt 16 ]; then
  log "NY hour=$NY_HOUR < 16:00, market still open or pre-market, skipping"
  exit 0
fi

# 4. Ensure dedicated Chrome is running on port 9222.
#    The dedicated profile lives at ~/ChromeDebug — its cookies preserve the
#    Finviz login, so we MUST use that user-data-dir (not the user's normal
#    Chrome profile) every time.
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_DATA_DIR="$HOME/ChromeDebug"

if ! curl -sf --max-time 3 http://localhost:9222/json/version >/dev/null 2>&1; then
  log "starting dedicated Chrome on :9222 (profile=$USER_DATA_DIR)"
  if [ ! -x "$CHROME_BIN" ]; then
    log "ERROR: Chrome not found at $CHROME_BIN"
    exit 1
  fi
  # Launch directly (not via `open`) so we don't conflict with a regular
  # Chrome instance on a different profile. `&` + redirect detaches it from
  # this shell so the script can continue.
  "$CHROME_BIN" \
    --remote-debugging-port=9222 \
    --user-data-dir="$USER_DATA_DIR" \
    >/dev/null 2>&1 &

  # Wait up to 30s for CDP to come up
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

# 5. Run screenshot. Pin --date to the NY trading date so the archive name
#    matches no matter what time of day this fires.
log "running screenshot for ${NY_DATE}"
/usr/bin/python3 server/finviz_screenshot.py --date "$NY_DATE"
log "=== done ==="
