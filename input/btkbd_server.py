#!/usr/bin/env python3
"""
btkbd_server.py — BT HID (keyboard+mouse) server for iconnect.

Responsibilities:
  1. Set adapter Class-of-Device to combo HID (0x002540) and name to ICONNECT_NAME.
  2. Make adapter Powered/Pairable/Discoverable.
  3. Register a HID SDP record with BlueZ via org.bluez.ProfileManager1.
  4. Listen on L2CAP PSM 17 (control) and 19 (interrupt) for the iPhone.
  5. Expose a local Unix socket (/tmp/iconnect-hid.sock) where relay.py pushes
     HID payloads framed as:
         [1B report_id][payload...]
     This server prepends the 0xA1 (DATA Input) byte and forwards to the
     iPhone over the interrupt L2CAP channel.

Run as root (needs L2CAP bind on privileged PSMs and adapter mgmt).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hid_descriptor import REPORT_DESCRIPTOR_HEX  # noqa: E402

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
PSM_CTRL = 0x0011
PSM_INTR = 0x0013
COD_COMBO_HID = "0x002540"  # peripheral + keyboard + pointing
ADAPTER_NAME = os.environ.get("ICONNECT_NAME", "iconnect")
LOCAL_SOCK = os.environ.get("ICONNECT_SOCK", "/tmp/iconnect-hid.sock")


def log(msg: str) -> None:
    print(f"[btkbd] {msg}", flush=True)


def sh(*argv: str, check: bool = True, timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=check, capture_output=True, text=True, timeout=timeout)


def build_sdp_xml() -> str:
    tmpl = (HERE / "sdp_record.xml.tmpl").read_text()
    return tmpl.replace("{DESCRIPTOR_HEX}", REPORT_DESCRIPTOR_HEX)


def configure_adapter(bus: dbus.SystemBus) -> str:
    """Power adapter, set name, class, pairable+discoverable. Returns hci device name."""
    mgr = dbus.Interface(
        bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager",
    )
    adapter_path = None
    for path, ifaces in mgr.GetManagedObjects().items():
        if "org.bluez.Adapter1" in ifaces:
            adapter_path = path
            break
    if adapter_path is None:
        raise RuntimeError("No Bluetooth adapter found")

    hci = adapter_path.rsplit("/", 1)[-1]  # e.g. 'hci0'
    log(f"Using adapter {hci} at {adapter_path}")

    props = dbus.Interface(
        bus.get_object("org.bluez", adapter_path),
        "org.freedesktop.DBus.Properties",
    )
    # Discoverable for a limited window after start so a fresh pair can find
    # us, then auto-hidden. Paired iPhone still reconnects (it has the LTK).
    discov_window = int(os.environ.get("ICONNECT_DISCOVERABLE_SECS", "180"))
    props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
    props.Set("org.bluez.Adapter1", "Alias", dbus.String(ADAPTER_NAME))
    props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))
    props.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(discov_window))
    props.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(0))
    props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(True))
    log(f"Discoverable for {discov_window}s, then auto-hidden")

    # Class-of-Device: best-effort. btmgmt blocks on this kernel/bluez combo,
    # hciconfig is gone from modern bluez-utils. Skip silently if both fail —
    # iOS still pairs and routes mouse through AssistiveTouch without it.
    index = hci.removeprefix("hci") or "0"
    set_class_cmds = (
        ("btmgmt", "--index", index, "class", "0x05", "0x40"),
        ("hciconfig", hci, "class", COD_COMBO_HID),
    )
    for cmd in set_class_cmds:
        try:
            sh(*cmd, timeout=2.0)
            log(f"Set CoD via {cmd[0]} (combo HID)")
            break
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            log(f"{cmd[0]} class timed out (likely needs adapter power-cycle) — skipping")
        except subprocess.CalledProcessError as e:
            log(f"{cmd[0]} class failed: {e.stderr.strip()}")
    else:
        log("CoD not set — pairing may still work (AssistiveTouch routes by HID descriptor).")

    return hci


class RejectAgent(dbus.service.Object):
    """BlueZ Agent1 that rejects every new pairing request. Existing paired
    devices reconnect without invoking the agent, so iPhone is unaffected."""

    REJECTED = "org.bluez.Error.Rejected"

    def _reject(self):
        raise dbus.DBusException("New pairing not allowed", name=self.REJECTED)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        log("Agent.Release()")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log(f"Agent.AuthorizeService({device}, {uuid}) — rejected")
        self._reject()

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        log(f"Agent.RequestPinCode({device}) — rejected")
        self._reject()

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        log(f"Agent.RequestPasskey({device}) — rejected")
        self._reject()

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log(f"Agent.RequestConfirmation({device}, {passkey:06d}) — rejected")
        self._reject()

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log(f"Agent.RequestAuthorization({device}) — rejected")
        self._reject()

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        pass


def register_reject_agent(bus: dbus.SystemBus) -> RejectAgent:
    agent_path = "/com/iconnect/agent"
    agent = RejectAgent(bus, agent_path)
    mgr = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"),
        "org.bluez.AgentManager1",
    )
    try:
        mgr.RegisterAgent(agent_path, "NoInputNoOutput")
        mgr.RequestDefaultAgent(agent_path)
        log("Reject-new-pair agent registered as default")
    except dbus.DBusException as ex:
        log(f"Agent register failed: {ex}")
    return agent


class HIDProfile(dbus.service.Object):
    """Minimal BlueZ Profile1 implementation. BlueZ will call NewConnection
    when a remote initiates SDP-discovered HID — but we bypass that path
    because iOS connects via L2CAP directly on PSM 17/19 once SDP is registered."""

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        log("Profile.Release()")

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, path, fd, properties):
        log(f"Profile.NewConnection({path}) fd={fd}")

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, path):
        log(f"Profile.RequestDisconnection({path})")


def register_profile(bus: dbus.SystemBus) -> HIDProfile:
    profile_path = "/com/iconnect/hid_profile"
    profile = HIDProfile(bus, profile_path)
    mgr = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"),
        "org.bluez.ProfileManager1",
    )
    opts = dbus.Dictionary({
        "ServiceRecord": dbus.String(build_sdp_xml()),
        "Role": dbus.String("server"),
        "RequireAuthentication": dbus.Boolean(False),
        "RequireAuthorization": dbus.Boolean(False),
    }, signature="sv")
    mgr.RegisterProfile(profile_path, HID_UUID, opts)
    log("HID profile registered with BlueZ ProfileManager1")
    return profile


HIDP_HANDSHAKE_SUCCESSFUL = 0x00
HIDP_HANDSHAKE_NOT_READY  = 0x01
HIDP_HANDSHAKE_ERR_INVALID_REPORT_ID = 0x02
HIDP_HANDSHAKE_ERR_UNSUPPORTED_REQUEST = 0x03
HIDP_HANDSHAKE_ERR_INVALID_PARAMETER = 0x04


class L2CAPListener:
    """Listens on a single L2CAP PSM, accepts one client at a time.

    If `reader` is provided, every accepted client gets a daemon thread that
    feeds incoming packets to `reader(self, data)`. Used on the control PSM
    to respond to iOS's HIDP requests (SET_PROTOCOL, GET_REPORT, etc).
    """

    def __init__(self, psm: int, label: str, reader=None) -> None:
        self.psm = psm
        self.label = label
        self.reader = reader
        self.client_sock: socket.socket | None = None
        self.client_addr: str | None = None
        self._lock = threading.Lock()
        self._srv = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
        )
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((socket.BDADDR_ANY, psm))
        self._srv.listen(1)
        log(f"L2CAP listening on PSM 0x{psm:04x} ({label})")

    def accept_loop(self) -> None:
        while True:
            sock, addr = self._srv.accept()
            log(f"{self.label}: accepted from {addr}")
            with self._lock:
                if self.client_sock is not None:
                    try:
                        self.client_sock.close()
                    except Exception:
                        pass
                self.client_sock = sock
                self.client_addr = str(addr)
            if self.reader is not None:
                threading.Thread(
                    target=self._reader_loop, args=(sock,), daemon=True
                ).start()

    def _reader_loop(self, sock: socket.socket) -> None:
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    log(f"{self.label}: client closed")
                    return
                self.reader(self, data)
        except OSError as e:
            log(f"{self.label}: reader OSError ({e}); client dropped")

    def send(self, data: bytes) -> bool:
        with self._lock:
            sock = self.client_sock
        if sock is None:
            if os.environ.get("ICONNECT_DEBUG", "1") != "0":
                log(f"{self.label}: drop (no client) bytes={data.hex()}")
            return False
        try:
            sock.send(data)
            if os.environ.get("ICONNECT_DEBUG", "1") != "0":
                log(f"{self.label}: tx {data.hex()}")
            return True
        except OSError as e:
            log(f"{self.label}: send failed ({e}); dropping client")
            with self._lock:
                self.client_sock = None
            return False


def handle_ctrl_message(listener: L2CAPListener, data: bytes) -> None:
    """Minimal HIDP control responder. iOS sends SET_PROTOCOL right after
    connect and waits for a HANDSHAKE before it'll route input. We ACK
    everything as SUCCESSFUL — sufficient for a stock combo HID."""
    if not data:
        return
    hdr = data[0]
    msg_type = (hdr >> 4) & 0x0f
    param = hdr & 0x0f
    # 0=HANDSHAKE 1=HID_CONTROL 4=GET_REPORT 5=SET_REPORT 6=GET_PROTOCOL
    # 7=SET_PROTOCOL 9=DATA
    log(f"ctrl: rx type=0x{msg_type:x} param=0x{param:x} len={len(data)}")
    if msg_type in (0x4, 0x5, 0x6, 0x7):
        # ACK with HANDSHAKE_SUCCESSFUL.
        listener.send(bytes([HIDP_HANDSHAKE_SUCCESSFUL]))


def discover_paired(bus: dbus.SystemBus) -> str | None:
    """Find a paired device that looks like an iPhone — prefer Apple OUI or
    a name containing 'iphone'/'phone'. Returns BT MAC or None."""
    mgr = dbus.Interface(
        bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager",
    )
    apple_ouis = ("1C:E2:09", "F0:DC:E2", "AC:1F:74", "00:1B:63", "3C:AB:8E")
    candidates = []
    for path, ifaces in mgr.GetManagedObjects().items():
        d = ifaces.get("org.bluez.Device1")
        if not d or not d.get("Paired"):
            continue
        addr = str(d.get("Address", ""))
        name = str(d.get("Name", "") or d.get("Alias", ""))
        score = 0
        if "phone" in name.lower():
            score += 10
        if any(addr.upper().startswith(oui) for oui in apple_ouis):
            score += 5
        candidates.append((score, addr, name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    score, addr, name = candidates[0]
    log(f"Discovered paired device: {addr} ({name}) score={score}")
    return addr


def reconnect_loop(ctrl: L2CAPListener, intr: L2CAPListener, mac: str) -> None:
    """Outbound L2CAP connect to a paired iPhone. iOS treats us as the HID
    device with ReconnectInitiate=true, so we dial it on PSM 17 then 19.
    Re-attempts every 5s when either side has no client."""
    import time
    while True:
        time.sleep(3.0)
        if ctrl.client_sock is not None and intr.client_sock is not None:
            continue
        for psm, listener in ((PSM_CTRL, ctrl), (PSM_INTR, intr)):
            if listener.client_sock is not None:
                continue
            try:
                s = socket.socket(
                    socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
                )
                s.settimeout(4.0)
                s.connect((mac, psm))
                s.settimeout(None)
                with listener._lock:
                    listener.client_sock = s
                    listener.client_addr = mac
                log(f"reconnect: dialed {mac} PSM 0x{psm:04x} ({listener.label})")
                if listener.reader is not None:
                    threading.Thread(
                        target=listener._reader_loop, args=(s,), daemon=True
                    ).start()
            except OSError as ex:
                log(f"reconnect: dial {mac} PSM 0x{psm:04x} failed: {ex}")
                break  # iPhone offline; try again next tick


def relay_unix_socket(intr: L2CAPListener) -> None:
    """Accepts payloads from relay.py and forwards as HID Input reports."""
    try:
        os.unlink(LOCAL_SOCK)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(LOCAL_SOCK)
    # Restrict socket to one specific user. Order: ICONNECT_USER, then
    # SUDO_USER, then leave world-writable (single-user laptop default).
    target_user = os.environ.get("ICONNECT_USER") or os.environ.get("SUDO_USER")
    if target_user:
        try:
            import pwd
            pw = pwd.getpwnam(target_user)
            os.chown(LOCAL_SOCK, pw.pw_uid, pw.pw_gid)
            os.chmod(LOCAL_SOCK, 0o600)
            log(f"Local relay socket at {LOCAL_SOCK} (owner {target_user}, 0600)")
        except (KeyError, OSError) as ex:
            log(f"chown {LOCAL_SOCK} failed: {ex}; falling back to 0666")
            os.chmod(LOCAL_SOCK, 0o666)
    else:
        os.chmod(LOCAL_SOCK, 0o666)
        log(f"Local relay socket at {LOCAL_SOCK} (mode 0666, set ICONNECT_USER to lock)")
    srv.listen(4)

    while True:
        client, _ = srv.accept()
        threading.Thread(target=_handle_relay, args=(client, intr), daemon=True).start()


def _handle_relay(client: socket.socket, intr: L2CAPListener) -> None:
    with client:
        buf = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                return
            buf += chunk
            # Frames: 1B len, len bytes payload (len includes report_id)
            while len(buf) >= 1 and len(buf) >= 1 + buf[0]:
                n = buf[0]
                payload = buf[1:1 + n]
                buf = buf[1 + n:]
                # Prepend 0xA1 (DATA, Input report) for HID over L2CAP interrupt
                intr.send(b"\xa1" + payload)


def main() -> None:
    if os.geteuid() != 0:
        print("btkbd_server.py must run as root (L2CAP bind on privileged PSMs).",
              file=sys.stderr)
        sys.exit(1)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    configure_adapter(bus)
    register_reject_agent(bus)
    register_profile(bus)

    ctrl = L2CAPListener(PSM_CTRL, "ctrl", reader=handle_ctrl_message)
    intr = L2CAPListener(PSM_INTR, "intr")
    threading.Thread(target=ctrl.accept_loop, daemon=True).start()
    threading.Thread(target=intr.accept_loop, daemon=True).start()
    threading.Thread(target=relay_unix_socket, args=(intr,), daemon=True).start()

    iphone_mac = os.environ.get("ICONNECT_IPHONE_MAC") or discover_paired(bus)
    if iphone_mac:
        log(f"Auto-reconnect target: {iphone_mac}")
        threading.Thread(
            target=reconnect_loop, args=(ctrl, intr, iphone_mac), daemon=True
        ).start()
    else:
        log("No paired device found — will only accept incoming from iPhone.")

    loop = GLib.MainLoop()
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    log("Ready. Pair iPhone from Settings → Bluetooth → 'iconnect'.")
    loop.run()


if __name__ == "__main__":
    main()
