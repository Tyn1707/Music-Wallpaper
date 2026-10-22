"""Settings window (PyQt6 widgets).

Design tokens follow the project's design system (Dark Mode OLED, green accent):
changes apply live and are saved automatically. Every control is keyboard
reachable with a visible focus ring, has an accessible name, and animations
are skipped when Windows' "Animation effects" setting is off.
"""
from __future__ import annotations

import colorsys
import ctypes
from dataclasses import replace
from typing import Callable

from PyQt6.QtCore import QEasingCurve, QRectF, QSize, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PyQt6.QtWidgets import (
    QAbstractButton, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

import autostart
from settings import FPS_CHOICES, IDLE_DELAY_RANGE, PARTICLE_RANGE, Settings
from themes import THEMES, Theme

# ---------------------------------------------------------------------- tokens
C = {
    "background": "#0F0F23",
    "card": "#1B1B30",
    "card_hover": "#232340",
    "muted": "#27273B",
    "muted_fg": "#94A3B8",
    "fg": "#F8FAFC",
    "border": "#312E81",
    "border_subtle": "#2E2E48",
    "secondary": "#4338CA",
    "accent": "#22C55E",
    "accent_hover": "#4ADE80",
    "accent_pressed": "#16A34A",
    "on_accent": "#0F172A",
    "ring": "#FFFFFF",
    "danger": "#EF4444",
    "switch_off_track": "#334155",
    "switch_off_knob": "#CBD5E1",
}
RADIUS = 12
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}

STYLESHEET = f"""
QWidget#root {{ background: {C['background']}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel {{ color: {C['fg']}; font-size: 10pt; }}
QLabel#title {{ font-size: 16pt; font-weight: 600; }}
QLabel#status {{ color: {C['accent']}; font-size: 9pt; font-weight: 600; }}
QLabel#sectionTitle {{ font-size: 11pt; font-weight: 600; }}
QLabel#muted {{ color: {C['muted_fg']}; font-size: 9pt; }}
QLabel#value {{ color: {C['muted_fg']}; font-size: 10pt; font-weight: 600; }}
QLabel#error {{ color: {C['danger']}; font-size: 9pt; }}
QFrame#card {{ background: {C['card']}; border: 1px solid {C['border_subtle']}; border-radius: {RADIUS}px; }}
QFrame#divider {{ background: {C['border_subtle']}; max-height: 1px; min-height: 1px; border: none; }}

QSlider {{ min-height: 28px; background: transparent; border: 2px solid transparent; border-radius: 8px; }}
QSlider::add-page:horizontal {{ background: {C['switch_off_track']}; border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 6px; background: {C['switch_off_track']}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {C['accent']}; border-radius: 3px; }}
QSlider::sub-page:horizontal:disabled {{ background: {C['muted_fg']}; }}
QSlider::handle:horizontal {{ background: {C['fg']}; width: 18px; height: 18px; margin: -6px 0; border-radius: 9px; }}
QSlider::handle:horizontal:hover {{ background: {C['accent_hover']}; }}
QSlider::handle:horizontal:disabled {{ background: {C['muted_fg']}; }}
QSlider:focus {{ border: 2px solid {C['ring']}; }}

QPushButton {{ min-height: 36px; padding: 0 18px; border-radius: 8px; font-size: 10pt; font-weight: 600; }}
QPushButton#primary {{ background: {C['accent']}; color: {C['on_accent']}; border: 2px solid {C['accent']}; }}
QPushButton#primary:hover {{ background: {C['accent_hover']}; border-color: {C['accent_hover']}; }}
QPushButton#primary:pressed {{ background: {C['accent_pressed']}; border-color: {C['accent_pressed']}; }}
QPushButton#secondary {{ background: transparent; color: {C['fg']}; border: 1px solid {C['border']}; }}
QPushButton#secondary:hover {{ background: {C['muted']}; }}
QPushButton#secondary:pressed {{ background: {C['border']}; }}
QPushButton#segment {{ background: {C['muted']}; color: {C['muted_fg']}; border: 1px solid {C['muted']};
                       border-radius: 8px; padding: 0 14px; min-width: 44px; }}
QPushButton#segment:hover {{ color: {C['fg']}; }}
QPushButton#segment:checked {{ background: {C['secondary']}; color: {C['fg']}; border-color: {C['secondary']}; }}
QPushButton:focus {{ border: 2px solid {C['ring']}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {C['muted']}; border-radius: 4px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {C['border']}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QToolTip {{ background: {C['card']}; color: {C['fg']}; border: 1px solid {C['border']}; padding: 6px; }}
"""


def reduced_motion() -> bool:
    """True if Windows 'Animation effects' is turned off."""
    SPI_GETCLIENTAREAANIMATION = 0x1042
    enabled = ctypes.c_bool(True)
    try:
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
    except OSError:
        return False
    return not enabled.value


def use_dark_title_bar(hwnd: int) -> None:
    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
    value = ctypes.c_int(1)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        pass


def theme_colors(theme: Theme) -> list[QColor]:
    return [QColor.fromRgbF(*colorsys.hsv_to_rgb(h, theme.saturation, 1.0)) for h in theme.band_hues]


# ---------------------------------------------------------------------- widgets
class ToggleSwitch(QAbstractButton):
    """Accessible on/off switch (Space toggles, focus ring, 3:1+ graphic contrast)."""

    def __init__(self, accessible_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(accessible_name)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(52, 36)  # 36 px tall hit area around a 26 px track

    def _on_anim(self, value) -> None:
        self._pos = float(value)
        self.update()

    def _animate(self, checked: bool) -> None:
        end = 1.0 if checked else 0.0
        if reduced_motion():
            self._pos = end
            self.update()
            return
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(end)
        self._anim.start()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 (Qt naming)
        super().setChecked(checked)
        if self._anim.state() != QVariantAnimation.State.Running:
            self._pos = 1.0 if checked else 0.0
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.4)
        track = QRectF(3, (self.height() - 26) / 2, 46, 26)
        off, on = QColor(C["switch_off_track"]), QColor(C["accent"])
        t = self._pos
        mix = QColor.fromRgbF(off.redF() + (on.redF() - off.redF()) * t,
                              off.greenF() + (on.greenF() - off.greenF()) * t,
                              off.blueF() + (on.blueF() - off.blueF()) * t)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix)
        p.drawRoundedRect(track, 13, 13)
        knob = QRectF(track.x() + 3 + t * 20, track.y() + 3, 20, 20)
        p.setBrush(QColor(C["on_accent"]) if t > 0.5 else QColor(C["switch_off_knob"]))
        p.drawEllipse(knob)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(C["ring"]), 2))
            p.drawRoundedRect(track.adjusted(-3, -3, 3, 3), 16, 16)


class ThemeCard(QAbstractButton):
    """Selectable theme swatch."""

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.colors = theme_colors(theme)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{theme.name.capitalize()} theme")
        self.setToolTip(f"{theme.name.capitalize()} theme")
        self.setMinimumSize(120, 76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(140, 76)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        checked = self.isChecked()
        bg = QColor(C["card_hover"] if self.underMouse() else C["muted"])
        p.setBrush(bg)
        p.setPen(QPen(QColor(C["accent"] if checked else C["border_subtle"]), 2 if checked else 1))
        p.drawRoundedRect(r, 10, 10)

        # Three overlapping colour dots (bass / mid / high).
        p.setPen(QPen(bg, 2))
        for i, color in enumerate(self.colors):
            p.setBrush(color)
            p.drawEllipse(QRectF(14 + i * 15, 14, 22, 22))

        p.setPen(QColor(C["fg"]))
        f = QFont(self.font())
        f.setPointSizeF(10)
        f.setWeight(QFont.Weight.DemiBold if checked else QFont.Weight.Normal)
        p.setFont(f)
        p.drawText(QRectF(14, 44, r.width() - 28, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.theme.name.capitalize())

        if checked:  # check badge so selection isn't conveyed by colour alone
            badge = QRectF(r.right() - 26, r.top() + 8, 18, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C["accent"]))
            p.drawEllipse(badge)
            path = QPainterPath()
            path.moveTo(badge.left() + 5, badge.center().y())
            path.lineTo(badge.left() + 8, badge.bottom() - 5)
            path.lineTo(badge.right() - 4.5, badge.top() + 5.5)
            p.setPen(QPen(QColor(C["on_accent"]), 2, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(C["ring"]), 2))
            p.drawRoundedRect(r.adjusted(3, 3, -3, -3), 8, 8)


def _label(text: str, name: str | None = None, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


class Card(QFrame):
    def __init__(self, title: str, subtitle: str | None = None) -> None:
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(SPACE["lg"] + 4, SPACE["lg"], SPACE["lg"] + 4, SPACE["lg"])
        self.body.setSpacing(SPACE["md"])
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(_label(title, "sectionTitle"))
        if subtitle:
            head.addWidget(_label(subtitle, "muted", wrap=True))
        self.body.addLayout(head)

    def divider(self) -> None:
        line = QFrame()
        line.setObjectName("divider")
        self.body.addWidget(line)


class ToggleRow(QWidget):
    def __init__(self, title: str, description: str, on_change: Callable[[bool], None]) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["lg"])
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = _label(title)
        self.description = _label(description, "muted", wrap=True)
        text.addWidget(self.title)
        text.addWidget(self.description)
        row.addLayout(text, 1)
        self.switch = ToggleSwitch(title)
        self.switch.setAccessibleDescription(description)
        self.switch.toggled.connect(on_change)
        row.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)

    def mouseReleaseEvent(self, event) -> None:
        # Clicking the label text toggles too (bigger target).
        if event.button() == Qt.MouseButton.LeftButton and self.switch.isEnabled():
            self.switch.toggle()
            self.switch.setFocus(Qt.FocusReason.MouseFocusReason)

    def set_checked(self, value: bool) -> None:
        self.switch.blockSignals(True)
        self.switch.setChecked(value)
        self.switch.blockSignals(False)


class SliderRow(QWidget):
    def __init__(self, title: str, lo: int, hi: int, step: int,
                 fmt: Callable[[int], str], on_change: Callable[[int], None],
                 hint_left: str | None = None, hint_right: str | None = None) -> None:
        super().__init__()
        self._fmt = fmt
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["xs"])
        head = QHBoxLayout()
        head.addWidget(_label(title))
        head.addStretch(1)
        self.value = _label("", "value")
        head.addWidget(self.value)
        col.addLayout(head)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.setSingleStep(step)
        self.slider.setPageStep(step * 5)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.setAccessibleName(title)
        self.slider.valueChanged.connect(self._changed)
        self._on_change = on_change
        self._step = step
        col.addWidget(self.slider)

        if hint_left or hint_right:
            hints = QHBoxLayout()
            hints.addWidget(_label(hint_left or "", "muted"))
            hints.addStretch(1)
            hints.addWidget(_label(hint_right or "", "muted"))
            col.addLayout(hints)

    def _changed(self, v: int) -> None:
        snapped = round(v / self._step) * self._step
        if snapped != v:
            self.slider.setValue(snapped)  # re-enters with the snapped value
            return
        self.value.setText(self._fmt(v))
        self.slider.setAccessibleDescription(self._fmt(v))
        self._on_change(v)

    def set_value(self, v: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        self.value.setText(self._fmt(v))


# ---------------------------------------------------------------------- window
class SettingsWindow(QWidget):
    settings_changed = pyqtSignal(object)  # Settings

    def __init__(self, settings: Settings, icon: QIcon, status_provider: Callable[[], str],
                 album_available: bool) -> None:
        super().__init__()
        self._settings = settings
        self._status_provider = status_provider
        self._album_available = album_available

        self.setWindowTitle("Music Wallpaper — Settings")
        self.setWindowIcon(icon)
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLESHEET)
        f = QFont("Segoe UI Variable Text")
        f.setStyleHint(QFont.StyleHint.SansSerif)
        f.setPointSizeF(10)
        self.setFont(f)
        self.resize(560, 760)
        self.setMinimumSize(440, 420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(SPACE["xl"], SPACE["xl"], SPACE["xl"], SPACE["lg"])
        col.setSpacing(SPACE["lg"])
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # Header
        header = QVBoxLayout()
        header.setSpacing(2)
        header.addWidget(_label("Music Wallpaper", "title"))
        self.status = _label("", "status")
        self.status.setAccessibleName("Visualizer status")
        header.addWidget(self.status)
        col.addLayout(header)
        col.addWidget(_label("Changes apply instantly and are saved automatically. "
                             "After closing, reopen from the tray icon (under ^ by the clock) "
                             "or by running main.py again.", "muted", wrap=True))

        col.addWidget(self._build_theme_card())
        col.addWidget(self._build_visuals_card())
        col.addWidget(self._build_idle_card())
        col.addWidget(self._build_system_card())
        col.addStretch(1)

        # Footer
        footer_frame = QFrame()
        footer_frame.setObjectName("footer")
        footer_frame.setStyleSheet(
            f"QFrame#footer {{ background: {C['card']}; border-top: 1px solid {C['border_subtle']}; }}")
        footer = QHBoxLayout(footer_frame)
        footer.setContentsMargins(SPACE["xl"], SPACE["md"], SPACE["xl"], SPACE["md"])
        reset = QPushButton("Reset to defaults")
        reset.setObjectName("secondary")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.clicked.connect(self._reset)
        done = QPushButton("Done")
        done.setToolTip("Hide this window. The wallpaper keeps running in the tray.")
        done.setObjectName("primary")
        done.setCursor(Qt.CursorShape.PointingHandCursor)
        done.setDefault(True)
        done.clicked.connect(self.hide)
        footer.addWidget(reset)
        footer.addStretch(1)
        footer.addWidget(done)
        outer.addWidget(footer_frame)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.hide)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status)

        self.set_settings(settings)

    # -- sections ------------------------------------------------------------
    def _build_theme_card(self) -> Card:
        card = Card("Theme", "Colours for bass, mids and highs (and the blobs in Liquid style).")
        grid = QGridLayout()
        grid.setSpacing(SPACE["sm"] + 2)
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        self.theme_cards: dict[str, ThemeCard] = {}
        for i, theme in enumerate(THEMES.values()):
            tc = ThemeCard(theme)
            tc.clicked.connect(lambda _=False, n=theme.name: self._update(theme=n))
            self._theme_group.addButton(tc)
            grid.addWidget(tc, i // 3, i % 3)
            self.theme_cards[theme.name] = tc
        card.body.addLayout(grid)
        card.divider()
        self.album_row = ToggleRow(
            "Match Spotify album art",
            "Uses the colours of the current Spotify cover. The theme above is used when Spotify isn't playing.",
            lambda v: self._update(album_sync=v),
        )
        card.body.addWidget(self.album_row)
        self.album_status = _label("", "muted", wrap=True)
        self.album_status.setAccessibleName("Album art status")
        card.body.addWidget(self.album_status)
        if not self._album_available:
            self.album_row.setEnabled(False)
            self.album_status.setObjectName("error")
            self.album_status.setText("Unavailable: install the winrt packages from requirements.txt.")
        return card

    def _segmented(self, options: list[tuple[object, str, str]], on_pick) -> tuple[QHBoxLayout, dict]:
        """Row of exclusive buttons: options = [(value, label, accessible name)]."""
        group = QButtonGroup(self)
        group.setExclusive(True)
        buttons: dict = {}
        row = QHBoxLayout()
        row.setSpacing(SPACE["xs"])
        for value, label, acc in options:
            b = QPushButton(label)
            b.setObjectName("segment")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setAccessibleName(acc)
            b.clicked.connect(lambda _=False, v=value: on_pick(v))
            group.addButton(b)
            row.addWidget(b)
            buttons[value] = b
        setattr(self, f"_group_{id(group)}", group)  # keep the group alive
        return row, buttons

    @staticmethod
    def _labelled_row(title: str, description: str, control_layout) -> QHBoxLayout:
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(_label(title))
        text.addWidget(_label(description, "muted", wrap=True))
        row.addLayout(text, 1)
        row.addLayout(control_layout)
        return row

    def _build_visuals_card(self) -> Card:
        card = Card("Visuals")
        seg, self.style_buttons = self._segmented(
            [("liquid", "Liquid", "Liquid gradient style"), ("particles", "Particles", "Particle style"),
             ("radial", "Radial", "Radial spectrum style"), ("vinyl", "Vinyl", "Vinyl turntable style")],
            lambda v: self._update(visual_style=v))
        card.body.addLayout(self._labelled_row(
            "Style", "Liquid: soft flowing colour. Particles: glowing dots. Radial: spectrum bars around a ring. "
                     "Vinyl: the album cover spins on a turntable.",
            seg))
        card.divider()

        self.particles_row = SliderRow(
            "Particles", *PARTICLE_RANGE, 100, lambda v: f"{v:,}",
            lambda v: self._update(max_particles=v), "Lighter", "Denser")
        card.body.addWidget(self.particles_row)
        self.sensitivity_row = SliderRow(
            "Beat sensitivity", 0, 100, 5, lambda v: f"{v}%",
            lambda v: self._update(beat_sensitivity=v), "Strong beats only", "Every hit")
        card.body.addWidget(self.sensitivity_row)
        self.reaction_row = SliderRow(
            "Beat reaction", 0, 200, 10, lambda v: "Off" if v == 0 else f"{v}%",
            lambda v: self._update(beat_reaction=v / 100.0), "Calm", "Explosive")
        card.body.addWidget(self.reaction_row)

        self.cover_rotate_row = ToggleRow(
            "Rotate album cover",
            "Radial style: slowly spin the Spotify album cover shown in the centre.",
            lambda v: self._update(cover_rotate=v))
        card.body.addWidget(self.cover_rotate_row)

        card.divider()
        seg, self.fps_buttons = self._segmented(
            [(fps, str(fps), f"{fps} frames per second") for fps in FPS_CHOICES],
            lambda v: self._update(target_fps=v))
        card.body.addLayout(self._labelled_row(
            "Frame rate", "Higher is smoother but uses more power.", seg))
        return card

    def _build_idle_card(self) -> Card:
        card = Card("When it's quiet")
        self.idle_row = ToggleRow(
            "Idle animation", "Keeps slowly moving while nothing is playing. Runs at 30 fps to save power.",
            lambda v: self._update(idle_enabled=v))
        card.body.addWidget(self.idle_row)
        lo, hi = (int(x) for x in IDLE_DELAY_RANGE)
        self.idle_delay_row = SliderRow(
            "Start after", lo, hi, 1, lambda v: f"{v} s of silence",
            lambda v: self._update(idle_delay=float(v)))
        card.body.addWidget(self.idle_delay_row)
        return card

    def _build_system_card(self) -> Card:
        card = Card("Display & system")
        self.monitors_row = ToggleRow(
            "Cover all monitors", "Off = primary monitor only (lighter on CPU).",
            lambda v: self._update(span_all_monitors=v))
        card.body.addWidget(self.monitors_row)
        self.pause_row = ToggleRow(
            "Pause behind fullscreen apps", "Stops rendering while a maximised or fullscreen window hides the desktop.",
            lambda v: self._update(pause_when_covered=v))
        card.body.addWidget(self.pause_row)
        card.divider()
        self.autostart_row = ToggleRow(
            "Start with Windows", "Runs in the tray when you sign in. Turning this off removes the startup entry.",
            self._toggle_autostart)
        card.body.addWidget(self.autostart_row)
        self.autostart_error = _label("", "error", wrap=True)
        self.autostart_error.hide()
        card.body.addWidget(self.autostart_error)
        return card

    # -- state ---------------------------------------------------------------
    def set_settings(self, s: Settings) -> None:
        """Show `s` in the controls without emitting change signals."""
        self._settings = s
        for value, b in self.style_buttons.items():
            b.blockSignals(True)
            b.setChecked(value == s.visual_style)
            b.blockSignals(False)
        self.particles_row.setEnabled(s.visual_style == "particles")
        self.cover_rotate_row.set_checked(s.cover_rotate)
        self.cover_rotate_row.setEnabled(s.visual_style == "radial")
        for name, tc in self.theme_cards.items():
            tc.blockSignals(True)
            tc.setChecked(name == s.theme)
            tc.blockSignals(False)
        self.album_row.set_checked(s.album_sync and self._album_available)
        self.particles_row.set_value(s.max_particles)
        self.sensitivity_row.set_value(s.beat_sensitivity)
        self.reaction_row.set_value(round(s.beat_reaction * 100))
        for fps, b in self.fps_buttons.items():
            b.blockSignals(True)
            b.setChecked(fps == s.target_fps)
            b.blockSignals(False)
        self.idle_row.set_checked(s.idle_enabled)
        self.idle_delay_row.set_value(int(round(s.idle_delay)))
        self.idle_delay_row.setEnabled(s.idle_enabled)
        self.monitors_row.set_checked(s.span_all_monitors)
        self.pause_row.set_checked(s.pause_when_covered)
        self.autostart_row.set_checked(autostart.is_enabled())

    def set_album_status(self, text: str) -> None:
        if self._album_available:
            self.album_status.setText(text)
            self.album_status.setVisible(bool(text))

    def _update(self, **changes) -> None:
        self._settings = replace(self._settings, **changes).normalised()
        self.idle_delay_row.setEnabled(self._settings.idle_enabled)
        self.particles_row.setEnabled(self._settings.visual_style == "particles")
        self.cover_rotate_row.setEnabled(self._settings.visual_style == "radial")
        self.settings_changed.emit(self._settings)

    def _reset(self) -> None:
        defaults = Settings()
        self.set_settings(defaults)
        self.settings_changed.emit(defaults)

    def _toggle_autostart(self, enabled: bool) -> None:
        try:
            autostart.set_enabled(enabled)
            self.autostart_error.hide()
        except OSError as exc:
            self.autostart_row.set_checked(not enabled)
            self.autostart_error.setText(f"Couldn't change the startup setting: {exc.strerror or exc}")
            self.autostart_error.show()

    def _refresh_status(self) -> None:
        self.status.setText(self._status_provider())

    # -- window behaviour ----------------------------------------------------
    def open(self) -> None:
        self.set_settings(self._settings)
        self._refresh_status()
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:
        use_dark_title_bar(int(self.winId()))
        self._status_timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._status_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        event.ignore()  # closing the window keeps the wallpaper running in the tray
        self.hide()
