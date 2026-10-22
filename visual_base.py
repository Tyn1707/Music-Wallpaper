"""State shared by every visual style: theme colours, idle blending, beats.

A visual style subclasses `VisualBase`, calls `self.update_common(...)` from its
`update()`, and implements `paint_gl(window)` (called inside paintGL with the
window's OpenGL context current).
"""
from __future__ import annotations

import colorsys
import math

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
)

from config import ColorConfig, IdleConfig, OverlayConfig
from fft_analyzer import AudioFeatures
from themes import Theme, get_theme

BAND_PHASES = np.array([0.0, 2.1, 4.2], dtype=np.float32)  # idle breathing offsets per band
_COVER_RES = 256   # px; the overlay cover is built once at this size, then scaled


def build_cover(image_bytes: bytes, shape: str, corner_frac: float) -> QPixmap | None:
    """Decode album-art bytes into a square pixmap masked to `shape`.

    shape = "circle"       -> round thumbnail (particles)
    shape = "rounded_rect" -> rounded square  (liquid)
    """
    img = QImage.fromData(image_bytes)
    if img.isNull():
        return None
    side = min(img.width(), img.height())           # centre-crop to a square
    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
    img = img.scaled(_COVER_RES, _COVER_RES, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)

    out = QPixmap(_COVER_RES, _COVER_RES)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHints(QPainter.RenderHint.Antialiasing
                         | QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        if shape == "rounded_rect":
            radius = _COVER_RES * max(0.0, min(0.5, corner_frac))
            path.addRoundedRect(0.0, 0.0, float(_COVER_RES), float(_COVER_RES), radius, radius)
        else:
            path.addEllipse(0.0, 0.0, float(_COVER_RES), float(_COVER_RES))
        p.setClipPath(path)
        p.drawImage(0, 0, img)
    finally:
        p.end()
    return out


class VisualBase:
    def __init__(self, ccfg: ColorConfig, icfg: IdleConfig) -> None:
        self.c = ccfg
        self.i = icfg
        self.time = 0.0
        self.hue = 0.0
        self.levels = np.zeros(3, dtype=np.float32)   # effective (audio/idle blended) levels
        self.energy = 0.0
        self.idle = 0.0                               # 0 = music, 1 = fully idle
        self.flash = 0.0                              # 1 on a beat, decays
        self.beats = 0
        self.beat = False
        self.beat_strength = 0.0
        self._silent_for = 0.0
        self.theme: Theme = get_theme(ccfg.theme)

        # Shared "now playing" overlay (used by liquid & particles; radial draws its own).
        self._np_bytes: bytes | None = None       # raw cover bytes (shape built lazily)
        self._np_cover: QPixmap | None = None      # built cover for the current shape
        self._np_shape = ""                        # shape the cached pixmap was built for
        self._np_title = ""
        self._np_artist = ""
        self._np_fade = 0.0                        # 0..1 fade-in of the whole card
        self.overlay = OverlayConfig()             # set per-style by main.py

    # ------------------------------------------------------------------ theme
    def set_theme(self, theme: Theme | str) -> None:
        self.theme = get_theme(theme) if isinstance(theme, str) else theme
        self.on_theme_changed()

    def on_theme_changed(self) -> None:
        """Hook for styles that cache colour-dependent resources."""

    # ------------------------------------------------------------------ per frame
    def update_common(self, f: AudioFeatures, dt: float) -> None:
        th = self.theme
        self.time += dt
        self._silent_for = self._silent_for + dt if f.silent else 0.0
        if self.i.enabled and self._silent_for >= self.i.delay:
            self.idle = min(1.0, self.idle + dt / self.i.fade_in)
        else:
            self.idle = max(0.0, self.idle - dt / self.i.fade_out)
        idle = self.idle

        # Blend real audio with a slow synthetic "breath" while idle.
        breath = 0.5 + 0.5 * np.sin(2.0 * math.pi * self.time / self.i.breath_period + BAND_PHASES)
        synthetic = (self.i.level * breath).astype(np.float32)
        self.levels = f.as_array() * (1.0 - idle) + synthetic * idle
        self.energy = f.energy * (1.0 - idle) + float(synthetic.mean()) * idle

        self.beat = f.beat and idle < 0.5
        self.beat_strength = f.beat_strength if self.beat else 0.0
        if self.beat:
            self.beats += 1
            self.flash = 1.0
            self.hue += th.beat_hue_jump * self.beat_strength
        self.flash *= math.exp(-dt / self.c.beat_flash_decay)
        self.hue = (self.hue + dt * (th.drift + th.energy_drift * self.energy)) % 1.0

        # Fade the now-playing overlay in/out (~0.4 s).
        target = 1.0 if self._np_title else 0.0
        step = dt / 0.4
        self._np_fade = (min(target, self._np_fade + step) if target > self._np_fade
                         else max(target, self._np_fade - step))

    # ------------------------------------------------------------------ now playing
    def set_cover(self, image_bytes: bytes | None) -> None:
        """Set (or clear) the album-cover image used by the overlay."""
        self._np_bytes = image_bytes or None
        self._np_cover = None          # rebuilt lazily for the current shape
        self._np_shape = ""

    def set_track(self, title: str, artist: str) -> None:
        """Set (or clear) the song title and artist shown by the overlay."""
        self._np_title = title or ""
        self._np_artist = artist or ""

    def _overlay_cover(self, shape: str, corner_frac: float) -> QPixmap | None:
        if self._np_bytes is None:
            return None
        if self._np_cover is None or self._np_shape != shape:
            self._np_cover = build_cover(self._np_bytes, shape, corner_frac)
            self._np_shape = shape
        return self._np_cover

    def draw_now_playing_card(self, painter: QPainter, width: int, height: int,
                              o: OverlayConfig) -> None:
        """Centre card: a large album cover with the title + artist stacked below it.

        Centred horizontally and vertically in the middle of the screen.
        Shared by the liquid and particle styles. No-op until a track is set.
        """
        if not o.enabled or self._np_fade <= 0.0 or not self._np_title:
            return
        unit = min(width, height)
        cover_px = max(32.0, unit * o.cover_size)
        fade = self._np_fade

        # Fonts + elided strings.
        title_font = QFont(painter.font())
        title_font.setPixelSize(int(round(max(10.0, unit * o.title_pt))))
        title_font.setWeight(QFont.Weight.Bold)
        artist_font = QFont(painter.font())
        artist_font.setPixelSize(int(round(max(9.0, unit * o.artist_pt))))
        artist_font.setWeight(QFont.Weight.Medium)
        tm, am = QFontMetricsF(title_font), QFontMetricsF(artist_font)

        cover = self._overlay_cover(o.cover_shape, o.corner_radius)
        max_text_w = min(unit * 1.6, width - 2 * unit * o.margin)
        title = tm.elidedText(self._np_title, Qt.TextElideMode.ElideRight, int(max_text_w))
        artist = (am.elidedText(self._np_artist, Qt.TextElideMode.ElideRight, int(max_text_w))
                  if self._np_artist else "")

        # Whole block (cover + gap + text lines) vertically centred on screen.
        line_gap = tm.height() * 0.2
        text_gap = cover_px * 0.12 if cover is not None else 0.0   # gap below the cover
        text_h = tm.height() + (am.height() + line_gap if artist else 0.0)
        block_h = (cover_px + text_gap if cover is not None else 0.0) + text_h
        top = (height - block_h) * 0.5
        cx = width * 0.5

        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(o.opacity * fade)
        if cover is not None:
            painter.drawPixmap(QRectF(cx - cover_px * 0.5, top, cover_px, cover_px),
                               cover, QRectF(cover.rect()))

        fg = QColor.fromRgbF(0.97, 0.98, 0.99)
        painter.setOpacity(1.0)        # text alpha is set per line; don't multiply it with the cover's
        halo = QColor(0, 0, 0, int(90 * fade))
        shadow = QColor(0, 0, 0, int(200 * fade))

        def line(font: QFont, text: str, line_top: float, alpha: float) -> float:
            if not text:
                return line_top
            m = QFontMetricsF(font)
            x = cx - m.horizontalAdvance(text) * 0.5   # centre each line under the cover
            baseline = line_top + m.ascent()
            painter.setFont(font)
            r = max(1.5, font.pixelSize() * 0.07)      # dark halo keeps it readable on bright visuals
            painter.setPen(halo)
            for k in range(8):
                a = k * math.pi / 4
                painter.drawText(QPointF(x + r * math.cos(a), baseline + r * math.sin(a)), text)
            painter.setPen(shadow)
            painter.drawText(QPointF(x + r, baseline + r * 1.4), text)
            fg.setAlphaF(alpha)
            painter.setPen(fg)
            painter.drawText(QPointF(x, baseline), text)
            return line_top + m.height()

        y = top + (cover_px + text_gap if cover is not None else 0.0)
        y = line(title_font, title, y, fade)
        if artist:
            line(artist_font, artist, y + line_gap, 0.9 * fade)
        painter.setOpacity(1.0)

    # ------------------------------------------------------------------ colours
    def band_hues(self) -> np.ndarray:
        """Current hue (0..1) for bass / mid / high."""
        th = self.theme
        bass, _, high = self.levels
        shift = th.shift * (float(high) - float(bass))
        wobble = th.wobble * math.sin(self.time * 0.15)
        return (np.array(th.band_hues, dtype=np.float32) + self.hue + shift + wobble) % 1.0

    def background_rgb(self) -> tuple[float, float, float]:
        th = self.theme
        hue = self.band_hues()[0] if th.background_hue is None else th.background_hue
        value = (self.c.background_value
                 + self.c.background_bass * float(self.levels[0])
                 + self.c.beat_flash * self.flash)
        return colorsys.hsv_to_rgb(float(hue), th.background_saturation, min(1.0, value))

    # ------------------------------------------------------------------ interface
    def update(self, f: AudioFeatures, dt: float, width: float, height: float) -> None:
        raise NotImplementedError

    def paint_gl(self, window) -> None:
        raise NotImplementedError
