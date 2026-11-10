#!/usr/bin/env python3
"""
relay.py — capture local keyboard + mouse via evdev, encode as HID reports,
push to btkbd_server.py over a Unix socket.

Frame format on the wire (matches btkbd_server._handle_relay):
    [length][report_id][payload ...]

Run as your user once you're in the 'input' group; --grab flag steals devices
so input goes ONLY to the iPhone (Esc+Esc+Esc within 1s ungrabs as escape).
"""

from __future__ import annotations

import argparse
import os
import select
import socket
import sys
import threading
import time
from pathlib import Path

import evdev
from evdev import ecodes as e

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hid_descriptor import kbd_report, mouse_report  # noqa: E402
from relay_security import DEFAULT_SOCKET, peer_uid, log_packet

SOCK_PATH = os.environ.get("ICONNECT_SOCK", DEFAULT_SOCKET)

# Linux KEY_* → HID Usage ID (HID Keyboard/Keypad page 0x07).
KEY_TO_HID: dict[int, int] = {
    e.KEY_A: 0x04, e.KEY_B: 0x05, e.KEY_C: 0x06, e.KEY_D: 0x07, e.KEY_E: 0x08,
    e.KEY_F: 0x09, e.KEY_G: 0x0a, e.KEY_H: 0x0b, e.KEY_I: 0x0c, e.KEY_J: 0x0d,
    e.KEY_K: 0x0e, e.KEY_L: 0x0f, e.KEY_M: 0x10, e.KEY_N: 0x11, e.KEY_O: 0x12,
    e.KEY_P: 0x13, e.KEY_Q: 0x14, e.KEY_R: 0x15, e.KEY_S: 0x16, e.KEY_T: 0x17,
    e.KEY_U: 0x18, e.KEY_V: 0x19, e.KEY_W: 0x1a, e.KEY_X: 0x1b, e.KEY_Y: 0x1c,
    e.KEY_Z: 0x1d,
    e.KEY_1: 0x1e, e.KEY_2: 0x1f, e.KEY_3: 0x20, e.KEY_4: 0x21, e.KEY_5: 0x22,
    e.KEY_6: 0x23, e.KEY_7: 0x24, e.KEY_8: 0x25, e.KEY_9: 0x26, e.KEY_0: 0x27,
    e.KEY_ENTER: 0x28, e.KEY_ESC: 0x29, e.KEY_BACKSPACE: 0x2a, e.KEY_TAB: 0x2b,
    e.KEY_SPACE: 0x2c, e.KEY_MINUS: 0x2d, e.KEY_EQUAL: 0x2e,
    e.KEY_LEFTBRACE: 0x2f, e.KEY_RIGHTBRACE: 0x30, e.KEY_BACKSLASH: 0x31,
    e.KEY_SEMICOLON: 0x33, e.KEY_APOSTROPHE: 0x34, e.KEY_GRAVE: 0x35,
    e.KEY_COMMA: 0x36, e.KEY_DOT: 0x37, e.KEY_SLASH: 0x38, e.KEY_CAPSLOCK: 0x39,
    e.KEY_F1: 0x3a, e.KEY_F2: 0x3b, e.KEY_F3: 0x3c, e.KEY_F4: 0x3d,
    e.KEY_F5: 0x3e, e.KEY_F6: 0x3f, e.KEY_F7: 0x40, e.KEY_F8: 0x41,
    e.KEY_F9: 0x42, e.KEY_F10: 0x43, e.KEY_F11: 0x44, e.KEY_F12: 0x45,
    e.KEY_RIGHT: 0x4f, e.KEY_LEFT: 0x50, e.KEY_DOWN: 0x51, e.KEY_UP: 0x52,
    e.KEY_HOME: 0x4a, e.KEY_END: 0x4d, e.KEY_PAGEUP: 0x4b, e.KEY_PAGEDOWN: 0x4e,
    e.KEY_DELETE: 0x4c, e.KEY_INSERT: 0x49,
}

MODIFIERS: dict[int, int] = {
    e.KEY_LEFTCTRL:  0x01, e.KEY_LEFTSHIFT: 0x02,
    e.KEY_LEFTALT:   0x04, e.KEY_LEFTMETA:  0x08,
    e.KEY_RIGHTCTRL: 0x10, e.KEY_RIGHTSHIFT: 0x20,
    e.KEY_RIGHTALT:  0x40, e.KEY_RIGHTMETA: 0x80,
}

MOUSE_BTN_BITS: dict[int, int] = {
    e.BTN_LEFT:   0x01,
    e.BTN_RIGHT:  0x02,
    e.BTN_MIDDLE: 0x04,
}


def log(msg: str) -> None:
    print(f"[relay] {msg}", flush=True)


def find_devices() -> tuple[list[evdev.InputDevice], list[evdev.InputDevice], list[evdev.InputDevice]]:
    """Return (keyboards, rel_mice, abs_touchpads) by sniffing capabilities."""
    keyboards: list[evdev.InputDevice] = []
    rel_mice: list[evdev.InputDevice] = []
    touchpads: list[evdev.InputDevice] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        caps = dev.capabilities()
        keys = set(caps.get(e.EV_KEY, []))
        rels = set(caps.get(e.EV_REL, []))
        # EV_ABS returns [(code, AbsInfo), ...] — extract just codes.
        abss = {c if isinstance(c, int) else c[0]
                for c in caps.get(e.EV_ABS, [])}
        if e.KEY_A in keys and dev not in keyboards:
            keyboards.append(dev)
        if (e.REL_X in rels and e.REL_Y in rels) and dev not in rel_mice:
            rel_mice.append(dev)
        # Touchpad: any device with absolute X+Y (single-touch or multitouch).
        # Reject joysticks/gamepads/tablets.
        has_abs_xy = (e.ABS_X in abss and e.ABS_Y in abss) \
            or (e.ABS_MT_POSITION_X in abss and e.ABS_MT_POSITION_Y in abss)
        is_joystick = e.BTN_JOYSTICK in keys or e.BTN_GAMEPAD in keys
        is_tablet = e.BTN_TOOL_PEN in keys
        if has_abs_xy and not is_joystick and not is_tablet \
                and dev not in touchpads:
            touchpads.append(dev)
    return keyboards, rel_mice, touchpads


def connect_server(retries: int = 20, delay: float = 0.5) -> socket.socket:
    last = None
    for _ in range(retries):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(SOCK_PATH)
            if peer_uid(s) != 0:
                raise PermissionError("Refusing non-root HID server")
            log(f"Connected to {SOCK_PATH}")
            return s
        except (FileNotFoundError, ConnectionRefusedError) as ex:
            s.close()
            last = ex
            time.sleep(delay)
        except BaseException:
            s.close()
            raise
    raise RuntimeError(f"Cannot connect to {SOCK_PATH}: {last}")


def frame(report_id: int, payload: bytes) -> bytes:
    body = bytes([report_id]) + payload
    return bytes([len(body)]) + body


class State:
    """Pressed keys + modifiers + mouse button state."""
    def __init__(self) -> None:
        self.modifiers = 0
        self.pressed: list[int] = []  # HID usage IDs, up to 6
        self.buttons = 0

    def kbd_event(self, code: int, value: int) -> bytes | None:
        if code in MODIFIERS:
            bit = MODIFIERS[code]
            new = (self.modifiers | bit) if value else (self.modifiers & ~bit)
            if new == self.modifiers:
                return None
            self.modifiers = new
            return kbd_report(self.modifiers, self.pressed)
        hid = KEY_TO_HID.get(code)
        if hid is None:
            return None
        if value == 1:  # press
            if hid in self.pressed:
                return None
            if len(self.pressed) >= 6:
                return None  # 6-key rollover limit
            self.pressed.append(hid)
        elif value == 0:  # release
            if hid not in self.pressed:
                return None
            self.pressed.remove(hid)
        else:
            return None  # autorepeat — let host handle
        return kbd_report(self.modifiers, self.pressed)

    def mouse_button(self, code: int, value: int) -> bytes | None:
        bit = MOUSE_BTN_BITS.get(code)
        if bit is None:
            return None
        new = (self.buttons | bit) if value else (self.buttons & ~bit)
        if new == self.buttons:
            return None
        self.buttons = new
        return mouse_report(self.buttons, 0, 0, 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grab", action="store_true",
                    help="start with exclusive grab — input goes only to iPhone")
    ap.add_argument("--no-toggle", action="store_true",
                    help="disable Shift+Tab toggle hotkey")
    args = ap.parse_args()

    sock = connect_server()
    state = State()
    send_lock = threading.Lock()
    forwarding = {"on": args.grab}

    def send(buf: bytes) -> None:
        if not forwarding["on"]:
            return
        with send_lock:
            try:
                sock.sendall(buf)
                log_packet(log, "tx", buf)
            except OSError as ex:
                log(f"server gone: {ex}")
                os._exit(1)

    keyboards, mice, touchpads = find_devices()
    if not keyboards and not mice and not touchpads:
        log("No keyboards/mice/touchpads found. Are you in the 'input' group?")
        sys.exit(1)

    log(f"Keyboards: {[d.name for d in keyboards]}")
    log(f"Mice (REL): {[d.name for d in mice]}")
    log(f"Touchpads (ABS): {[d.name for d in touchpads]}")
    if not touchpads and os.environ.get("ICONNECT_DEBUG", "0") == "1":
        # Dump devices we didn't classify so the user can spot the touchpad
        # path manually with `sudo evtest`.
        chosen = {d.path for d in keyboards + mice + touchpads}
        for path in evdev.list_devices():
            if path in chosen:
                continue
            try:
                d = evdev.InputDevice(path)
            except OSError:
                continue
            caps = d.capabilities()
            has = []
            if e.EV_KEY in caps: has.append("KEY")
            if e.EV_REL in caps: has.append("REL")
            if e.EV_ABS in caps: has.append("ABS")
            log(f"  unmatched {path}: {d.name} caps={','.join(has) or 'none'}")

    # Touchpad → mouse delta translation. Per device, track last ABS_X/Y and
    # whether finger is currently down. When finger goes up, drop history.
    tp_state: dict[str, dict] = {
        d.path: {"last_x": None, "last_y": None, "touching": False}
        for d in touchpads
    }
    tp_sensitivity = float(os.environ.get("ICONNECT_TP_SCALE", "0.3"))

    devices = keyboards + mice + touchpads

    def grab_all() -> None:
        for d in devices:
            try:
                d.grab()
            except OSError as ex:
                log(f"grab failed {d.path}: {ex}")

    def ungrab_all() -> None:
        for d in devices:
            try:
                d.ungrab()
            except OSError:
                pass

    def set_forwarding(on: bool) -> None:
        if forwarding["on"] == on:
            return
        if on:
            # Switching to iPhone: clear stale state so the in-progress Shift+Tab
            # combo on the PC side doesn't leak as a held Shift on iPhone.
            state.modifiers = 0
            state.pressed.clear()
            state.buttons = 0
            forwarding["on"] = True
            grab_all()
            log("→ iPhone mode (grabbed)")
        else:
            # Release any held keys/buttons on iPhone before handing input back.
            forwarding["on"] = True  # allow these last sends through
            send(frame(0x01, kbd_report(0, [])))
            send(frame(0x02, mouse_report(0, 0, 0, 0)))
            forwarding["on"] = False
            state.modifiers = 0
            state.pressed.clear()
            state.buttons = 0
            ungrab_all()
            log("→ PC mode (ungrabbed)")

    if forwarding["on"]:
        grab_all()
        log("Started in iPhone mode (grabbed).")
    else:
        log("Started in PC mode. Double-tap Right_Ctrl to switch to iPhone.")

    fd_map = {d.fd: d for d in devices}
    # Accumulate REL_X/Y/WHEEL within one SYN_REPORT cycle, then flush.
    accum_dx = accum_dy = accum_wheel = 0
    active_mouse: evdev.InputDevice | None = None
    rel_seen: set[str] = set()  # devices that have emitted REL — for debug
    last_toggle_press = [0.0]  # mutable holder for double-tap detection

    try:
        while True:
            r, _, _ = select.select(fd_map.keys(), [], [])
            for fd in r:
                dev = fd_map[fd]
                for ev in dev.read():
                    if ev.type == e.EV_KEY:
                        # Toggle: double-tap Right_Ctrl within 400ms.
                        # Pure modifier — no terminal escape sequence leak.
                        if (not args.no_toggle
                                and ev.code == e.KEY_RIGHTCTRL
                                and ev.value == 1):
                            now = time.time()
                            if now - last_toggle_press[0] < 0.4:
                                set_forwarding(not forwarding["on"])
                                last_toggle_press[0] = 0.0
                                continue
                            last_toggle_press[0] = now
                        if ev.code == e.BTN_TOUCH and dev.path in tp_state:
                            st = tp_state[dev.path]
                            if ev.value:
                                st["touching"] = True
                                st["last_x"] = None
                                st["last_y"] = None
                            else:
                                st["touching"] = False
                        elif ev.code in MOUSE_BTN_BITS:
                            rep = state.mouse_button(ev.code, ev.value)
                            if rep is not None:
                                send(frame(0x02, rep))
                        else:
                            rep = state.kbd_event(ev.code, ev.value)
                            if rep is not None:
                                send(frame(0x01, rep))
                    elif ev.type == e.EV_REL:
                        active_mouse = dev
                        if dev.path not in rel_seen:
                            rel_seen.add(dev.path)
                            log(f"REL events from {dev.path} ({dev.name})")
                        if ev.code == e.REL_X:
                            accum_dx += ev.value
                        elif ev.code == e.REL_Y:
                            accum_dy += ev.value
                        elif ev.code == e.REL_WHEEL:
                            accum_wheel += ev.value
                    elif ev.type == e.EV_ABS and dev.path in tp_state:
                        st = tp_state[dev.path]
                        if ev.code in (e.ABS_X, e.ABS_MT_POSITION_X):
                            if st["touching"] and st["last_x"] is not None:
                                accum_dx += int((ev.value - st["last_x"]) * tp_sensitivity)
                                active_mouse = dev
                            st["last_x"] = ev.value
                        elif ev.code in (e.ABS_Y, e.ABS_MT_POSITION_Y):
                            if st["touching"] and st["last_y"] is not None:
                                accum_dy += int((ev.value - st["last_y"]) * tp_sensitivity)
                                active_mouse = dev
                            st["last_y"] = ev.value
                        elif ev.code == e.ABS_MT_TRACKING_ID:
                            # -1 = finger lifted; >=0 = new touch
                            if ev.value == -1:
                                st["touching"] = False
                            else:
                                st["touching"] = True
                                st["last_x"] = None
                                st["last_y"] = None
                    elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
                        if active_mouse is dev and (accum_dx or accum_dy or accum_wheel):
                            send(frame(0x02, mouse_report(
                                state.buttons, accum_dx, accum_dy, accum_wheel
                            )))
                            accum_dx = accum_dy = accum_wheel = 0
    finally:
        if args.grab:
            for d in devices:
                try:
                    d.ungrab()
                except OSError:
                    pass


if __name__ == "__main__":
    main()
