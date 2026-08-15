#!/bin/bash
# Daily EVENTS pull: Notion 全球重点事件 -> src/data/{NY-date}/events.json
#
# Replaces the manual "copy from Notion -> Events tab -> Classify -> Save" and
# also removes the dependency on clicking Admin -> Update: this runs on its own
# schedule so events land automatically each morning.
#
# Scheduled (com.panoramichills.events-pull.plist) at BOTH 08:05 and 08:10 BJT,
# Tue-Sat. alphapai_to_notion.py fires at 08:05 too, so the 08:05 pull often
# races it (toggle not ready -> notion_to_dashboard.py exits non-zero -> NO
# marker written -> the 08:10 pull retries and catches it). Once a pull
# succeeds the marker makes the other fire a no-op.
#
# Macro is intentionally NOT pulled here (still manual via the Macro tab) per
# user 2026-08-14. Add `--kind` handling only if that changes.
#
# Does NOT git push — it prepares events.json locally, exactly like
# daily_update.sh prepares market data locally; your Publish carries both to
# prod together.
#
# Manual run:
#   bash scripts/daily_events_pull.sh
#   bash scripts/daily_events_pull.sh --force   # ignore today's marker
#
# Logs: ~/Library/Logs/events-pull.{out,err}.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

BJT_DOW=$(TZ=Asia/Shanghai date +%u)
BJT_DATE=$(TZ=Asia/Shanghai date +%Y-%m-%d)
MARKER="data_archive/events_pull/.published_${BJT_DATE}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

log "=== daily_events_pull.sh start (BJT ${BJT_DATE} DOW=${BJT_DOW}) ==="

# Skip Sun/Mon BJT — no NY trading content generated overnight.
if [ "$BJT_DOW" -eq 7 ] || [ "$BJT_DOW" -eq 1 ]; then
  log "BJT DOW=${BJT_DOW} (Sun/Mon), skipping"
  exit 0
fi

# Idempotent: once a pull succeeds today, the other scheduled fire no-ops.
if [ "$FORCE" -eq 0 ] && [ -f "$MARKER" ]; then
  log "marker ${MARKER} exists — events already pulled today, skipping"
  exit 0
fi

# Pull events. notion_to_dashboard.py exits non-zero if the toggle isn't ready
# yet (alphapai still publishing) or is empty — in which case we DON'T write
# the marker, so the next scheduled fire retries.
log "running notion_to_dashboard.py --kind events"
if /usr/bin/python3 -u server/notion_to_dashboard.py --kind events; then
  mkdir -p "$(dirname "$MARKER")"
  touch "$MARKER"
  log "=== done (events.json pulled, marker written) ==="
else
  log "events pull failed/not-ready (exit $?); NO marker — next fire (08:10) will retry"
  exit 1
fi
