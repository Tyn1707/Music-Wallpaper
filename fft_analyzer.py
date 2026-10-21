"""Turns raw samples into smoothed, auto-normalised band levels plus beat events.

Beat detection uses spectral flux (sum of positive changes in log-magnitude) over
the low-frequency range, compared against an adaptive threshold built from the
recent flux history. A refractory window prevents double triggers, and the
inter-beat intervals give a rough BPM estimate.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from config import FFTConfig


@dataclass
class AudioFeatures:
    bass: float = 0.0           # 0..1
    mid: float = 0.0            # 0..1
    high: float = 0.0           # 0..1
    energy: float = 0.0         # weighted overall level 0..1
    beat: bool = False          # True on the frame a beat is detected
    beat_strength: float = 0.0  # 0..1, only meaningful when beat is True
    bpm: float = 0.0            # 0 until enough beats have been seen
    silent: bool = True         # nothing above the noise floor this frame
    spectrum: np.ndarray | None = None  # 0..1 per log-spaced band (FFTConfig.spectrum_bands), low -> high

    def as_array(self) -> np.ndarray:
        return np.array([self.bass, self.mid, self.high], dtype=np.float32)


class FFTAnalyzer:
    def __init__(self, cfg: FFTConfig, sample_rate: int) -> None:
        self.cfg = cfg
        self.set_sample_rate(sample_rate)

    def set_sample_rate(self, sample_rate: int) -> None:
        cfg = self.cfg
        self.sample_rate = sample_rate
        self._window = np.hanning(cfg.fft_size).astype(np.float32)
        # Scale so a full-scale sine reads ~1.0.
        self._scale = 2.0 / float(self._window.sum())

        freqs = np.fft.rfftfreq(cfg.fft_size, 1.0 / sample_rate)
        self._bands = [self._bin_slice(freqs, lo, hi)
                       for lo, hi in (cfg.bass_range, cfg.mid_range, cfg.high_range)]
        self._beat_bins = self._bin_slice(freqs, *cfg.beat_range)

        self._levels = np.zeros(3, dtype=np.float32)
        self._peaks = np.full(3, cfg.min_peak, dtype=np.float32)

        # Fine log-spaced bands for spectrum visuals. Each band covers >= 1 FFT bin;
        # band power comes from a cumulative sum so it's one vectorised step per frame.
        n = cfg.spectrum_bands
        lo, hi = cfg.spectrum_range
        hi = min(hi, float(freqs[-1]))
        edges = np.geomspace(lo, hi, n + 1)
        idx = np.clip(np.searchsorted(freqs, edges), 1, freqs.size - 1)
        starts = idx[:-1]
        ends = np.maximum(idx[1:], starts + 1)
        self._spec_starts, self._spec_ends = starts, ends
        centres = np.sqrt(edges[:-1] * edges[1:])
        # Music has less energy up high: tilt so treble bars still move.
        self._spec_tilt = ((centres / 1000.0) ** cfg.spectrum_tilt).astype(np.float32)
        self._spec_levels = np.zeros(n, dtype=np.float32)
        self._spec_peak = cfg.min_peak

        self._time = 0.0
        self._prev_log: np.ndarray | None = None
        self._flux = deque(maxlen=cfg.beat_history)
        self._last_beat = -1.0
        self._beat_times = deque(maxlen=16)
        self.bpm = 0.0

    @staticmethod
    def _bin_slice(freqs: np.ndarray, lo: float, hi: float) -> slice:
        idx = np.nonzero((freqs >= lo) & (freqs < min(hi, freqs[-1])))[0]
        return slice(int(idx[0]), int(idx[-1]) + 1) if idx.size else slice(0, 1)

    # ------------------------------------------------------------------ main entry
    def process(self, samples: np.ndarray, dt: float) -> AudioFeatures:
        cfg = self.cfg
        dt = max(dt, 1e-4)
        self._time += dt

        spectrum = np.abs(np.fft.rfft(samples * self._window)) * self._scale
        # RMS magnitude per band.
        raw = np.array(
            [math.sqrt(float(np.mean(np.square(spectrum[b])))) for b in self._bands],
            dtype=np.float32,
        )
        raw[raw < cfg.noise_floor] = 0.0
        silent = not raw.any()

        # Adaptive gain: remember recent peaks, decay slowly toward min_peak.
        decay = math.exp(-dt / cfg.peak_decay_time)
        self._peaks = np.maximum(np.maximum(self._peaks * decay, raw), cfg.min_peak)
        target = np.clip(raw / self._peaks, 0.0, 1.0) ** cfg.gamma

        # Attack / release smoothing.
        a_up = 1.0 - math.exp(-dt / cfg.attack_time)
        a_down = 1.0 - math.exp(-dt / cfg.release_time)
        alpha = np.where(target > self._levels, a_up, a_down).astype(np.float32)
        self._levels += (target - self._levels) * alpha

        bass, mid, high = (float(v) for v in self._levels)
        beat, strength = self._detect_beat(spectrum, bass)
        energy = min(1.0, 0.5 * bass + 0.3 * mid + 0.2 * high)
        spec = self._spectrum_bands(spectrum, decay, a_up, a_down, silent)
        return AudioFeatures(bass, mid, high, energy, beat, strength, self.bpm, silent, spec)

    # ------------------------------------------------------------------ spectrum
    def _spectrum_bands(self, spectrum: np.ndarray, decay: float,
                        a_up: float, a_down: float, silent: bool) -> np.ndarray:
        cfg = self.cfg
        csum = np.concatenate(([0.0], np.cumsum(np.square(spectrum, dtype=np.float64))))
        s, e = self._spec_starts, self._spec_ends
        amp = np.sqrt((csum[e] - csum[s]) / (e - s)).astype(np.float32) * self._spec_tilt
        if silent:
            amp[:] = 0.0
        # One shared adaptive peak keeps the spectrum's shape (per-band gain would flatten it).
        self._spec_peak = max(self._spec_peak * decay, float(amp.max()), cfg.min_peak)
        target = np.clip(amp / self._spec_peak, 0.0, 1.0) ** cfg.spectrum_gamma
        alpha = np.where(target > self._spec_levels, a_up, a_down).astype(np.float32)
        self._spec_levels += (target - self._spec_levels) * alpha
        return self._spec_levels.copy()

    # ------------------------------------------------------------------ beats
    def _detect_beat(self, spectrum: np.ndarray, bass: float) -> tuple[bool, float]:
        cfg = self.cfg
        # Log compression makes flux respond to relative (not absolute) changes.
        log_mag = np.log1p(1000.0 * spectrum[self._beat_bins])
        prev, self._prev_log = self._prev_log, log_mag
        if prev is None:
            return False, 0.0
        flux = float(np.maximum(log_mag - prev, 0.0).sum())

        history = self._flux
        beat, strength = False, 0.0
        if len(history) >= history.maxlen // 2:
            arr = np.fromiter(history, dtype=np.float32)
            mean, std = float(arr.mean()), float(arr.std())
            threshold = mean + cfg.beat_sensitivity * std
            if (
                flux > threshold
                and flux > 1e-3
                and bass >= cfg.beat_min_level
                and self._time - self._last_beat >= cfg.beat_min_interval
            ):
                beat = True
                strength = min(1.0, 0.35 + (flux - threshold) / (threshold + 1e-6))
                self._register_beat()
        history.append(flux)
        return beat, strength

    def _register_beat(self) -> None:
        cfg = self.cfg
        if self._last_beat > 0 and self._time - self._last_beat > 2.0:
            self._beat_times.clear()  # long gap: start a fresh tempo estimate
            self.bpm = 0.0
        self._last_beat = self._time
        self._beat_times.append(self._time)
        if len(self._beat_times) < 4:
            return
        intervals = np.diff(np.fromiter(self._beat_times, dtype=np.float64))
        intervals = intervals[(intervals > 0.25) & (intervals < 2.0)]
        if intervals.size < 3:
            return
        bpm = 60.0 / float(np.median(intervals))
        lo, hi = cfg.bpm_range
        while bpm < lo:
            bpm *= 2.0
        while bpm > hi:
            bpm /= 2.0
        # Smooth so the estimate doesn't jump on every beat.
        self.bpm = bpm if self.bpm == 0.0 else self.bpm + (bpm - self.bpm) * 0.3
