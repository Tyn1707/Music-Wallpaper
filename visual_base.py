"""State shared by every visual style: theme colours, idle blending, beats.

A visual style subclasses `VisualBase`, calls `self.update_common(...)` from its
`update()`, and implements `paint_gl(window)` (called inside paintGL with the
window's OpenGL context current).
"""
from __future__ import annotations

import colorsys
import math

import numpy as np

from config import ColorConfig, IdleConfig
from fft_analyzer import AudioFeatures
from themes import Theme, get_theme

BAND_PHASES = np.array([0.0, 2.1, 4.2], dtype=np.float32)  # idle breathing offsets per band


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
