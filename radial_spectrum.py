"""Radial spectrum visual: glowing frequency bars around a pulsing ring.

Audio mapping
- Spectrum: one bar per log-spaced band (FFTAnalyzer spectrum), mirrored left /
            right so bass sits at the top and treble meets at the bottom.
- Bass:     the ring swells; beats add a short pulse and brighten the bars.
- Energy:   soft glow around the ring.
- Idle:     a slow breathing wave travels around the ring when silent.
Colours come from the active Theme: hue sweeps bass -> mid -> high along the bars.

Rendering uses QPainter with cached soft-edged sprites (a stretchable bar strip
and a rounded cap per quantised hue), drawn rotated with additive blending.
Sprites get bilinear filtering, so edges stay smooth without MSAA.
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
    QRadialGradient,
    QTransform,
)

from config import ColorConfig, IdleConfig, RadialConfig
from fft_analyzer import AudioFeatures
from visual_base import VisualBase

HUE_STEPS = 64
SPRITE = 32            # px across the bar (core + glow halo)
STRIP_LEN = 4          # px along the bar; stretched to the bar length
CORE_FRAC = 0.3        # core diameter as a share of the sprite width
COVER_RES = 256        # px; circular cover is rendered once at this size, then scaled


def _circular_cover(image_bytes: bytes) -> QPixmap | None:
    """Decode album-art bytes into a circular (alpha-masked) square pixmap."""
    img = QImage.fromData(image_bytes)
    if img.isNull():
        return None
    # Centre-crop to a square, then scale to a fixed working resolution.
    side = min(img.width(), img.height())
    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
    img = img.scaled(COVER_RES, COVER_RES, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)

    out = QPixmap(COVER_RES, COVER_RES)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHints(QPainter.RenderHint.Antialiasing
                         | QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addEllipse(0.0, 0.0, float(COVER_RES), float(COVER_RES))
        p.setClipPath(path)
        p.drawImage(0, 0, img)
    finally:
        p.end()
    return out


def _profile(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Alpha and white-core mix for normalised distance d (0 = centre, 1 = sprite edge)."""
    core = np.clip((CORE_FRAC - d) / 0.1 + 0.5, 0.0, 1.0)     # anti-aliased core edge
    glow = 0.42 * np.clip(1.0 - d, 0.0, 1.0) ** 2.5
    alpha = np.maximum(core, glow)
    white = 0.5 * np.clip((0.14 - d) / 0.1, 0.0, 1.0)          # hot centre line
    return alpha, white


def _to_pixmap(rgb: tuple[float, float, float], alpha: np.ndarray, white: np.ndarray) -> QPixmap:
    col = np.array(rgb, dtype=np.float32)
    c = col + (1.0 - col) * white[..., None]                   # tint towards white in the core
    rgba = np.empty(alpha.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = np.clip(c * alpha[..., None] * 255.0, 0, 255)   # premultiplied
    rgba[..., 3] = np.clip(alpha * 255.0, 0, 255)
    h, w = alpha.shape
    img = QImage(rgba.tobytes(), w, h, 4 * w, QImage.Format.Format_RGBA8888_Premultiplied)
    return QPixmap.fromImage(img)


class RadialSpectrum(VisualBase):
    name = "radial"

    def __init__(self, rcfg: RadialConfig, ccfg: ColorConfig, icfg: IdleConfig) -> None:
        super().__init__(ccfg, icfg)
        self.r = rcfg
        self.bars = np.zeros(0, dtype=np.float32)   # displayed level per band, 0..1
        self._pulse = 0.0
        self._rotation = 0.0
        self._sprites: dict[int, tuple[QPixmap, QPixmap]] = {}
        self._cover: QPixmap | None = None    # circular album cover, or None
        self._cover_fade = 0.0                # 0..1 fade-in of the cover
        self._cover_angle = 0.0               # degrees; cover spin when enabled
        self._title = ""                      # current song title
        self._artist = ""                     # current artist
        self._text_fade = 0.0                 # 0..1 fade-in of the track text

        # Cross-section profiles are colour independent: compute once.
        y = (np.arange(SPRITE, dtype=np.float32) + 0.5) / SPRITE * 2.0 - 1.0
        self._strip_profile = _profile(np.repeat(np.abs(y)[:, None], STRIP_LEN, axis=1))
        x = (np.arange(SPRITE // 2, dtype=np.float32) + 0.5) / (SPRITE // 2)   # right half only
        self._cap_profile = _profile(np.sqrt(x[None, :] ** 2 + y[:, None] ** 2))

    def on_theme_changed(self) -> None:
        self._sprites.clear()  # sprite colours depend on theme saturation

    def set_cover(self, image_bytes: bytes | None) -> None:
        """Set (or clear) the album cover shown inside the ring and as the blurred background."""
        super().set_cover(image_bytes)
        self._cover = _circular_cover(image_bytes) if image_bytes else None
        if self._cover is None:
            self._cover_fade = 0.0

    def set_track(self, title: str, artist: str) -> None:
        """Set (or clear) the song title and artist shown below the ring."""
        self._title = title or ""
        self._artist = artist or ""

    # ================================================================ simulation
    def update(self, f: AudioFeatures, dt: float, width: float, height: float) -> None:
        r = self.r
        self.update_common(f, dt)
        idle = self.idle

        spec = f.spectrum if f.spectrum is not None else np.zeros(max(self.bars.size, 1), np.float32)
        n = spec.size
        if self.bars.size != n:
            self.bars = np.zeros(n, dtype=np.float32)

        # Idle: a slow wave rolling around the ring, quieter towards the treble.
        pos = np.linspace(0.0, 1.0, n, dtype=np.float32)
        wave = 0.5 + 0.5 * np.sin(2.0 * math.pi * (self.time / self.i.breath_period * 1.5 - pos * 1.5))
        synthetic = self.i.level * (0.35 + 0.65 * wave) * (1.0 - 0.5 * pos)
        target = spec * (1.0 - idle) + synthetic * idle
        if n >= 3:  # light neighbour blur: organic shape, no lone single-bin spikes
            target = np.convolve(np.pad(target, 1, mode="edge"), (0.25, 0.5, 0.25), "valid")

        # Bars jump up instantly but fall at a capped speed ("gravity").
        self.bars = np.maximum(target, self.bars - r.fall_speed * dt).astype(np.float32)

        self._pulse = max(self._pulse * math.exp(-dt / r.beat_pulse_decay), self.beat_strength)
        self._rotation = (self._rotation + dt * r.rotate_speed * (1.0 + self.energy)) % 1.0

        # Fade the cover in when present, out when cleared (~0.4 s either way).
        target_fade = 1.0 if (self._cover is not None and r.cover_enabled) else 0.0
        step = dt / 0.4
        self._cover_fade = min(target_fade, self._cover_fade + step) if target_fade > self._cover_fade \
            else max(target_fade, self._cover_fade - step)

        # Fade the track text the same way (needs a title and the feature enabled).
        text_target = 1.0 if (r.text_enabled and self._title) else 0.0
        self._text_fade = min(text_target, self._text_fade + step) if text_target > self._text_fade \
            else max(text_target, self._text_fade - step)

        # Optional slow spin, a touch faster with more energy.
        if r.cover_rotate:
            self._cover_angle = (self._cover_angle
                                 + dt * r.cover_rotate_speed * 360.0 * (1.0 + self.energy)) % 360.0

    # ================================================================ colours
    def _bar_hues(self, n: int) -> np.ndarray:
        """Hue per band, sweeping bass -> mid -> high along the shortest way round."""
        bass_h, mid_h, high_h = (float(h) for h in self.band_hues())
        d1 = (mid_h - bass_h + 0.5) % 1.0 - 0.5
        d2 = (high_h - mid_h + 0.5) % 1.0 - 0.5
        t = np.linspace(0.0, 2.0, n, dtype=np.float32)
        hue = np.where(t < 1.0, bass_h + d1 * t, mid_h + d2 * (t - 1.0))
        return hue % 1.0

    def _sprite(self, hue_index: int) -> tuple[QPixmap, QPixmap]:
        sprite = self._sprites.get(hue_index)
        if sprite is None:
            rgb = colorsys.hsv_to_rgb(hue_index / HUE_STEPS, self.theme.saturation, 1.0)
            sprite = (_to_pixmap(rgb, *self._strip_profile), _to_pixmap(rgb, *self._cap_profile))
            self._sprites[hue_index] = sprite
        return sprite

    # ================================================================ rendering
    def paint_gl(self, window) -> None:
        painter = QPainter(window)
        try:
            self.render(painter, window.width(), window.height())
        finally:
            painter.end()

    def render(self, painter: QPainter, width: int, height: int) -> None:
        r = self.r
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(0, 0, width, height, QColor.fromRgbF(*self.background_rgb()))
        self.draw_backdrop(painter, width, height)
        n = self.bars.size
        if n == 0:
            return

        unit = min(width, height)
        cx, cy = width * 0.5, height * 0.5
        bass = float(self.levels[0])
        r0 = unit * (r.radius + r.bass_swell * bass + r.beat_pulse * self._pulse)
        hues = self._bar_hues(n)
        ring_rgb = colorsys.hsv_to_rgb(float(hues[0]), self.theme.saturation * 0.6, 1.0)
        glow_rgb = colorsys.hsv_to_rgb(float(hues[n // 2]), self.theme.saturation, 1.0)

        # Album cover inside the ring. It swells with the ring (same radius) so the
        # two always match; the spectrum bars radiate out from the ring's edge.
        if self._cover is not None and self._cover_fade > 0.0:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            cover_r = r0 * r.cover_fill   # tracks the reactive ring radius
            painter.setOpacity(r.cover_opacity * self._cover_fade)
            if r.cover_rotate:
                painter.setTransform(
                    QTransform().translate(cx, cy).rotate(self._cover_angle))
                painter.drawPixmap(QRectF(-cover_r, -cover_r, 2 * cover_r, 2 * cover_r),
                                   self._cover, QRectF(self._cover.rect()))
                painter.resetTransform()
            else:
                painter.drawPixmap(QRectF(cx - cover_r, cy - cover_r, 2 * cover_r, 2 * cover_r),
                                   self._cover, QRectF(self._cover.rect()))
            painter.setOpacity(1.0)

        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Ring + glow as one radial gradient: smooth edges without MSAA.
        outer = r0 + unit * (r.bar_length + 0.08)
        f0, eps = r0 / outer, 2.0 / outer
        ring_a = min(1.0, 0.65 + 0.35 * self.flash)
        glow_a = r.center_glow * (0.25 + 0.75 * self.energy)
        g = QRadialGradient(QPointF(cx, cy), outer)
        g.setColorAt(0.0, QColor.fromRgbF(*glow_rgb, 0.0))
        g.setColorAt(max(0.0, f0 - 3 * eps), QColor.fromRgbF(*glow_rgb, glow_a * 0.3))
        g.setColorAt(max(0.0, f0 - eps), QColor.fromRgbF(*ring_rgb, ring_a))
        g.setColorAt(min(1.0, f0 + eps), QColor.fromRgbF(*ring_rgb, ring_a))
        g.setColorAt(min(1.0, f0 + 3 * eps), QColor.fromRgbF(*glow_rgb, glow_a))
        g.setColorAt(1.0, QColor.fromRgbF(*glow_rgb, 0.0))
        painter.fillRect(QRectF(cx - outer, cy - outer, 2 * outer, 2 * outer), g)

        # Bars: band i on the right half, mirrored onto the left; bass at the top.
        slot = 2.0 * math.pi * r0 / (2 * n)
        core_w = max(1.5, slot * r.bar_fill)
        w = core_w / CORE_FRAC
        start = r0 + 3.0
        lengths = (unit * (r.min_bar + r.bar_length * self.bars)).tolist()
        opacity = np.clip(r.min_brightness + (1.0 - r.min_brightness) * self.bars
                          + r.beat_brighten * self.flash, 0.0, 1.0).tolist()
        hue_q = ((hues * HUE_STEPS).astype(np.int32) % HUE_STEPS).tolist()
        base = -0.5 * math.pi + 2.0 * math.pi * self._rotation
        strip_src = QRectF(0, 0, STRIP_LEN, SPRITE)
        cap_src = QRectF(0, 0, SPRITE // 2, SPRITE)

        for i in range(n):
            strip, cap = self._sprite(hue_q[i])
            length = lengths[i]
            bar_rect = QRectF(start, -w * 0.5, length, w)
            cap_rect = QRectF(start + length, -w * 0.5, w * 0.5, w)
            offset = (i + 0.5) / n * math.pi
            painter.setOpacity(opacity[i])
            for angle in (base + offset, base - offset):
                painter.setTransform(QTransform().translate(cx, cy).rotateRadians(angle))
                painter.drawPixmap(bar_rect, strip, strip_src)
                painter.drawPixmap(cap_rect, cap, cap_src)

        painter.resetTransform()
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        if self._text_fade > 0.0 and self._title:
            self._draw_track_text(painter, width, height, unit, cx, cy, r0)

    # ================================================================ track text
    def _draw_track_text(self, painter: QPainter, width: int, height: int,
                          unit: float, cx: float, cy: float, r0: float) -> None:
        r = self.r
        title_px = max(10.0, unit * r.text_title_pt)
        artist_px = max(9.0, unit * r.text_artist_pt)

        title_font = QFont(painter.font())
        title_font.setPixelSize(int(round(title_px)))
        title_font.setWeight(QFont.Weight.DemiBold)
        artist_font = QFont(painter.font())
        artist_font.setPixelSize(int(round(artist_px)))
        artist_font.setWeight(QFont.Weight.Normal)

        tm, am = QFontMetricsF(title_font), QFontMetricsF(artist_font)
        line_gap = title_px * 0.2
        block_h = tm.height() + (am.height() + line_gap if self._artist else 0.0)

        # Anchor the block a fixed distance above the screen bottom so it clears the
        # taskbar and never follows the swelling bars off-screen. Only nudge it up if
        # it would somehow overlap the ring (tiny screens / huge ring).
        bottom = height - height * r.text_bottom_margin
        top = bottom - block_h
        ring_bottom = cy + r0 + unit * 0.02
        if top < ring_bottom:
            top = min(ring_bottom, height - block_h - unit * 0.01)

        # Elide long names to a sensible width.
        max_w = min(width * 0.8, unit * 2.2)
        title = tm.elidedText(self._title, Qt.TextElideMode.ElideRight, int(max_w))
        artist = am.elidedText(self._artist, Qt.TextElideMode.ElideRight, int(max_w)) if self._artist else ""

        fg = QColor.fromRgbF(0.97, 0.98, 0.99)
        shadow = QColor(0, 0, 0, int(160 * self._text_fade))
        painter.setOpacity(1.0)

        def line(font: QFont, text: str, y: float, alpha: float) -> float:
            if not text:
                return y
            m = QFontMetricsF(font)
            tw = m.horizontalAdvance(text)
            x = cx - tw * 0.5
            baseline = y + m.ascent()
            painter.setFont(font)
            painter.setPen(shadow)                       # soft drop shadow for legibility
            painter.drawText(QPointF(x + 1.5, baseline + 1.5), text)
            fg.setAlphaF(alpha)
            painter.setPen(fg)
            painter.drawText(QPointF(x, baseline), text)
            return y + m.height()

        y = line(title_font, title, top, r.text_opacity * self._text_fade)
        if artist:
            line(artist_font, artist, y + line_gap, r.text_opacity * 0.8 * self._text_fade)
