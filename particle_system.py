"""Audio-reactive particle simulation (NumPy) and renderer (QPainter).

Behaviour
- Particles spawn on a ring around the screen centre (ring grows with bass) plus
  a share of "ambient" particles anywhere on screen.
- Bass: larger particles, faster outward launch.
- Beats: outward shockwave on all particles, spawn burst, background flash.
- Mids: swirl around the centre.  Highs: small twinkling particles + jitter.
- Colours come from the active Theme (themes.py).
- Idle: after a few seconds of silence the system blends into a slow
  "breathing" flow-field animation, and blends back when music returns.

Rendering uses cached soft-glow sprites (one per quantised hue + size) drawn
with additive blending, which keeps per-frame Python work to a single loop.
"""
from __future__ import annotations

import colorsys
import math

import numpy as np
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap, QRadialGradient

from config import ColorConfig, IdleConfig, ParticleConfig
from fft_analyzer import AudioFeatures
from visual_base import VisualBase

HUE_STEPS = 48
SIZE_STEP = 2          # px; sprite sizes are quantised to multiples of this
MIN_SPRITE = 4
MAX_SPRITE = 80
FADE_IN = 0.35         # s


class ParticleSystem(VisualBase):
    name = "particles"

    def __init__(self, pcfg: ParticleConfig, ccfg: ColorConfig, icfg: IdleConfig,
                 seed: int | None = None) -> None:
        super().__init__(ccfg, icfg)
        self.p = pcfg
        self.rng = np.random.default_rng(seed)

        n = pcfg.max_particles
        self.pos = np.zeros((n, 2), dtype=np.float32)
        self.vel = np.zeros((n, 2), dtype=np.float32)
        self.life = np.zeros(n, dtype=np.float32)
        self.max_life = np.ones(n, dtype=np.float32)
        self.size = np.zeros(n, dtype=np.float32)
        self.band = np.zeros(n, dtype=np.int8)
        self.phase = np.zeros(n, dtype=np.float32)
        self.alive = np.zeros(n, dtype=bool)

        self._emit_acc = 0.0
        self._size_lo = np.array([r[0] for r in pcfg.size_ranges], dtype=np.float32)
        self._size_hi = np.array([r[1] for r in pcfg.size_ranges], dtype=np.float32)
        self._sprites: dict[int, QPixmap] = {}

    def on_theme_changed(self) -> None:
        self._sprites.clear()  # sprite colours depend on theme saturation

    def set_max_particles(self, n: int) -> None:
        """Resize the particle pool, keeping as many live particles as fit."""
        old = self.alive.shape[0]
        if n == old:
            return
        keep = min(n, old)
        for name in ("pos", "vel", "life", "max_life", "size", "band", "phase", "alive"):
            arr = getattr(self, name)
            new = np.zeros((n,) + arr.shape[1:], dtype=arr.dtype)
            new[:keep] = arr[:keep]
            setattr(self, name, new)
        self.max_life[keep:] = 1.0

    @property
    def alive_count(self) -> int:
        return int(self.alive.sum())

    def paint_gl(self, window) -> None:
        painter = QPainter(window)
        try:
            self.render(painter, window.width(), window.height())
        finally:
            painter.end()

    # ================================================================ simulation
    def update(self, f: AudioFeatures, dt: float, width: float, height: float) -> None:
        p = self.p
        self.update_common(f, dt)
        idle = self.idle
        bass, mid, high = (float(v) for v in self.levels)
        beat, strength = self.beat, self.beat_strength

        cx, cy = width * 0.5, height * 0.5
        idx = np.flatnonzero(self.alive)
        if idx.size:
            pos = self.pos[idx]
            vel = self.vel[idx]
            d = pos - (cx, cy)
            r = np.sqrt(np.einsum("ij,ij->i", d, d)) + 1e-3
            u = d / r[:, None]
            tangent = np.stack((-u[:, 1], u[:, 0]), axis=1)

            vel += tangent * (p.swirl_accel * (p.swirl_base + mid) * (1.0 - 0.7 * idle) * dt)
            if beat:
                vel += u * (strength * p.beat_impulse)
            if high > 0.01 and idle < 0.99:
                vel += self.rng.standard_normal(vel.shape).astype(np.float32) * (
                    p.jitter * high * (1.0 - idle) * dt)
            if idle > 0.01:
                vel += self._flow(pos) * (self.i.flow_strength * idle * dt)
            vel *= math.exp(-p.damping * dt)
            pos += vel * dt

            self.pos[idx] = pos
            self.vel[idx] = vel
            self.life[idx] -= dt

            margin = MAX_SPRITE
            dead = (
                (self.life[idx] <= 0.0)
                | (pos[:, 0] < -margin) | (pos[:, 0] > width + margin)
                | (pos[:, 1] < -margin) | (pos[:, 1] > height + margin)
            )
            self.alive[idx[dead]] = False

        self._emit_acc += (p.emit_base + p.emit_energy * self.energy) * dt + strength * p.beat_burst
        count = int(self._emit_acc)
        if count:
            self._emit_acc -= count
            self._spawn(count, cx, cy, width, height, bass, mid)

    def _flow(self, pos: np.ndarray) -> np.ndarray:
        """Smooth, slowly evolving 2-D flow field (unit vectors)."""
        s, t = self.i.flow_scale, self.time
        angle = (np.sin(pos[:, 0] * s + t * 0.11) + np.cos(pos[:, 1] * s * 1.3 - t * 0.07)) * math.pi
        return np.stack((np.cos(angle), np.sin(angle)), axis=1).astype(np.float32)

    def _spawn(self, count: int, cx: float, cy: float, w: float, h: float,
               bass: float, mid: float) -> None:
        free = np.flatnonzero(~self.alive)[:count]
        n = free.size
        if n == 0:
            return
        p, rng = self.p, self.rng
        # Idle: mostly ambient particles spread over the screen instead of the centre ring.
        ambient_fraction = p.ambient_fraction + (0.9 - p.ambient_fraction) * self.idle

        weights = self.levels + p.band_bias
        band = rng.choice(3, size=n, p=weights / weights.sum()).astype(np.int8)

        angle = rng.uniform(0.0, 2.0 * math.pi, n).astype(np.float32)
        u = np.stack((np.cos(angle), np.sin(angle)), axis=1)
        tangent = np.stack((-u[:, 1], u[:, 0]), axis=1)

        radius = min(w, h) * (p.ring_radius + p.ring_bass * bass) * rng.uniform(0.8, 1.2, n)
        pos = np.array((cx, cy), dtype=np.float32) + u * radius[:, None]
        speed = (p.speed_base + p.speed_bass * bass) * rng.uniform(0.6, 1.2, n)
        swirl = p.spawn_swirl * (p.swirl_base + mid) * rng.uniform(0.5, 1.0, n)
        vel = u * speed[:, None] + tangent * swirl[:, None]

        ambient = rng.random(n) < ambient_fraction
        if ambient.any():
            m = int(ambient.sum())
            pos[ambient] = np.column_stack((rng.uniform(0, w, m), rng.uniform(0, h, m)))
            vel[ambient] = u[ambient] * (p.speed_base * p.ambient_speed_factor)

        self.pos[free] = pos
        self.vel[free] = vel
        self.band[free] = band
        self.size[free] = rng.uniform(self._size_lo[band], self._size_hi[band])
        life = rng.uniform(*p.life_range, n)
        self.life[free] = life
        self.max_life[free] = life
        self.phase[free] = rng.uniform(0.0, 2.0 * math.pi, n)
        self.alive[free] = True

    # ================================================================ colours
    def background_color(self) -> QColor:
        return QColor.fromRgbF(*self.background_rgb())

    # ================================================================ rendering
    def render(self, painter: QPainter, width: int, height: int) -> None:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(0, 0, width, height, self.background_color())

        idx = np.flatnonzero(self.alive)
        if idx.size == 0:
            return

        band = self.band[idx]
        level = self.levels[band]
        life = self.life[idx]
        max_life = self.max_life[idx]

        fade_in = np.clip((max_life - life) / FADE_IN, 0.0, 1.0)
        fade_out = np.clip(life / max_life * 1.5, 0.0, 1.0)
        opacity = fade_in * fade_out * (0.35 + 0.65 * level) * (1.0 + 0.4 * self.flash)
        high = band == 2
        if high.any():
            twinkle = 0.55 + 0.45 * np.sin(self.time * 11.0 + self.phase[idx][high])
            opacity[high] *= twinkle
        np.clip(opacity, 0.0, 1.0, out=opacity)

        diameter = self.size[idx] * (1.0 + 0.4 * level)
        size_q = np.clip((diameter / SIZE_STEP).round().astype(np.int32) * SIZE_STEP, MIN_SPRITE, MAX_SPRITE)
        hue_q = (self.band_hues()[band] * HUE_STEPS).astype(np.int32) % HUE_STEPS
        keys = hue_q * 1000 + size_q

        half = size_q * 0.5
        xs = (self.pos[idx, 0] - half).tolist()
        ys = (self.pos[idx, 1] - half).tolist()

        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        sprites = self._sprites
        for x, y, o, key in zip(xs, ys, opacity.tolist(), keys.tolist()):
            if o < 0.02:
                continue
            sprite = sprites.get(key)
            if sprite is None:
                sprite = sprites[key] = self._make_sprite(key // 1000, key % 1000)
            painter.setOpacity(o)
            painter.drawPixmap(QPointF(x, y), sprite)
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self.draw_now_playing_card(painter, width, height, self.overlay)

    def _make_sprite(self, hue_index: int, size: int) -> QPixmap:
        hue = hue_index / HUE_STEPS
        sat = self.theme.saturation
        r, g, b = colorsys.hsv_to_rgb(hue, sat, 1.0)
        core = colorsys.hsv_to_rgb(hue, sat * 0.35, 1.0)

        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        grad = QRadialGradient(size / 2, size / 2, size / 2)
        grad.setColorAt(0.0, QColor.fromRgbF(*core, 1.0))
        grad.setColorAt(0.18, QColor.fromRgbF(r, g, b, 0.9))
        grad.setColorAt(0.45, QColor.fromRgbF(r, g, b, 0.28))
        grad.setColorAt(1.0, QColor.fromRgbF(r, g, b, 0.0))
        qp = QPainter(pix)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(grad)
        qp.drawEllipse(0, 0, size, size)
        qp.end()
        return pix
