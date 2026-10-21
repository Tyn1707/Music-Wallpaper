"""System tray icon + menu."""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QObject, QRectF, Qt
from PyQt6.QtGui import QAction, QActionGroup, QColor, QIcon, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from themes import THEMES


def make_app_icon() -> QIcon:
    """Equaliser-bars icon drawn in code (no image files to ship)."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0.0, QColor("#4338CA"))
        grad.setColorAt(1.0, QColor("#1E1B4B"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        p.setBrush(QColor("#22C55E"))
        bar_w = size * 0.14
        gap = size * 0.07
        heights = (0.38, 0.62, 0.48)
        x = (size - (3 * bar_w + 2 * gap)) / 2
        for h in heights:
            bh = size * h
            p.drawRoundedRect(QRectF(x, (size - bh) / 2, bar_w, bh), bar_w / 2, bar_w / 2)
            x += bar_w + gap
        p.end()
        icon.addPixmap(pix)
    return icon


class TrayController(QObject):
    def __init__(
        self,
        icon: QIcon,
        on_settings: Callable[[], None],
        on_pause: Callable[[bool], None],
        on_theme: Callable[[str], None],
        on_album: Callable[[bool], None],
        on_quit: Callable[[], None],
        album_available: bool,
    ) -> None:
        super().__init__()
        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip("Music Wallpaper")
        self._on_settings = on_settings

        menu = QMenu()
        act_settings = menu.addAction("Settings…")
        act_settings.triggered.connect(on_settings)
        menu.setDefaultAction(act_settings)

        self.act_pause = QAction("Pause visuals", menu, checkable=True)
        self.act_pause.toggled.connect(on_pause)
        menu.addAction(self.act_pause)
        menu.addSeparator()

        theme_menu = menu.addMenu("Theme")
        self._theme_group = QActionGroup(theme_menu)
        self._theme_group.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        for name in THEMES:
            act = QAction(name.capitalize(), theme_menu, checkable=True)
            act.triggered.connect(lambda _=False, n=name: on_theme(n))
            self._theme_group.addAction(act)
            theme_menu.addAction(act)
            self.theme_actions[name] = act
        theme_menu.addSeparator()
        self.act_album = QAction("Match Spotify album art", theme_menu, checkable=True)
        self.act_album.setEnabled(album_available)
        self.act_album.toggled.connect(on_album)
        theme_menu.addAction(self.act_album)

        menu.addSeparator()
        act_quit = menu.addAction("Quit")
        act_quit.triggered.connect(on_quit)

        self.menu = menu  # keep a reference; Qt does not own it
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    def notify(self, title: str, message: str) -> None:
        if self.tray.isVisible():
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)

    def sync(self, theme: str, album_sync: bool, user_paused: bool, status: str) -> None:
        """Reflect app state in the menu without re-triggering callbacks."""
        for widget in (self.act_pause, self.act_album, *self.theme_actions.values()):
            widget.blockSignals(True)
        self.theme_actions[theme].setChecked(True)
        self.act_album.setChecked(album_sync)
        self.act_pause.setChecked(user_paused)
        for widget in (self.act_pause, self.act_album, *self.theme_actions.values()):
            widget.blockSignals(False)
        self.tray.setToolTip(f"Music Wallpaper — {status}")

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._on_settings()
