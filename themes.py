"""Colour themes. Hues are 0..1 (0 = red, 0.33 = green, 0.66 = blue)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    band_hues: tuple[float, float, float]   # base hue for bass / mid / high particles
    saturation: float = 0.85
    drift: float = 0.0             # hue cycles/s while quiet (0 = palette stays put)
    energy_drift: float = 0.0      # extra hue cycles/s at full energy
    wobble: float = 0.03           # slow +/- hue swing so static palettes still breathe
    shift: float = 0.04            # bass pulls hues warmer, highs pull cooler
    beat_hue_jump: float = 0.0     # hue jump on every beat (dynamic themes)
    background_hue: float | None = None  # None = follow the bass hue
    background_saturation: float = 0.65


THEMES: dict[str, Theme] = {
    t.name: t
    for t in (
        # Phase 1 behaviour: whole palette rotates, faster with energy, nudged by beats.
        Theme("dynamic", (0.0, 0.33, 0.62), drift=0.008, energy_drift=0.05,
              wobble=0.0, shift=0.08, beat_hue_jump=0.025),
        Theme("neon", (0.92, 0.52, 0.76), saturation=0.9, background_hue=0.75),
        Theme("sunset", (0.98, 0.06, 0.12), saturation=0.85, background_hue=0.95),
        Theme("ocean", (0.62, 0.53, 0.46), saturation=0.8, background_hue=0.6),
        Theme("aurora", (0.78, 0.38, 0.50), saturation=0.8, wobble=0.05, background_hue=0.7),
        Theme("ice", (0.58, 0.55, 0.60), saturation=0.3, wobble=0.01, shift=0.0,
              background_hue=0.6, background_saturation=0.4),
    )
}


def get_theme(name: str) -> Theme:
    try:
        return THEMES[name.lower()]
    except KeyError:
        raise ValueError(f"Unknown theme '{name}'. Available: {', '.join(THEMES)}") from None
