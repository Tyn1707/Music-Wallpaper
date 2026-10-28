"""Vinyl turntable visual: the album cover spins as a record on a wooden deck.

Not audio-reactive by design: the record turns at a steady speed. Static parts
(background, deck, grooves, sheen, tonearm) are drawn once per window size into
two cached pixmaps; each frame only draws the rotated cover between them plus
the "ARTIST — TITLE" caption.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QConicalGradient,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)

from config import ColorConfig, IdleConfig, VinylConfig
from fft_analyzer import AudioFeatures
from visual_base import VisualBase, build_cover

# CD jewel cases: (dx, dy, size) in units of min(w, h) from the deck centre, tilt in degrees.
# The first one is opened (black insert, Album4 disc in the tray); the rest show the art in assets/.
CASES = ((0.52, -0.335, 0.235, 14.0), (0.64, -0.02, 0.23, 8.0), (0.635, 0.265, 0.26, 10.0),
         (-0.52, -0.37, 0.2, -9.0))
SPARE_ART = (("Album1.jpg", 11), ("Album2.jpg", 29), ("Album3.jpg", 47))   # file, fallback seed
ASSETS = Path(__file__).resolve().parent / "assets"
SHEETS = ((-0.68, 0.04, -12.0), (-0.62, 0.08, 6.0))   # sheet music on the left: dx, dy, tilt
TURNTABLE_SCALE = 0.62   # deck + record + tonearm size; the props keep their full-size layout

# Where the empty platter sits inside turntable.png, as fractions of the image's width/height,
# and the platter radius as a fraction of image width. Used to line our spinning record up with
# the photo's platter. Tuned by eye against the generated image.
DECK_PLATTER = (0.313, 0.484, 0.205)   # (centre x, centre y, radius)
COVER_FRAC = 0.8   # album cover (centre label) size as a fraction of the record radius
# tonearm.png: pivot and stylus tip as fractions of the image, and where they go in the scene,
# as offsets from the record centre in record radii. The photo is scaled/rotated to fit both.
ARM_IMAGE = ((0.674, 0.254), (0.135, 0.903))   # (pivot, tip)
ARM_SCENE = ((1.6, -0.86), (0.85, 0.42))       # (pivot, tip) -> tip at 0.95 r, in the groove band

NO_PEN = Qt.PenStyle.NoPen
NO_BRUSH = Qt.BrushStyle.NoBrush
ROUND = Qt.PenCapStyle.RoundCap


def _metal(center: QPointF, radius: float, light=(240, 236, 228), dark=(96, 92, 86)) -> QRadialGradient:
    """Brushed-metal disc lit from the upper left."""
    g = QRadialGradient(center - QPointF(radius * 0.35, radius * 0.35), radius * 1.4)
    g.setColorAt(0.0, QColor(*light))
    g.setColorAt(1.0, QColor(*dark))
    return g


def _random_cover(seed: int, size: int = 256) -> QPixmap:
    """Made-up abstract album art for the closed cases; seeded, so it never changes."""
    rng = random.Random(seed)
    hue = rng.random()
    pix = QPixmap(size, size)
    pix.fill(QColor.fromHsvF(hue, 0.55, 0.22))
    p = QPainter(pix)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(NO_PEN)
        for _ in range(8):                                            # soft colour blobs
            colour = QColor.fromHsvF((hue + rng.uniform(-0.12, 0.4)) % 1.0, rng.uniform(0.45, 0.9),
                                     rng.uniform(0.55, 0.95), rng.uniform(0.55, 0.9))
            centre, r = QPointF(rng.uniform(0, size), rng.uniform(0, size)), rng.uniform(40, 150)
            g = QRadialGradient(centre, r)
            g.setColorAt(0.0, colour)
            colour.setAlphaF(0.0)
            g.setColorAt(1.0, colour)
            p.setBrush(g)
            p.drawEllipse(centre, r, r)
        p.setBrush(QColor.fromHsvF((hue + 0.5) % 1.0, 0.35, 0.95, 0.55))   # a title strip
        p.drawRect(QRectF(size * 0.1, size * rng.uniform(0.7, 0.82), size * 0.8, size * 0.035))
    finally:
        p.end()
    return pix


def _load_pixmap(name: str) -> QPixmap | None:
    """Load an image from assets/ as a pixmap, preserving transparency. None if missing."""
    pix = QPixmap(str(ASSETS / name))
    return None if pix.isNull() else pix


def _asset_cover(name: str, seed: int | None, shape: str = "rounded_rect") -> QPixmap | None:
    """Art from assets/ masked to `shape`; if missing/unreadable, made-up art (or None with no seed)."""
    try:
        art = build_cover((ASSETS / name).read_bytes(), shape, 0.0)
    except OSError:
        art = None
    return art if art is not None or seed is None else _random_cover(seed)


class VinylRecord(VisualBase):
    name = "vinyl"

    def __init__(self, vcfg: VinylConfig, ccfg: ColorConfig, icfg: IdleConfig) -> None:
        super().__init__(ccfg, icfg)
        self.v = vcfg
        self._angle = 0.0
        self._layers: tuple | None = None   # (size key, back pixmap, front pixmap)
        self._spares = [_asset_cover(name, seed) for name, seed in SPARE_ART]   # static art for closed cases
        self._disc = _asset_cover("Album4.jpg", None, "circle")   # CD in the open case; empty tray if missing
        self._earphones = _load_pixmap("earphones.png")   # wired earbuds resting on the open case
        self._vinyl = _asset_cover("vinyl_disc.png", None, "circle")   # realistic grooved disc, spins
        self._sheet = _load_pixmap("sheet_music.png")   # realistic sheet music on the left
        self._turntable = _load_pixmap("turntable.png")   # realistic deck + empty platter photo
        self._tonearm = _load_pixmap("tonearm.png")       # stylus photo, drawn above the disc

    # ================================================================ simulation
    def update(self, f: AudioFeatures, dt: float, width: float, height: float) -> None:
        self.update_common(f, dt)            # keeps the caption fade and timing in step
        self._angle = (self._angle + dt * self.v.rpm * 6.0) % 360.0   # rpm -> degrees/s

    # ================================================================ rendering
    def paint_gl(self, window) -> None:
        painter = QPainter(window)
        try:
            self.render(painter, window.width(), window.height())
        finally:
            painter.end()

    @staticmethod
    def _geometry(w: float, h: float) -> tuple[float, float, float, QPointF, float]:
        """Turntable unit, deck centre x/y, record centre, record radius (proportions of the mockup)."""
        u = min(w, h) * TURNTABLE_SCALE
        cx, cy = w * 0.5, h * 0.49
        radius = 0.265 * u
        # Platter sits left of centre in the deck photo; shift so the whole deck is centred.
        return u, cx, cy, QPointF(cx + (DECK_PLATTER[0] - 0.5) * radius / DECK_PLATTER[2], cy), radius

    def render(self, painter: QPainter, width: int, height: int) -> None:
        dpr = painter.device().devicePixelRatioF()
        key = (width, height, dpr)
        if self._layers is None or self._layers[0] != key:
            self._layers = (key, self._layer(width, height, dpr, self._paint_back),
                            self._layer(width, height, dpr, self._paint_front))
        _, back, front = self._layers

        painter.drawPixmap(QPointF(0, 0), back)
        self._paint_cases(painter, width, height)   # per frame: their art follows the current track
        _, _, _, c, radius = self._geometry(width, height)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self._vinyl is not None:                 # grooved disc photo spins beneath the label
            painter.save()
            painter.translate(c)
            painter.rotate(self._angle)
            painter.drawPixmap(QRectF(-radius, -radius, 2 * radius, 2 * radius),
                               self._vinyl, QRectF(self._vinyl.rect()))
            painter.restore()
        cover = self._overlay_cover("circle", 0.0)
        if cover is not None:
            label = radius * COVER_FRAC             # cover sits as the centre label of the record
            painter.save()
            painter.translate(c)
            painter.rotate(self._angle)
            painter.drawPixmap(QRectF(-label, -label, 2 * label, 2 * label), cover, QRectF(cover.rect()))
            painter.restore()
        painter.drawPixmap(QPointF(0, 0), front)   # grooves/sheen stay put, like real reflections
        self._paint_caption(painter, width, height)

    @staticmethod
    def _layer(w: int, h: int, dpr: float, paint) -> QPixmap:
        pix = QPixmap(round(w * dpr), round(h * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            paint(p, w, h)
        finally:
            p.end()
        return pix

    # ---------------------------------------------------------------- back layer
    def _paint_back(self, p: QPainter, w: int, h: int) -> None:
        u, cx, cy, c, radius = self._geometry(w, h)
        p.fillRect(QRectF(0, 0, w, h), QColor(12, 9, 7))

        spot = QRadialGradient(QPointF(cx, cy - 0.3 * u), 0.95 * u)   # warm overhead lamp
        spot.setColorAt(0.0, QColor(96, 66, 38, 150))
        spot.setColorAt(0.45, QColor(42, 29, 18, 90))
        spot.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, 0, w, h), spot)
        self._paint_sheets(p, min(w, h), cx, cy)

        if self._turntable is not None:
            self._paint_deck_photo(p, c, radius)
            return
        self._paint_deck_vector(p, u, cx, cy, c, radius)

    def _paint_deck_photo(self, p: QPainter, c: QPointF, radius: float) -> None:
        """Draw the turntable photo so its platter lines up with our spinning record.

        The record (radius `radius`, centred on `c`) is drawn later on top. We scale the
        photo so its platter matches the record and anchor the platter centre on `c`.
        """
        pix = self._turntable
        pcx, pcy, pr = DECK_PLATTER
        scale = radius / (pr * pix.width())              # photo px -> scene px so the platter matches
        dw, dh = pix.width() * scale, pix.height() * scale
        dest = QRectF(c.x() - pcx * dw, c.y() - pcy * dh, dw, dh)   # platter centre lands on c
        src = QRectF(pix.rect())

        shadow = self._tinted_shadow(pix)                # soft contact shadow under the whole unit
        if shadow is not None:
            p.setOpacity(0.33)
            for off in (0.02 * radius, 0.012 * radius):
                p.drawPixmap(dest.translated(off, off * 1.4), shadow, src)
            p.setOpacity(1.0)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(dest, pix, src)

    def _paint_deck_vector(self, p: QPainter, u: float, cx: float, cy: float,
                           c: QPointF, radius: float) -> None:
        deck = QRectF(cx - 0.40 * u, cy - 0.29 * u, 0.80 * u, 0.58 * u)
        corner = 0.02 * u
        p.setPen(NO_PEN)
        for i in range(8, 0, -1):                                     # soft drop shadow
            s = i * 0.005 * u
            p.setBrush(QColor(0, 0, 0, 16))
            p.drawRoundedRect(deck.translated(0, 0.015 * u).adjusted(-s, -s, s, s), corner + s, corner + s)

        wood = QLinearGradient(deck.topLeft(), deck.bottomRight())
        wood.setColorAt(0.0, QColor(98, 66, 41))
        wood.setColorAt(0.5, QColor(70, 45, 28))
        wood.setColorAt(1.0, QColor(42, 27, 17))
        p.setBrush(wood)
        p.drawRoundedRect(deck, corner, corner)

        p.save()                                                      # wood grain
        clip = QPainterPath()
        clip.addRoundedRect(deck, corner, corner)
        p.setClipPath(clip)
        rng = random.Random(7)                                        # fixed seed: same grain every rebuild
        for _ in range(160):
            y = rng.uniform(deck.top(), deck.bottom())
            p.setPen(QPen(QColor(22, 13, 7, rng.randint(12, 40)), rng.uniform(0.6, 1.8)))
            p.drawLine(QPointF(deck.left(), y), QPointF(deck.right(), y + rng.uniform(-0.012, 0.012) * u))
        light = QRadialGradient(QPointF(cx, cy - 0.3 * u), 0.7 * u)
        light.setColorAt(0.0, QColor(255, 190, 120, 50))
        light.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(deck, light)
        p.restore()

        p.setBrush(NO_BRUSH)                                          # bevel: recessed inner edge + lit rim
        inset = 0.018 * u
        p.setPen(QPen(QColor(0, 0, 0, 90), max(1.0, 0.004 * u)))
        p.drawRoundedRect(deck.adjusted(inset, inset, -inset, -inset), corner * 0.6, corner * 0.6)
        p.setPen(QPen(QColor(255, 220, 170, 30), 1.0))
        p.drawRoundedRect(deck.adjusted(0.5, 0.5, -0.5, -0.5), corner, corner)

        p.setPen(NO_PEN)                                              # platter shadow + bare black vinyl
        shadow = QRadialGradient(c + QPointF(0.01 * u, 0.015 * u), radius * 1.12)
        shadow.setColorAt(0.85, QColor(0, 0, 0, 170))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(shadow)
        p.drawEllipse(c + QPointF(0.01 * u, 0.015 * u), radius * 1.12, radius * 1.12)
        rim = QLinearGradient(c - QPointF(radius, radius), c + QPointF(radius, radius))   # aluminium platter edge
        rim.setColorAt(0.0, QColor(205, 200, 192))
        rim.setColorAt(0.5, QColor(95, 92, 88))
        rim.setColorAt(1.0, QColor(150, 146, 140))
        p.setBrush(rim)
        p.drawEllipse(c, radius * 1.035, radius * 1.035)
        p.setBrush(QColor(16, 14, 13))
        p.drawEllipse(c, radius * 1.012, radius * 1.012)

        knob = QPointF(cx - 0.32 * u, cy + 0.21 * u)                  # start/stop knob
        kr = 0.022 * u
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawEllipse(knob + QPointF(0.004 * u, 0.006 * u), kr, kr)
        p.setBrush(_metal(knob, kr))
        p.drawEllipse(knob, kr, kr)

    def _paint_sheets(self, p: QPainter, u: float, cx: float, cy: float) -> None:
        """Realistic sheet-music photos scattered on the left; vector fallback if missing."""
        pix = self._sheet
        if pix is None:
            self._paint_sheets_vector(u, cx, cy, p)
            return
        aspect = pix.height() / pix.width() if pix.width() else 1.3
        pw = 0.36 * u
        ph = pw * aspect
        src = QRectF(pix.rect())
        shadow = self._tinted_shadow(pix)                            # paper-shaped silhouette, not a box
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        for dx, dy, tilt in SHEETS:
            p.save()
            p.translate(cx + dx * u, cy + dy * u)
            p.rotate(tilt)
            dest = QRectF(-pw / 2, -ph / 2, pw, ph)
            if shadow is not None:                                   # soft contact shadow, offset twice
                p.setOpacity(0.35)
                for off in (0.016 * u, 0.009 * u):
                    p.drawPixmap(dest.translated(off, off * 1.3), shadow, src)
            p.setOpacity(0.92)                                       # settle into the dim lamplight
            p.drawPixmap(dest, pix, src)
            p.setOpacity(1.0)
            p.restore()

    @staticmethod
    def _tinted_shadow(pix: QPixmap) -> QPixmap | None:
        """A black copy of `pix` keeping its alpha, used as a shape-matched drop shadow."""
        if pix.isNull():
            return None
        shadow = QPixmap(pix.size())
        shadow.fill(Qt.GlobalColor.transparent)
        p = QPainter(shadow)
        try:
            p.drawPixmap(0, 0, pix)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            p.fillRect(shadow.rect(), QColor(0, 0, 0))
        finally:
            p.end()
        return shadow

    @staticmethod
    def _paint_sheets_vector(u: float, cx: float, cy: float, p: QPainter) -> None:
        rng = random.Random(3)                                        # fixed seed: same notes every rebuild
        pw, ph = 0.36 * u, 0.5 * u
        for dx, dy, tilt in SHEETS:
            p.save()
            p.translate(cx + dx * u, cy + dy * u)
            p.rotate(tilt)
            sheet = QRectF(-pw / 2, -ph / 2, pw, ph)
            p.setPen(NO_PEN)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRect(sheet.translated(0.006 * u, 0.01 * u))
            paper = QLinearGradient(sheet.topRight(), sheet.bottomLeft())   # lit from the lamp side
            paper.setColorAt(0.0, QColor(132, 114, 88))
            paper.setColorAt(1.0, QColor(66, 55, 42))
            p.setBrush(paper)
            p.drawRect(sheet)
            left, right = sheet.left() + pw * 0.08, sheet.right() - pw * 0.08
            for g in range(7):                                        # staves with a few note heads
                y0 = sheet.top() + ph * (0.1 + g * 0.12)
                p.setPen(QPen(QColor(40, 30, 20, 110), 0.8))
                for k in range(5):
                    y = y0 + k * ph * 0.012
                    p.drawLine(QPointF(left, y), QPointF(right, y))
                p.setPen(NO_PEN)
                p.setBrush(QColor(35, 26, 18, 150))
                for _ in range(rng.randint(5, 9)):
                    x = rng.uniform(left + pw * 0.06, right)
                    y = y0 + rng.randint(0, 8) * ph * 0.006
                    p.drawEllipse(QPointF(x, y), ph * 0.006, ph * 0.0045)
            p.restore()

    def _paint_cases(self, p: QPainter, w: int, h: int) -> None:
        _, cx, cy, _, _ = self._geometry(w, h)
        u = min(w, h)
        disc = self._disc
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        for i, (dx, dy, size, tilt) in enumerate(CASES):
            art_src = None if i == 0 else self._spares[i - 1]          # the open case has a plain black insert
            s = size * u
            p.save()
            p.translate(cx + dx * u, cy + dy * u)
            p.rotate(tilt)
            art = QRectF(-s / 2, -s / 2, s, s)
            box = art.adjusted(0, 0, s * 0.85, 0) if i == 0 else art   # opened case: art + disc tray
            frame = box.adjusted(-0.03 * s, -0.03 * s, 0.03 * s, 0.03 * s)

            p.setPen(NO_PEN)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRoundedRect(frame.translated(0.008 * u, 0.012 * u), 4, 4)
            p.setBrush(QColor(28, 27, 30))
            p.drawRoundedRect(frame, 4, 4)
            if art_src is not None:
                p.drawPixmap(art, art_src, QRectF(art_src.rect()))
            else:
                p.setBrush(QColor(8, 8, 9))
                p.drawRect(art)

            if i == 0:
                dc, r = QPointF(s / 2 + s * 0.425, 0), s * 0.4
                p.setBrush(QColor(16, 16, 18))                        # tray recess
                p.drawEllipse(dc, r * 1.05, r * 1.05)
                if disc is not None:
                    p.drawPixmap(QRectF(dc.x() - r, dc.y() - r, 2 * r, 2 * r), disc, QRectF(disc.rect()))
                p.setBrush(QColor(28, 27, 30))                        # centre hole
                p.drawEllipse(dc, r * 0.16, r * 0.16)
                p.setBrush(NO_BRUSH)
                p.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
                p.drawEllipse(dc, r * 0.3, r * 0.3)
                p.setPen(QPen(QColor(0, 0, 0, 160), 1.5))             # hinge
                p.drawLine(QPointF(s / 2, frame.top()), QPointF(s / 2, frame.bottom()))

            p.setPen(NO_PEN)                                          # out of the lamp's light + plastic gloss
            p.setBrush(QColor(0, 0, 0, 115))
            p.drawRect(frame)
            gloss = QLinearGradient(frame.topLeft(), frame.bottomRight())
            gloss.setColorAt(0.0, QColor(255, 255, 255, 30))
            gloss.setColorAt(0.45, QColor(255, 255, 255, 0))
            p.setBrush(gloss)
            p.drawRect(frame)
            p.setBrush(NO_BRUSH)
            p.setPen(QPen(QColor(255, 255, 255, 28), 1.0))
            p.drawRoundedRect(frame, 4, 4)
            if i == 0:
                self._paint_earphones(p, s)
            p.restore()

    def _paint_earphones(self, p: QPainter, s: float) -> None:
        """Wired earbuds resting on the open case, in its rotated frame (s = case size).

        Draws the transparent earphone photo in assets/; if it is missing, nothing
        is drawn (the case simply shows without earbuds).
        """
        pix = self._earphones
        if pix is None:
            return
        # Lay the image over the art and spilling down-left, roughly matching where the
        # old vector earbuds + cable sat. Centred on the case art, scaled to ~1.5x its size.
        side = 1.5 * s
        dest = QRectF(-side * 0.55, -side * 0.62, side, side)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.setOpacity(0.96)                                            # sit it into the lamp-lit scene
        p.drawPixmap(dest, pix, QRectF(pix.rect()))
        p.setOpacity(1.0)

    # ---------------------------------------------------------------- front layer
    def _paint_front(self, p: QPainter, w: int, h: int) -> None:
        u, cx, cy, c, radius = self._geometry(w, h)
        label = radius * COVER_FRAC

        # The grooves themselves come from the spinning photo drawn in render(); this fixed
        # layer only adds lighting that should stay put, like real reflections on the disc.
        if self._vinyl is None:                                      # fallback tint only when no photo
            p.setPen(NO_PEN)
            shade = QRadialGradient(c, radius)
            shade.setColorAt(0.0, QColor(0, 0, 0, 30))
            shade.setColorAt(0.85, QColor(0, 0, 0, 55))
            shade.setColorAt(1.0, QColor(0, 0, 0, 140))
            p.setBrush(shade)
            p.drawEllipse(c, radius, radius)

        sheen = QConicalGradient(c, 35.0)                             # anisotropic "butterfly" highlight
        for pos, a in ((0.0, 0), (0.025, 18), (0.055, 78), (0.085, 18), (0.12, 0),
                       (0.5, 0), (0.525, 14), (0.555, 58), (0.585, 14), (0.62, 0), (1.0, 0)):
            sheen.setColorAt(pos, QColor(255, 242, 225, a))
        ring = QPainterPath()
        ring.addEllipse(c, radius, radius)
        hole = QPainterPath()
        hole.addEllipse(c, label, label)
        p.setPen(NO_PEN)
        p.setBrush(sheen)
        p.drawPath(ring.subtracted(hole))
        gloss = QLinearGradient(c - QPointF(0, radius), c)            # soft overhead lamp gloss
        gloss.setColorAt(0.0, QColor(255, 235, 210, 26))
        gloss.setColorAt(1.0, QColor(255, 235, 210, 0))
        p.setBrush(gloss)
        p.drawEllipse(c, radius, radius)

        p.setBrush(NO_BRUSH)
        p.setPen(QPen(QColor(0, 0, 0, 90), max(1.0, radius * 0.008)))   # label step
        p.drawEllipse(c, label, label)
        p.setPen(QPen(QColor(255, 235, 210, 30), 1.0))
        p.drawEllipse(c, label - radius * 0.006, label - radius * 0.006)
        p.setPen(QPen(QColor(8, 7, 6, 235), max(1.5, radius * 0.014)))  # bevelled outer edge
        p.drawEllipse(c, radius, radius)
        p.setPen(QPen(QColor(255, 230, 200, 40), 1.0))
        p.drawEllipse(c, radius * 0.99, radius * 0.99)

        sp = radius * 0.028                                           # spindle with a contact shadow
        p.setPen(NO_PEN)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawEllipse(c + QPointF(sp * 0.3, sp * 0.45), sp * 1.1, sp * 1.1)
        p.setBrush(_metal(c, sp))
        p.drawEllipse(c, sp, sp)

        if self._tonearm is not None:       # stylus on top of the disc (deck photo has no arm)
            self._paint_tonearm_photo(p, c, radius)
        else:
            self._paint_tonearm(p, u, cx, cy)

    def _paint_tonearm_photo(self, p: QPainter, c: QPointF, radius: float) -> None:
        """Scale and rotate the tonearm photo so its pivot and stylus tip land on ARM_SCENE."""
        pix = self._tonearm
        (ipx, ipy), (itx, ity) = ARM_IMAGE
        (spx, spy), (stx, sty) = ARM_SCENE
        img_vec = QPointF((itx - ipx) * pix.width(), (ity - ipy) * pix.height())
        scn_vec = QPointF((stx - spx) * radius, (sty - spy) * radius)
        scale = math.hypot(scn_vec.x(), scn_vec.y()) / math.hypot(img_vec.x(), img_vec.y())
        turn = math.degrees(math.atan2(scn_vec.y(), scn_vec.x()) - math.atan2(img_vec.y(), img_vec.x()))
        # Image rect positioned so the pivot is at the origin; rotation happens about the pivot.
        dest = QRectF(-ipx * pix.width() * scale, -ipy * pix.height() * scale,
                      pix.width() * scale, pix.height() * scale)
        src = QRectF(pix.rect())
        shadow = self._tinted_shadow(pix)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.save()
        p.translate(c + QPointF(spx * radius, spy * radius))
        p.rotate(turn)
        if shadow is not None:                      # shadow falls across the record below
            p.setOpacity(0.4)
            p.drawPixmap(dest.translated(0.05 * radius, 0.07 * radius), shadow, src)
            p.setOpacity(1.0)
        p.drawPixmap(dest, pix, src)
        p.restore()

    @staticmethod
    def _paint_tonearm(p: QPainter, u: float, cx: float, cy: float) -> None:
        pivot = QPointF(cx + 0.24 * u, cy - 0.17 * u)
        tip = QPointF(cx + 0.085 * u, cy + 0.125 * u)                 # stylus resting on the outer grooves
        d = tip - pivot
        heading = math.degrees(math.atan2(d.y(), d.x()))
        shadow_off = QPointF(0.012 * u, 0.018 * u)
        arm_w = 0.011 * u

        def part(at: QPointF, rect: QRectF, brush, radius: float) -> None:
            for offset, fill in ((shadow_off, QColor(0, 0, 0, 110)), (QPointF(0, 0), brush)):
                p.save()
                p.translate(at + offset)
                p.rotate(heading)
                p.setPen(NO_PEN)
                p.setBrush(fill)
                p.drawRoundedRect(rect, radius, radius)
                p.restore()

        steel = QLinearGradient(0, -0.022 * u, 0, 0.022 * u)          # in the rotated frame: across the part
        steel.setColorAt(0.0, QColor(225, 220, 212))
        steel.setColorAt(0.5, QColor(150, 146, 138))
        steel.setColorAt(1.0, QColor(80, 77, 72))

        part(pivot, QRectF(-0.07 * u, -0.02 * u, 0.045 * u, 0.04 * u), steel, 0.006 * u)     # counterweight

        p.setPen(QPen(QColor(0, 0, 0, 110), arm_w, cap=ROUND))        # arm tube
        p.drawLine(pivot + shadow_off, tip + shadow_off)
        p.setPen(QPen(QColor(150, 146, 138), arm_w, cap=ROUND))
        p.drawLine(pivot, tip)
        p.setPen(QPen(QColor(238, 233, 225, 170), arm_w * 0.35, cap=ROUND))
        p.drawLine(pivot - QPointF(1.0, 1.0), tip - QPointF(1.0, 1.0))

        part(tip, QRectF(-0.006 * u, -0.017 * u, 0.042 * u, 0.034 * u), steel, 0.004 * u)    # headshell

        p.setPen(NO_PEN)                                              # pivot base + cap
        for r, brush in ((0.042 * u, QColor(0, 0, 0, 120)), (0.04 * u, None), (0.018 * u, None)):
            centre = pivot + (shadow_off if brush is not None else QPointF(0, 0))
            p.setBrush(brush if brush is not None else _metal(pivot, r))
            p.drawEllipse(centre, r, r)

    # ---------------------------------------------------------------- caption
    def _paint_caption(self, p: QPainter, w: int, h: int) -> None:
        if self._np_fade <= 0.0 or not self._np_title:
            return
        u = min(w, h)
        text = f"{self._np_artist} — {self._np_title}" if self._np_artist else self._np_title
        font = QFont(p.font())
        font.setPixelSize(max(11, round(u * 0.019)))
        font.setWeight(QFont.Weight.Light)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, u * 0.0045)
        m = QFontMetricsF(font)
        text = m.elidedText(text.upper(), Qt.TextElideMode.ElideRight, int(w * 0.8))
        p.setFont(font)
        p.setPen(QColor(215, 200, 180, int(190 * self._np_fade)))
        p.drawText(QPointF((w - m.horizontalAdvance(text)) * 0.5, h * 0.905), text)
