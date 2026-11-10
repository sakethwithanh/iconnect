import contextlib
import io
import importlib.util
import re
import os
from pathlib import Path
import pwd
import socket
import stat
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "input"))
from relay_security import RelaySocket, log_packet, peer_uid, read_reports, relay_account

try:
    import btkbd_server as server
except ImportError:
    server = None

KEYBOARD = bytes.fromhex("010000040506000000")
MOUSE = bytes.fromhex("02017f8100")


def frame(report):
    return bytes([len(report)]) + report


class AccountTests(unittest.TestCase):
    def test_missing_account_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "Set ICONNECT_USER"):
            relay_account({})

    def test_unknown_account_never_falls_back(self):
        with patch("relay_security.pwd.getpwnam", side_effect=KeyError):
            with self.assertRaisesRegex(RuntimeError, "Unknown"):
                relay_account({"ICONNECT_USER": "missing", "SUDO_USER": "valid"})

    def test_root_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "non-root"):
            relay_account({"ICONNECT_USER": "root"})

    def test_explicit_user_precedes_sudo(self):
        with patch("relay_security.pwd.getpwnam") as lookup:
            lookup.return_value.pw_uid = 1000
            self.assertIs(relay_account({"ICONNECT_USER": "selected", "SUDO_USER": "other"}),
                          lookup.return_value)
            lookup.assert_called_once_with("selected")

    def test_sudo_fallback(self):
        with patch("relay_security.pwd.getpwnam") as lookup:
            lookup.return_value.pw_uid = 1000
            relay_account({"SUDO_USER": "selected"})
            lookup.assert_called_once_with("selected")


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "service" / "hid.sock"
        self.account = pwd.getpwuid(os.geteuid())

    def test_private_creation_real_credentials_and_cleanup(self):
        old_mask = os.umask(0)
        self.addCleanup(os.umask, old_mask)
        with RelaySocket(str(self.path), self.account) as listener:
            info = self.path.stat()
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(info.st_uid, self.account.pw_uid)
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(self.path))
                accepted, _ = listener.sock.accept()
                with accepted:
                    self.assertEqual(peer_uid(accepted), os.geteuid())
        self.assertFalse(self.path.exists())
        self.assertEqual(os.umask(0), 0)

    def test_restrictive_umask_does_not_block_account_traversal(self):
        old_mask = os.umask(0o077)
        self.addCleanup(os.umask, old_mask)
        with RelaySocket(str(self.path), self.account):
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(os.umask(0o077), 0o077)

    def test_socket_is_private_before_ownership_transfer(self):
        change_owner = os.chown
        seen = []

        def check_then_chown(path, uid, gid):
            seen.append(stat.S_IMODE(Path(path).stat().st_mode))
            change_owner(path, uid, gid)

        old_mask = os.umask(0)
        self.addCleanup(os.umask, old_mask)
        with patch("relay_security.os.chown", side_effect=check_then_chown):
            with RelaySocket(str(self.path), self.account):
                pass
        self.assertEqual(seen, [0o600])

    def test_ownership_failure_removes_socket(self):
        with patch("relay_security.os.chown", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                RelaySocket(str(self.path), self.account)
        self.assertFalse(self.path.exists())
        # A failed attempt releases its lock and permits a corrected retry.
        with RelaySocket(str(self.path), self.account):
            pass

    def test_chmod_failure_removes_socket(self):
        self.path.parent.mkdir()
        with patch("relay_security.os.chmod", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                RelaySocket(str(self.path), self.account)
        self.assertFalse(self.path.exists())

    def test_writable_parent_rejected(self):
        self.path.parent.mkdir(mode=0o777)
        self.path.parent.chmod(0o777)
        with self.assertRaisesRegex(RuntimeError, "writable"):
            RelaySocket(str(self.path), self.account)
        self.assertFalse(self.path.exists())

    def test_writable_ancestor_rejected(self):
        Path(self.tmp.name).chmod(0o777)
        with self.assertRaisesRegex(RuntimeError, "writable"):
            RelaySocket(str(self.path), self.account)

    def test_symlink_parent_rejected(self):
        target = Path(self.tmp.name) / "real"
        target.mkdir()
        self.path.parent.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "owner or type"):
            RelaySocket(str(self.path), self.account)

    def test_symlink_socket_preserves_target(self):
        self.path.parent.mkdir()
        target = Path(self.tmp.name) / "victim"
        target.write_text("untouched")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            RelaySocket(str(self.path), self.account)
        self.assertEqual(target.read_text(), "untouched")
        self.assertTrue(self.path.is_symlink())

    def test_regular_file_socket_is_not_deleted(self):
        self.path.parent.mkdir()
        self.path.write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            RelaySocket(str(self.path), self.account)
        self.assertEqual(self.path.read_text(), "keep")

    def test_second_listener_cannot_remove_live_socket(self):
        with RelaySocket(str(self.path), self.account):
            inode = self.path.stat().st_ino
            with self.assertRaises(BlockingIOError):
                RelaySocket(str(self.path), self.account)
            self.assertEqual(self.path.stat().st_ino, inode)

    def test_stale_socket_replaced(self):
        self.path.parent.mkdir()
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(self.path))
        with RelaySocket(str(self.path), self.account):
            self.assertTrue(self.path.is_socket())

    def test_lock_symlink_is_rejected(self):
        self.path.parent.mkdir()
        target = Path(self.tmp.name) / "victim"
        target.write_text("keep")
        Path(str(self.path) + ".lock").symlink_to(target)
        with self.assertRaises(OSError):
            RelaySocket(str(self.path), self.account)
        self.assertEqual(target.read_text(), "keep")

    def test_relative_and_parent_traversal_paths_rejected(self):
        for path in ("hid.sock", str(self.path.parent / ".." / "hid.sock")):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                RelaySocket(path, self.account)


class ReportTests(unittest.TestCase):
    def reports(self, chunks):
        peer = Mock()
        peer.recv.side_effect = [*chunks, b""]
        return list(read_reports(peer))

    def test_all_fragmentation_boundaries_and_coalescing(self):
        data = frame(KEYBOARD) + frame(MOUSE)
        for split in range(1, len(data)):
            with self.subTest(split=split):
                self.assertEqual(self.reports([data[:split], data[split:]]), [KEYBOARD, MOUSE])
        self.assertEqual(self.reports([data]), [KEYBOARD, MOUSE])
        self.assertEqual(self.reports([bytes([n]) for n in data]), [KEYBOARD, MOUSE])

    def test_invalid_lengths_fail_immediately(self):
        for length in (0, 1, 4, 8, 10, 255):
            with self.subTest(length=length), self.assertRaises(ValueError):
                self.reports([bytes([length])])

    def test_invalid_reports_rejected(self):
        for data in (b"\x05\x03\x00\x00\x00\x00", b"\x05\x01\x00\x00\x00\x00",
                     frame(b"\x01\x00\x01" + b"\x00" * 6), frame(b"\x02\x80\x00\x00\x00")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.reports([data])

    def test_truncated_reports_rejected(self):
        for count in range(1, len(frame(KEYBOARD))):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.reports([frame(KEYBOARD)[:count]])

    def test_empty_stream(self):
        self.assertEqual(self.reports([]), [])

    def test_real_stream(self):
        writer, reader = socket.socketpair()
        with writer, reader:
            writer.sendall(frame(KEYBOARD) + frame(MOUSE))
            writer.shutdown(socket.SHUT_WR)
            self.assertEqual(list(read_reports(reader)), [KEYBOARD, MOUSE])


class RelayClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import without evdev or opening input devices; only exercise transport.
        path = Path(__file__).resolve().parents[1] / "input" / "relay.py"
        names = sorted(set(re.findall(r"e\.([A-Z][A-Z0-9_]+)", path.read_text())))
        fake = types.ModuleType("evdev")
        fake.ecodes = types.SimpleNamespace(**dict(zip(names, range(len(names)))))
        spec = importlib.util.spec_from_file_location("relay_under_test", path)
        cls.relay = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"evdev": fake}):
            spec.loader.exec_module(cls.relay)

    def test_non_root_server_closed_before_input(self):
        client = Mock()
        with patch.object(self.relay.socket, "socket", return_value=client), \
             patch.object(self.relay, "peer_uid", return_value=1000):
            with self.assertRaises(PermissionError):
                self.relay.connect_server(retries=1, delay=0)
        client.close.assert_called_once()
        client.sendall.assert_not_called()

    def test_root_server_accepted(self):
        client = Mock()
        with patch.object(self.relay.socket, "socket", return_value=client), \
             patch.object(self.relay, "peer_uid", return_value=0), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(self.relay.connect_server(retries=1, delay=0), client)
        client.close.assert_not_called()

    def test_failed_connect_closes_socket_each_retry(self):
        clients = [Mock(), Mock()]
        for client in clients:
            client.connect.side_effect = ConnectionRefusedError
        with patch.object(self.relay.socket, "socket", side_effect=clients):
            with self.assertRaises(RuntimeError):
                self.relay.connect_server(retries=2, delay=0)
        for client in clients:
            client.close.assert_called_once()

    def test_socket_creation_error_is_preserved(self):
        with patch.object(self.relay.socket, "socket", side_effect=OSError("descriptors")):
            with self.assertRaisesRegex(OSError, "descriptors"):
                self.relay.connect_server(retries=1, delay=0)


class LoggingTests(unittest.TestCase):
    def test_quiet_by_default_and_when_disabled(self):
        for env in ({}, {"ICONNECT_DEBUG": "0"}, {"ICONNECT_DEBUG": "true"}):
            with patch.dict(os.environ, env, clear=True):
                logger = Mock()
                log_packet(logger, "tx", KEYBOARD)
                logger.assert_not_called()

    def test_debug_only_contains_length(self):
        with patch.dict(os.environ, {"ICONNECT_DEBUG": "1"}):
            logger = Mock()
            log_packet(logger, "tx", KEYBOARD)
            logger.assert_called_once_with("tx length=9")


@unittest.skipIf(server is None, "Python D-Bus/GObject bindings not installed")
class ServerTests(unittest.TestCase):
    def test_startup_failure_precedes_bluetooth_configuration(self):
        with patch.object(server.os, "geteuid", return_value=0), \
             patch.object(server, "relay_account", side_effect=RuntimeError("unset")), \
             patch.object(server, "configure_adapter") as configure:
            with self.assertRaises(RuntimeError):
                server.main()
            configure.assert_not_called()

    def test_socket_failure_precedes_bluetooth_configuration(self):
        with patch.object(server.os, "geteuid", return_value=0), \
             patch.object(server, "relay_account"), \
             patch.object(server, "RelaySocket", side_effect=PermissionError("owner")), \
             patch.object(server, "configure_adapter") as configure:
            with self.assertRaises(PermissionError):
                server.main()
            configure.assert_not_called()

    def test_handler_forwards_only_valid_frames(self):
        intr = Mock()
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.recv.side_effect = [frame(KEYBOARD), b"\x00"]
        with contextlib.redirect_stdout(io.StringIO()):
            server._handle_relay(client, intr)
        intr.send.assert_called_once_with(b"\xa1" + KEYBOARD)

    def test_unauthorized_peer_rejected_before_thread(self):
        client = Mock()
        srv = Mock()
        srv.accept.side_effect = [(client, None), KeyboardInterrupt]
        with patch.object(server, "peer_uid", return_value=999), \
             patch.object(server.threading, "Thread") as worker, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                server.relay_unix_socket(Mock(), srv, 1000)
        client.close.assert_called_once()
        worker.assert_not_called()

    def test_send_debug_does_not_disclose_keyboard(self):
        listener = object.__new__(server.L2CAPListener)
        listener._lock = threading.Lock()
        listener.client_sock = Mock()
        listener.label = "intr"
        with patch.dict(os.environ, {"ICONNECT_DEBUG": "1"}), \
             patch.object(server, "log") as logger:
            self.assertTrue(listener.send(b"\xa1" + KEYBOARD))
            logger.assert_called_once_with("intr: tx length=10")
        listener.client_sock = None
        with patch.dict(os.environ, {"ICONNECT_DEBUG": "1"}), \
             patch.object(server, "log") as logger:
            self.assertFalse(listener.send(b"\xa1" + KEYBOARD))
            logger.assert_called_once_with("intr: drop (no client) length=10")


if __name__ == "__main__":
    unittest.main()
