# Music Wallpaper

A music-reactive live wallpaper for Windows 10/11. It listens to whatever your PC is playing (system audio via WASAPI loopback) and animates your desktop behind the icons, in time with the beat.

## Visual styles

| Style | Look | Reacts to |
|---|---|---|
| **Liquid** | Soft flowing colour blobs (GPU shader) | Bass swells the blobs, mids speed up the flow, highs add warp |
| **Particles** | Glowing particles bursting from the centre | Bass launches and enlarges them, beats send shockwaves, highs twinkle |
| **Radial** | Glowing spectrum bars around a pulsing ring | Each bar is a frequency band; bass and beats pulse the ring |

All styles share the colour themes (`dynamic`, `neon`, `sunset`, `ocean`, `aurora`, `ice`) and fall back to a slow breathing idle animation when nothing is playing.

## Features

- Renders behind the desktop icons; your normal wallpaper comes back on exit
- Beat detection with live BPM estimate
- Optional colours from the current Spotify album cover (no login, uses the Windows media session)
- Tray icon with quick theme switch, pause and settings window
- Pauses automatically behind fullscreen or maximised apps
- Drops to 30 fps when idle
- Primary monitor or the whole multi-monitor desktop
- Optional start with Windows (per-user, no admin rights)

## Requirements

- Windows 10 or 11 (x64)
- Python 3.11
- A GPU with OpenGL 2.0 (the Liquid style falls back to a CPU renderer if shaders are unavailable)

## Installation

```powershell
git clone https://github.com/Tyn1707/Music-Wallpaper.git
cd Music-Wallpaper
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The `winrt-*` packages are only needed for Spotify album-art colours; the app runs without them.

## Usage

```powershell
python main.py                # run with a console (shows stats)
pythonw main.py               # run without a console (logs to data\app.log)
python main.py --settings     # open the settings window on start
python main.py --theme neon   # use a theme for this run
python main.py --all-monitors # span all monitors for this run
python main.py --list-themes  # print available themes
```

On the first run the settings window opens. Afterwards, click the tray icon (under `^` next to the clock) or run `main.py` again to open it. Quit from the tray menu or with Ctrl+C in the terminal.

## Settings

User settings are saved to `data\settings.json`: style, theme, album sync, particle count, beat sensitivity and reaction, frame rate, idle animation, pause behaviour and monitor span.

Every internal tunable (FFT bands, beat detection, per-style motion, colours, idle behaviour) lives in [`config.py`](config.py), so the look can be adjusted without touching the other files.

## How it works

```
AudioCapture -> FFTAnalyzer -> Visual (Liquid | Particles | Radial) -> WallpaperWindow
 (WASAPI)      (bands, beats,     (VisualBase subclass)                (embedded behind icons)
               spectrum)
```

| File | Purpose |
|---|---|
| `main.py` | Entry point, frame loop, wiring |
| `audio_capture.py` | System audio capture (WASAPI loopback) |
| `fft_analyzer.py` | Band levels, adaptive gain, beat/BPM detection, spectrum |
| `visual_base.py` | Shared theme, idle and beat state for all styles |
| `liquid_gradient.py` | Liquid style (GLSL shader) |
| `particle_system.py` | Particle style (NumPy simulation) |
| `radial_spectrum.py` | Radial spectrum style |
| `wallpaper_manager.py` | Embeds the window behind the desktop icons |
| `themes.py` | Colour themes |
| `album_art.py` | Spotify album-art colour extraction |
| `settings.py` / `settings_ui.py` | Persisted settings and settings window |
| `tray.py` / `autostart.py` | Tray menu and start-with-Windows |

## Adding a visual style

1. Subclass `VisualBase`, call `self.update_common(features, dt)` in `update()` and draw in `paint_gl(window)`.
2. Add a config dataclass to `config.py`.
3. Register the style name in `STYLE_CHOICES` (`settings.py`), `_make_visual` (`main.py`) and the Style buttons (`settings_ui.py`).

## Troubleshooting

- **No reaction to music:** the app captures the *default* output device. Check the console for the `[audio] capturing ...` line, and that audio is actually playing through that device.
- **Wallpaper disappears after Explorer restarts:** it re-embeds itself within a few seconds.
- **Running without a console:** see `data\app.log` for output and errors.
