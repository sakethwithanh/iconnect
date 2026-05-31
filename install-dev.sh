#!/usr/bin/env bash
# install-dev.sh — wire iconnect into the user's app menu + systemd user units
# without building a pacman package. Points everything at this project dir.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/applications"
UNIT_DIR="$HOME/.config/systemd/user"
SYS_UNIT_DIR=/etc/systemd/system
ENV_FILE="$HOME/.config/iconnect/iconnect.env"

log() { printf '\033[1;34m[install-dev]\033[0m %s\n' "$*"; }

mkdir -p "$APP_DIR" "$UNIT_DIR" "$(dirname "$ENV_FILE")"

# 1. Desktop launcher → calls project start.sh in a terminal.
TERM_BIN=$(command -v alacritty || command -v kitty || command -v gnome-terminal \
  || command -v konsole || command -v xterm || echo xterm)

cat > "$APP_DIR/iconnect.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=iconnect
GenericName=iPhone Bridge
Comment=Mirror and control your iPhone from Linux
Exec=$TERM_BIN -e bash -c 'cd "$PROJECT" && ./start.sh; exec bash'
Icon=phone
Terminal=false
Categories=Network;Utility;RemoteAccess;
Keywords=iphone;airplay;mirror;bluetooth;
StartupNotify=false
EOF
log "Wrote $APP_DIR/iconnect.desktop (terminal: $TERM_BIN)"

# 2. Default env file the user can edit.
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$PROJECT/packaging/iconnect.env" "$ENV_FILE"
  log "Wrote default config $ENV_FILE"
fi

# 3. User systemd units → mirror + relay (no root, no sudo).
cat > "$UNIT_DIR/iconnect-mirror.service" <<EOF
[Unit]
Description=iconnect AirPlay receiver (uxplay)
After=network-online.target

[Service]
Type=simple
EnvironmentFile=-$ENV_FILE
ExecStart=$PROJECT/mirror/start-uxplay.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/iconnect-relay.service" <<EOF
[Unit]
Description=iconnect input relay
After=iconnect-btkbd.service

[Service]
Type=simple
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/bin/python3 -u $PROJECT/input/relay.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
log "Wrote user services to $UNIT_DIR"

# 4. System service for btkbd (needs root for L2CAP privileged PSMs).
sudo tee "$SYS_UNIT_DIR/iconnect-btkbd.service" >/dev/null <<EOF
[Unit]
Description=iconnect Bluetooth HID server
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/bin/python3 -u $PROJECT/input/btkbd_server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
log "Wrote system service to $SYS_UNIT_DIR/iconnect-btkbd.service"

systemctl --user daemon-reload
sudo systemctl daemon-reload
update-desktop-database "$APP_DIR" 2>/dev/null || true

# 5. Enable lingering so user services run even when not logged in (optional).
loginctl enable-linger "$USER" 2>/dev/null || true

cat <<EOF

==> Done.

App menu: search for "iconnect" — clicking runs ./start.sh in a terminal.

Background mode:
  sudo systemctl enable --now iconnect-btkbd.service
  systemctl --user enable --now iconnect-mirror.service iconnect-relay.service

Stop background:
  systemctl --user stop iconnect-mirror.service iconnect-relay.service
  sudo systemctl stop iconnect-btkbd.service

Logs:
  sudo journalctl -u iconnect-btkbd.service -f
  journalctl --user -u iconnect-mirror.service -u iconnect-relay.service -f

Config: edit $ENV_FILE then restart services.
EOF
