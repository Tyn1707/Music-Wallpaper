"""Embeds a Qt window behind the desktop icons (WorkerW trick) and manages it.

Desktop window trees differ by Windows build:

  Windows 11 24H2+                         Older builds
  Progman                                  Progman (+ SHELLDLL_DefView in some setups)
   ├─ SHELLDLL_DefView (icons)             WorkerW  (contains SHELLDLL_DefView)
   └─ WorkerW   <- we parent here          WorkerW  <- we parent here (sibling behind icons)

Sending the undocumented message 0x052C to Progman makes Explorer create the
WorkerW. Nothing on disk or in the registry is changed; on exit the current
wallpaper is simply re-applied for this session so the desktop repaints.
"""
from __future__ import annotations

import time

import win32api
import win32con
import win32gui
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLWindow
from PyQt6.QtCore import Qt

from config import WallpaperConfig

WM_SPAWN_WORKER = 0x052C
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _to_signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x1_0000_0000 if value & 0x8000_0000 else value


def _find(parent: int, after: int, cls: str) -> int:
    try:
        return win32gui.FindWindowEx(parent, after, cls, None) or 0
    except win32gui.error:
        return 0


class WallpaperWindow(QOpenGLWindow):
    """GPU-backed native window; the active visual's `paint_gl(window)` draws each frame."""

    def __init__(self, visual) -> None:
        super().__init__(QOpenGLWindow.UpdateBehavior.NoPartialUpdate)
        self.visual = visual
        self.setFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        fmt = QSurfaceFormat()
        fmt.setSwapInterval(1)  # vsync: no tearing, no wasted frames
        self.setFormat(fmt)

    def paintGL(self) -> None:
        self.visual.paint_gl(self)


class WallpaperManager:
    def __init__(self, cfg: WallpaperConfig) -> None:
        self.cfg = cfg
        self.hwnd = 0
        self.workerw = 0
        self._last_rect: tuple[int, int, int, int] | None = None
        self._wallpaper_path = self._current_wallpaper()

    # ------------------------------------------------------------------ geometry
    def target_rect(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) in physical screen pixels."""
        if self.cfg.span_all_monitors:
            return (
                win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN),
                win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN),
                win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN),
                win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN),
            )
        left, top, right, bottom = self._primary_monitor_rect()
        return left, top, right - left, bottom - top

    @staticmethod
    def _primary_monitor_rect() -> tuple[int, int, int, int]:
        mon = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
        return tuple(win32api.GetMonitorInfo(mon)["Monitor"])

    # ------------------------------------------------------------------ embedding
    def attach(self, hwnd: int, layered: bool | None = None) -> None:
        workerw = self._find_workerw()
        if not workerw:
            raise RuntimeError(
                "Could not find the desktop WorkerW window. Is Explorer running?"
            )
        self.hwnd, self.workerw = hwnd, workerw

        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & 0xFFFFFFFF
        style &= ~(win32con.WS_POPUP | win32con.WS_CAPTION | win32con.WS_THICKFRAME
                   | win32con.WS_SYSMENU | win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX)
        style |= win32con.WS_CHILD | win32con.WS_VISIBLE
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _to_signed32(style))

        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & 0xFFFFFFFF
        ex &= ~win32con.WS_EX_APPWINDOW
        ex |= win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _to_signed32(ex))

        win32gui.SetParent(hwnd, workerw)

        # 24H2+ ("raised desktop"): Progman is composed by DWM and a plain child
        # window never reaches the screen. It must be a *layered* child, and the
        # layered style only takes effect if it is applied AFTER SetParent
        # (verified on build 26200: before = invisible, after = visible).
        if layered is None:
            layered = win32gui.GetParent(workerw) != 0
        if layered:
            ex |= win32con.WS_EX_LAYERED
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _to_signed32(ex))
            win32gui.SetLayeredWindowAttributes(hwnd, 0, 255, win32con.LWA_ALPHA)
        self.reposition()

    def reposition(self, force: bool = True) -> None:
        rect = self.target_rect()
        if not force and rect == self._last_rect:
            return
        self._last_rect = rect
        left, top, width, height = rect
        x, y = win32gui.ScreenToClient(self.workerw, (left, top))
        win32gui.SetWindowPos(
            self.hwnd, 0, x, y, width, height,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
            | win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW,
        )

    def is_healthy(self) -> bool:
        """False if Explorer restarted / the WorkerW vanished / we got unparented."""
        return (
            bool(self.hwnd)
            and win32gui.IsWindow(self.workerw)
            and win32gui.IsWindow(self.hwnd)
            and win32gui.GetParent(self.hwnd) == self.workerw
        )

    def window_alive(self) -> bool:
        return bool(self.hwnd) and bool(win32gui.IsWindow(self.hwnd))

    def _find_workerw(self) -> int:
        progman = _find(0, 0, "Progman")
        if not progman:
            return 0
        for wparam, lparam in ((0, 0), (0xD, 0x1)):
            for _ in range(10):  # Explorer creates the WorkerW asynchronously
                try:
                    win32gui.SendMessageTimeout(
                        progman, WM_SPAWN_WORKER, wparam, lparam,
                        win32con.SMTO_NORMAL, 1000,
                    )
                except win32gui.error:
                    pass
                workerw = self._locate_workerw(progman)
                if workerw:
                    return workerw
                time.sleep(0.1)
        return 0

    @staticmethod
    def _locate_workerw(progman: int) -> int:
        # 24H2+: WorkerW is a direct child of Progman.
        child = _find(progman, 0, "WorkerW")
        if child:
            return child
        # Legacy: the top-level WorkerW right after the one hosting SHELLDLL_DefView.
        found = [0]

        def enum(hwnd: int, _):
            if _find(hwnd, 0, "SHELLDLL_DefView"):
                found[0] = _find(0, hwnd, "WorkerW")
                return found[0] == 0  # stop enumerating once found
            return True

        try:
            win32gui.EnumWindows(enum, None)
        except win32gui.error:
            pass  # EnumWindows reports an "error" when the callback stops it early
        return found[0]

    # ------------------------------------------------------------------ occlusion
    def desktop_covered(self) -> bool:
        """True if the foreground window is maximised/fullscreen on the primary monitor."""
        fg = win32gui.GetForegroundWindow()
        if not fg or fg == self.hwnd:
            return False
        try:
            if win32gui.GetClassName(fg) in SHELL_CLASSES:
                return False
            if win32gui.IsIconic(fg) or not win32gui.IsWindowVisible(fg):
                return False
            primary = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
            if win32api.MonitorFromWindow(fg, win32con.MONITOR_DEFAULTTONEAREST) != primary:
                return False
            if win32gui.GetWindowPlacement(fg)[1] == win32con.SW_SHOWMAXIMIZED:
                return True
            l, t, r, b = win32gui.GetWindowRect(fg)
            ml, mt, mr, mb = self._primary_monitor_rect()
            return l <= ml and t <= mt and r >= mr and b >= mb
        except win32gui.error:
            return False

    # ------------------------------------------------------------------ cleanup
    @staticmethod
    def _current_wallpaper() -> str:
        try:
            return win32gui.SystemParametersInfo(win32con.SPI_GETDESKWALLPAPER) or ""
        except win32gui.error:
            return ""

    def restore_wallpaper(self) -> None:
        """Re-apply the current wallpaper (fWinIni=0: nothing is written to disk)."""
        try:
            win32gui.SystemParametersInfo(win32con.SPI_SETDESKWALLPAPER, self._wallpaper_path, 0)
        except win32gui.error:
            pass
