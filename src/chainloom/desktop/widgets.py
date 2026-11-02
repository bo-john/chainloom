from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class CopyToast(QFrame):
    """A brief in-window confirmation that never takes focus or intercepts clicks."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("copyToast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedWidth(280)
        self.setStyleSheet("""
            QFrame#copyToast { background: #F0F8F3; border: 1px solid #BBD8C8; border-radius: 12px; }
            QLabel#toastCheck { background: #226B53; color: white; border-radius: 14px; font-size: 18px; font-weight: 600; }
            QLabel#toastTitle { color: #226B53; font-size: 14px; font-weight: 600; }
            QLabel#toastDetail { color: #5B7061; font-size: 12px; }
        """)
        layout = box(self, horizontal=True, margins=(16, 12, 16, 12), spacing=12)
        check = text("✓", "toastCheck")
        check.setFixedSize(28, 28)
        check.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(check)
        content = QWidget()
        lines = box(content, spacing=3)
        self.title = text("复制成功", "toastTitle")
        self.detail = text("", "toastDetail")
        lines.addWidget(self.title)
        lines.addWidget(self.detail)
        layout.addWidget(content, 1)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(26, 60, 42, 35))
        self.setGraphicsEffect(shadow)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.hide()

    def reposition(self):
        parent = self.parentWidget()
        self.move(max(16, parent.width() - self.width() - 32), parent.menuBar().height() + 20)

    def show_message(self, message):
        self.detail.setText(message)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self.timer.start(1000)


def text(value, name="", wrap=False):
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def box(widget=None, horizontal=False, margins=(0, 0, 0, 0), spacing=12):
    layout = QHBoxLayout(widget) if horizontal else QVBoxLayout(widget)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return layout


def row(*widgets):
    container = QWidget()
    layout = box(container, horizontal=True, spacing=8)
    for widget in widgets:
        if widget is None:
            layout.addStretch()
        else:
            layout.addWidget(widget)
    return container


class BrandMark(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(36, 36)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#226B53"))
        painter.drawRoundedRect(0, 0, 36, 36, 10, 10)
        painter.setPen(QPen(QColor("white"), 2.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(9, 8, 11, 15, 5, 5)
        painter.drawRoundedRect(16, 14, 11, 15, 5, 5)
