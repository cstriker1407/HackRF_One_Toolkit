#!/bin/bash
# Launch-or-open: if the web console isn't running, start it; then open the browser.
# Used by the desktop shortcut (HackRF-Toolkit.desktop).
PROJ="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:30000"

if ! curl -s --max-time 3 "$URL/api/health" >/dev/null 2>&1; then
    # not running → start it, then wait (max ~12s) for it to come up
    "$PROJ/start.sh" >/dev/null 2>&1
    for _ in $(seq 1 24); do
        curl -s --max-time 2 "$URL/api/health" >/dev/null 2>&1 && break
        sleep 0.5
    done
fi

# open in the default browser (non-blocking)
( xdg-open "$URL" >/dev/null 2>&1 || sensible-browser "$URL" >/dev/null 2>&1 || firefox "$URL" >/dev/null 2>&1 ) &
