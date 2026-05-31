"""
HID Report Descriptor: combo keyboard (Report ID 1) + mouse (Report ID 2).

Keyboard report (9 bytes incl. report ID):
    [0x01, modifier, reserved, key0..key5]

Mouse report (5 bytes incl. report ID):
    [0x02, buttons, dx, dy, wheel]   (dx/dy/wheel signed int8)

iOS AssistiveTouch consumes the mouse half; the keyboard half is the standard
Bluetooth keyboard surface (works in any text field).
"""

REPORT_DESCRIPTOR = bytes([
    # --- Keyboard collection (Report ID 1) ---
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xa1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0xe0,        #   Usage Min (LeftCtrl)
    0x29, 0xe7,        #   Usage Max (RightGUI)
    0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08,
    0x81, 0x02,        #   Input — 8 modifier bits
    0x95, 0x01, 0x75, 0x08,
    0x81, 0x01,        #   Input — reserved byte
    0x95, 0x06, 0x75, 0x08,
    0x15, 0x00, 0x25, 0x65,
    0x05, 0x07,
    0x19, 0x00, 0x29, 0x65,
    0x81, 0x00,        #   Input — 6 key array
    0xc0,              # End Collection

    # --- Mouse collection (Report ID 2) ---
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x02,        # Usage (Mouse)
    0xa1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)
    0x09, 0x01,        #   Usage (Pointer)
    0xa1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Buttons)
    0x19, 0x01, 0x29, 0x03,
    0x15, 0x00, 0x25, 0x01,
    0x95, 0x03, 0x75, 0x01,
    0x81, 0x02,        #     3 button bits
    0x95, 0x01, 0x75, 0x05,
    0x81, 0x01,        #     padding
    0x05, 0x01,
    0x09, 0x30,        #     X
    0x09, 0x31,        #     Y
    0x09, 0x38,        #     Wheel
    0x15, 0x81, 0x25, 0x7f,
    0x75, 0x08, 0x95, 0x03,
    0x81, 0x06,        #     3 signed bytes, relative
    0xc0,              #   End Collection
    0xc0,              # End Collection
])

REPORT_DESCRIPTOR_HEX = REPORT_DESCRIPTOR.hex()


def kbd_report(modifiers: int = 0, keys: list[int] | None = None) -> bytes:
    """Build an 8-byte keyboard payload (no report ID prefix)."""
    keys = (keys or [])[:6]
    keys = keys + [0] * (6 - len(keys))
    return bytes([modifiers & 0xff, 0x00, *keys])


def mouse_report(buttons: int = 0, dx: int = 0, dy: int = 0, wheel: int = 0) -> bytes:
    """Build a 4-byte mouse payload (no report ID prefix)."""
    def s8(v: int) -> int:
        v = max(-127, min(127, v))
        return v & 0xff
    return bytes([buttons & 0x07, s8(dx), s8(dy), s8(wheel)])
