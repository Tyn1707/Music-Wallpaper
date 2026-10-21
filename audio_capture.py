"""System audio capture via WASAPI loopback (PyAudioWPatch).

Captures whatever is playing on the default output device, downmixes to mono
float32 and keeps the most recent samples in a lock-protected ring buffer.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

from config import AudioConfig


class AudioCapture:
    def __init__(self, cfg: AudioConfig) -> None:
        self.cfg = cfg
        self.sample_rate: int = 48000
        self.channels: int = 2
        self.device_name: str = ""

        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._device_index: int | None = None
        self._lock = threading.Lock()
        self._buffer = np.zeros(cfg.buffer_size, dtype=np.float32)
        self._last_data_time = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Open a loopback stream on the current default output device."""
        if self._pa is None:
            self._pa = pyaudio.PyAudio()
        device = self._default_loopback()

        self._device_index = int(device["index"])
        self.sample_rate = int(device["defaultSampleRate"])
        self.channels = max(1, int(device["maxInputChannels"]))
        self.device_name = str(device["name"])

        with self._lock:
            self._buffer[:] = 0.0
        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=self._device_index,
            frames_per_buffer=self.cfg.chunk_size,
            stream_callback=self._callback,
        )
        self._stream.start_stream()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except OSError:
                pass  # device may already be gone
            self._stream = None

    def close(self) -> None:
        self.stop()
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    def check_device(self) -> bool:
        """Re-open the stream if the default output device changed (e.g. headphones
        plugged in) or the stream died. Returns True if a restart happened."""
        if self._pa is None:
            return False
        try:
            current = int(self._default_loopback()["index"])
        except (OSError, LookupError):
            return False  # no output device right now; keep the old stream
        stream_dead = self._stream is None or not self._stream.is_active()
        if current == self._device_index and not stream_dead:
            return False
        self.stop()
        # PortAudio caches the device list; re-create it to see new devices.
        self._pa.terminate()
        self._pa = pyaudio.PyAudio()
        self.start()
        return True

    # ------------------------------------------------------------------ data access
    def get_samples(self, n: int) -> np.ndarray:
        """Return the newest `n` mono samples (zeros while nothing is playing)."""
        n = min(n, self.cfg.buffer_size)
        if time.perf_counter() - self._last_data_time > self.cfg.silence_timeout:
            return np.zeros(n, dtype=np.float32)
        with self._lock:
            return self._buffer[-n:].copy()

    # ------------------------------------------------------------------ internals
    def _default_loopback(self) -> dict:
        assert self._pa is not None
        try:
            return self._pa.get_default_wasapi_loopback()
        except OSError as exc:
            raise RuntimeError("WASAPI is not available on this system") from exc
        except LookupError as exc:
            raise RuntimeError("No loopback device found for the default speakers") from exc

    def _callback(self, in_data, frame_count, time_info, status):
        # Runs on PortAudio's thread: keep it short and allocation-light.
        data = np.frombuffer(in_data, dtype=np.float32)
        if self.channels > 1:
            data = data.reshape(-1, self.channels).mean(axis=1)
        k = data.shape[0]
        with self._lock:
            buf = self._buffer
            if k >= buf.shape[0]:
                buf[:] = data[-buf.shape[0]:]
            else:
                buf[:-k] = buf[k:]
                buf[-k:] = data
        self._last_data_time = time.perf_counter()
        return (None, pyaudio.paContinue)
