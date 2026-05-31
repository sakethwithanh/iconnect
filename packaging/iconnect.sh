#!/usr/bin/env bash
# iconnect CLI wrapper. Subcommands: run, start, stop, status, logs.
set -euo pipefail

PREFIX=/opt/iconnect
ENV_FILE=/etc/iconnect/iconnect.env
[[ -r "$ENV_FILE" ]] && set -a && . "$ENV_FILE" && set +a

usage() {
  cat <<EOF
iconnect — control iPhone from Linux

Usage: iconnect <command>

Commands:
  run         Launch uxplay + BT HID server + relay in the foreground (Ctrl+C to stop)
  start       Start iconnect systemd services (btkbd as root, mirror+relay as user)
  stop        Stop all iconnect services
  status      Show status of all units
  logs        Tail logs from all three services
  enable      Enable services to auto-start on boot/login
  disable     Disable auto-start

Env file: $ENV_FILE (override ICONNECT_TP_SCALE, ICONNECT_NAME, etc.)
EOF
}

cmd="${1:-run}"
shift || true

case "$cmd" in
  run)
    /usr/bin/python3 -u "$PREFIX/input/relay.py" "$@" &
    RELAY=$!
    sudo /usr/bin/python3 -u "$PREFIX/input/btkbd_server.py" &
    BTKBD=$!
    "$PREFIX/mirror/start-uxplay.sh" &
    UXPLAY=$!
    trap 'kill $RELAY $UXPLAY 2>/dev/null; sudo kill $BTKBD 2>/dev/null; exit 0' INT TERM
    wait
    ;;
  start)
    sudo systemctl start iconnect-btkbd.service
    systemctl --user start iconnect-mirror.service iconnect-relay.service
    ;;
  stop)
    systemctl --user stop iconnect-mirror.service iconnect-relay.service 2>/dev/null || true
    sudo systemctl stop iconnect-btkbd.service 2>/dev/null || true
    ;;
  status)
    sudo systemctl status iconnect-btkbd.service --no-pager || true
    systemctl --user status iconnect-mirror.service iconnect-relay.service --no-pager || true
    ;;
  logs)
    sudo journalctl -u iconnect-btkbd.service --user-unit iconnect-mirror.service --user-unit iconnect-relay.service -f
    ;;
  enable)
    sudo systemctl enable iconnect-btkbd.service
    systemctl --user enable iconnect-mirror.service iconnect-relay.service
    loginctl enable-linger "$USER" 2>/dev/null || true
    ;;
  disable)
    sudo systemctl disable iconnect-btkbd.service 2>/dev/null || true
    systemctl --user disable iconnect-mirror.service iconnect-relay.service 2>/dev/null || true
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    echo "unknown command: $cmd" >&2
    usage
    exit 2
    ;;
esac
