# iconnect

> Mirror and control your iPhone from a Linux PC — over Wi-Fi (screen) and Bluetooth (mouse + keyboard + touchpad). No jailbreak required.

iconnect bridges the two things Apple won't let you do natively on Linux:

1. **See your iPhone screen** on your PC via AirPlay (uxplay).
2. **Type and point** on your iPhone from your PC's keyboard, mouse, and touchpad via a custom Bluetooth HID device the PC pretends to be.

It runs as a set of background systemd services, comes with a tray-icon GUI for on/off control, and exposes a CLI for power users.

---

## Features

- **AirPlay 2 receiver** (uxplay) — wireless screen mirroring over LAN.
- **Custom Bluetooth combo HID** (keyboard + mouse) — your iPhone treats the PC as a paired pointer/keyboard.
- **Touchpad → relative-mouse translation** so laptop trackpads work, not just external mice (libinput EV_ABS → HID mouse deltas).
- **Hotkey toggle**: double-tap Right_Ctrl to switch input between PC and iPhone (no terminal escape leaks).
- **PyQt6 control panel + tray icon** — green/red phone glyph shows ON/OFF state at a glance.
- **Auto-reconnect** to a paired iPhone via outbound L2CAP dial — no manual BT toggle on each restart.
- **Hardened by default**:
  - Reject-new-pair BlueZ agent (existing paired devices unaffected).
  - Adapter Discoverable auto-hides after a configurable window.
  - Unix relay socket locked to a single user (mode 0600).
- **Arch-native packaging** — PKGBUILD + .install + systemd units + .desktop launcher.

---

## Why this exists

Apple ships native AirPlay receivers and BT HID support on macOS and iPadOS, but Linux gets nothing official. Existing projects either:

- Mirror only (uxplay) — no input.
- Require jailbreak (Veency VNC) — full control but breaks app support.
- Are Android-only (scrcpy).

iconnect glues the legal/unauthenticated APIs that *do* exist on stock iOS into a single workflow: AirPlay for screen + AssistiveTouch for pointer.

---

## Hard limits (read before installing)

- iOS will not accept input over Wi-Fi. Pointer/keyboard must go over Bluetooth. iconnect handles both transports automatically.
- AssistiveTouch must be enabled on the iPhone (Settings → Accessibility → Touch → AssistiveTouch → ON).
- No multi-touch gestures (pinch, edge-swipe). HID mouse model has no notion of two fingers.
- Latency: 100–200 ms over Wi-Fi for the mirror, 30–60 ms over Bluetooth for input.
- Tap-to-click on laptop touchpads doesn't work — physical button press only (libinput-level gesture not exposed to raw evdev).

---

## Requirements

| Side | Requirement |
|---|---|
| Linux PC | Arch Linux (or derivative). BlueZ ≥ 5.86. Python 3.11+. A Bluetooth adapter. |
| iPhone | iOS 13+ (any model). AssistiveTouch on. |
| Network | Both devices on the same Wi-Fi network for screen mirroring. |

Tested on:
- Arch Linux, kernel 7.x, Hyprland + Waybar, ThinkPad with Intel BT.
- iPhone running iOS 17+.

---

## Install (Arch)

### Method 1 — pacman package (recommended)

```bash
git clone https://github.com/<your-username>/iconnect.git
cd iconnect/packaging
makepkg -si
```

`makepkg` will:

- Install all dependencies (`bluez`, `bluez-utils`, `python-dbus`, `python-evdev`, `python-pyqt6`, gstreamer, avahi).
- Lay files under `/opt/iconnect`, `/usr/bin/iconnect`, `/usr/lib/systemd/...`.
- Drop a bluetoothd override that disables BlueZ's built-in input plugin (needed for our custom HID profile to register).
- Install the tray-app .desktop launcher.

Then add yourself to the `input` group (one-time, requires re-login):

```bash
sudo usermod -aG input "$USER"
```

`uxplay` is in the AUR — install with your helper:

```bash
yay -S uxplay   # or paru -S uxplay
```

### Method 2 — dev install (no pacman package)

For developers or non-pacman users:

```bash
git clone https://github.com/<your-username>/iconnect.git
cd iconnect
./install.sh        # installs system deps via pacman
./install-dev.sh    # wires desktop launcher + systemd user units to this checkout
```

---

## Quick start

1. **iPhone**: Settings → Accessibility → Touch → AssistiveTouch → **ON**.
2. **iPhone**: Settings → Bluetooth → **ON**, leave the screen open.
3. **PC**: start the services
   ```bash
   sudo systemctl enable --now iconnect-btkbd.service
   systemctl --user enable --now iconnect-mirror.service iconnect-relay.service
   ```
4. **iPhone**: under "Other Devices" tap `iconnect`. Pair (Just Works, no PIN).
5. **iPhone**: Control Center → Screen Mirroring → pick `iconnect`. Your phone screen pops up on your PC.
6. **PC**: double-tap **Right_Ctrl** to switch input to iPhone. Mouse + keyboard + touchpad now drive the phone. Double-tap again to switch back.

Open the tray GUI for one-click on/off:

```bash
python3 /opt/iconnect/gui/iconnect_gui.py   # or click "iconnect" in your app menu
```

---

## CLI

```bash
iconnect run         # foreground (uxplay + btkbd + relay)
iconnect start       # start the three systemd services
iconnect stop        # stop them
iconnect status      # show systemd unit state
iconnect logs        # tail all three logs
iconnect enable      # auto-start on boot/login
iconnect disable     # remove auto-start
```

---

## Configuration

Edit `/etc/iconnect/iconnect.env` (or `~/.config/iconnect/iconnect.env` for dev install) and restart services.

| Variable | Default | Meaning |
|---|---|---|
| `ICONNECT_NAME` | `iconnect` | Advertised BT/AirPlay name. |
| `ICONNECT_TP_SCALE` | `0.3` | Touchpad sensitivity multiplier (raw ABS delta × this). |
| `ICONNECT_RES` | `1920x1080` | AirPlay mirror resolution cap. |
| `ICONNECT_FPS` | `60` | AirPlay mirror framerate cap. |
| `ICONNECT_USER` | unset | User who owns `/tmp/iconnect-hid.sock` (locks it 0600). |
| `ICONNECT_DISCOVERABLE_SECS` | `180` | Time BT adapter is visible to scans after start. Paired iPhone still reconnects after this. |
| `ICONNECT_IPHONE_MAC` | auto | iPhone's BT MAC for outbound auto-reconnect. Leave blank to auto-discover. |
| `ICONNECT_DEBUG` | `1` | Per-event tx/rx logs. Set `0` for quiet operation. |

---

## How it works

```
┌──────────────────┐   AirPlay/Wi-Fi   ┌──────────────────┐
│    iPhone        │ ────────────────► │  uxplay (PC)     │
│                  │                    │  → gstreamer     │
│                  │ ◄──────────────── │   pipeline       │
│                  │   HID over BT     └──────────────────┘
│  AssistiveTouch  │
│  consumes the    │   ┌──────────────────┐
│  HID Mouse half  │ ◄─┤ btkbd_server.py  │
│  iOS keyboard    │   │ (root, L2CAP)    │
│  consumes the    │   │ PSM 17 control   │
│  HID Keyboard    │   │ PSM 19 interrupt │
│  half            │   └────────▲─────────┘
└──────────────────┘            │ /tmp/iconnect-hid.sock
                                │ (Unix, mode 0600)
                                │
                       ┌────────┴─────────┐
                       │ relay.py (user)  │
                       │ reads evdev,     │
                       │ encodes HID,     │
                       │ pushes frames    │
                       └──────────────────┘
```

- **btkbd_server.py** registers a BlueZ Profile1 with a custom SDP record carrying our HID report descriptor (combo keyboard + relative mouse, report IDs 1 and 2). It opens L2CAP sockets on PSM 17 (control) and 19 (interrupt) and ACKs every HIDP control message from iOS with `HANDSHAKE_SUCCESSFUL`.
- **relay.py** reads `/dev/input/event*` directly, classifies devices (keyboard / REL mouse / ABS touchpad), grabs them when in iPhone mode, and serializes HID reports onto the Unix socket. Touchpad ABS events are converted to relative deltas in software (the raw `Mouse` interface that the kernel exposes alongside touchpads is often silent on libinput-driven systems).
- **uxplay** is the AirPlay 2 receiver — independent process, AirPlay over Wi-Fi/Avahi, GStreamer pipeline rendering to glimagesink.
- **iconnect_gui.py** is a PyQt6 panel with a `QSystemTrayIcon`. Single-instance lock via `QLocalServer`. Talks to user-scope systemd to start/stop the relay + mirror services.

The PC announces itself as a Combo HID device (class 0x002540). iOS pairs with Just-Works (no PIN). Once paired, iOS's AssistiveTouch picks up the mouse half of our descriptor and renders a circular pointer.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `iconnect` doesn't appear in iPhone's Bluetooth list | Adapter not Discoverable. Restart `iconnect-btkbd.service` or run `bluetoothctl discoverable on`. Window is 180 s after start. |
| Pair fails repeatedly | Stale state. On iPhone "Forget This Device"; on PC `bluetoothctl remove <MAC>`; restart `iconnect-btkbd.service`; pair again. |
| Paired but no pointer | AssistiveTouch off. Settings → Accessibility → Touch → AssistiveTouch → ON. |
| Pointer jittery — both local and iPhone moving | You're in PC mode. Double-tap Right_Ctrl to enter iPhone mode (relay grabs devices). |
| Touchpad not detected | Run `relay.py` with `ICONNECT_DEBUG=1` and look at `Touchpads (ABS): [...]`. If empty, the touchpad has no `ABS_X`/`ABS_MT_POSITION_X` — open an issue with `sudo evtest /dev/input/eventN` output. |
| `iconnect` missing from AirPlay/Screen Mirroring list | Check iPhone is on the same Wi-Fi. Confirm `avahi-browse -art \| grep airplay` shows the receiver from PC. |
| Auto-reconnect dial fails (`Connection refused`) | iOS sometimes refuses outbound HID connections. Tap `iconnect` once in BT settings to reconnect manually — only needed after server restart. |
| Tray icon doesn't change color | Kill stale GUI processes: `pkill -9 -f iconnect_gui.py` then relaunch. |

---

## Security notes

- `btkbd_server.py` runs as **root** (BlueZ L2CAP PSM bind requires it). Read the source — it's ~300 lines, no `exec`/`eval`, no outbound network beyond Bluetooth.
- The BT adapter is Discoverable for `ICONNECT_DISCOVERABLE_SECS` (default 180 s) after each start. A reject-new-pair BlueZ agent blocks all new pair attempts during and after that window. Your existing paired iPhone still reconnects (it has the LTK).
- `/tmp/iconnect-hid.sock` is `0600` and owned by `$ICONNECT_USER`. No other local user can inject HID reports.
- Pair is Just-Works (no PIN). On a single-user laptop in a non-hostile environment this is fine; in a coffee-shop threat model, set `ICONNECT_DISCOVERABLE_SECS=10` and the agent rejects new pairs after that.

---

## Roadmap

- Mirror-stop button on the GUI panel.
- Auto-detect touchpad sensitivity per-device.
- `xdg-mime` association so dragging files onto the tray pushes via AirDrop (libimobiledevice).
- Optional pymobiledevice3 USB transport for lower-latency mirroring.

---

## Contributing

Open issues with full logs (`journalctl --user -u iconnect-relay.service -n 200`). PRs welcome — keep the codebase dependency-light (no heavy frameworks), and document any new env var in this README's table.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Credits

- [uxplay](https://github.com/FDH2/UxPlay) — AirPlay 2 receiver.
- BlueZ HID server reference work by Liam Fraser, ruundii, and the original `yaptb` Pi keyboard project.
- python-evdev for clean Linux input access.
