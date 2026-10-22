"""Central configuration. All tunables live here; no other file needs editing."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AudioConfig:
    chunk_size: int = 512            # frames per PortAudio callback
    buffer_size: int = 8192          # mono samples kept in the ring buffer (must be >= FFTConfig.fft_size)
    silence_timeout: float = 0.25    # s without callbacks => silence (WASAPI loopback sends nothing when idle)


@dataclass(frozen=True)
class FFTConfig:
    fft_size: int = 2048             # ~43 ms window at 48 kHz
    bass_range: tuple[float, float] = (20.0, 250.0)
    mid_range: tuple[float, float] = (250.0, 4000.0)
    high_range: tuple[float, float] = (4000.0, 16000.0)
    attack_time: float = 0.025       # s, how fast levels rise
    release_time: float = 0.22       # s, how fast levels fall
    peak_decay_time: float = 4.0     # s, adaptive-gain memory (auto volume normalisation)
    min_peak: float = 0.02           # band amplitude treated as "full scale" at minimum (stops noise being amplified)
    noise_floor: float = 0.002       # band amplitude below this is silence
    gamma: float = 1.4               # >1 = punchier contrast between quiet and loud

    # Beat detection (spectral flux onsets in the beat range)
    beat_range: tuple[float, float] = (30.0, 200.0)
    beat_history: int = 60           # flux frames used for the adaptive threshold (~1 s at 60 fps)
    beat_sensitivity: float = 1.6    # threshold = mean + sensitivity * std  (lower = more beats)
    beat_min_interval: float = 0.28  # s refractory period (caps detection at ~214 BPM)
    beat_min_level: float = 0.08     # bass level needed for a beat (ignores noise)
    bpm_range: tuple[float, float] = (70.0, 180.0)  # BPM estimate is folded into this range

    # Fine spectrum (used by the radial visual)
    spectrum_bands: int = 64         # log-spaced bands
    spectrum_range: tuple[float, float] = (40.0, 16000.0)
    spectrum_tilt: float = 0.45      # treble boost: amp * (f / 1 kHz) ** tilt
    spectrum_gamma: float = 1.2      # >1 = more contrast between bars


@dataclass(frozen=True)
class ParticleConfig:
    max_particles: int = 1000
    emit_base: float = 25.0          # particles/s during silence
    emit_energy: float = 350.0       # extra particles/s at full energy
    ambient_fraction: float = 0.35   # share of particles spawned anywhere on screen (rest spawn on the centre ring)
    life_range: tuple[float, float] = (2.5, 6.0)

    ring_radius: float = 0.08        # spawn ring radius as fraction of min(width, height)
    ring_bass: float = 0.10          # ring grows with bass
    speed_base: float = 30.0         # px/s outward
    speed_bass: float = 260.0        # extra outward px/s at full bass
    ambient_speed_factor: float = 0.35
    spawn_swirl: float = 60.0        # tangential px/s at spawn, scaled by mids

    swirl_accel: float = 45.0        # px/s^2 tangential force
    swirl_base: float = 0.25         # swirl present even without mids
    damping: float = 0.35            # 1/s velocity decay
    jitter: float = 160.0            # random px/s^2 driven by highs

    beat_impulse: float = 140.0      # px/s outward push on all particles per beat (x strength)
    beat_burst: float = 45.0         # extra particles spawned per beat (x strength)

    # (min, max) diameter in logical px for bass / mid / high particles
    size_ranges: tuple[tuple[float, float], ...] = ((22.0, 44.0), (12.0, 24.0), (5.0, 11.0))
    band_bias: float = 0.15          # keeps every band spawning a little even when silent


@dataclass(frozen=True)
class LiquidConfig:
    base_radius: float = 0.29        # blob radius as fraction of screen height
    level_swell: float = 0.45        # radius growth at full band level
    beat_pulse: float = 0.12         # extra bass-blob radius on a beat (x strength)
    beat_pulse_decay: float = 0.25   # s
    drift_speed: float = 0.06        # path speed while quiet
    mid_speed: float = 0.30          # extra path speed at full mids
    warp: float = 0.035              # liquid domain warp
    high_warp: float = 0.05          # extra warp at full highs
    min_brightness: float = 0.38     # blob brightness at silence (HSV value)
    max_brightness: float = 0.82     # at full level; kept < 1 so icons stay readable
    beat_brighten: float = 0.10
    vignette: float = 0.45


@dataclass(frozen=True)
class RadialConfig:
    radius: float = 0.16             # inner ring radius as fraction of min(width, height)
    bass_swell: float = 0.06         # ring growth at full bass
    beat_pulse: float = 0.07         # extra ring radius on a beat (x strength)
    beat_pulse_decay: float = 0.18   # s
    bar_length: float = 0.26         # max bar length as fraction of min(width, height)
    min_bar: float = 0.012           # bar stub length at silence (same unit)
    bar_fill: float = 0.7            # bar width as a share of its angular slot
    fall_speed: float = 1.6          # displayed bars fall at most this much (0..1) per second
    rotate_speed: float = 0.006      # rings/s slow rotation
    glow: float = 0.22               # opacity of the wide glow pass
    center_glow: float = 0.35        # background glow behind the ring at full energy
    min_brightness: float = 0.55
    beat_brighten: float = 0.25
    cover_enabled: bool = True       # show the Spotify album cover inside the ring
    cover_fill: float = 1.0          # cover diameter as a share of the (stable) inner ring
    cover_opacity: float = 0.95      # base opacity of the cover image
    cover_rotate: bool = False       # slowly spin the cover
    cover_rotate_speed: float = 0.03 # turns/s when rotating (x energy)
    text_enabled: bool = True        # show the song title + artist below the ring
    text_title_pt: float = 0.022     # title font size as a fraction of min(w, h)
    text_artist_pt: float = 0.015    # artist font size as a fraction of min(w, h)
    text_bottom_margin: float = 0.075  # block's distance from the screen bottom (frac of height)
    text_opacity: float = 0.9        # base opacity of the text


@dataclass(frozen=True)
class VinylConfig:
    rpm: float = 8.0                 # record speed; a real 33 1/3 rpm is too busy for a desktop


@dataclass(frozen=True)
class OverlayConfig:
    """Now-playing card (album cover + title + artist) for the liquid & particle styles.

    The radial style draws its own cover inside the ring and ignores this.
    """
    enabled: bool = True
    cover_shape: str = "circle"      # "circle" or "rounded_rect"; set per style
    cover_size: float = 0.26         # cover edge length as a fraction of min(w, h)
    corner_radius: float = 0.14      # rounded-rect corner radius as a share of the cover size
    opacity: float = 0.95            # base opacity of the card
    margin: float = 0.03             # distance from the screen's left/bottom edges (frac of min(w, h))
    bottom_margin: float = 0.06      # distance from the screen bottom (frac of height; clears taskbar)
    gap: float = 0.4                 # gap between cover and text as a share of the cover size
    title_pt: float = 0.034          # title font size as a fraction of min(w, h)
    artist_pt: float = 0.024         # artist font size as a fraction of min(w, h)


@dataclass(frozen=True)
class IdleConfig:
    enabled: bool = True
    delay: float = 3.0               # s of silence before the idle animation starts
    fade_in: float = 2.5             # s to blend fully into idle
    fade_out: float = 0.3            # s to blend back when music returns
    level: float = 0.22              # synthetic band level while idle
    breath_period: float = 7.0       # s per breathing cycle
    flow_strength: float = 40.0      # px/s^2 flow-field force while idle
    flow_scale: float = 0.0025       # spatial frequency of the flow field (smaller = bigger swirls)


@dataclass(frozen=True)
class ColorConfig:
    theme: str = "dynamic"           # see themes.py: dynamic, neon, sunset, ocean, aurora, ice
    background_value: float = 0.035  # background brightness at silence
    background_bass: float = 0.05    # extra background brightness at full bass
    beat_flash: float = 0.05         # extra background brightness on a beat
    beat_flash_decay: float = 0.15   # s


@dataclass(frozen=True)
class RenderConfig:
    visual_style: str = "liquid"     # "liquid", "particles", "radial" or "vinyl"
    target_fps: int = 60
    idle_fps: int = 30               # frame rate once fully idle (saves CPU/GPU)
    max_dt: float = 0.1              # clamp simulation step after stalls
    pause_when_covered: bool = True  # stop rendering when a fullscreen/maximised app hides the desktop
    print_stats: bool = True
    stats_interval: float = 5.0      # s


@dataclass(frozen=True)
class WallpaperConfig:
    span_all_monitors: bool = False  # False = primary monitor only (cheaper); True = whole virtual desktop
    maintenance_interval: float = 2.0  # s between health / pause / audio checks


@dataclass(frozen=True)
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    fft: FFTConfig = field(default_factory=FFTConfig)
    particles: ParticleConfig = field(default_factory=ParticleConfig)
    liquid: LiquidConfig = field(default_factory=LiquidConfig)
    radial: RadialConfig = field(default_factory=RadialConfig)
    vinyl: VinylConfig = field(default_factory=VinylConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    colors: ColorConfig = field(default_factory=ColorConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    wallpaper: WallpaperConfig = field(default_factory=WallpaperConfig)
