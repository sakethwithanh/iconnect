#!/usr/bin/env python3
"""
iconnect_gui.py — PyQt6 control panel + tray icon for iconnect.

Toggles the user-scope systemd services (mirror + relay). The system-scope
btkbd daemon stays running so the iPhone Bluetooth link is preserved across
on/off cycles — flipping OFF just stops mirror and grabs input back.

Tray icon shows current state; the panel window has one big toggle button.
Closing the window minimizes to tray (does not quit). Right-click tray →
Quit to exit fully.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QIcon, QPainter, QPixmap, QColor, QBrush
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QLabel, QMenu, QPushButton, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

SINGLE_INSTANCE_KEY = "iconnect-gui-saketh"

USER_UNITS = ["iconnect-mirror.service", "iconnect-relay.service"]
REFRESH_MS = 2500
HERE = Path(__file__).resolve().parent


def iphone_connected() -> bool:
    """True if at least one paired Bluetooth device is currently Connected.
    Uses bluetoothctl (no extra deps); falls back to False on any error."""
    try:
        r = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True, text=True, timeout=2.0,
        )
        return bool(r.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def systemctl_user(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True)


def is_active() -> bool:
    r = systemctl_user("is-active", *USER_UNITS)
    # `is-active` prints one line per unit. ON only when ALL active.
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return bool(lines) and all(ln == "active" for ln in lines)


def make_phone_icon(screen_color: str) -> QIcon:
    """Phone-shaped tray icon whose screen color reflects connection state.
    Green = iPhone connected, Red = not connected. Drawn large/bold so it
    survives shrinking to tray pixel sizes."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QPen

    pix = QPixmap(128, 128)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Phone body — fills most of the frame for legibility at small sizes.
    p.setPen(QPen(QColor("#f0f0f0"), 6))
    p.setBrush(QBrush(QColor("#0a0a0a")))
    p.drawRoundedRect(QRectF(28, 6, 72, 116), 14, 14)

    # Screen — fills body interior; color = connection state.
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(screen_color)))
    p.drawRoundedRect(QRectF(36, 22, 56, 84), 4, 4)

    # Speaker notch
    p.setBrush(QBrush(QColor("#555")))
    p.drawRoundedRect(QRectF(52, 13, 24, 4), 2, 2)

    # Home indicator
    p.setBrush(QBrush(QColor("#888")))
    p.drawRoundedRect(QRectF(48, 113, 32, 4), 2, 2)

    p.end()
    return QIcon(pix)


# Backwards-compatible alias so older callers keep working.
make_dot_icon = make_phone_icon


class Panel(QWidget):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.setWindowTitle("iconnect")

        self.status_label = QLabel("Status: checking…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-size: 14px;")

        self.toggle_btn = QPushButton("Loading…")
        self.toggle_btn.setMinimumHeight(60)
        self.toggle_btn.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.toggle_btn.clicked.connect(self.toggle)

        self.quit_btn = QPushButton("Close App")
        self.quit_btn.setMinimumHeight(34)
        self.quit_btn.setStyleSheet(
            "font-size: 13px; background: #444; color: white;"
        )
        # Close hides window but leaves the tray icon alive — quit only via
        # tray right-click → Quit.
        self.quit_btn.clicked.connect(self.hide)

        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.toggle_btn)
        layout.addWidget(self.quit_btn)
        self.setLayout(layout)
        self.setFixedSize(300, 220)

        # Tray icon — pick green/red depending on state.
        self.tray = QSystemTrayIcon(self)
        menu = QMenu()
        self.act_toggle = QAction("Turn ON", self)
        self.act_toggle.triggered.connect(self.toggle)
        self.act_show = QAction("Show window", self)
        self.act_show.triggered.connect(self.show_and_raise)
        self.act_quit = QAction("Quit", self)
        self.act_quit.triggered.connect(self.quit_app)
        menu.addAction(self.act_toggle)
        menu.addAction(self.act_show)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        Reason = QSystemTrayIcon.ActivationReason
        # Toggle on any click flavor that isn't the context menu — bar
        # implementations vary in which signal they send for a left click.
        if reason in (Reason.Trigger, Reason.DoubleClick, Reason.MiddleClick):
            self.toggle()

    def closeEvent(self, ev) -> None:  # minimize to tray
        ev.ignore()
        self.hide()
        self.tray.showMessage(
            "iconnect", "Running in tray. Right-click → Quit to exit.",
            QSystemTrayIcon.MessageIcon.Information, 2000,
        )

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self.tray.hide()
        self.app.quit()

    def toggle(self) -> None:
        self.toggle_btn.setEnabled(False)
        if is_active():
            systemctl_user("stop", *USER_UNITS)
        else:
            systemctl_user("start", *USER_UNITS)
        QTimer.singleShot(500, self.refresh)

    def refresh(self) -> None:
        on = is_active()
        connected = iphone_connected()

        # Tray icon color is driven by the iPhone link, not the services.
        # Green = phone connected; red = phone offline.
        # Screen color reflects service ON/OFF (user-controlled), not the
        # passive Bluetooth link state.
        screen_color = "#2ecc71" if on else "#e74c3c"
        # Hide+show forces some StatusNotifier hosts (Waybar) to re-pull the
        # pixmap instead of caching the old one.
        new_icon = make_phone_icon(screen_color)
        if getattr(self, "_last_color", None) != screen_color:
            self.tray.hide()
            self.tray.setIcon(new_icon)
            self.tray.show()
            self._last_color = screen_color
        else:
            self.tray.setIcon(new_icon)
        self.tray.setToolTip(
            f"iconnect: {'ON' if on else 'OFF'} · "
            f"iPhone {'connected' if connected else 'disconnected'}"
        )

        link_line = (
            "<span style='color:#2ecc71;'>● iPhone connected</span>"
            if connected else
            "<span style='color:#e74c3c;'>● iPhone disconnected</span>"
        )
        service_line = "ON  (forwarding)" if on else "OFF  (idle)"
        self.status_label.setText(
            f"<div style='font-size:14px;'>Service: <b>{service_line}</b></div>"
            f"<div style='font-size:13px;'>{link_line}</div>"
        )
        self.status_label.setTextFormat(Qt.TextFormat.RichText)

        if on:
            self.toggle_btn.setText("Turn OFF")
            self.toggle_btn.setStyleSheet(
                "font-size: 18px; font-weight: bold; "
                "background: #d33; color: white;"
            )
            self.act_toggle.setText("Turn OFF")
        else:
            self.toggle_btn.setText("Turn ON")
            self.toggle_btn.setStyleSheet(
                "font-size: 18px; font-weight: bold; "
                "background: #2ecc71; color: white;"
            )
            self.act_toggle.setText("Turn ON")

        self.toggle_btn.setEnabled(True)


def try_signal_running_instance() -> bool:
    """If another iconnect-gui is already running, tell it to show its window
    and return True. Otherwise return False."""
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE_KEY)
    if sock.waitForConnected(300):
        sock.write(b"show\n")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def main() -> None:
    if try_signal_running_instance():
        # Already running — raised its window, we're done.
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setApplicationName("iconnect")
    app.setQuitOnLastWindowClosed(False)
    panel = Panel(app)

    # Stale socket file from a crashed prior run blocks listen — remove first.
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_KEY)

    def on_new_connection() -> None:
        client = server.nextPendingConnection()
        if client is None:
            return
        client.readyRead.connect(lambda: (client.readAll(), panel.show_and_raise()))

    server.newConnection.connect(on_new_connection)

    if "--minimized" not in sys.argv:
        panel.show_and_raise()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
