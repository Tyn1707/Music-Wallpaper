"""Liquid gradient visual: large soft colour blobs that drift, swell and blend.

Audio mapping
- Bass:   bass blobs swell; beats add a short pulse and brighten everything.
- Mids:   speed up the drift (the "flow").
- Highs:  more liquid warp and brighter highlight blobs.
- Idle:   the shared breathing levels keep it slowly alive when silent.
Colours come from the active Theme (incl. Spotify album-art themes).

Rendering is one full-screen GLSL fragment shader (GPU), so CPU cost is just a
few uniforms per frame. Positions are computed in Python from smooth
Lissajous paths; the shader mixes the blobs Gaussian-weighted (mesh-gradient
look), warps the domain for the liquid motion, and dithers to avoid banding.
If the shader can't be compiled, it falls back to QPainter radial gradients.
"""
from __future__ import annotations

import colorsys
import math

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QPainter, QRadialGradient, QVector2D
from PyQt6.QtOpenGL import (
    QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVersionFunctionsFactory,
    QOpenGLVersionProfile,
)

from config import ColorConfig, IdleConfig, LiquidConfig
from fft_analyzer import AudioFeatures
from visual_base import VisualBase

GL_TRIANGLE_STRIP = 0x0005
BLOBS = 6
BLOB_BANDS = (0, 0, 1, 1, 2, 1)                 # which band drives each blob
BLOB_HUE_OFFSETS = (0.0, 0.035, 0.0, -0.04, 0.0, 0.07)
# Lissajous path parameters per blob: (centre x, centre y, amp x, amp y, freq x, freq y, phase x, phase y)
PATHS = (
    (0.30, 0.38, 0.22, 0.20, 0.71, 0.53, 0.0, 1.3),
    (0.72, 0.62, 0.20, 0.22, 0.59, 0.83, 2.1, 0.4),
    (0.55, 0.30, 0.28, 0.16, 0.92, 0.67, 4.0, 2.7),
    (0.25, 0.72, 0.18, 0.18, 0.63, 1.01, 1.2, 5.1),
    (0.80, 0.25, 0.16, 0.18, 1.13, 0.77, 3.3, 3.9),
    (0.50, 0.65, 0.30, 0.20, 0.49, 0.61, 5.5, 0.9),
)
RADIUS_SCALE = (1.15, 0.95, 1.0, 0.9, 0.7, 0.85)

VERTEX_SHADER = """
#version 120
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
    v_uv = a_pos * 0.5 + 0.5;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

FRAGMENT_SHADER = """
#version 120
varying vec2 v_uv;
uniform vec2  u_res;
uniform float u_time;
uniform float u_warp;
uniform float u_vignette;
uniform vec3  u_bg;
uniform vec3  u_blob[6];   // x, y (aspect-corrected), radius
uniform vec3  u_col[6];    // linear-ish rgb
uniform sampler2D u_tex;   // blurred album cover (unit 0)
uniform float u_tex_mix;   // 0 = no cover, 1 = cover fully faded in
uniform vec4  u_tex_xf;    // cover crop: scale xy, offset zw (texture units)
uniform float u_tex_dim;   // cover brightness

float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }

void main() {
    float aspect = u_res.x / u_res.y;
    vec2 p = vec2(v_uv.x * aspect, 1.0 - v_uv.y);
    float t = u_time;

    // Two layers of smooth domain warp = the "liquid" motion.
    vec2 q = p;
    q.x += u_warp * sin(p.y * 3.1 + t * 0.70);
    q.y += u_warp * sin(p.x * 2.7 - t * 0.60);
    q += 0.5 * u_warp * vec2(sin(q.y * 5.3 + t * 1.10), cos(q.x * 4.7 - t * 0.90));

    vec3 acc = vec3(0.0);
    float wsum = 0.0;
    for (int i = 0; i < 6; i++) {
        vec2 d = q - u_blob[i].xy;
        float r = u_blob[i].z;
        float w = exp(-dot(d, d) / (r * r));
        acc += u_col[i] * w;
        wsum += w;
    }
    vec3 mixcol = acc / max(wsum, 1e-4);         // smooth colour blend between blobs
    float cover = 1.0 - exp(-wsum * 1.8);        // colour vs. background
    vec3 c = mix(u_bg, mixcol, cover);

    if (u_tex_mix > 0.0) {
        // The cover ripples gently with the blob flow; the blobs become soft pools of light on it.
        vec2 tuv = (vec2(v_uv.x, 1.0 - v_uv.y) - 0.5) * u_tex_xf.xy + 0.5 + u_tex_xf.zw;
        tuv += vec2((q.x - p.x) / aspect, q.y - p.y) * u_tex_xf.xy * 0.25;
        vec3 art = texture2D(u_tex, tuv).rgb * u_tex_dim;
        vec3 lit = 1.0 - (1.0 - art) * (1.0 - mixcol * cover * 0.22);   // screen blend
        c = mix(c, lit, u_tex_mix);
    }

    vec2 v = v_uv - 0.5;                         // calm, darker edges
    c *= 1.0 - u_vignette * dot(v, v) * 1.8;

    // ~1.5/255 dither removes 8-bit banding in the dark gradients.
    c += (hash(gl_FragCoord.xy + fract(t * 7.0) * 113.0) - 0.5) * (1.5 / 255.0);
    gl_FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
}
"""


class LiquidGradient(VisualBase):
    name = "liquid"

    def __init__(self, lcfg: LiquidConfig, ccfg: ColorConfig, icfg: IdleConfig) -> None:
        super().__init__(ccfg, icfg)
        self.l = lcfg
        self._flow_t = 0.0
        self._pulse = 0.0
        self.blobs: list[tuple[float, float, float]] = []   # (x, y, r) in shader units
        self.colors: list[tuple[float, float, float]] = []
        self.background = (0.0, 0.0, 0.0)
        self.warp = lcfg.warp
        self._aspect = 16 / 9

        self._program: QOpenGLShaderProgram | None = None
        self._gl = None
        self._ctx = None
        self._shader_failed = False
        self._locs: dict[str, int] = {}
        self._tex: QOpenGLTexture | None = None   # blurred cover on the GPU
        self._tex_id = -1                         # _backdrop_id the texture was built from
        self._quad = [QVector2D(-1, -1), QVector2D(1, -1), QVector2D(-1, 1), QVector2D(1, 1)]

    # ================================================================ simulation
    def update(self, f: AudioFeatures, dt: float, width: float, height: float) -> None:
        l = self.l
        self.update_common(f, dt)
        bass, mid, high = (float(v) for v in self.levels)
        self._aspect = max(width, 1.0) / max(height, 1.0)

        self._flow_t += dt * (l.drift_speed + l.mid_speed * mid) * (1.0 - 0.4 * self.idle)
        self._pulse = max(self._pulse * math.exp(-dt / l.beat_pulse_decay), self.beat_strength)
        self.warp = l.warp + l.high_warp * high

        hues = self.band_hues()
        sat = self.theme.saturation
        t = self._flow_t
        blobs, colors = [], []
        for i, (cx, cy, ax, ay, fx, fy, px, py) in enumerate(PATHS):
            band = BLOB_BANDS[i]
            level = float(self.levels[band])
            x = (cx + ax * math.sin(px + t * fx)) * self._aspect
            y = cy + ay * math.sin(py + t * fy)
            r = l.base_radius * RADIUS_SCALE[i] * (1.0 + l.level_swell * level)
            if band == 0:
                r *= 1.0 + l.beat_pulse * self._pulse
            blobs.append((x, y, r))

            value = min(1.0, l.min_brightness + (l.max_brightness - l.min_brightness) * level
                        + l.beat_brighten * self.flash)
            hue = (float(hues[band]) + BLOB_HUE_OFFSETS[i]) % 1.0
            colors.append(colorsys.hsv_to_rgb(hue, sat, value))
        self.blobs, self.colors = blobs, colors
        self.background = self.background_rgb()

    # ================================================================ rendering
    def paint_gl(self, window) -> None:
        if not self.blobs:
            return
        ctx = window.context()
        if ctx is not self._ctx:          # first frame or window was recreated
            self._init_gl(ctx)
        if self._program is None:
            self._paint_fallback(window)
            return

        dpr = window.devicePixelRatio()
        w, h = int(window.width() * dpr), int(window.height() * dpr)
        gl, prog, loc = self._gl, self._program, self._locs

        # One QPainter for the frame: the raw GL shader runs inside a native-painting
        # block so Qt saves/restores its GL state, then the card is drawn on top.
        painter = QPainter(window)
        try:
            painter.beginNativePainting()
            gl.glViewport(0, 0, w, h)
            prog.bind()
            prog.setUniformValue(loc["u_res"], float(w), float(h))
            prog.setUniformValue(loc["u_time"], float(self.time))
            prog.setUniformValue(loc["u_warp"], float(self.warp))
            prog.setUniformValue(loc["u_vignette"], float(self.l.vignette))
            prog.setUniformValue(loc["u_bg"], *map(float, self.background))
            for i in range(BLOBS):
                prog.setUniformValue(loc[f"u_blob{i}"], *map(float, self.blobs[i]))
                prog.setUniformValue(loc[f"u_col{i}"], *map(float, self.colors[i]))
            tex = self._cover_texture()
            mix = self._backdrop_fade if tex is not None else 0.0
            prog.setUniformValue(loc["u_tex_mix"], float(mix))
            if mix > 0.0:
                tex.bind(0)                    # u_tex defaults to unit 0
                prog.setUniformValue(loc["u_tex_xf"], *self.backdrop_view(w, h))
                prog.setUniformValue(loc["u_tex_dim"], float(self.backdrop_dim()))
            a = loc["a_pos"]
            prog.enableAttributeArray(a)
            prog.setAttributeArray(a, self._quad)
            gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
            prog.disableAttributeArray(a)
            if mix > 0.0:
                tex.release()
            prog.release()
            painter.endNativePainting()

            self.draw_now_playing_card(painter, window.width(), window.height(), self.overlay)
        finally:
            painter.end()

    def _cover_texture(self) -> QOpenGLTexture | None:
        """GPU copy of the blurred cover, re-uploaded when the cover changes (context is current)."""
        if self._backdrop is None:
            return None
        if self._tex is None or self._tex_id != self._backdrop_id:
            if self._tex is not None:
                self._tex.destroy()
            tex = QOpenGLTexture(self._backdrop, QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps)
            tex.setMinMagFilters(QOpenGLTexture.Filter.Linear, QOpenGLTexture.Filter.Linear)
            tex.setWrapMode(QOpenGLTexture.WrapMode.MirroredRepeat)   # warp past the edge stays seamless
            self._tex, self._tex_id = tex, self._backdrop_id
        return self._tex

    def _init_gl(self, ctx) -> None:
        self._ctx = ctx
        self._program = None
        self._tex = None                  # belonged to the old context
        if self._shader_failed:
            return
        profile = QOpenGLVersionProfile()
        profile.setVersion(2, 0)
        self._gl = QOpenGLVersionFunctionsFactory.get(profile, ctx)
        prog = QOpenGLShaderProgram()
        ok = (self._gl is not None
              and prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VERTEX_SHADER)
              and prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT_SHADER)
              and prog.link())
        if not ok:
            print(f"[liquid] shader unavailable, using fallback renderer: {prog.log().strip()}")
            self._shader_failed = True
            return
        locs = {n: prog.uniformLocation(n) for n in ("u_res", "u_time", "u_warp", "u_vignette", "u_bg",
                                                      "u_tex_mix", "u_tex_xf", "u_tex_dim")}
        for i in range(BLOBS):
            locs[f"u_blob{i}"] = prog.uniformLocation(f"u_blob[{i}]")
            locs[f"u_col{i}"] = prog.uniformLocation(f"u_col[{i}]")
        locs["a_pos"] = prog.attributeLocation("a_pos")
        self._locs = locs
        self._program = prog

    def _paint_fallback(self, window) -> None:
        """No shaders: approximate the look with screen-blended radial gradients."""
        w, h = window.width(), window.height()
        p = QPainter(window)
        p.fillRect(0, 0, w, h, QColor.fromRgbF(*self.background))
        self.draw_backdrop(p, w, h)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
        k = 1.0 - 0.75 * self._backdrop_fade      # softer blobs so the cover stays visible
        for (x, y, r), (cr, cg, cb) in zip(self.blobs, self.colors):
            cx, cy, rad = x / self._aspect * w, y * h, r * h * 1.6
            g = QRadialGradient(QPointF(cx, cy), rad)
            g.setColorAt(0.0, QColor.fromRgbF(cr, cg, cb, 0.9 * k))
            g.setColorAt(0.5, QColor.fromRgbF(cr, cg, cb, 0.35 * k))
            g.setColorAt(1.0, QColor.fromRgbF(cr, cg, cb, 0.0))
            p.fillRect(0, 0, w, h, g)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self.draw_now_playing_card(p, w, h, self.overlay)
        p.end()
