#!/usr/bin/env bash
# Launch uxplay AirPlay receiver.
# iPhone: Control Center → Screen Mirroring → pick the advertised name.
set -euo pipefail

NAME="${ICONNECT_NAME:-iconnect}"
RES="${ICONNECT_RES:-1920x1080}"
FPS="${ICONNECT_FPS:-60}"

# -n  advertised name
# -nh hide on-screen banner
# -s  resolution cap
# -fps frame rate cap
# -p   prefer wired (we are wifi-only so leaves default)
# Pick a video sink that actually exists. glimagesink first (low latency),
# then xvimagesink, then autovideosink. uxplay takes the gstreamer element
# name verbatim — not shorthands like "gl".
SINK="autovideosink"
for cand in glimagesink xvimagesink; do
  if gst-inspect-1.0 "$cand" >/dev/null 2>&1; then SINK="$cand"; break; fi
done

exec uxplay \
  -n "$NAME" \
  -nh \
  -s "$RES" \
  -fps "$FPS" \
  -vs "$SINK"
