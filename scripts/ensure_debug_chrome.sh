#!/bin/bash
# Watchdog: keep the dedicated "debug" Chrome on :9222 alive for the scraper
# jobs (finviz_screenshot.py, alphapai_to_notion.py, daily_events_pull.sh).
#
# WHY THIS EXISTS
#   :9222 keeps dying — Chrome auto-updates, a restart, or a power outage leaves
#   the process gone, and NOTHING brought it back until a human noticed stale
#   data and ran start_debug_chrome.sh by hand (hit ~Nx by 2026-10). This script
#   is run on a short interval by launchd (com.panoramichills.debug-chrome.plist)
#   so the instance self-heals within ~2 minutes of going down.
#
# IDEMPOTENT ON PURPOSE — this is NOT start_debug_chrome.sh:
#   • :9222 reachable  → NO-OP. We never kill a healthy, logged-in session.
#   • :9222 unreachable → relaunch with the PERSISTENT ChromeDebug profile (so
#     the saved alphapai/finviz/slack logins come straight back — cookies live
#     in the profile, not the window) and a VISIBLE window with the login pages
#     pre-opened, so if a login ever did expire you can fix it in place.
#   It only ever touches the ChromeDebug instance (matched by its user-data-dir);
#   your MAIN Chrome (default profile, no --user-data-dir) is never affected.
#
#   Limitation: this heals the "process is gone" case (by far the common one).
#   It deliberately does NOT try to detect an "up but logged-out" Chrome and
#   kill it — that risks nuking a healthy session on a false positive. The
#   scrapers' own freshness guards (e.g. _report_is_stale) stop a logged-out
#   run from writing stale data silently.
#
# Manual run is safe anytime:  bash scripts/ensure_debug_chrome.sh
# To force a clean relaunch WITH a kill (e.g. after a login expired), use the
# sibling script instead:      bash scripts/start_debug_chrome.sh

set -uo pipefail

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE="$HOME/ChromeDebug"
PORT=9222

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

# Fast path: already up → do nothing. Never disturb a working session.
if curl -sf --max-time 3 "http://localhost:$PORT/json/version" >/dev/null 2>&1; then
  exit 0
fi

log "ensure_debug_chrome: :$PORT not reachable — relaunching"

if [ ! -x "$CHROME" ]; then
  log "ERROR: Chrome not found at: $CHROME"
  exit 1
fi

# A stale/zombie ChromeDebug process can hold the profile lock without serving
# the CDP port. If the port is dead but a ChromeDebug process lingers, clear it
# first so the relaunch can take the profile. (Still scoped to ChromeDebug —
# can NOT match main Chrome, which has no --user-data-dir.)
if pgrep -f "user-data-dir=$PROFILE" >/dev/null 2>&1; then
  log "clearing a lingering ChromeDebug process that isn't serving :$PORT"
  pkill -f "user-data-dir=$PROFILE" 2>/dev/null || true
  sleep 2
fi

# Pages to pre-open so a login can be fixed in place if one ever expires.
URLS=(
  "https://alphapai-web.rabyte.cn/reading/home/my-focus"
  "https://finviz.com/bubbles?x=sector&y=lastChange&size=marketCap&color=sector&idx=any&cap=midover"
  "https://app.slack.com/client"
)

# No --no-startup-window: we want a visible window so a human can log in.
"$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  "${URLS[@]}" >/dev/null 2>&1 &

for i in $(seq 1 15); do
  sleep 1
  if curl -sf --max-time 2 "http://localhost:$PORT/json/version" >/dev/null 2>&1; then
    log "ensure_debug_chrome: :$PORT back up after ${i}s"
    exit 0
  fi
done

log "ERROR: :$PORT did not come up within 15s"
exit 1
