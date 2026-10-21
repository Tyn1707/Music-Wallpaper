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
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap, QRadialGradient, QTransform

from config import ColorConfig, IdleConfig, RadialConfig
from fft_analyzer import AudioFeatures
from visual_base import VisualBase

HUE_STEPS = 64
SPRITE = 32            # px across the bar (core + glow halo)
STRIP_LEN = 4          # px along the bar; stretched to the bar length
CORE_FRAC = 0.3        # core diameter as a share of the sprite width


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

        # Cross-section profiles are colour independent: compute once.
        y = (np.arange(SPRITE, dtype=np.float32) + 0.5) / SPRITE * 2.0 - 1.0
        self._strip_profile = _profile(np.repeat(np.abs(y)[:, None], STRIP_LEN, axis=1))
        x = (np.arange(SPRITE // 2, dtype=np.float32) + 0.5) / (SPRITE // 2)   # right half only
        self._cap_profile = _profile(np.sqrt(x[None, :] ** 2 + y[:, None] ** 2))

    def on_theme_changed(self) -> None:
        self._sprites.clear()  # sprite colours depend on theme saturation

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
