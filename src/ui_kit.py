# -*- coding: utf-8 -*-
"""PySide6-only UI kit for the installer.

This replaces the former qfluentwidgets dependency with a small, local set of
widgets that keep the same visual language: warm off-white surfaces, rounded
cards, flat typography, segmented pill controls and compact dialogs.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QIcon, QPainter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLayout,
    QLineEdit, QPushButton, QRadioButton, QScrollArea, QSizePolicy,
    QStyle, QTextEdit, QToolTip, QVBoxLayout, QWidget,
)

ACCENT = QColor("#4F6B59")
TEXT = QColor("#1b1b1b")
MUTED = QColor("#5c5c5c")


def _app_qss() -> str:
    a = ACCENT.name()
    return f"""
    QPushButton {{
        background: #FFFEFA; color: {TEXT.name()}; border: 1px solid #D8D3C8;
        border-radius: 8px; padding: 6px 14px; min-height: 18px;
    }}
    QPushButton:hover {{ background: #F7F4EC; }}
    QPushButton:pressed {{ background: #EDE8DC; }}
    QPushButton:disabled {{ color: #9A9A9A; background: #EFECE4; }}
    QPushButton[primary="true"] {{
        background: {a}; color: white; border: 1px solid {a};
    }}
    QPushButton[primary="true"]:hover {{ background: {QColor(a).lighter(108).name()}; }}
    QPushButton[transparent="true"] {{ background: transparent; border: none; }}
    QLineEdit {{
        background: #FFFEFA; color: {TEXT.name()}; border: 1px solid #D8D3C8;
        border-radius: 8px; padding: 7px 10px; min-height: 18px;
    }}
    QTextEdit {{
        background: #FFFEFA; color: {TEXT.name()}; border: 1px solid #D8D3C8;
        border-radius: 8px; padding: 6px;
    }}
    QCheckBox, QRadioButton {{ color: {TEXT.name()}; spacing: 8px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
    QToolTip {{ background: #27353D; color: #F6F1E7; border: 0; padding: 6px; }}
    """


def _apply_style():
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(_app_qss())


def setThemeColor(color):
    global ACCENT
    ACCENT = QColor(color)
    _apply_style()


def setTheme(*_args, **_kwargs):
    _apply_style()


class Theme:
    LIGHT = "light"
    DARK = "dark"


class _IconSpec:
    def __init__(self, pixmap):
        self._pixmap = pixmap

    def icon(self, *_args, **_kwargs):
        return QApplication.style().standardIcon(self._pixmap)


class FluentIcon:
    FOLDER_ADD = _IconSpec(QStyle.StandardPixmap.SP_DirOpenIcon)
    UPDATE = _IconSpec(QStyle.StandardPixmap.SP_ComputerIcon)


class _TextLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setStyleSheet(f"color:{TEXT.name()};")

    def setTextColor(self, light, _dark=None):
        self.setStyleSheet(f"color:{QColor(light).name()};")


class TitleLabel(_TextLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        f = QFont(self.font()); f.setPixelSize(28); f.setBold(True); self.setFont(f)


class SubtitleLabel(_TextLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        f = QFont(self.font()); f.setPixelSize(18); f.setBold(True); self.setFont(f)


class StrongBodyLabel(_TextLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        f = QFont(self.font()); f.setPixelSize(14); f.setBold(True); self.setFont(f)


class BodyLabel(_TextLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        f = QFont(self.font()); f.setPixelSize(13); self.setFont(f)


class CaptionLabel(_TextLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        f = QFont(self.font()); f.setPixelSize(12); self.setFont(f)
        self.setStyleSheet(f"color:{MUTED.name()};")


class TextEdit(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(True)
        self.setStyleSheet(
            "QTextEdit{background:#FFFEFA;color:#1b1b1b;border:1px solid #D8D3C8;"
            "border-radius:8px;padding:6px;}")


class LineEdit(QLineEdit):
    pass


class CheckBox(QCheckBox):
    pass


class RadioButton(QRadioButton):
    pass


class PushButton(QPushButton):
    def __init__(self, *args, **kwargs):
        icon = None
        if args and isinstance(args[0], _IconSpec):
            icon = args[0]
            args = args[1:]
        text = ""
        if args and isinstance(args[0], str):
            text = args[0]
            args = args[1:]
        super().__init__(text, *args, **kwargs)
        if icon is not None:
            self.setIcon(icon.icon())


class PrimaryPushButton(PushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setProperty("primary", True)


class TransparentPushButton(PushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setProperty("transparent", True)


class CardWidget(QFrame):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bg = QColor(255, 255, 255, 170)
        self._pressed = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setBackgroundColor(self, color):
        self._bg = QColor(color)
        self.update()

    @property
    def backgroundColor(self):
        return QColor(self._bg)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._bg)
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 12, 12)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class SingleDirectionScrollArea(QScrollArea):
    def __init__(self, parent=None, orient=Qt.Orientation.Vertical):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setStyleSheet("QScrollArea{background:transparent;border:none;}")


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, hspacing=-1, vspacing=-1,
                 needAni=False, isTight=False):
        super().__init__(parent)
        self._items = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = effective.x(), effective.y(), 0
        hspace = self.horizontalSpacing()
        vspace = self.verticalSpacing()
        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisible():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + hspace
            if next_x - hspace > effective.right() + 1 and line_h > 0:
                x = effective.x()
                y += line_h + vspace
                next_x = x + hint.width() + hspace
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - effective.y()

    def horizontalSpacing(self):
        if self._hspace >= 0:
            return self._hspace
        return 10

    def verticalSpacing(self):
        if self._vspace >= 0:
            return self._vspace
        return 10


class MessageBoxBase(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setStyleSheet("QDialog{background:#F2F0EA;}")

        self.widget = QWidget(self)
        self.viewLayout = QVBoxLayout(self.widget)
        self.viewLayout.setContentsMargins(28, 28, 28, 16)
        self.viewLayout.setSpacing(12)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancelButton = PushButton("")
        self.yesButton = PrimaryPushButton("")
        self.cancelButton.clicked.connect(self.reject)
        self.yesButton.clicked.connect(self.accept)
        buttons.addWidget(self.cancelButton)
        buttons.addWidget(self.yesButton)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.widget)
        outer.addLayout(buttons)


class InfoBar:
    @staticmethod
    def success(title, _content="", duration=2000, parent=None):
        QToolTip.showText(QCursor.pos(), title, parent)
