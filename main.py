"""Music Reactive Live Wallpaper - entry point.

Pipeline: AudioCapture -> FFTAnalyzer -> visual (LiquidGradient | ParticleSystem | RadialSpectrum) -> WallpaperManager
Extras:   tray icon, settings window, autostart, Spotify album-art colours

Run:   python main.py [--theme neon] [--all-monitors] [--settings] [--list-themes]
       pythonw main.py   (no console; logs go to data\\app.log)
Stop:  tray icon -> Quit, or Ctrl+C in the terminal
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path

import win32api
import win32event
import win32process
import winerror
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication

import album_art
from audio_capture import AudioCapture
from fft_analyzer import FFTAnalyzer
from liquid_gradient import LiquidGradient
from particle_system import ParticleSystem
from radial_spectrum import RadialSpectrum
from settings import APP_DIR, SETTINGS_PATH, Settings
from settings_ui import SettingsWindow
from themes import THEMES
from tray import TrayController, make_app_icon
from wallpaper_manager import WallpaperManager, WallpaperWindow

MUTEX_NAME = "Local\\MusicWallpaper.SingleInstance"
SHOW_EVENT_NAME = "Local\\MusicWallpaper.ShowSettings"  # set by a 2nd launch -> open settings
LOG_PATH = APP_DIR / "app.log"


class App:
    def __init__(self, settings: Settings, first_run: bool, open_settings: bool) -> None:
        self.qt = QApplication(sys.argv)
        self.qt.setQuitOnLastWindowClosed(False)
        self.qt.setApplicationName("Music Wallpaper")
        self.icon = make_app_icon()
        self.qt.setWindowIcon(self.icon)

        self.settings = settings
        self.cfg = settings.to_config()

        self.audio = AudioCapture(self.cfg.audio)
        self.audio.start()
        print(f"[audio] capturing '{self.audio.device_name}' "
              f"@ {self.audio.sample_rate} Hz, {self.audio.channels} ch")

        self.analyzer = FFTAnalyzer(self.cfg.fft, self.audio.sample_rate)
        self.album_theme = None
        self.album_cover = None
        self.album_title = ""
        self.album_artist = ""
        self.visual = self._make_visual(self.cfg.render.visual_style)
        print(f"[visual] {self.visual.name}")
        self.manager = WallpaperManager(self.cfg.wallpaper)
        self.window: WallpaperWindow | None = None
        self._create_window()

        self.user_paused = False
        self.covered = False
        self._last = time.perf_counter()

        # Album art
        self.album_theme = None
        self.album_track = ""
        self.album_title = ""
        self.album_artist = ""
        self.album_cover: bytes | None = None
        self.album = album_art.AlbumArtWatcher()
        self.album.theme_changed.connect(self._on_album_theme, Qt.ConnectionType.QueuedConnection)
        self.album.track_changed.connect(self._on_album_track, Qt.ConnectionType.QueuedConnection)
        self.album.cover_changed.connect(self._on_album_cover, Qt.ConnectionType.QueuedConnection)
        self.album.now_playing.connect(self._on_now_playing, Qt.ConnectionType.QueuedConnection)

        # UI
        self.settings_window = SettingsWindow(settings, self.icon, self.status_text, album_art.AVAILABLE)
        self.settings_window.settings_changed.connect(self.apply_settings)
        self.tray = TrayController(
            self.icon,
            on_settings=self.settings_window.open,
            on_pause=self._set_user_paused,
            on_theme=lambda name: self._change(theme=name),
            on_album=lambda on: self._change(album_sync=on),
            on_quit=self.qt.quit,
            album_available=album_art.AVAILABLE,
        )
        self.tray.show()

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save)

        # Timers
        self.frame_timer = QTimer()
        self.frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.frame_timer.setInterval(max(1, round(1000 / self.cfg.render.target_fps)))
        self.frame_timer.timeout.connect(self._tick)
        self.frame_timer.start()

        self.maint_timer = QTimer()
        self.maint_timer.setInterval(int(self.cfg.wallpaper.maintenance_interval * 1000))
        self.maint_timer.timeout.connect(self._maintenance)
        self.maint_timer.start()

        self.fps = 0.0
        self._fps_frames = 0
        self._fps_wall = time.perf_counter()
        self.status_timer = QTimer()
        self.status_timer.setInterval(1000)
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start()

        self._frames = 0
        self._stats_beats = 0
        self._stats_wall = time.perf_counter()
        self._stats_cpu = time.process_time()
        self._ncpu = os.cpu_count() or 1
        if self.cfg.render.print_stats:
            self.stats_timer = QTimer()
            self.stats_timer.setInterval(int(self.cfg.render.stats_interval * 1000))
            self.stats_timer.timeout.connect(self._print_stats)
            self.stats_timer.start()

        # Let Python handle Ctrl+C while Qt's event loop runs; the same timer also
        # listens for a second launch asking us to show the settings window.
        signal.signal(signal.SIGINT, lambda *_: self.qt.quit())
        self._show_event = win32event.CreateEvent(None, False, False, SHOW_EVENT_NAME)
        self._signal_pump = QTimer()
        self._signal_pump.timeout.connect(self._poll_show_request)
        self._signal_pump.start(200)

        print(f"[theme] {settings.theme}{' + album art' if settings.album_sync else ''}")
        if settings.album_sync:
            self._set_album_sync(True)
        self._sync_tray()
        if first_run:
            self.tray.notify("Music Wallpaper is running",
                             "Find the icon under ^ next to the clock. Click it for settings, "
                             "or run main.py again.")
        if open_settings or first_run:
            self.settings_window.open()

    # ------------------------------------------------------------------ settings
    def _change(self, **changes) -> None:
        """Change from the tray menu: apply and mirror into the settings window."""
        new = replace(self.settings, **changes).normalised()
        self.settings_window.set_settings(new)
        self.apply_settings(new)

    def apply_settings(self, new: Settings) -> None:
        old, self.settings = self.settings, new
        cfg = new.to_config()
        self.cfg = cfg
        self.analyzer.cfg = cfg.fft
        self.visual.c = cfg.colors
        self.visual.i = cfg.idle
        if isinstance(self.visual, ParticleSystem):
            self.visual.p = cfg.particles
            self.visual.set_max_particles(cfg.particles.max_particles)
        elif isinstance(self.visual, RadialSpectrum):
            self.visual.r = cfg.radial
        else:
            self.visual.l = cfg.liquid
        if old.visual_style != new.visual_style:
            self._switch_visual(new.visual_style)
        self.manager.cfg = cfg.wallpaper
        if old.span_all_monitors != new.span_all_monitors:
            self.manager.reposition(force=True)
        if old.album_sync != new.album_sync:
            self._set_album_sync(new.album_sync)
        elif old.theme != new.theme and not (new.album_sync and self.album_theme):
            self.visual.set_theme(new.theme)
        self._apply_frame_rate()
        self._sync_tray()
        self._save_timer.start()

    def _save(self) -> None:
        try:
            self.settings.save()
        except OSError as exc:
            print(f"[settings] save failed: {exc}")

    def _set_album_sync(self, enabled: bool) -> None:
        if enabled and album_art.AVAILABLE:
            self.album.start()
            self.settings_window.set_album_status("Waiting for Spotify…")
            print("[album] watching Spotify")
        else:
            self.album.stop()
            self.album_theme, self.album_track = None, ""
            self.album_cover = None
            self.album_title, self.album_artist = "", ""
            self._apply_cover()
            self._apply_track()
            self.settings_window.set_album_status("")
            self.visual.set_theme(self.settings.theme)

    def _on_album_theme(self, theme) -> None:
        if not self.settings.album_sync:
            return
        self.album_theme = theme
        self.visual.set_theme(theme if theme is not None else self.settings.theme)
        if theme is None:
            self.settings_window.set_album_status("Spotify isn't playing. Using the selected theme.")

    def _on_album_track(self, text: str) -> None:
        self.album_track = text
        if text and self.settings.album_sync:
            self.settings_window.set_album_status(f"Now matching: {text}")
            print(f"[album] {text}")

    def _on_album_cover(self, data) -> None:
        self.album_cover = data if self.settings.album_sync else None
        self._apply_cover()

    def _apply_cover(self) -> None:
        """Push the current album cover to the radial visual (ignored by others)."""
        if isinstance(self.visual, RadialSpectrum):
            self.visual.set_cover(self.album_cover)

    def _on_now_playing(self, title: str, artist: str) -> None:
        if self.settings.album_sync:
            self.album_title, self.album_artist = title, artist
        else:
            self.album_title, self.album_artist = "", ""
        self._apply_track()

    def _apply_track(self) -> None:
        """Push the current title/artist to the radial visual (ignored by others)."""
        if isinstance(self.visual, RadialSpectrum):
            self.visual.set_track(self.album_title, self.album_artist)

    def _set_user_paused(self, paused: bool) -> None:
        self.user_paused = paused
        print("[render] paused from tray" if paused else "[render] resumed from tray")
        self._sync_tray()

    def _poll_show_request(self) -> None:
        if win32event.WaitForSingleObject(self._show_event, 0) == win32event.WAIT_OBJECT_0:
            self.settings_window.open()

    # ------------------------------------------------------------------ status
    @property
    def paused(self) -> bool:
        return self.user_paused or self.covered

    def status_text(self) -> str:
        if self.user_paused:
            return "Paused"
        if self.covered:
            return "Paused · desktop covered"
        if self.visual.idle >= 1.0:
            return f"Idle · waiting for audio · {self.fps:.0f} fps"
        bpm = f"{self.analyzer.bpm:.0f} BPM" if self.analyzer.bpm else "detecting tempo"
        return f"Live · {bpm} · {self.fps:.0f} fps"

    def _sync_tray(self) -> None:
        self.tray.sync(self.settings.theme, self.settings.album_sync, self.user_paused, self.status_text())

    def _update_status(self) -> None:
        now = time.perf_counter()
        self.fps = self._fps_frames / max(now - self._fps_wall, 1e-6)
        self._fps_frames, self._fps_wall = 0, now
        self._sync_tray()

    # ------------------------------------------------------------------ visuals
    def _make_visual(self, style: str):
        cfg = self.cfg
        if style == "particles":
            visual = ParticleSystem(cfg.particles, cfg.colors, cfg.idle)
        elif style == "radial":
            visual = RadialSpectrum(cfg.radial, cfg.colors, cfg.idle)
        else:
            visual = LiquidGradient(cfg.liquid, cfg.colors, cfg.idle)
        if self.settings.album_sync and self.album_theme is not None:
            visual.set_theme(self.album_theme)
        if isinstance(visual, RadialSpectrum) and self.settings.album_sync and self.album_cover:
            visual.set_cover(self.album_cover)
        if isinstance(visual, RadialSpectrum) and self.settings.album_sync and self.album_title:
            visual.set_track(self.album_title, self.album_artist)
        return visual

    def _switch_visual(self, style: str) -> None:
        old = self.visual
        self.visual = self._make_visual(style)
        self.visual.hue, self.visual.time = old.hue, old.time  # seamless colours
        self.visual.beats = old.beats
        self._apply_cover()
        self._apply_track()
        if self.window is not None:
            self.window.visual = self.visual
        print(f"[visual] {self.visual.name}")

    # ------------------------------------------------------------------ window
    def _create_window(self) -> None:
        if self.window is not None:
            self.window.destroy()
        self.window = WallpaperWindow(self.visual)
        # Place it on the target first so the brief pre-embed frame shows in the right spot.
        left, top, w, h = self.manager.target_rect()
        dpr = self.qt.primaryScreen().devicePixelRatio()
        self.window.setGeometry(int(left / dpr), int(top / dpr), int(w / dpr), int(h / dpr))
        self.window.show()
        self.manager.attach(int(self.window.winId()))
        print(f"[wallpaper] embedded behind desktop icons ({w}x{h})")

    # ------------------------------------------------------------------ loop
    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(now - self._last, self.cfg.render.max_dt)
        self._last = now
        if self.paused or self.window is None:
            return
        samples = self.audio.get_samples(self.cfg.fft.fft_size)
        features = self.analyzer.process(samples, dt)
        self.visual.update(features, dt, self.window.width(), self.window.height())
        self.window.update()
        self._frames += 1
        self._fps_frames += 1
        self._apply_frame_rate()

    def _apply_frame_rate(self) -> None:
        r = self.cfg.render
        fps = min(r.idle_fps, r.target_fps) if self.visual.idle >= 1.0 else r.target_fps
        interval = max(1, round(1000 / fps))
        if self.frame_timer.interval() != interval:
            self.frame_timer.setInterval(interval)

    def _maintenance(self) -> None:
        try:
            if self.audio.check_device():
                self.analyzer.set_sample_rate(self.audio.sample_rate)
                print(f"[audio] switched to '{self.audio.device_name}' @ {self.audio.sample_rate} Hz")
        except Exception as exc:  # keep rendering even if audio re-open fails
            print(f"[audio] device check failed: {exc}")

        if not self.manager.is_healthy():
            print("[wallpaper] desktop changed (Explorer restart?) - re-embedding")
            try:
                if self.manager.window_alive():
                    self.manager.attach(self.manager.hwnd)
                else:
                    self._create_window()
            except RuntimeError as exc:
                print(f"[wallpaper] re-embed failed, retrying later: {exc}")
        else:
            self.manager.reposition(force=False)  # follows resolution / monitor changes

        covered = self.cfg.render.pause_when_covered and self.manager.desktop_covered()
        if covered != self.covered:
            self.covered = covered
            print("[render] paused (desktop covered)" if covered else "[render] resumed")

    def _print_stats(self) -> None:
        wall, cpu = time.perf_counter(), time.process_time()
        span = wall - self._stats_wall
        fps = self._frames / span
        cpu_pct = (cpu - self._stats_cpu) / span / self._ncpu * 100.0
        ram_mb = win32process.GetProcessMemoryInfo(win32api.GetCurrentProcess())["WorkingSetSize"] / 2**20
        ps = self.visual
        bpm = f"{self.analyzer.bpm:5.1f}" if self.analyzer.bpm else "  -  "
        state = "paused" if self.paused else ("idle" if ps.idle >= 1.0 else "live")
        extra = f"particles {ps.alive_count:4d} | " if isinstance(ps, ParticleSystem) else ""
        print(f"[stats] {fps:5.1f} fps | CPU {cpu_pct:4.1f}% | RAM {ram_mb:5.1f} MB | {ps.name} | "
              f"{extra}bass {ps.levels[0]:.2f} mid {ps.levels[1]:.2f} "
              f"high {ps.levels[2]:.2f} | beats {ps.beats - self._stats_beats:3d} | BPM {bpm} | "
              f"{ps.theme.name} | {state}")
        self._frames, self._stats_wall, self._stats_cpu = 0, wall, cpu
        self._stats_beats = ps.beats

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        try:
            return self.qt.exec()
        finally:
            for t in (self.frame_timer, self.maint_timer, self.status_timer, self._save_timer):
                t.stop()
            self._save()
            self.album.stop()
            self.tray.hide()
            if self.window is not None:
                self.window.destroy()
            self.manager.restore_wallpaper()
            self.audio.close()
            print("[app] stopped, wallpaper restored")


# ---------------------------------------------------------------------- startup helpers
def setup_logging() -> None:
    """Under pythonw there is no console: send output to a small log file."""
    no_console = (sys.stdout is None or sys.stderr is None
                  or Path(sys.executable).name.lower() == "pythonw.exe")
    if not no_console:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(errors="replace")  # track titles in any script won't crash prints
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
        LOG_PATH.unlink()
    log = open(LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = log
    print(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===")


def install_excepthook() -> None:
    # PyQt6 aborts on unhandled exceptions in slots unless a custom hook is set.
    def hook(exc_type, exc, tb):
        print("".join(traceback.format_exception(exc_type, exc, tb)), file=sys.stderr)
    sys.excepthook = hook


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Music reactive live wallpaper")
    parser.add_argument("--theme", help=f"colour theme for this run: {', '.join(THEMES)}")
    parser.add_argument("--all-monitors", action="store_true",
                        help="span the whole virtual desktop for this run")
    parser.add_argument("--settings", action="store_true", help="open the settings window on start")
    parser.add_argument("--list-themes", action="store_true", help="print themes and exit")
    return parser.parse_args(argv)


def main() -> int:
    setup_logging()
    install_excepthook()
    args = parse_args(sys.argv[1:])
    if args.list_themes:
        print("\n".join(THEMES))
        return 0
    if args.theme and args.theme.lower() not in THEMES:
        print(f"[error] unknown theme '{args.theme}'. Available: {', '.join(THEMES)}", file=sys.stderr)
        return 2

    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)  # noqa: F841 (held for process lifetime)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        try:
            event = win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
            win32event.SetEvent(event)
            print("[app] already running - opened its settings window")
        except win32api.error:
            print("[app] already running (see the tray icon)", file=sys.stderr)
        return 0

    first_run = not SETTINGS_PATH.exists()
    settings = Settings.load()
    if args.theme:
        settings = replace(settings, theme=args.theme.lower())
    if args.all_monitors:
        settings = replace(settings, span_all_monitors=True)
    if first_run:
        try:
            settings.save()
        except OSError as exc:
            print(f"[settings] save failed: {exc}")

    try:
        app = App(settings, first_run=first_run, open_settings=args.settings)
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print("[app] running - use the tray icon (or Ctrl+C here) to stop")
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
