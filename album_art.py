"""Spotify album-art colour sync.

Reads the current track's cover from Windows' media session API (the same data
the volume flyout / lock screen shows), so no Spotify login or network access
is needed. A background thread polls every couple of seconds; when the cover
changes, its dominant hues become a Theme that is emitted to the Qt thread.
"""
from __future__ import annotations

import asyncio
import colorsys
import threading

import numpy as np
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QImage

from themes import Theme

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
    AVAILABLE = True
except ImportError:  # winrt packages not installed
    AVAILABLE = False

SOURCE_MATCH = "spotify"   # matched against the session's app id (e.g. "Spotify.exe")
HUE_BINS = 36
SAMPLE_SIZE = 48


# ---------------------------------------------------------------------- palette
def extract_theme(image_bytes: bytes) -> Theme | None:
    """Build a Theme from the 3 dominant hues of an image."""
    img = QImage.fromData(image_bytes)
    if img.isNull():
        return None
    img = img.scaled(SAMPLE_SIZE, SAMPLE_SIZE, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    bgra = np.frombuffer(ptr, dtype=np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)
    rgb = bgra[:, : img.width(), 2::-1].reshape(-1, 3).astype(np.float32) / 255.0
    return theme_from_rgb(rgb)


def theme_from_rgb(rgb: np.ndarray) -> Theme:
    mx, mn = rgb.max(axis=1), rgb.min(axis=1)
    delta = mx - mn
    sat = np.where(mx > 0, delta / np.maximum(mx, 1e-6), 0.0)
    val = mx

    # Vectorised RGB -> hue.
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    d = np.maximum(delta, 1e-6)
    hue = np.select(
        [mx == r, mx == g],
        [((g - b) / d) % 6.0, (b - r) / d + 2.0],
        (r - g) / d + 4.0,
    ) / 6.0

    colourful = (sat > 0.2) & (val > 0.15)
    if colourful.sum() < 20:
        # Black-and-white / very muted cover: soft monochrome palette.
        return Theme("album", (0.6, 0.6, 0.6), saturation=0.15, wobble=0.01, shift=0.0,
                     background_hue=0.6, background_saturation=0.2)

    weight = (sat * val)[colourful]
    hist, _ = np.histogram(hue[colourful], bins=HUE_BINS, range=(0.0, 1.0), weights=weight)
    hist = np.roll(hist, 1) + 2 * hist + np.roll(hist, -1)  # circular smoothing

    hues: list[float] = []
    work = hist.copy()
    top = work.max()
    for _ in range(3):
        i = int(work.argmax())
        if hues and work[i] < 0.1 * top:
            break
        hues.append((i + 0.5) / HUE_BINS)
        for k in range(-3, 4):  # suppress neighbours so peaks are distinct
            work[(i + k) % HUE_BINS] = 0.0
    while len(hues) < 3:  # fill with analogous hues
        hues.append((hues[0] + (0.07 if len(hues) == 1 else -0.07)) % 1.0)

    saturation = float(np.clip(np.median(sat[colourful]) * 1.15, 0.45, 0.95))
    return Theme("album", tuple(hues), saturation=saturation, wobble=0.02, shift=0.03,
                 background_hue=hues[0], background_saturation=0.6)


def theme_preview_rgb(theme: Theme) -> list[tuple[float, float, float]]:
    return [colorsys.hsv_to_rgb(h, theme.saturation, 1.0) for h in theme.band_hues]


# ---------------------------------------------------------------------- watcher
class AlbumArtWatcher(QObject):
    """Emits `theme_changed(Theme | None)`; None = no Spotify session (use fallback)."""

    theme_changed = pyqtSignal(object)
    track_changed = pyqtSignal(str)
    cover_changed = pyqtSignal(object)   # raw image bytes, or None when no session

    def __init__(self, poll_interval: float = 2.0) -> None:
        super().__init__()
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._key: tuple | None = None
        self._had_theme = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not AVAILABLE or self.running:
            return
        self._stop.clear()
        self._key, self._had_theme = None, False
        self._thread = threading.Thread(target=self._run, name="album-art", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # -- worker thread -------------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._poll_loop())
        except Exception as exc:  # never take the app down from this thread
            print(f"[album] watcher stopped: {exc}")

    async def _poll_loop(self) -> None:
        manager = await MediaManager.request_async()
        while not self._stop.is_set():
            try:
                await self._poll_once(manager)
            except Exception as exc:
                print(f"[album] poll failed: {exc}")
            # Sleep in short steps so stop() is responsive.
            for _ in range(int(self.poll_interval / 0.25)):
                if self._stop.is_set():
                    return
                await asyncio.sleep(0.25)

    async def _poll_once(self, manager) -> None:
        session = next(
            (s for s in manager.get_sessions()
             if SOURCE_MATCH in (s.source_app_user_model_id or "").lower()),
            None,
        )
        if session is None:
            if self._had_theme:
                self._key, self._had_theme = None, False
                self.track_changed.emit("")
                self.cover_changed.emit(None)
                self.theme_changed.emit(None)
            return

        props = await session.try_get_media_properties_async()
        key = (props.title, props.artist, props.album_title)
        if key == self._key or props.thumbnail is None:
            return
        data = await self._read_thumbnail(props.thumbnail)
        theme = extract_theme(data) if data else None
        if theme is None:
            return  # cover not ready yet (common right after a skip); retry next poll
        self._key, self._had_theme = key, True
        self.track_changed.emit(f"{props.artist} - {props.title}" if props.artist else props.title)
        self.cover_changed.emit(data)
        self.theme_changed.emit(theme)

    @staticmethod
    async def _read_thumbnail(ref) -> bytes:
        stream = await ref.open_read_async()
        size = int(stream.size)
        if size <= 0 or size > 16 * 2**20:
            return b""
        buf = Buffer(size)
        out = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        return bytes(memoryview(out))
