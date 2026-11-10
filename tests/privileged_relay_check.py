#!/usr/bin/env python3
"""Opt-in, disposable root-server/non-root-client transport integration check.

No Bluetooth sockets, services, input devices, phone, network, or real input data.
Uses existing local accounts only and changes permissions only inside its own
new temporary directory. Never invoke btkbd_server.main().
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import select
import signal
import socket
import stat
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_REPORT = b"\x01" + b"\x00" * 8  # all keys released, no typed text
EXPECTED = b"\xa1" + SYNTHETIC_REPORT


def run_client(path, account, expectation, close_fds):
    """Drop identity in a short-lived child only; never create/change an account."""
    pid = os.fork()
    if pid == 0:
        try:
            for fd in close_fds:
                os.close(fd)
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                try:
                    client.connect(str(path))
                except PermissionError:
                    if expectation == "filesystem-denial":
                        os._exit(0)
                    raise
                if expectation == "filesystem-denial":
                    raise AssertionError("Socket mode did not deny the second UID")
                from relay_security import peer_uid
                if peer_uid(client) != 0:
                    raise AssertionError("Client did not see a root server")
                try:
                    client.sendall(bytes([len(SYNTHETIC_REPORT)]) + SYNTHETIC_REPORT)
                    client.shutdown(socket.SHUT_WR)
                    if client.recv(1):
                        raise AssertionError("Unexpected server response")
                except (ConnectionResetError, BrokenPipeError):
                    if expectation != "peer-denial":
                        raise
            os._exit(0)
        except BaseException as exc:
            print(f"Client check failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            os._exit(1)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            if os.waitstatus_to_exitcode(status) != 0:
                raise AssertionError("Client subprocess failed")
            return
        time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    raise TimeoutError("Client subprocess exceeded five seconds")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="existing non-root controller account")
    parser.add_argument("--denied-user", default="nobody", help="existing second non-root account")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run this optional check through sudo; no sudoers changes are needed")
    allowed = pwd.getpwnam(args.user)
    denied = pwd.getpwnam(args.denied_user)
    if not allowed.pw_uid or not denied.pw_uid or allowed.pw_uid == denied.pw_uid:
        parser.error("Two distinct existing non-root accounts are required")

    sys.path.insert(0, str(ROOT / "input"))
    from relay_security import RelaySocket
    import btkbd_server  # Import definitions only, never start Bluetooth/main().

    read_fd, write_fd = os.pipe()
    server_pid = None
    old_mask = os.umask(0o077)
    def timeout(*_):
        raise TimeoutError("Privileged check exceeded thirty seconds")
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(30)
    try:
        with tempfile.TemporaryDirectory(prefix="iconnect-uid-check-") as temporary:
            os.chmod(temporary, 0o755)
            path = Path(temporary) / "service" / "hid.sock"
            with RelaySocket(str(path), allowed) as listener:
                info = path.stat()
                assert stat.S_IMODE(info.st_mode) == 0o600
                assert info.st_uid == allowed.pw_uid
                assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
                assert path.parent.stat().st_uid == 0
                server_pid = os.fork()
                if server_pid == 0:
                    os.close(read_fd)
                    class FakeBluetoothSink:
                        def send(self, data):
                            os.write(write_fd, data)
                            return True
                    try:
                        btkbd_server.relay_unix_socket(FakeBluetoothSink(), listener.sock,
                                                      allowed.pw_uid)
                    finally:
                        os._exit(0)
                os.close(write_fd)
                write_fd = None
                try:
                    run_client(path, allowed, "allowed", (listener.sock.fileno(), read_fd))
                    if not select.select([read_fd], [], [], 3)[0]:
                        raise AssertionError("Authorized synthetic report was not forwarded")
                    if os.read(read_fd, 4096) != EXPECTED:
                        raise AssertionError("Unexpected synthetic report")
                    print("PASS: selected non-root client connected to root server and forwarded only synthetic release report", flush=True)
                    run_client(path, denied, "filesystem-denial", (listener.sock.fileno(), read_fd))
                    print("PASS: socket mode 0600 denied the second existing UID", flush=True)
                    # Deliberately widen THIS disposable socket only. Peer credentials
                    # must still reject the unrelated UID despite filesystem access.
                    os.chmod(path, 0o666)
                    run_client(path, denied, "peer-denial", (listener.sock.fileno(), read_fd))
                    if select.select([read_fd], [], [], 0.3)[0]:
                        raise AssertionError("Unauthorized report reached the fake sink")
                    print("PASS: kernel peer-UID check rejected second UID after temporary socket mode was widened", flush=True)
                finally:
                    os.kill(server_pid, signal.SIGTERM)
                    os.waitpid(server_pid, 0)
                    server_pid = None
            assert not path.exists(), "Disposable socket was not removed"
        print("PASS: disposable directory removed; no services, devices, or settings changed", flush=True)
    finally:
        if server_pid is not None:
            os.kill(server_pid, signal.SIGKILL)
            os.waitpid(server_pid, 0)
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        os.umask(old_mask)
        signal.alarm(0)


if __name__ == "__main__":
    main()
