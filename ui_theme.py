"""The look of Finger Mouse's windows: colours, the stylesheet, icons, and the
few controls Qt doesn't have (a switch, a sidebar item, a press-and-hold button).

It follows the operating system's light or dark mode, the way the system's own
Settings app does. Colours carry state only together with an icon and words,
never on their own.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QApplication, QCheckBox, QSizePolicy, QWidget

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

LIGHT = {
    "ground": "#f3f3f3",      # window and sidebar
    "surface": "#ffffff",     # grouped settings, the status card
    "surface2": "#f9f9f9",    # a row being hovered, inputs
    "stroke": "#e1e1e1",
    "stroke2": "#c9c9c9",     # input borders
    "text": "#1b1b1b",
    "text2": "#5c5c5c",
    "accent": "#005fb8",
    "accent_hover": "#1a6fc4",
    "on_accent": "#ffffff",
    "select": "#e9e9e9",      # the current sidebar page
    "focus": "#1b1b1b",
    "ok": "#0f7b0f", "ok_bg": "#dff6dd",
    "warn": "#8a5300", "warn_bg": "#fff4ce",
    "bad": "#c42b1c", "bad_bg": "#fde7e9",
    "idle": "#5c5c5c", "idle_bg": "#ebebeb",
    "preview": "#1b1b1b", "preview_text": "#d6d6d6",
}
DARK = {
    "ground": "#202020",
    "surface": "#2b2b2b",
    "surface2": "#323232",
    "stroke": "#3a3a3a",
    "stroke2": "#5a5a5a",
    "text": "#ffffff",
    "text2": "#c8c8c8",
    "accent": "#60cdff",
    "accent_hover": "#4cc2ff",
    "on_accent": "#000000",
    "select": "#2d2d2d",
    "focus": "#ffffff",
    "ok": "#6ccb5f", "ok_bg": "#1f3a1c",
    "warn": "#fce100", "warn_bg": "#433519",
    "bad": "#ff99a4", "bad_bg": "#442726",
    "idle": "#c8c8c8", "idle_bg": "#383838",
    "preview": "#141414", "preview_text": "#c8c8c8",
}


def is_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # an older Qt without colour schemes: light
        return False


def colors() -> dict[str, str]:
    return DARK if is_dark() else LIGHT


# The tone each state is shown in: "ok" (working), "warn" (needs a look), "bad"
# (stopped by a problem), "idle" (stopped on purpose, or starting).
STATE_TONES = {
    "ready": "ok", "pinched": "ok", "dragging": "ok", "scrolling": "ok", "open": "ok", "pointing": "ok",
    "gazing": "ok", "dwelling": "ok", "tracking": "ok",
    "starting": "idle", "stopped": "idle",
    "no_hand": "warn", "no_face": "warn", "paused": "warn", "hide": "warn", "uncalibrated": "warn",
    "error": "bad",
}


def tone(state: str) -> str:
    return STATE_TONES.get(state, "ok")


# ---------------------------------------------------------------------------
# Type
# ---------------------------------------------------------------------------

def apply_app_font(app: QApplication) -> None:
    """The system's own UI face, a little larger than Qt's default so it reads from a seat."""
    families = set(QFontDatabase.families())
    font = QFont(app.font())
    for name in ("Segoe UI Variable Text", "Segoe UI", ".AppleSystemUIFont", "SF Pro Text", "Cantarell", "Noto Sans"):
        if name in families:
            font.setFamily(name)
            break
    font.setPixelSize(15)
    app.setFont(font)


# ---------------------------------------------------------------------------
# Icons (Lucide, ISC licence: https://lucide.dev)
# ---------------------------------------------------------------------------

ICONS = {
    "home": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "setup": '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
    "head": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><path d="M9 9h.01"/><path d="M15 9h.01"/>',
    "hand": '<path d="M18 11V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2"/><path d="M14 10V4a2 2 0 0 0-2-2a2 2 0 0 0-2 2v2"/><path d="M10 10.5V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/>',
    "eye": '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    "sign": '<path d="M10 8h.01"/><path d="M12 12h.01"/><path d="M14 8h.01"/><path d="M16 12h.01"/><path d="M18 8h.01"/><path d="M6 8h.01"/><path d="M7 16h10"/><path d="M8 12h.01"/><rect width="20" height="16" x="2" y="4" rx="2"/>',
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
    "camera_off": '<path d="M10.66 6H14a2 2 0 0 1 2 2v2.5l5.248-3.062A.5.5 0 0 1 22 7.87v8.196"/><path d="M16 16a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h2"/><path d="m2 2 20 20"/>',
    "pointer": '<path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"/>',
    "caregiver": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "advanced": '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "stop": '<rect width="14" height="14" x="5" y="5" rx="2"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
    "scan": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/>',
    "loader": '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "unlock": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "chevron": '<path d="m9 18 6-6-6-6"/>',
    "monitor": '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "power": '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
    "chevron_down": '<path d="m6 9 6 6 6-6"/>',
}

MODE_ICONS = {"head": "head", "sign": "sign", "hand": "hand", "eye": "eye"}


@lru_cache(maxsize=256)
def _pixmap(name: str, color: str, size: int, stroke: float, dpr: float) -> QPixmap:
    fill = color if name in ("play", "stop", "pause") else "none"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="{fill}" '
           f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">'
           f'{ICONS[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    px = QPixmap(round(size * dpr), round(size * dpr))
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    renderer.render(p)
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


def icon_pixmap(name: str, color: str, size: int = 20, stroke: float = 2.0) -> QPixmap:
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    return _pixmap(name, color, size, stroke, dpr)


def icon(name: str, color: Optional[str] = None, size: int = 20) -> QIcon:
    """An icon in ``color``, greyed like its button's words when the button is disabled."""
    result = QIcon(icon_pixmap(name, color or colors()["text"], size))
    result.addPixmap(icon_pixmap(name, colors()["text2"], size), QIcon.Mode.Disabled)
    return result


# ---------------------------------------------------------------------------
# The stylesheet
# ---------------------------------------------------------------------------

def _icon_file(name: str, color: str, size: int = 16) -> str:
    """An icon written to a small cache file, for stylesheet rules that need an image URL."""
    import tempfile
    from pathlib import Path
    folder = Path(tempfile.gettempdir()) / "finger-mouse-ui"
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}-{color.lstrip('#')}-{size}.png"
    if not path.exists():
        _pixmap(name, color, size, 2.0, 2.0).save(str(path))
    return path.as_posix()


def stylesheet() -> str:
    c = colors()
    chevron = _icon_file("chevron_down", c["text2"])
    return f"""
        QWidget {{ color: {c['text']}; }}
        QMainWindow, QDialog, QWidget#ground {{ background: {c['ground']}; }}
        QWidget#sidebar {{ background: {c['ground']}; }}
        QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget {{ background: {c['ground']}; border: none; }}
        QWidget#page {{ background: transparent; }}

        QLabel {{ background: transparent; }}
        QLabel#appName {{ font-size: 17px; font-weight: 600; }}
        QLabel#pageTitle {{ font-size: 28px; font-weight: 600; }}
        QLabel#pageIntro, QLabel#hint, QLabel#rowHint, QLabel#secondary {{ color: {c['text2']}; }}
        QLabel#hint, QLabel#rowHint {{ font-size: 13px; }}
        QLabel#groupTitle {{ font-size: 15px; font-weight: 600; padding-top: 6px; }}
        QLabel#rowTitle {{ font-size: 15px; }}
        QLabel#stepTitle {{ font-size: 16px; font-weight: 600; }}
        QLabel#value {{ font-weight: 600; }}
        QLabel#rowValue {{ color: {c['text2']}; font-size: 14px; min-width: 64px; }}
        QLabel#headline {{ font-size: 26px; font-weight: 600; }}
        QLabel#status {{ font-size: 15px; }}
        QLabel#howTo {{ font-size: 16px; }}
        QLabel#stepNumber {{ background: {c['accent']}; color: {c['on_accent']}; border-radius: 14px;
            font-weight: 700; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; }}
        QLabel#stepNumber[done="true"] {{ background: {c['ok']}; }}
        QLabel#stepState {{ color: {c['text2']}; font-size: 14px; }}
        QLabel#stepState[done="true"] {{ color: {c['ok']}; font-weight: 600; }}
        QLabel#error {{ color: {c['bad']}; }}
        QLabel#footnote {{ color: {c['text2']}; font-size: 13px; }}
        QLabel#diag {{ color: {c['text2']}; font-family: Consolas, Menlo, monospace; font-size: 12px; }}
        QLabel#preview {{ background: {c['preview']}; color: {c['preview_text']}; border-radius: 8px; }}
        QLabel#summary {{ background: {c['surface2']}; border: 1px solid {c['stroke']}; border-radius: 6px;
            padding: 12px 14px; font-size: 14px; }}
        QLabel#previewText {{ color: {c['preview_text']}; font-size: 17px; }}
        QWidget#previewEmpty {{ background: {c['preview']}; border-radius: 8px; }}
        QFrame#tile {{ background: {c['surface']}; border: 1px solid {c['stroke2']}; border-radius: 8px; }}
        QFrame#tile:hover {{ background: {c['surface2']}; }}
        QFrame#tile[checked="true"] {{ border: 2px solid {c['accent']}; background: {c['select']}; }}
        QLabel#tileTitle {{ font-size: 17px; font-weight: 600; }}
        QLabel#signLetter {{ font-size: 88px; font-weight: 700; }}
        QLabel#warnText {{ color: {c['warn']}; font-size: 15px; }}
        QLabel#okText {{ color: {c['ok']}; font-size: 15px; font-weight: 600; }}

        QFrame#group, QFrame#statusCard {{ background: {c['surface']}; border: 1px solid {c['stroke']};
            border-radius: 8px; }}
        QFrame#rowLine {{ background: {c['stroke']}; border: none; max-height: 1px; min-height: 1px; }}
        QLabel#chip {{ border-radius: 11px; padding: 2px 10px; font-size: 13px; font-weight: 600; }}

        QPushButton {{ background: {c['surface']}; border: 1px solid {c['stroke2']}; border-radius: 6px;
            padding: 8px 16px; min-height: 26px; font-size: 14px; }}
        QPushButton:hover {{ background: {c['surface2']}; }}
        QPushButton:pressed {{ background: {c['select']}; }}
        QPushButton:disabled {{ color: {c['text2']}; background: {c['ground']}; border-color: {c['stroke']}; }}
        QPushButton#accent {{ background: {c['accent']}; color: {c['on_accent']}; border-color: {c['accent']};
            font-weight: 600; }}
        QPushButton#accent:hover {{ background: {c['accent_hover']}; }}
        QPushButton#accent:disabled, QPushButton#bigToggle:disabled {{ background: {c['ground']};
            color: {c['text2']}; border-color: {c['stroke']}; }}
        QPushButton#bigToggle {{ font-size: 18px; font-weight: 600; min-height: 40px; min-width: 132px;
            padding: 8px 18px; border-radius: 8px; }}
        QPushButton#bigToggle[running="false"] {{ background: {c['accent']}; color: {c['on_accent']};
            border-color: {c['accent']}; }}
        QPushButton#bigToggle[running="false"]:hover {{ background: {c['accent_hover']}; }}
        QPushButton:focus, QComboBox:focus, QLineEdit:focus, QDoubleSpinBox:focus, QSlider:focus,
        QRadioButton:focus {{ border: 2px solid {c['focus']}; }}

        QComboBox, QLineEdit, QDoubleSpinBox {{ background: {c['surface2']}; border: 1px solid {c['stroke2']};
            border-radius: 6px; padding: 7px 10px; min-height: 22px; font-size: 14px; }}
        QComboBox {{ min-width: 220px; }}
        QComboBox::drop-down {{ border: none; width: 30px; }}
        QComboBox::down-arrow {{ image: url("{chevron}"); width: 16px; height: 16px; }}
        QComboBox QAbstractItemView {{ background: {c['surface']}; border: 1px solid {c['stroke']};
            selection-background-color: {c['select']}; selection-color: {c['text']}; outline: none; padding: 4px; }}

        QSlider {{ min-height: 30px; min-width: 220px; border: 2px solid transparent; border-radius: 6px; }}
        QSlider::groove:horizontal {{ height: 4px; background: {c['stroke2']}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {c['surface']}; border: 5px solid {c['accent']};
            width: 10px; height: 10px; margin: -8px 0; border-radius: 10px; }}

        QRadioButton {{ spacing: 12px; min-height: 30px; border: 2px solid transparent; border-radius: 6px;
            padding: 2px 4px; font-size: 15px; }}

        QProgressBar {{ background: {c['stroke']}; border: none; border-radius: 3px; max-height: 6px; }}
        QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}

        QListWidget {{ background: {c['surface']}; border: 1px solid {c['stroke']}; border-radius: 8px;
            font-size: 14px; padding: 4px; outline: none; }}
        QListWidget::item {{ padding: 6px 8px; border-radius: 4px; }}
        QListWidget::item:selected {{ background: {c['select']}; color: {c['text']}; }}

        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {c['stroke2']}; border-radius: 3px; min-height: 30px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QToolTip {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['stroke']}; padding: 6px; }}
        QMenu {{ background: {c['surface']}; border: 1px solid {c['stroke']}; padding: 4px; }}
        QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
        QMenu::item:selected {{ background: {c['select']}; }}
    """


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

class ToggleSwitch(QCheckBox):
    """An on/off switch with the word beside it, so the state never rests on colour.
    It is a QCheckBox underneath (checked, toggled, setChecked all work)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(96, 36)

    def hitButton(self, pos) -> bool:  # noqa: N802 (Qt name)
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt name)
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        on, enabled = self.isChecked(), self.isEnabled()
        track = QRectF(self.width() - 48, (self.height() - 22) / 2, 44, 22)
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["accent"] if enabled else c["stroke2"]))
        else:
            p.setPen(QPen(QColor(c["text2"] if enabled else c["stroke2"]), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(track, 11, 11)
        knob = 12 if on else 10
        x = track.right() - 5 - knob if on else track.left() + 5
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c["on_accent"] if on else c["text2"]))
        p.drawEllipse(QRectF(x, track.center().y() - knob / 2, knob, knob))
        p.setPen(QColor(c["text"] if enabled else c["text2"]))
        p.drawText(QRectF(0, 0, track.left() - 10, self.height()),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "On" if on else "Off")
        if self.hasFocus():
            p.setPen(QPen(QColor(c["focus"]), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(-4, -4, 4, 4), 15, 15)


class NavButton(QAbstractButton):
    """One page in the sidebar: icon, name, and a short accent pill when it's the current page."""

    def __init__(self, icon_name: str, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.icon_name = icon_name
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.badge = ""          # a short note on the right, e.g. "In use"
        self.setAccessibleName(text)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(200, 44)

    def set_badge(self, text: str) -> None:
        if text != self.badge:
            self.badge = text
            self.update()

    def enterEvent(self, e) -> None:  # noqa: N802 (Qt name)
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802 (Qt name)
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt name)
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self.isChecked() or self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["select"]))
            p.drawRoundedRect(r, 6, 6)
        if self.isChecked():
            p.setBrush(QColor(c["accent"]))
            p.drawRoundedRect(QRectF(r.left(), r.center().y() - 8, 3, 16), 1.5, 1.5)
        p.drawPixmap(int(r.left() + 14), int(r.center().y() - 10), icon_pixmap(self.icon_name, c["text"], 20))
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold if self.isChecked() else QFont.Weight.Normal)
        p.setFont(font)
        p.setPen(QColor(c["text"]))
        text_rect = r.adjusted(46, 0, -10, 0)
        if self.badge:
            small = QFont(font)
            small.setPixelSize(12)
            small.setWeight(QFont.Weight.Normal)
            p.save()
            p.setFont(small)
            p.setPen(QColor(c["text2"]))
            p.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.badge)
            p.restore()
        p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.hasFocus():
            p.setPen(QPen(QColor(c["focus"]), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 6, 6)


class HoldButton(QAbstractButton):
    """A button that only acts after being held down for ``hold_ms``: a stray
    click, wink or pinch can't trigger it. It fills up while held."""

    held = Signal()

    def __init__(self, icon_name: str, text: str, hold_ms: int = 3000, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.icon_name = icon_name
        self.setText(text)
        self.hold_ms = hold_ms
        self._progress = 0.0
        self._timer = QTimer(self, interval=30)
        self._timer.timeout.connect(self._step)
        self.setMinimumHeight(44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)
        self.setAccessibleDescription(f"Press and hold for {hold_ms // 1000} seconds")
        self.pressed.connect(self._timer.start)
        self.released.connect(self._cancel)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(200, 44)

    def keyPressEvent(self, e) -> None:  # noqa: N802 (Qt name)
        # Space or Enter held down works too (auto-repeat keeps it pressed).
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.isAutoRepeat():
            self._timer.start()
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e) -> None:  # noqa: N802 (Qt name)
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.isAutoRepeat():
            self._cancel()
            return
        super().keyReleaseEvent(e)

    def _cancel(self) -> None:
        self._timer.stop()
        self._progress = 0.0
        self.update()

    def _step(self) -> None:
        self._progress = min(1.0, self._progress + 30 / self.hold_ms)
        self.update()
        if self._progress >= 1.0:
            self._cancel()
            self.held.emit()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt name)
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(QPen(QColor(c["stroke2"]), 1))
        p.setBrush(QColor(c["surface"]))
        p.drawRoundedRect(r, 6, 6)
        if self._progress > 0:
            fill = QRectF(r.left(), r.top(), r.width() * self._progress, r.height())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["select"]))
            p.drawRoundedRect(fill, 6, 6)
        p.drawPixmap(int(r.left() + 12), int(r.center().y() - 9), icon_pixmap(self.icon_name, c["text"], 18))
        p.setPen(QColor(c["text"]))
        label = self.text() if self._progress == 0 else f"Keep holding… {max(1, round((1 - self._progress) * self.hold_ms / 1000))}"
        p.drawText(r.adjusted(40, 0, -8, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        if self.hasFocus():
            p.setPen(QPen(QColor(c["focus"]), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 6, 6)


class StateDisc(QWidget):
    """The big round state mark on Home: a tinted disc with an icon in it."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(72, 72)
        self._tone = "idle"
        self._icon = "stop"

    def set_state(self, tone_name: str, icon_name: str) -> None:
        if (tone_name, icon_name) != (self._tone, self._icon):
            self._tone, self._icon = tone_name, icon_name
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt name)
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c[f"{self._tone}_bg"]))
        p.drawEllipse(QRectF(0, 0, 72, 72))
        p.drawPixmap(QPointF(18, 18), icon_pixmap(self._icon, c[self._tone], 36, 2.2))
