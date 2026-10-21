"""User settings: persisted as JSON in <project>\\data\\settings.json.

Stored next to the app rather than in %APPDATA% because the Microsoft Store
build of Python silently redirects AppData writes into its own package folder
(hidden, and deleted if Python is uninstalled).

Settings are the user-facing knobs; `to_config()` maps them onto the internal
Config dataclasses so the rest of the app only ever reads Config.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from config import Config
from themes import THEMES

APP_DIR = Path(__file__).resolve().parent / "data"
SETTINGS_PATH = APP_DIR / "settings.json"

FPS_CHOICES = (30, 60, 120)
PARTICLE_RANGE = (200, 2000)
SENSITIVITY_RANGE = (1.0, 2.6)   # internal threshold multiplier; lower = more beats
REACTION_RANGE = (0.0, 2.0)
IDLE_DELAY_RANGE = (1.0, 30.0)


STYLE_CHOICES = ("liquid", "particles", "radial")


@dataclass
class Settings:
    visual_style: str = "liquid"
    theme: str = "dynamic"
    album_sync: bool = False
    max_particles: int = 1000
    beat_sensitivity: int = 50     # 0..100 in the UI; higher = more beats
    beat_reaction: float = 1.0     # multiplier for beat impulse / burst / flash
    target_fps: int = 60
    idle_enabled: bool = True
    idle_delay: float = 3.0
    pause_when_covered: bool = True
    span_all_monitors: bool = False

    # ------------------------------------------------------------------ validation
    def normalised(self) -> "Settings":
        def clamp(v, lo, hi):
            return max(lo, min(hi, v))

        return replace(
            self,
            visual_style=self.visual_style if self.visual_style in STYLE_CHOICES else "liquid",
            theme=self.theme if self.theme in THEMES else "dynamic",
            album_sync=bool(self.album_sync),
            max_particles=int(clamp(int(self.max_particles), *PARTICLE_RANGE)),
            beat_sensitivity=int(clamp(int(self.beat_sensitivity), 0, 100)),
            beat_reaction=float(clamp(float(self.beat_reaction), *REACTION_RANGE)),
            target_fps=int(self.target_fps) if int(self.target_fps) in FPS_CHOICES else 60,
            idle_enabled=bool(self.idle_enabled),
            idle_delay=float(clamp(float(self.idle_delay), *IDLE_DELAY_RANGE)),
            pause_when_covered=bool(self.pause_when_covered),
            span_all_monitors=bool(self.span_all_monitors),
        )

    # ------------------------------------------------------------------ mapping
    def sensitivity_multiplier(self) -> float:
        lo, hi = SENSITIVITY_RANGE
        return hi - (hi - lo) * self.beat_sensitivity / 100.0

    def to_config(self, base: Config | None = None) -> Config:
        base = base or Config()
        r = self.beat_reaction
        return replace(
            base,
            fft=replace(base.fft, beat_sensitivity=self.sensitivity_multiplier()),
            particles=replace(
                base.particles,
                max_particles=self.max_particles,
                beat_impulse=base.particles.beat_impulse * r,
                beat_burst=base.particles.beat_burst * r,
            ),
            liquid=replace(
                base.liquid,
                beat_pulse=base.liquid.beat_pulse * r,
                beat_brighten=base.liquid.beat_brighten * r,
            ),
            radial=replace(
                base.radial,
                beat_pulse=base.radial.beat_pulse * r,
                beat_brighten=base.radial.beat_brighten * r,
            ),
            idle=replace(base.idle, enabled=self.idle_enabled, delay=self.idle_delay),
            colors=replace(base.colors, theme=self.theme, beat_flash=base.colors.beat_flash * r),
            render=replace(base.render, visual_style=self.visual_style, target_fps=self.target_fps,
                           pause_when_covered=self.pause_when_covered),
            wallpaper=replace(base.wallpaper, span_all_monitors=self.span_all_monitors),
        )

    # ------------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "Settings":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in known}).normalised()
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError, TypeError) as exc:
            print(f"[settings] could not read {path} ({exc}); using defaults")
            return cls()

    def save(self, path: Path = SETTINGS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic: never leaves a half-written file
