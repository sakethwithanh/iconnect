#!/usr/bin/env bash
# iconnect — launch mirroring + BT HID server + evdev relay.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
  printf '\n[iconnect] shutting down...\n'
  jobs -p | xargs -r kill 2>/dev/null || true
  sudo pkill -f "btkbd_server.py" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Kill any stragglers from a prior aborted run so PSM 17/19 + unix sock are free.
sudo pkill -f "btkbd_server.py" 2>/dev/null || true
pkill -f "uxplay" 2>/dev/null || true
sleep 0.5

# 1. AirPlay receiver
"$DIR/mirror/start-uxplay.sh" &

# 2. BT HID server (root). Force /usr/bin/python3 so dbus/PyGObject (pacman
# packages) are found even when a venv is active.
sudo env ICONNECT_USER="${ICONNECT_USER:-$(id -un)}" \
      ICONNECT_SOCK="${ICONNECT_SOCK:-/run/iconnect/hid.sock}" \
      ICONNECT_DEBUG="${ICONNECT_DEBUG:-0}" \
      /usr/bin/python3 "$DIR/input/btkbd_server.py" &

# Wait for the unix socket to appear before launching relay
SOCK="${ICONNECT_SOCK:-/run/iconnect/hid.sock}"
for _ in $(seq 1 40); do
  [[ -S "$SOCK" ]] && break
  sleep 0.25
done
[[ -S "$SOCK" ]] || { echo "[iconnect] HID server socket never appeared: $SOCK" >&2; exit 1; }

# 3. Local evdev → HID relay (user). Force system python so deps installed via
# pacman (python-evdev, etc.) are visible even when a venv is active.
SYSPY="${ICONNECT_PY:-/usr/bin/python3}"
"$SYSPY" "$DIR/input/relay.py" "$@"
