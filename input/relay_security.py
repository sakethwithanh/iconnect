"""Local HID transport security; deliberately independent of BlueZ and evdev."""
from __future__ import annotations

import fcntl
import os
import pwd
import socket
import stat
import struct
from pathlib import Path

DEFAULT_SOCKET = "/run/iconnect/hid.sock"
REPORT_LENGTHS = {1: 9, 2: 5}  # report ID included


def relay_account(environ=None):
    """Resolve the explicitly selected local controller, without permissive fallback."""
    environ = os.environ if environ is None else environ
    name = environ.get("ICONNECT_USER") or environ.get("SUDO_USER")
    if not name:
        raise RuntimeError("Set ICONNECT_USER to the local account allowed to control the phone")
    try:
        account = pwd.getpwnam(name)
    except KeyError as exc:
        raise RuntimeError(f"Unknown ICONNECT_USER account: {name}") from exc
    if account.pw_uid == 0:
        raise RuntimeError("ICONNECT_USER must be a non-root local account")
    return account


def log_packet(logger, label: str, payload: bytes) -> None:
    """Opt-in diagnostics without raw keycodes, pointer values, or payload bytes."""
    if os.environ.get("ICONNECT_DEBUG", "0") == "1":
        logger(f"{label} length={len(payload)}")


def peer_uid(sock: socket.socket) -> int:
    """Linux kernel credentials of a connected Unix peer, never a client claim."""
    credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                  struct.calcsize("3i"))
    return struct.unpack("3i", credentials)[1]


def _check_directory(path: Path, *, final: bool) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
        raise RuntimeError(f"Unsafe socket directory owner or type: {path}")
    if info.st_mode & 0o022:
        # A root-owned sticky ancestor (e.g. /tmp in tests) cannot rename a
        # service-owned child. The actual socket directory must never be writable.
        if final or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
            raise RuntimeError(f"Socket directory is writable by other users: {path}")


def _prepare_directory(path: Path) -> None:
    # Check each existing ancestor from root down. No symlinks (including /var/run)
    # or untrusted owners: otherwise a parent could be replaced around bind/chown.
    for ancestor in reversed(path.parents):
        _check_directory(ancestor, final=False)
    try:
        path.mkdir(mode=0o755)
    except FileExistsError:
        pass
    else:
        # mkdir respects the inherited umask. The selected non-root account
        # needs traversal even when the root launcher uses umask 077.
        os.chmod(path, 0o755)
    _check_directory(path, final=True)


class RelaySocket:
    """A fail-closed, single-instance listener in a protected service directory.

    Create before starting threads: the temporary umask is process-wide.
    main() runs this as root; the same code can be tested as an ordinary user.
    """
    def __init__(self, path: str, account):
        self.path = Path(path)
        self.sock = None
        self._lock_fd = None
        self._identity = None
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise RuntimeError("ICONNECT_SOCK must be an absolute path without '..'")
        _prepare_directory(self.path.parent)
        try:
            self._lock_fd = os.open(str(self.path) + ".lock",
                                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            lock_info = os.fstat(self._lock_fd)
            if (not stat.S_ISREG(lock_info.st_mode)
                    or lock_info.st_uid != os.geteuid()
                    or lock_info.st_mode & 0o077):
                raise RuntimeError("Unsafe relay lock file")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                old = self.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(old.st_mode) or old.st_uid not in (os.geteuid(), account.pw_uid):
                    raise RuntimeError("Refusing to replace an unexpected relay socket path")
                self.path.unlink()
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old_umask = os.umask(0o177)
            try:
                self.sock.bind(str(self.path))
            finally:
                os.umask(old_umask)
            info = self.path.lstat()
            self._identity = (info.st_dev, info.st_ino)
            os.chmod(self.path, 0o600)
            os.chown(self.path, account.pw_uid, account.pw_gid)
            self.sock.listen(4)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        try:
            if self._identity is not None:
                try:
                    info = self.path.lstat()
                    if (info.st_dev, info.st_ino) == self._identity:
                        self.path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            self._identity = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def read_reports(client: socket.socket):
    """Yield only descriptor-sized reports; reject malformed/truncated streams."""
    buf = b""
    while True:
        chunk = client.recv(4096)
        if not chunk:
            if buf:
                raise ValueError("Truncated HID frame")
            return
        buf += chunk
        while buf:
            length = buf[0]
            if length not in REPORT_LENGTHS.values():
                raise ValueError("Invalid HID frame length")
            if len(buf) < length + 1:
                break
            report = buf[1:length + 1]
            if REPORT_LENGTHS.get(report[0]) != length:
                raise ValueError("Unknown HID report or mismatched length")
            if report[0] == 1 and report[2] != 0:
                raise ValueError("Invalid keyboard reserved byte")
            if report[0] == 2 and report[1] & ~0x07:
                raise ValueError("Invalid mouse button bits")
            yield report
            buf = buf[length + 1:]
