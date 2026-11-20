# -*- coding: utf-8 -*-
"""Deathwatch community patch installer · PySide6 baseline UI.

Design and current behavior are tracked in the project plan and changelog.
- **PySide6 + local ui_kit.py** supplies all widgets and styling; no qfluentwidgets dependency.
- Four-page wizard: terms → setup (locate the exe + action) → features (patch grid) → finish/results.
- Every selectable tile lights up as a whole; the local CardWidget paints its own rounded background,
  with no visible checkbox or radio dot.
- Branding and install-state tags appear only in the Windows title bar; language radios sit in the bottom
  nav; a 220px task rail runs down the left.
- Title bar colors are pinned via DWM and a light Fusion palette is forced (system dark mode and magenta
  accents do not bleed through).
- Real engine (patcher.py): centralized `.dwpatch_backup` with a global index; selected files are
  pre-restored before re-applying; Chinese text is generated at runtime (locres + loose .uasset + BFL);
  bug fixes are deployed from manifest packages; the save is checked before Black Shield injection.

Run: INSTALLER/.venv/Scripts/python.exe INSTALLER/src/main.py
"""
import ctypes
import html as _html
import json
import re
import sys
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QRect, QUrl, Qt, QThread, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QWidget, QFileDialog, QFrame, QLabel, QSizePolicy,
)
from ui_kit import (
    CardWidget, TitleLabel, SubtitleLabel, StrongBodyLabel, BodyLabel,
    CaptionLabel, CheckBox, LineEdit, RadioButton,
    PrimaryPushButton, PushButton, TransparentPushButton, FlowLayout,
    MessageBoxBase, TextEdit, SingleDirectionScrollArea,
    InfoBar, FluentIcon, setTheme, Theme, setThemeColor,
)

from i18n import STEP_KEYS, detect_system_lang, t
import patcher
from zhgen import zh_runtime

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PAYLOAD_DIR = APP_DIR / "payload"
REGISTRY = json.loads((APP_DIR / "features.json").read_text(encoding="utf-8"))
FEATURES = REGISTRY["features"]
BONUS = [f for f in FEATURES if f.get("bonus")]
# Display order: the two Chinese-language tiles come first — Chinese text, then Chinese voice.
_DISPLAY_ORDER = ["zh_text", "vo_zh", "visual", "camera", "bugfix", "balance"]
CORE_FEATURES = sorted((f for f in FEATURES if not f.get("bonus")),
                       key=lambda f: _DISPLAY_ORDER.index(f["id"]))
_support_path = APP_DIR / "support.json"
SUPPORT = json.loads(_support_path.read_text(encoding="utf-8")) if _support_path.exists() \
    else {"qr_image": "support_qr.png", "links": []}

GREEN, BLUE = "#4F6B59", "#5B7183"
GRAY = QColor(160, 160, 160)
INK = QColor("#1b1b1b")
SUB = QColor("#5c5c5c")
RED = QColor(176, 0, 32)
WHITE = QColor("#ffffff")
LIGHT_RED = QColor("#FFE3E3")
LIGHT_GREEN = QColor("#B7F0B1")

# Card sizing: FlowLayout wraps automatically; widths are recomputed from the window width
# (2 cards/row at 980px, 3 when wider); heights follow content (14px vertical padding, text centered).


def apply_light_palette():
    """Under system dark mode the framework theme leaves the application QPalette untouched, so native
    widgets (scroll viewport, input backgrounds, etc.) show through dark — force a light Fusion palette at startup."""
    app = QApplication.instance()
    if app is None:
        return
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#F2F0EA"))
    pal.setColor(QPalette.Base, QColor("#FFFEFA"))
    pal.setColor(QPalette.Button, QColor("#F2F0EA"))
    pal.setColor(QPalette.Text, INK)
    pal.setColor(QPalette.WindowText, INK)
    pal.setColor(QPalette.ButtonText, INK)
    pal.setColor(QPalette.PlaceholderText, QColor(0x6f, 0x6f, 0x6f))
    pal.setColor(QPalette.Highlight, QColor(BLUE))
    pal.setColor(QPalette.HighlightedText, WHITE)
    pal.setColor(QPalette.Link, QColor(BLUE))
    app.setPalette(pal)


def apply_caption_style(hwnd: int):
    """Pin title bar background/border/text colors via the official DWM API (system dark mode and
    magenta accents would otherwise bleed through).

    DWMWA_BORDER_COLOR=34 / CAPTION_COLOR=35 / TEXT_COLOR=36; COLORREF=0x00BBGGRR.
    Fails silently: on non-Windows or older systems, the defaults are kept.
    """
    try:
        dwm = ctypes.windll.dwmapi
        for attr, colorref in ((34, 0x00C8C8C8), (35, 0x00F3F3F3), (36, 0x001B1B1B)):
            val = wintypes.DWORD(colorref)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr,
                                      ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def derive_root(exe_path: str):
    """Derive the install root from the game exe chosen by the user. Pure function, easy to test."""
    p = Path(exe_path)
    name = p.name.lower()
    if name == "deathwatchgame.exe":
        root = p.parent
    elif name == "deathwatchgame-win32-shipping.exe" and p.parent.name.lower() == "win32" \
            and p.parent.parent.name.lower() == "binaries":
        root = p.parents[3]
    else:
        return None
    return root if (root / "deathwatchgame" / "Content").is_dir() else None


class NavigationRail(QWidget):
    """Left task rail: keeps steps as a stable orientation instead of decoration that takes over the content."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.current = 0
        self.accent = QColor(BLUE)
        self.labels = ["", "", "", ""]
        self.setFixedWidth(220)
        self.setMinimumHeight(560)

    def retranslate(self, lang):
        self.labels = [t(key, lang) for key in STEP_KEYS]
        self.update()

    def update_state(self, current, accent):
        self.current = min(current, len(self.labels) - 1)
        self.accent = QColor(accent)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#27353D"))

        p.setPen(QColor("#F6F1E7"))
        brand = QFont(self.font())
        brand.setPixelSize(20)
        brand.setBold(True)
        p.setFont(brand)
        p.drawText(QRect(28, 34, self.width() - 48, 28),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "DEATHWATCH")
        p.setPen(QColor("#AAB7B9"))
        meta = QFont(self.font())
        meta.setPixelSize(11)
        p.setFont(meta)
        p.drawText(QRect(29, 65, self.width() - 48, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "COMMUNITY PATCH · OFFLINE")

        start, gap = 146, 70
        for index, label in enumerate(self.labels):
            y = start + index * gap
            state = "active" if index == self.current else ("done" if index < self.current else "todo")
            if index < len(self.labels) - 1:
                p.setPen(QColor("#78909A") if index < self.current else QColor("#465960"))
                p.drawLine(40, y + 24, 40, y + gap - 12)
            if state == "active":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(self.accent)
                p.drawRoundedRect(QRect(20, y - 12, self.width() - 40, 52), 12, 12)
            dot = self.accent if state in ("active", "done") else QColor("#60727A")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QRect(31, y + 3, 18, 18))
            p.setPen(QColor("#FFFFFF") if state == "active" else QColor("#D7E0DE"))
            number = QFont(self.font())
            number.setPixelSize(10)
            number.setBold(True)
            p.setFont(number)
            p.drawText(QRect(31, y + 3, 18, 18), Qt.AlignmentFlag.AlignCenter, str(index + 1))
            p.setPen(QColor("#FFFFFF") if state == "active" else QColor("#AEBBBC"))
            label_font = QFont(self.font())
            label_font.setPixelSize(14)
            label_font.setBold(state == "active")
            p.setFont(label_font)
            p.drawText(QRect(63, y, self.width() - 82, 24),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

        p.setPen(QColor("#90A1A4"))
        foot = QFont(self.font())
        foot.setPixelSize(11)
        p.setFont(foot)
        p.drawText(QRect(28, self.height() - 60, self.width() - 52, 28),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "LOCAL · REVERSIBLE · BACKED UP")


class SelectableCard(CardWidget):
    """Whole-tile selection card: selected = solid accent background + white text, no visible checkbox/radio dot.

    The local CardWidget paints its own background (no QSS), overriding the `_normal/_hover/_pressedBackgroundColor`
    hooks provided by ui_kit; subclasses only need `_sync_state`. Note: the base initializer calls
    `_normalBackgroundColor`, so fields must be set before `super().__init__()`.
    """

    toggled = Signal(bool)

    def __init__(self, parent=None):
        self._selected = False
        self._accent = QColor(GREEN)
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setAccent(self, color):
        self._accent = QColor(color)
        self.setBackgroundColor(self._normalBackgroundColor())
        self._sync_state()

    def isChecked(self):
        return self._selected

    def setChecked(self, checked, emit=False):
        checked = bool(checked)
        if checked == self._selected:
            return
        self._selected = checked
        # Apply the color directly; avoid the 120ms background animation affecting screenshots/assertions.
        self.setBackgroundColor(self._normalBackgroundColor())
        self._sync_state()
        if emit:
            self.toggled.emit(checked)

    def _sync_state(self):
        pass

    def _normalBackgroundColor(self):
        return QColor(self._accent) if self._selected else QColor(255, 255, 255, 170)

    def _hoverBackgroundColor(self):
        return QColor(self._accent).lighter(108) if self._selected else QColor(255, 255, 255, 210)

    def _pressedBackgroundColor(self):
        return QColor(self._accent).darker(108) if self._selected else QColor(255, 255, 255, 64)

    def _disabledBackgroundColor(self):
        return self._normalBackgroundColor()


class SegmentButton(QWidget):
    """A single button in a segmented control: fully clickable, modern rounded pill, no tiny radio dot."""

    clicked = Signal()

    def __init__(self, text):
        super().__init__()
        self._text = text
        self._checked = False
        self._hover = False
        self._accent = QColor(GREEN)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_checked(self, checked):
        self._checked = bool(checked)
        self.update()

    def set_accent(self, color):
        self._accent = QColor(color)
        self.update()

    def set_text(self, text):
        self._text = text
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            e.accept()          # Consume the event so it doesn't bubble to the parent card and toggle it off.
            self.clicked.emit()

    def mouseReleaseEvent(self, e):
        # The press was consumed, so the release must be too: otherwise it bubbles to the parent CardWidget
        # and emits clicked (e.g. opening the visual tier dialog, then clicking a segment reopens it).
        if e.button() == Qt.MouseButton.LeftButton:
            e.accept()
        else:
            super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.setPen(Qt.PenStyle.NoPen)
        if self._checked:
            p.setBrush(QColor("#FFFFFF"))
            p.drawRoundedRect(r, 6, 6)
            ink = QColor(self._accent)
        else:
            if self._hover:
                p.setBrush(QColor(255, 255, 255, 46))
                p.drawRoundedRect(r, 6, 6)
            ink = QColor(255, 255, 255, 235)
        font = QFont(self.font())
        font.setPixelSize(13)
        font.setBold(self._checked)
        p.setFont(font)
        p.setPen(ink)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


class SegmentedChoice(QWidget):
    """Tiers are shown as one modern segmented bar instead of a dropdown or small radio dots."""

    changed = Signal()

    def __init__(self, options, current_id):
        super().__init__()
        self._keys = [o[0] for o in options]
        self._current = current_id
        h = QHBoxLayout(self)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(4)
        self.buttons = []
        for key, text, tip in options:
            btn = SegmentButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda k=key: self.set_current(k, emit=True))
            h.addWidget(btn, 1)
            self.buttons.append(btn)
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.set_accent(GREEN)
        self._sync()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(self.rect(), 8, 8)

    def set_accent(self, color):
        for btn in self.buttons:
            btn.set_accent(color)
        self.update()

    def _sync(self):
        for key, btn in zip(self._keys, self.buttons):
            btn.set_checked(key == self._current)

    def set_current(self, key, emit=False):
        if key == self._current:
            return
        self._current = key
        self._sync()
        if emit:
            self.changed.emit()

    def current_key(self):
        return self._current

    def set_texts(self, texts, tips):
        for btn, text, tip in zip(self.buttons, texts, tips):
            btn.set_text(text)
            btn.setToolTip(tip)


class FeatureCard(SelectableCard):
    """Feature tile: click anywhere to toggle; selected tiles light up; disabled tiles are grayed out; DIY tiles open the prompt."""

    def __init__(self, feat, parent=None):
        self.feat = feat
        self._diy = bool(feat.get("diy_prompt"))
        self._warning_modal = bool(feat.get("warning_modal"))
        self._enabled = bool(feat["enabled"])
        self._lang = "zh"
        super().__init__(parent)
        self._locked = False
        if not self._enabled and not self._diy and not self._warning_modal:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title = StrongBodyLabel()
        title_row.addWidget(self.title)
        self.badge = CaptionLabel("DIY")
        self.badge.setVisible(self._diy)
        title_row.addWidget(self.badge)
        self.bonus_badge = None
        if feat.get("bonus"):
            self.bonus_badge = CaptionLabel("BONUS")
            title_row.addWidget(self.bonus_badge)
        # Put the detail link in the title row (same spot as the DIY badge) instead of taking a whole bottom row.
        self.detail_link = None
        if feat.get("detail_doc") or feat.get("detail_link_key"):
            self.detail_link = QLabel()
            self.detail_link.setTextFormat(Qt.TextFormat.RichText)
            self.detail_link.setOpenExternalLinks(False)
            self.detail_link.setTextInteractionFlags(
                Qt.TextInteractionFlag.LinksAccessibleByMouse)
            self.detail_link.setCursor(Qt.CursorShape.PointingHandCursor)
            self.detail_link.linkActivated.connect(self._open_detail)
            title_row.addWidget(self.detail_link)
        title_row.addStretch(1)
        col.addLayout(title_row)

        self.desc = BodyLabel()
        self.desc.setWordWrap(True)
        if feat.get("hide_desc"):
            self.desc.setVisible(False)
        col.addWidget(self.desc)

        self.variant_row = None
        self.variant_choice = None
        if feat.get("variants"):
            default_id = feat.get("default_variant") or feat["variants"][0]["id"]
            opts = [(v["id"],
                     v.get("short", {}).get("zh") or v["label"]["zh"],
                     v["label"]["zh"]) for v in feat["variants"]]
            self.variant_choice = SegmentedChoice(opts, default_id)
            self.variant_row = self.variant_choice
            self.variant_row.setVisible(False)
            col.addWidget(self.variant_row)

        self.warn = None
        if feat.get("warning"):
            self.warn = BodyLabel()
            self.warn.setWordWrap(True)
            col.addWidget(self.warn)

        outer.addLayout(col, 1)
        self._sync_state()

    def _sync_state(self):
        if self._selected:
            self.title.setTextColor(WHITE, WHITE)
            self.desc.setTextColor(WHITE, WHITE)
            self.badge.setTextColor(LIGHT_GREEN, LIGHT_GREEN)
            if self.bonus_badge is not None:
                self.bonus_badge.setTextColor(LIGHT_GREEN, LIGHT_GREEN)
            if self.warn is not None:
                self.warn.setTextColor(LIGHT_RED, LIGHT_RED)
        elif not self._enabled:
            self.title.setTextColor(GRAY, GRAY)
            self.desc.setTextColor(GRAY, GRAY)
            self.badge.setTextColor(QColor(GREEN), QColor(GREEN))
            if self.bonus_badge is not None:
                self.bonus_badge.setTextColor(QColor(GREEN), QColor(GREEN))
            if self.warn is not None:
                self.warn.setTextColor(RED, RED)
        else:
            self.title.setTextColor(INK, WHITE)
            self.desc.setTextColor(SUB, QColor("#c8c8c8"))
            if self.bonus_badge is not None:
                self.bonus_badge.setTextColor(QColor(GREEN), QColor(GREEN))
            if self.warn is not None:
                self.warn.setTextColor(RED, RED)
        if self.variant_choice is not None:
            self.variant_choice.set_accent(self._accent)
        if self.warn is not None:
            # Modal-style warnings (camera) are not shown inline, to keep card heights consistent.
            self.warn.setVisible(False if self._warning_modal
                                 else (self._selected or not self._enabled))
        if self.variant_row is not None:
            self.variant_row.setVisible(self._selected)
            # Showing/hiding the variant row does not change card height: all cards share one height (same as balance).
        if self.detail_link is not None:
            color = WHITE.name() if self._selected else (
                GRAY.name() if not self._enabled else BLUE)
            link_label = t(self.feat.get("detail_link_key", "bug_detail"), self._lang)
            self.detail_link.setText(
                f'<a href="bug" style="color:{color}; text-decoration:underline;">'
                 f'{_html.escape(link_label)}</a>')

    def set_lang(self, lang):
        self._lang = lang
        self._sync_state()

    def _open_detail(self, _link=""):
        if self.feat.get("detail_kind") == "diy":
            VoDiyDialog(self.window()).exec()
            return
        if self.feat.get("detail_kind") == "caution":
            # Camera tile "caution" link: show the same red caution dialog used when selecting it.
            CameraWarningDialog(self.window()).exec()
            return
        BugDetailDialog(self.window(), self.feat["id"]).exec()

    def set_locked(self, locked):
        """When locked the tile is read-only (finish-page summary); clicks no longer toggle it."""
        self._locked = bool(locked)

    def set_enabled(self, enabled):
        """Enable/disable the tile at runtime (restore mode disables Bonus on the finish page)."""
        self._enabled = bool(enabled)
        if not self._enabled and self._selected:
            self.setChecked(False, emit=False)
        hand = (self._enabled or self._diy or self._warning_modal)
        self.setCursor(Qt.CursorShape.PointingHandCursor if hand
                       else Qt.CursorShape.ArrowCursor)
        self._sync_state()

    def mousePressEvent(self, e):
        if self._locked:
            return
        if e.button() == Qt.MouseButton.LeftButton and self._enabled:
            self.setChecked(not self._selected, emit=True)
        # clicked is emitted by CardWidget on release; do not emit again here (would open the dialog twice).
        super().mousePressEvent(e)

    def sync_variants(self, lang):
        if self.variant_choice is not None:
            texts = [(v.get("short", {}).get(lang) or v["label"][lang])
                     for v in self.feat["variants"]]
            tips = [v["label"][lang] for v in self.feat["variants"]]
            self.variant_choice.set_texts(texts, tips)

    def current_variant_label(self, lang):
        if self.variant_choice is None:
            return ""
        key = self.variant_choice.current_key()
        return next(v["label"][lang] for v in self.feat["variants"] if v["id"] == key)


class ModeCard(SelectableCard):
    """Large mode tile: whole-tile highlight, no visible radio dot; mutual exclusion is driven by window state."""

    def __init__(self, mode, parent=None):
        self.mode = mode
        super().__init__(parent)
        self.setMinimumHeight(88)

        h = QHBoxLayout(self)
        h.setContentsMargins(18, 14, 18, 14)
        h.setSpacing(10)
        v = QVBoxLayout()
        v.setSpacing(4)
        self.mtitle = StrongBodyLabel()
        self.mdesc = CaptionLabel()
        self.mdesc.setWordWrap(True)
        v.addWidget(self.mtitle)
        v.addWidget(self.mdesc)
        v.addStretch(1)
        h.addLayout(v, 1)
        self._sync_state()

    def _sync_state(self):
        color = WHITE if self._selected else INK
        desc = WHITE if self._selected else SUB
        self.mtitle.setTextColor(color, color)
        self.mdesc.setTextColor(desc, desc)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class VoDiyDialog(MessageBoxBase):
    """Chinese voice DIY prompt dialog (uses the framework MessageBoxBase, no custom painting)."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.lang = win.lang
        self.setWindowTitle(t("vo_diy_dialog_title", self.lang))
        note = BodyLabel(t("vo_diy_dialog_note", self.lang))
        note.setWordWrap(True)
        self.viewLayout.addWidget(note)
        self.editor = TextEdit()
        self.editor.setReadOnly(True)
        self.editor.setText(self.prompt_text())
        self.editor.setMinimumHeight(360)
        self.viewLayout.addWidget(self.editor)
        self.yesButton.setText(t("vo_diy_copy", self.lang))
        self.cancelButton.setText(t("vo_diy_close", self.lang))
        self.yesButton.clicked.connect(self._copy)
        self.widget.setMinimumWidth(760)

    def prompt_text(self):
        fname = "voice_diy_prompt_zh.txt" if self.lang == "zh" else "voice_diy_prompt_en.txt"
        p = APP_DIR / fname
        if p.exists():
            return p.read_text(encoding="utf-8")
        return t("vo_diy_missing", self.lang)

    def _copy(self):
        QApplication.clipboard().setText(self.prompt_text())
        InfoBar.success(t("vo_diy_copied", self.lang), "", duration=2000,
                        parent=self.win)


class CameraWarningDialog(MessageBoxBase):
    """Camera-unlock caution dialog (red body text; interaction matches the Chinese voice DIY dialog)."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.lang = win.lang
        feat = next(f for f in FEATURES if f["id"] == "camera")
        self.setWindowTitle(t("camera_warning_title", self.lang))
        self.viewLayout.addWidget(SubtitleLabel(t("camera_warning_title", self.lang)))
        self.body = BodyLabel(feat["warning"][self.lang])
        self.body.setWordWrap(True)
        self.body.setTextColor(RED, RED)
        self.viewLayout.addWidget(self.body)
        self.yesButton.setText(t("vo_diy_close", self.lang))
        self.cancelButton.hide()
        self.widget.setMinimumWidth(760)


class NoticeDialog(MessageBoxBase):
    """Generic notice dialog (optionally red text) for states needing confirmation, e.g. no save for Black Shield."""

    def __init__(self, win, title, body, danger=True):
        super().__init__(win)
        self.setWindowTitle(title)
        self.viewLayout.addWidget(SubtitleLabel(title))
        self.body = BodyLabel(body)
        self.body.setWordWrap(True)
        if danger:
            self.body.setTextColor(RED, RED)
        self.viewLayout.addWidget(self.body)
        self.yesButton.setText(t("vo_diy_close", win.lang))
        self.cancelButton.hide()
        self.widget.setMinimumWidth(720)


_MD_SEP = re.compile(r"^\|?[\s:\-|]+\|?$")


def md_to_plain(md: str) -> str:
    """Flatten manifest Markdown into readable plain text: table rows become cells joined by "｜" with
    emphasis markers stripped; headings lose "#" and "- " list items become "· "."""
    out = []
    for ln in md.splitlines():
        s = ln.strip()
        if s.startswith("|"):
            if _MD_SEP.match(s):
                continue
            cells = [c.strip().replace("**", "").replace("`", "")
                     for c in s.strip("|").split("|")]
            out.append(" ｜ ".join(c for c in cells if c))
        else:
            t2 = ln.replace("**", "").replace("`", "")
            s2 = t2.strip()
            if s2.startswith("#"):
                t2 = s2.lstrip("#").strip()
            elif s2.startswith("- "):
                t2 = "· " + s2[2:]
            out.append(t2)
    return "\n".join(out)


class BugDetailDialog(MessageBoxBase):
    """Bundled-list dialog: read-only view of a feature's detail document (bug fix list / visual tier parameters)."""

    def __init__(self, win, feat_id="bugfix"):
        super().__init__(win)
        self.win = win
        self.lang = win.lang
        self.feat_id = feat_id
        _feat = next(f for f in FEATURES if f["id"] == feat_id)
        self._title = t(_feat.get("detail_title_key", "bug_dialog_title"), self.lang)
        self.setWindowTitle(self._title)
        self.viewLayout.addWidget(SubtitleLabel(self._title))
        self.view = TextEdit()
        self.view.setReadOnly(True)
        self.view.setMinimumSize(760, 560)   # Four tiers + frame times + streaming notes exceed 40 lines; a shorter height clips the last rows.
        text = md_to_plain(self.doc_text())
        _feat = next(f for f in FEATURES if f["id"] == feat_id)
        _warn = _feat.get("warning")
        if _warn:
            intro = str(_warn.get(self.lang) or _warn.get("zh") or "").strip()
            if intro:
                text = intro + "\n\n" + text
        self.view.setPlainText(text)
        self.viewLayout.addWidget(self.view)
        self.yesButton.setText(t("vo_diy_close", self.lang))
        self.cancelButton.hide()
        self.widget.setMinimumWidth(820)

    def doc_text(self):
        feat = next(f for f in FEATURES if f["id"] == self.feat_id)
        docs = feat.get("detail_doc") or {}
        p = APP_DIR / docs.get(self.lang, docs.get("zh", ""))
        if p.is_file():
            return p.read_text(encoding="utf-8")
        return t("bug_dialog_missing", self.lang)


class Page(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(28, 28, 28, 22)
        self.lay.setSpacing(16)
        self.title = TitleLabel()
        self.lay.addWidget(self.title)

    def is_valid(self):
        return True

    def on_enter(self):
        pass

    def retranslate(self):
        pass


class DisclaimerPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.scroll = SingleDirectionScrollArea(self, orient=Qt.Orientation.Vertical)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        hv = QVBoxLayout(host)
        hv.setContentsMargins(0, 0, 8, 0)
        self.body = BodyLabel()
        self.body.setWordWrap(True)
        hv.addWidget(self.body)
        hv.addStretch(1)
        self.scroll.setWidget(host)
        self.accept = CheckBox()
        self.lay.addWidget(self.scroll, 1)
        self.lay.addWidget(self.accept)
        self.accept.stateChanged.connect(lambda _: win.refresh_nav())

    def is_valid(self):
        return self.accept.isChecked()

    def retranslate(self):
        lang = self.win.lang
        self.title.setText(t("disclaimer_title", lang))
        self.body.setText(t("disclaimer_body", lang).format(
            appid=REGISTRY["game_appid"], build=REGISTRY["game_buildid"]))
        self.accept.setText(t("accept", lang))


class SetupPage(Page):
    """Page 2 · Setup: locate the exe, show the ownership notice, and choose install/restore on one page."""

    def __init__(self, win):
        super().__init__(win)
        self.root = None
        self.hint = CaptionLabel()
        self.hint.setWordWrap(True)
        self.lay.addWidget(self.hint)

        self.ownership_card = CardWidget()
        ov = QVBoxLayout(self.ownership_card)
        ov.setContentsMargins(18, 12, 18, 12)
        self.ownership = BodyLabel()
        self.ownership.setWordWrap(True)
        ov.addWidget(self.ownership)
        self.lay.addWidget(self.ownership_card)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.path_edit = LineEdit()
        self.path_edit.setPlaceholderText("DeathwatchGame.exe")
        self.browse = PushButton(FluentIcon.FOLDER_ADD, "")
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.browse)
        self.lay.addLayout(row)

        self.info = StrongBodyLabel()
        self.info.setWordWrap(True)
        self.lay.addWidget(self.info)
        self.pending = CaptionLabel()
        self.pending.setWordWrap(True)
        self.lay.addWidget(self.pending)

        self.mode_label = SubtitleLabel()
        self.lay.addSpacing(4)
        self.lay.addWidget(self.mode_label)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(10)
        self.install_card = ModeCard("install")
        self.restore_card = ModeCard("restore")
        self.install_card.clicked.connect(lambda: win.set_mode("install"))
        self.restore_card.clicked.connect(lambda: win.set_mode("restore"))
        mode_row.addWidget(self.install_card, 1)
        mode_row.addWidget(self.restore_card, 1)
        self.lay.addLayout(mode_row)
        self.lay.addStretch(1)

        self.browse.clicked.connect(self.on_browse)
        self.path_edit.textChanged.connect(self.validate)

    def on_browse(self):
        lang = self.win.lang
        path, _ = QFileDialog.getOpenFileName(
            self, t("exe_title", lang), "",
            t("exe_filter", lang) + " (*.exe);;All files (*)")
        if path:
            self.path_edit.setText(path)

    def validate(self):
        text = self.path_edit.text()
        self.root = derive_root(text) if text else None
        lang = self.win.lang
        if self.root:
            self.info.setText(t("root_ok", lang) + str(self.root))
            self.info.setTextColor(QColor(self.win.accent), QColor(self.win.accent))
        elif text:
            self.info.setText(t("root_bad", lang))
            self.info.setTextColor(RED, RED)
        else:
            self.info.setText("")
        self.win.refresh_nav()

    def is_valid(self):
        return self.root is not None and self.win.mode is not None

    def retranslate(self):
        lang = self.win.lang
        self.title.setText(t("setup_title", lang))
        self.hint.setText(t("exe_hint", lang))
        self.ownership.setText(t("path_ownership", lang).format(
            appid=REGISTRY.get("game_appid", "—"),
            build=REGISTRY.get("game_buildid", "—")))
        self.browse.setText(t("browse", lang))
        self.pending.setText(t("engine_pending", lang))
        self.mode_label.setText(t("mode_title", lang))
        self.install_card.mtitle.setText(t("mode_install", lang))
        self.install_card.mdesc.setText(t("mode_install_desc", lang))
        self.restore_card.mtitle.setText(t("mode_restore", lang))
        self.restore_card.mdesc.setText(t("mode_restore_desc", lang))
        self.validate()


class BonusPicker(QWidget):
    """Bonus extras picker: title + hint + card flow (shared by the features and finish pages)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.title = SubtitleLabel()
        self.hint = CaptionLabel()
        self.hint.setWordWrap(True)
        self.available = True
        host = QWidget()
        self.flow = FlowLayout(host, needAni=False, isTight=False)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.cards = {}
        for feat in BONUS:
            card = FeatureCard(feat)
            self.cards[feat["id"]] = card
            self.flow.addWidget(card)
        lay.addWidget(self.title)
        lay.addWidget(self.hint)
        lay.addWidget(host)

    def sync_card_widths(self, width):
        for card in self.cards.values():
            card.setFixedWidth(max(300, width))

    def selected(self):
        return [f["id"] for f in BONUS if self.cards[f["id"]].isChecked()]

    def apply_defaults(self):
        for feat in BONUS:
            self.cards[feat["id"]].setChecked(False, emit=False)

    def set_locked(self, locked):
        for card in self.cards.values():
            card.set_locked(locked)

    def set_available(self, available):
        """Disable Bonus in restore mode so the save is not modified after restoring originals."""
        self.available = bool(available)
        for card in self.cards.values():
            card.set_enabled(self.available)
        self.retranslate()

    def retranslate(self):
        lang = self.win.lang
        self.title.setText(t("bonus_title", lang))
        self.hint.setText(t("bonus_hint" if self.available
                            else "bonus_hint_restore", lang))
        for feat in BONUS:
            card = self.cards[feat["id"]]
            card.title.setText(feat["name"][lang])
            card.desc.setText(feat["desc"][lang])
            if card.warn is not None:
                card.warn.setText(feat["warning"][lang])
            card._sync_state()


class FeaturesPage(Page):
    """Page 3 · Features: uniform-size card grid; the bottom-right button runs the installer directly."""

    def __init__(self, win):
        super().__init__(win)
        self.hint = CaptionLabel()
        self.hint.setWordWrap(True)
        self.lay.addWidget(self.hint)

        self.scroll = SingleDirectionScrollArea(self, orient=Qt.Orientation.Vertical)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        host_lay = QVBoxLayout(host)
        host_lay.setContentsMargins(0, 0, 0, 0)
        host_lay.setSpacing(12)
        flow_host = QWidget()
        self.flow = FlowLayout(flow_host, needAni=False, isTight=False)
        self.cards = {}
        for feat in CORE_FEATURES:
            card = FeatureCard(feat)
            self.cards[feat["id"]] = card
            self.flow.addWidget(card)
        host_lay.addWidget(flow_host)
        host_lay.addStretch(1)
        self.scroll.setWidget(host)
        self.lay.addWidget(self.scroll, 1)
        # Clicking a tile only toggles selection; details always go through the title-row link, and the
        # dialog never toggles selection back.
        self._uniform_h = 0
        self.apply_defaults()

    @property
    def variant_buttons(self):
        return self.cards["visual"].variant_choice.buttons

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.sync_card_widths()

    def sync_card_widths(self):
        """Split width evenly across 2–3 columns; all cards share one size, styled after the balance card.

        Height comes from the sizeHint of a standard card without a variant row (balance / Chinese text,
        about 76px); every card is aligned to it, and the visual card's variant row (bottom ≈ 66px) fits
        within that height.
        """
        avail = self.scroll.viewport().width()
        if avail < 200:
            avail = max(560, self.width() - 48)
        m = self.flow.contentsMargins()
        avail -= m.left() + m.right() + 18   # FlowLayout content margins + vertical scrollbar
        gap = self.flow.horizontalSpacing()
        cols = 3 if avail >= 1240 else 2
        w = max(300, (avail - gap * (cols - 1)) // cols)
        all_cards = list(self.cards.values())
        # Reference height = the balance card (standard title row + two description lines), used as the
        # uniform style baseline.
        ref = self.cards.get("balance")
        h = ref.layout().sizeHint().height() if ref is not None else 0
        if h < 60:
            h = 76
        for card in all_cards:
            card.setFixedWidth(w)
            card.setFixedHeight(h)

    def on_enter(self):
        self.sync_card_widths()

    def apply_defaults(self):
        zh = self.win.lang == "zh"
        for feat in CORE_FEATURES:
            d = feat["default_checked"]
            self.cards[feat["id"]].setChecked(d == "zh_system" and zh or d is True, emit=False)

    def selected(self):
        return [f["id"] for f in CORE_FEATURES
                if self.cards[f["id"]].isChecked()]

    def build_diy_dialog(self):
        return VoDiyDialog(self.win)

    def open_diy(self):
        self.build_diy_dialog().exec()

    def build_camera_warning(self):
        return CameraWarningDialog(self.win)

    def open_camera_warning(self):
        self.build_camera_warning().exec()

    def open_visual_notice(self):
        BugDetailDialog(self.win, "visual").exec()

    def retranslate(self):
        lang = self.win.lang
        self.title.setText(t("features_title", lang))
        self.hint.setText(t("features_hint", lang))
        for feat in CORE_FEATURES:
            card = self.cards[feat["id"]]
            card.title.setText(feat["name"][lang])
            card.desc.setText(feat["desc"][lang])
            if card.warn is not None:
                card.warn.setText(feat["warning"][lang])
            card.sync_variants(lang)
            card.set_lang(lang)
            card._sync_state()


class BonusAuthorSection(QWidget):
    """Bottom of the finish page: left = Bonus summary (read-only), right = about the author."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.picker = BonusPicker(win)
        self.cards = self.picker.cards            # Compatible with older references.
        self.author_title = SubtitleLabel()
        self.author = self._build_author()
        grid.addWidget(self.picker, 0, 0, 2, 1)
        grid.addWidget(self.author_title, 0, 1, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.author, 1, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

    def _build_author(self):
        """Author card on the right: description + optional links / QR code; no placeholder text when unconfigured."""
        frame = CardWidget()
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        h = QHBoxLayout(frame)
        h.setContentsMargins(18, 14, 18, 14)
        h.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(6)
        self.author_desc = BodyLabel()
        self.author_desc.setWordWrap(True)
        self.links_label = BodyLabel()
        self.links_label.setWordWrap(True)
        self.links_label.setOpenExternalLinks(True)
        self.links_label.setVisible(False)
        self.notice_link = BodyLabel()
        self.notice_link.setOpenExternalLinks(False)
        self.notice_link.linkActivated.connect(self._open_notices)
        left.addWidget(self.author_desc)
        left.addWidget(self.links_label)
        left.addWidget(self.notice_link)
        left.addStretch(1)
        h.addLayout(left, 1)
        self.qr = None
        qr_path = APP_DIR / SUPPORT.get("qr_image", "support_qr.png")
        if qr_path.exists():
            pm = QPixmap(str(qr_path))
            if not pm.isNull():
                self.qr = QLabel()
                self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.qr.setFixedSize(96, 96)
                self.qr.setPixmap(pm.scaled(96, 96,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                h.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignTop)
        return frame

    def _open_notices(self, _link=""):
        p = APP_DIR / "THIRD_PARTY_NOTICES.md"
        if not p.is_file():
            p = APP_DIR.parent / "THIRD_PARTY_NOTICES.md"
        if p.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def sync_card_widths(self):
        """Equal-width columns; Bonus cards adapt to the left column width (keeping their natural height)."""
        col = max(300, (self.width() - 10) // 2 - 6)
        self.picker.sync_card_widths(col)

    def selected(self):
        return self.picker.selected()

    def apply_defaults(self):
        self.picker.apply_defaults()

    def set_locked(self, locked):
        self.picker.set_locked(locked)

    def retranslate(self):
        lang = self.win.lang
        self.picker.retranslate()
        self.author_title.setText(t("author_title", lang))
        self.author_desc.setText(t("author_desc", lang))
        links = SUPPORT.get("links", [])
        if links:
            self.links_label.setText("<br>".join(
                f'<a href="{l["url"]}">{l.get("label_en") if lang == "en" else l["label"]}</a>'
                for l in links))
            self.links_label.setVisible(True)
        else:
            self.links_label.setVisible(False)
        notice = APP_DIR / "THIRD_PARTY_NOTICES.md"
        if not notice.is_file():
            notice = APP_DIR.parent / "THIRD_PARTY_NOTICES.md"
        if notice.is_file():
            self.notice_link.setText(
                f'<a href="notices" style="color:#6f6f6f; text-decoration:underline;">'
                f'{t("third_party_notices", lang)}</a>')
            self.notice_link.setVisible(True)
        else:
            self.notice_link.setVisible(False)


class DonePage(Page):
    """Page 4 · Finish: top = results log, bottom = Black Shield contract (click to inject) + about the author."""

    def __init__(self, win):
        super().__init__(win)
        self.log_title = SubtitleLabel()
        self.lay.addWidget(self.log_title)
        self.log = TextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(120)
        self.lay.addWidget(self.log, 1)

        self.section = BonusAuthorSection(win)
        self.lay.addWidget(self.section)
        self.cards = self.section.cards          # Compatible with older references.
        self.author = self.section.author
        self.author_title = self.section.author_title

    def set_bonus_available(self, available):
        self.section.picker.set_available(available)

    def reset_log(self):
        self.log.clear()

    def append(self, text, ok=True):
        mark = "✓" if ok else "✕"
        color = GREEN if ok else "#B00020"
        self.log.append(f'<span style="color:{color}; font-weight:600;">{mark}</span> '
                        f'<span style="color:#1b1b1b;">{_html.escape(text)}</span>')

    def note(self, text):
        self.log.append(f'<span style="color:#6f6f6f;">{_html.escape(text)}</span>')

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.section.sync_card_widths()

    def on_enter(self):
        self.section.sync_card_widths()

    def retranslate(self):
        lang = self.win.lang
        self.title.setText(t("done_title", lang))
        self.log_title.setText(t("log_title", lang))
        self.section.retranslate()


class PatchWorker(QThread):
    """Background execution thread: the UI thread only receives log lines and never blocks."""

    line = Signal(str, bool)
    note = Signal(str)
    finished_ok = Signal()

    def __init__(self, win, actions, root, mode, session=None):
        super().__init__()
        self.win = win
        self.actions = actions
        self.root = Path(root)
        self.mode = mode
        self.lang = win.lang
        self.session = session

    def run(self):
        try:
            if self.mode == "restore":
                self._restore()
            else:
                self._install()
        except Exception as exc:                  # noqa: BLE001
            self.note.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished_ok.emit()

    def _report(self, name, r):
        if r["errors"]:
            self.line.emit(t("log_failed", self.lang).format(name=name) + " — " + r["detail"], False)
            for e in r["errors"][:5]:
                self.note.emit(e)
        elif r["applied"] == 0 and r["skipped"]:
            self.line.emit(t("log_already", self.lang).format(name=name), True)
        else:
            self.line.emit(t("log_ok", self.lang).format(name=name) + " — " + r["detail"], True)

    def _package_rels(self, pkg_dir):
        try:
            doc = json.loads((Path(pkg_dir) / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            return []
        return [e.get("relative_path") for e in (doc.get("entries") or [])
                if e.get("relative_path")]

    def _selected_rels(self):
        """Relative paths written by the selected features; the shared text layer is pre-restored by zh_text."""
        rels = set()
        for act in self.actions:
            aid = act.get("id")
            if aid == "zh_text":
                data_file = APP_DIR / "zh_runtime.json"
                if data_file.is_file():
                    data = zh_runtime.load_data(data_file)
                    rels.update(zh_runtime.target_rels(self.root, data))
            elif aid == "bugfix":
                for pkg in act.get("packages") or []:
                    rels.update(self._package_rels(PAYLOAD_DIR / "bugfix" / pkg))
            elif aid == "visual":
                rels.update(self._package_rels(
                    PAYLOAD_DIR / "visual" / act.get("variant", "")))
            elif aid == "balance":
                for pkg in act.get("packages") or []:
                    rels.update(self._package_rels(PAYLOAD_DIR / "balance" / pkg))
            elif aid == "camera":
                try:
                    doc = json.loads((APP_DIR / "camera_rules.json").read_text(encoding="utf-8"))
                    rels.update(t.get("relative_path") for t in (doc.get("targets") or [])
                                if t.get("relative_path"))
                except Exception:
                    pass
        # Shared locres text layer: pre-restore only when zh_text is selected; otherwise let the bugfix
        # already_fixed / balance adaptive-text logic keep the existing Chinese text.
        if "zh_text" not in {a.get("id") for a in self.actions}:
            rels.discard(patcher.SPLASH_DESC_REL)
        return sorted(r for r in rels if r)

    def _install(self):
        session = self.session or patcher.new_session(self.root)
        try:
            rels = self._selected_rels()
            if rels:
                pre = patcher.pre_restore_selected(self.root, rels, log=self.note.emit)
                if pre["restored"] or pre["deleted"]:
                    self.note.emit(t("log_pre_restore", self.lang).format(
                        restored=pre["restored"], deleted=pre["deleted"]))
                for e in pre["errors"][:5]:
                    self.note.emit(e)
            for act in self.actions:
                aid, name = act["id"], act["name"]
                if aid == "zh_text":
                    data_file = APP_DIR / "zh_runtime.json"
                    if not data_file.is_file():
                        self.line.emit(t("log_zh_missing", self.lang), False)
                        continue
                    data = zh_runtime.load_data(data_file)
                    results = zh_runtime.apply_zh_text(
                        self.root, data, session.dir, log=self.note.emit)
                    for r in results:
                        label = t("chain_" + r["chain"], self.lang)
                        ok = r["ok"] and not r["errors"]
                        self.line.emit(f"{label} — {r['detail']}", ok)
                        for e in r["errors"][:3]:
                            self.note.emit(e)
                elif aid == "visual":
                    pkg = PAYLOAD_DIR / "visual" / act.get("variant", "")
                    self._report(name, patcher.apply_manifest_package(
                        self.root, pkg, session, log=self.note.emit))
                elif aid == "camera":
                    self._report(name, patcher.apply_camera_patch(
                        self.root, APP_DIR / "camera_rules.json", session,
                        log=self.note.emit))
                elif aid == "bugfix":
                    for pkgname in act.get("packages") or []:
                        self._report(f"{name} · {pkgname}", patcher.apply_manifest_package(
                            self.root, PAYLOAD_DIR / "bugfix" / pkgname,
                            session, log=self.note.emit))
                elif aid == "balance":
                    for pkgname in act.get("packages") or []:
                        self._report(f"{name} · {pkgname}", patcher.apply_manifest_package(
                            self.root, PAYLOAD_DIR / "balance" / pkgname,
                            session, log=self.note.emit))
                    self._report(f"{name} · 溅射文本", patcher.apply_balance_splash_text(
                        self.root, session, log=self.note.emit))
                    self._report(f"{name} · 致残射击概率文本", patcher.apply_balance_crippling_text(
                        self.root, session, log=self.note.emit))
                elif aid == "bonus_blackshield":
                    pre = patcher.blackshield_preflight()
                    if not pre.get("exists"):
                        self.note.emit(t("log_save_missing", self.lang))
                        continue
                    if not pre.get("parse_ok"):
                        self.note.emit(t("log_save_unreadable", self.lang))
                        continue
                    r = patcher.inject_blackshield(session, log=self.note.emit)
                    if r.get("skipped"):
                        self.line.emit(t("log_save_already", self.lang), True)
                    elif r.get("ok"):
                        self.line.emit(t("log_save_ok", self.lang), True)
                    else:
                        self.line.emit(t("log_save_failed", self.lang).format(
                            err="; ".join(r.get("errors") or ["?"])), False)
        finally:
            p = session.finalize()
            if p is not None:
                self.note.emit(t("log_backup_dir", self.lang).format(path=session.dir))
            else:
                self.note.emit(t("log_no_changes", self.lang))

    def _restore(self):
        r = patcher.restore_all(self.root)
        if not r.get("found"):
            self.note.emit(t("log_restore_none", self.lang))
            return
        self.line.emit(t("log_restored", self.lang).format(n=r["restored"]), r["ok"])
        for e in r["errors"][:5]:
            self.note.emit(e)
        for save_bak in r.get("save_backups") or []:
            self.note.emit(t("log_restore_save_kept", self.lang).format(path=save_bak))


class InstallerWindow(QWidget):
    def __init__(self, lang=None):
        super().__init__()
        setTheme(Theme.LIGHT)
        apply_light_palette()
        self.setObjectName("Root")
        self.lang = lang or detect_system_lang()
        self.mode = None
        self.accent = BLUE
        self.running = False
        self._plan = []
        self._worker = None
        self._last_session = None
        setThemeColor(QColor(BLUE))
        self.resize(1120, 760)
        self.setMinimumSize(940, 660)
        self.setWindowIcon(FluentIcon.UPDATE.icon(Theme.LIGHT))
        # The root container sets a single background color (platform integration, not widget styling);
        # all widgets/cards/text use the framework components.
        self.setStyleSheet("QWidget#Root { background: #F2F0EA; }")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.rail = NavigationRail(self)
        root.addWidget(self.rail)

        workspace = QWidget()
        workspace_lay = QVBoxLayout(workspace)
        workspace_lay.setContentsMargins(20, 20, 20, 18)
        workspace_lay.setSpacing(14)

        header = QHBoxLayout()
        header.setContentsMargins(10, 0, 10, 0)
        self.workspace_title = TitleLabel()
        self.workspace_meta = CaptionLabel()
        header_text = QVBoxLayout()
        header_text.setSpacing(0)
        header_text.addWidget(self.workspace_title)
        header_text.addWidget(self.workspace_meta)
        header.addLayout(header_text)
        header.addStretch(1)
        workspace_lay.addLayout(header)

        content_surface = CardWidget()
        content_lay = QVBoxLayout(content_surface)
        content_lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.pages = [DisclaimerPage(self), SetupPage(self),
                      FeaturesPage(self), DonePage(self)]
        for p in self.pages:
            self.stack.addWidget(p)
        self.pages[3].cards["bonus_blackshield"].clicked.connect(
            self._on_blackshield_click)
        content_lay.addWidget(self.stack)
        workspace_lay.addWidget(content_surface, 1)

        # Bottom nav: cancel / language radios / back / next.
        nav = QHBoxLayout()
        nav.setContentsMargins(10, 0, 10, 0)
        nav.setSpacing(10)
        self.cancel_btn = TransparentPushButton()
        self.back_btn = PushButton()
        self.next_btn = PrimaryPushButton()
        self.lang_label = CaptionLabel()
        self.lang_zh = RadioButton("中文")
        self.lang_en = RadioButton("English")
        self.lang_group = QButtonGroup(self)
        self.lang_group.addButton(self.lang_zh)
        self.lang_group.addButton(self.lang_en)
        self.lang_zh.setChecked(self.lang == "zh")
        self.lang_en.setChecked(self.lang == "en")
        nav.addWidget(self.cancel_btn)
        nav.addStretch(1)
        nav.addWidget(self.lang_label)
        nav.addWidget(self.lang_zh)
        nav.addWidget(self.lang_en)
        nav.addSpacing(4)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        workspace_lay.addLayout(nav)
        root.addWidget(workspace, 1)

        self.cancel_btn.clicked.connect(self.close)
        self.back_btn.clicked.connect(self.prev_page)
        self.next_btn.clicked.connect(self.next_page)
        self.lang_zh.toggled.connect(lambda on: on and self.set_lang("zh"))
        self.lang_en.toggled.connect(lambda on: on and self.set_lang("en"))

        self.retranslate()
        self.goto(0)
        self._apply_accent()

    # ---- state ----
    def current(self):
        return self.stack.currentIndex()

    def goto(self, i):
        self.stack.setCurrentIndex(i)
        self.pages[i].on_enter()
        self.rail.update_state(min(i, len(STEP_KEYS) - 1), self.accent)
        self.refresh_workspace_header()
        self.refresh_nav()

    def refresh_workspace_header(self):
        current = min(self.current(), len(STEP_KEYS) - 1)
        step_name = t(STEP_KEYS[current], self.lang)
        self.workspace_title.setText(step_name)
        self.workspace_meta.setText(t("step_count", self.lang).format(
            cur=min(self.current() + 1, len(STEP_KEYS)),
            total=len(STEP_KEYS), name=step_name))

    def next_page(self):
        i = self.current()
        if i == 2:                      # Features page: the bottom-right button runs the installer.
            self.run_patch()
            return
        if i == len(self.pages) - 1:    # Finish page: close.
            self.close()
            return
        self.goto(i + 1)

    def prev_page(self):
        if self.running or self.current() >= len(self.pages) - 1:
            return
        if self.current() > 0:
            self.goto(self.current() - 1)

    def refresh_nav(self):
        i = self.current()
        last = len(self.pages) - 1
        self.back_btn.setEnabled(0 < i < last and not self.running)
        if i == 2:
            self.next_btn.setText(t("exec_running" if self.running else "exec_title", self.lang))
            self.next_btn.setEnabled(not self.running)
        elif i == last:
            self.next_btn.setText(t("finish", self.lang))
            self.next_btn.setEnabled(not self.running)
        else:
            self.next_btn.setText(t("next", self.lang))
            self.next_btn.setEnabled(self.pages[i].is_valid())

    # ---- Execution (real engine: background thread, per-item log output) ----
    # Install order: bugfix (English layer) → Chinese text chain → visual → camera → balance.
    # Fixes are applied to the English files first; localization is an overlay, and the crippling locres
    # uses the Steam original as its baseline, so it must run before zh. Balance runs last: its 35→99
    # splash display text must be patched equal-length after zh_cn overwrites the locres, otherwise
    # localization would revert it to 35; the backend float32 multiplier is decoupled from the text.
    APPLY_ORDER = ["bugfix", "zh_text", "visual", "camera", "balance"]

    def _build_plan(self):
        lang = self.lang
        if self.mode == "restore":
            return [{"id": "restore", "name": t("log_restore", lang)}]
        fp = self.pages[2]
        plan = []
        selected = fp.selected()
        for fid in self.APPLY_ORDER:
            if fid not in selected:
                continue
            feat = next(f for f in FEATURES if f["id"] == fid)
            act = {"id": fid, "name": feat["name"][lang],
                   "packages": feat.get("packages") or []}
            if fid == "visual":
                act["variant"] = fp.cards["visual"].variant_choice.current_key()
                act["name"] += " · " + fp.cards["visual"].current_variant_label(lang)
            plan.append(act)
        if not plan:
            plan.append({"id": None, "name": t("summary_none", lang)})
        return plan

    def run_patch(self):
        if self.running:
            return
        self.running = True
        self.lang_zh.setEnabled(False)
        self.lang_en.setEnabled(False)
        self.pages[3].reset_log()
        self._plan = self._build_plan()
        self.refresh_nav()
        root = getattr(self.pages[1], "root", None)
        if root is None:
            self._last_session = None
            self.pages[3].note(t("root_bad", self.lang))
            self._finish_run()
            return
        mode = self.mode or "install"
        session = patcher.new_session(root) if mode != "restore" else None
        self._last_session = session
        self._worker = PatchWorker(self, self._plan, root, mode, session=session)
        self._worker.line.connect(self.pages[3].append)
        self._worker.note.connect(self.pages[3].note)
        self._worker.finished_ok.connect(self._finish_run)
        self._worker.start()

    def _finish_run(self):
        self.running = False
        self.lang_zh.setEnabled(True)
        self.lang_en.setEnabled(True)
        self._worker = None
        self.pages[3].set_bonus_available(self.mode != "restore")
        self.goto(len(self.pages) - 1)

    def _on_blackshield_click(self):
        """Black Shield contract on the finish page: check the save first; if missing or unreadable, ask the user to try later."""
        card = self.pages[3].cards["bonus_blackshield"]
        if self.mode == "restore":
            card.setChecked(False, emit=False)
            return
        if not card.isChecked():
            return
        pre = patcher.blackshield_preflight()
        if not pre.get("exists") or not pre.get("parse_ok"):
            card.setChecked(False, emit=False)
            NoticeDialog(self, t("bs_nosave_title", self.lang),
                         t("bs_nosave_body", self.lang)).exec()
            return
        root = getattr(self.pages[1], "root", None)
        if root is None:
            card.setChecked(False, emit=False)
            return
        # Reuse this install's session: a separate Black Shield save backup would create a "newest empty
        # session" and hide the real install backup from the restore index.
        sess = self._last_session
        if sess is None or Path(getattr(sess, "root", "")) != Path(root):
            sess = patcher.new_session(root)
        try:
            r = patcher.inject_blackshield(sess, log=self.pages[3].note)
        finally:
            sess.finalize()
        if r.get("ok") or r.get("skipped"):
            key = "log_save_already" if r.get("skipped") else "log_save_ok"
            self.pages[3].append(t(key, self.lang), True)
            if r.get("backup"):
                self.pages[3].note(t("log_save_backup", self.lang).format(
                    path=r["backup"]))
            card.set_locked(True)
        else:
            card.setChecked(False, emit=False)
            NoticeDialog(self, t("bs_fail_title", self.lang),
                         t("log_save_failed", self.lang).format(
                             err="; ".join(r.get("errors") or ["?"]))).exec()

    def _all_cards(self):
        yield from self.pages[2].cards.values()
        yield from self.pages[3].cards.values()
        yield self.pages[1].install_card
        yield self.pages[1].restore_card

    def _apply_accent(self):
        for card in self._all_cards():
            card.setAccent(self.accent)

    def set_mode(self, mode):
        if mode not in ("install", "restore"):
            return
        self.mode = mode
        self.accent = GREEN if mode == "install" else BLUE
        setThemeColor(QColor(self.accent))
        self.pages[1].install_card.setChecked(mode == "install")
        self.pages[1].restore_card.setChecked(mode == "restore")
        self._apply_accent()
        self.pages[1].validate()
        self.pages[3].set_bonus_available(mode != "restore")
        self.rail.update_state(self.current(), self.accent)
        self.refresh_nav()

    def set_lang(self, lang):
        if self.running:                # Ignore language switches while running (keep log language consistent).
            self._sync_lang_radios()
            return
        self.lang = lang
        self._sync_lang_radios()
        self.retranslate()
        self.pages[2].apply_defaults()
        self.refresh_nav()

    def _sync_lang_radios(self):
        for radio, code in ((self.lang_zh, "zh"), (self.lang_en, "en")):
            radio.blockSignals(True)
            radio.setChecked(self.lang == code)
            radio.blockSignals(False)

    def retranslate(self):
        lang = self.lang
        self.setWindowTitle(f"{t('brand', lang)} · {t('skeleton_tag', lang)}")
        self.lang_label.setText(t("lang_label", lang))
        self.cancel_btn.setText(t("cancel", lang))
        self.back_btn.setText(t("back", lang))
        self.refresh_workspace_header()
        self.rail.retranslate(lang)
        for p in self.pages:
            p.retranslate()
        self.rail.update_state(self.current(), self.accent)
        self.refresh_nav()

    def showEvent(self, e):
        super().showEvent(e)
        apply_caption_style(int(self.winId()))


def main():
    app = QApplication(sys.argv)
    setTheme(Theme.LIGHT)
    win = InstallerWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

