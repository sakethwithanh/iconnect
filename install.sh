#!/usr/bin/env bash
# iconnect — install system deps on Arch
set -euo pipefail

log() { printf '\033[1;34m[iconnect]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[iconnect]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && err "Run as your user, not root. sudo prompts inside as needed."

# AUR helper
AUR=""
for h in yay paru; do command -v "$h" >/dev/null && AUR="$h" && break; done
[[ -z "$AUR" ]] && err "No AUR helper found. Install yay or paru first."
log "Using AUR helper: $AUR"

# Repo packages
log "Installing repo packages..."
sudo pacman -S --needed --noconfirm \
  bluez bluez-utils bluez-libs \
  python python-dbus python-gobject \
  avahi nss-mdns \
  pipewire pipewire-pulse wireplumber \
  gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav

# python-evdev (repo or AUR fallback)
if pacman -Si python-evdev >/dev/null 2>&1; then
  sudo pacman -S --needed --noconfirm python-evdev
else
  "$AUR" -S --needed --noconfirm python-evdev
fi

# uxplay (AUR)
log "Installing uxplay from AUR..."
"$AUR" -S --needed --noconfirm uxplay

# Services
log "Enabling services..."
sudo systemctl enable --now bluetooth.service
sudo systemctl enable --now avahi-daemon.service

# Disable BlueZ default input plugin so we can register custom HID profile.
# Without this, BlueZ claims SDP record slots for input devices and our HID server fails.
OVERRIDE_DIR=/etc/systemd/system/bluetooth.service.d
if [[ ! -f "$OVERRIDE_DIR/10-iconnect-noinput.conf" ]]; then
  log "Disabling BlueZ 'input' plugin (needed for custom HID server)..."
  sudo mkdir -p "$OVERRIDE_DIR"
  sudo tee "$OVERRIDE_DIR/10-iconnect-noinput.conf" >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/lib/bluetooth/bluetoothd -P input
EOF
  sudo systemctl daemon-reload
  sudo systemctl restart bluetooth.service
fi

# Add user to input group for evdev access
if ! groups | grep -qw input; then
  log "Adding $USER to 'input' group (re-login required for effect)..."
  sudo usermod -aG input "$USER"
fi

log "Done."
log "Next:"
log "  1. iPhone: Settings → Accessibility → Touch → AssistiveTouch → ON"
log "  2. Mirror only:   ./mirror/start-uxplay.sh"
log "  3. Mirror+input:  ./start.sh   (after pairing, see docs/pairing.md)"
