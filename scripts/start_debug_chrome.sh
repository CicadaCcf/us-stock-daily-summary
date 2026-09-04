#!/bin/bash
# Launch (or relaunch) the dedicated "debug" Chrome that the daily scraper jobs
# drive over CDP — finviz_screenshot.py, alphapai_to_notion.py, daily_events_pull.sh
# (and the planned Slack macro). They all connect to Chrome on localhost:9222.
#
# WHY THIS EXISTS
#   Chrome auto-updates silently restart that instance — and it has come back
#   EMPTY, LOGGED OUT, and with NO visible window (--no-startup-window), which
#   breaks every scraper job with no warning until you notice stale data.
#   This script re-establishes it the RIGHT way:
#     • a VISIBLE window            → so you can actually log in
#     • the PERSISTENT ChromeDebug profile → logins survive future updates
#     • the key pages pre-opened    → alphapai / finviz / slack, ready for login
#   It only ever touches the ChromeDebug instance — your MAIN Chrome (default
#   profile, no --user-data-dir) is never affected.
#
# USAGE
#   bash scripts/start_debug_chrome.sh
#   Then log into any tab showing a login screen (SMS code, etc.) and KEEP the
#   window open. Re-run this after any Chrome update that empties :9222.

set -uo pipefail

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE="$HOME/ChromeDebug"
PORT=9222

if [ ! -x "$CHROME" ]; then
  echo "ERROR: Chrome not found at: $CHROME" >&2
  exit 1
fi

# Pages to pre-open so you can log in once. Edit as your sources change.
URLS=(
  "https://alphapai-web.rabyte.cn/reading/home/my-focus"
  "https://finviz.com/bubbles?x=sector&y=lastChange&size=marketCap&color=sector&idx=any&cap=midover"
  "https://app.slack.com/client"
)

# Quit any existing debug instance so we can relaunch it with a visible window.
# Matched by its ChromeDebug profile path — this can NOT match your main Chrome,
# which runs on the default profile (no --user-data-dir).
if pgrep -f "user-data-dir=$PROFILE" >/dev/null 2>&1; then
  echo "[info] quitting existing debug Chrome (ChromeDebug profile)…"
  pkill -f "user-data-dir=$PROFILE" 2>/dev/null || true
  sleep 2
fi

echo "[info] launching debug Chrome on :$PORT  (profile: $PROFILE, visible window)"
# No --no-startup-window on purpose — we want a window you can see and log into.
# Detach so this script returns while Chrome keeps running.
"$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  "${URLS[@]}" >/dev/null 2>&1 &

# Give it a moment, then confirm the CDP endpoint is live.
for i in 1 2 3 4 5 6 7 8; do
  sleep 1
  if curl -s "http://localhost:$PORT/json/version" >/dev/null 2>&1; then
    echo "[ok] debug Chrome is up on :$PORT."
    echo "     → Log into any tab showing a login page (alphapai / finviz / slack)."
    echo "     → Keep this window open; the daily jobs drive it over :$PORT."
    exit 0
  fi
done
echo "[warn] :$PORT not responding yet — check that a Chrome window opened, then re-run if needed."
