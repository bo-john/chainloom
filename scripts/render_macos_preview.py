"""Render a visual concept with actual PySide6 widgets; all data is illustrative.

Run: QT_QPA_PLATFORM=offscreen uv run --no-project --with 'PySide6-Essentials==6.10.2' python \
     scripts/render_macos_preview.py output/previews/preview.png
"""

import argparse
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout, QWidget,
)


INK = "#1C2D2A"
MUTED = "#87928E"
GREEN = "#226B53"


def label(text, size=14, color=INK, weight=400):
    widget = QLabel(text)
    widget.setStyleSheet(
        f"color:{color};font-size:{size}px;font-weight:{weight};background:transparent;border:none;border-radius:0;"
    )
    return widget


def layout(widget, kind=QVBoxLayout, margins=(0, 0, 0, 0), spacing=0):
    box = kind(widget)
    box.setContentsMargins(*margins)
    box.setSpacing(spacing)
    return box


def row(*widgets, spacing=8):
    widget = QWidget()
    box = layout(widget, QHBoxLayout, spacing=spacing)
    for child in widgets:
        if child is None:
            box.addStretch()
        else:
            box.addWidget(child)
    return widget


def line():
    widget = QFrame()
    widget.setFixedHeight(1)
    widget.setStyleSheet("background:#EBEFEC;")
    return widget


class Mark(QWidget):
    def __init__(self, kind="logo", size=34):
        super().__init__()
        self.kind = kind
        self.setFixedSize(size, size)

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = self.width()
        painter.scale(size / 36, size / 36)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(GREEN if self.kind == "logo" else "#EDF2F7"))
        painter.drawRoundedRect(0, 0, 36, 36, 11, 11)
        if self.kind == "logo":
            painter.setPen(QPen(QColor("#FFFFFF"), 2.3))
            painter.drawRoundedRect(9, 8, 11, 15, 5, 5)
            painter.drawRoundedRect(16, 14, 11, 15, 5, 5)
        else:
            painter.setPen(QPen(QColor("#75839F"), 1.5))
            painter.drawLine(18, 7, 11, 18)
            painter.drawLine(11, 18, 18, 22)
            painter.drawLine(18, 22, 25, 18)
            painter.drawLine(25, 18, 18, 7)
            painter.drawLine(18, 7, 18, 22)
            painter.drawLine(11, 22, 18, 29)
            painter.drawLine(18, 29, 25, 22)


def field(content, height=58):
    frame = QFrame()
    frame.setFixedHeight(height)
    frame.setStyleSheet("QFrame{background:#FFFFFF;border:1px solid #DEE5DF;border-radius:12px;}")
    box = layout(frame, QHBoxLayout, (16, 0, 16, 0), 10)
    box.addWidget(content)
    return frame


def build():
    canvas = QWidget()
    canvas.setFixedSize(1440, 990)
    canvas.setObjectName("canvas")
    canvas.setStyleSheet("QWidget#canvas{background:#E8EDE8;}")
    shell = QFrame(canvas)
    shell.setGeometry(70, 65, 1300, 860)
    shell.setObjectName("shell")
    shell.setStyleSheet("QFrame#shell{background:#FDFEFC;border:1px solid #CED6CF;border-radius:17px;}")
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(52)
    shadow.setOffset(0, 18)
    shadow.setColor(QColor(33, 52, 39, 40))
    shell.setGraphicsEffect(shadow)
    outer = layout(shell, margins=(1, 1, 1, 1))

    titlebar = QWidget()
    titlebar.setFixedHeight(58)
    bar = layout(titlebar, QHBoxLayout, (23, 0, 23, 0), 9)
    for color in ("#FF6058", "#FEBC2E", "#28C840"):
        dot = QLabel()
        dot.setFixedSize(12, 12)
        dot.setStyleSheet(f"background:{color};border-radius:6px;")
        bar.addWidget(dot)
    bar.addSpacing(23)
    bar.addWidget(label("Chainloom", 13, "#69756E", 500))
    bar.addStretch()
    bar.addWidget(label("本地桌面应用", 11, MUTED))
    outer.addWidget(titlebar)
    outer.addWidget(line())

    body = QWidget()
    bodybox = layout(body, QHBoxLayout)
    sidebar = QFrame()
    sidebar.setFixedWidth(225)
    sidebar.setObjectName("sidebar")
    sidebar.setStyleSheet("QFrame#sidebar{background:#F4F6F1;border-bottom-left-radius:16px;}")
    side = layout(sidebar, margins=(22, 28, 22, 24), spacing=0)
    side.addWidget(row(Mark(), label("Chainloom", 19, INK, 650), None, spacing=10))
    side.addSpacing(41)
    side.addWidget(label("工作空间", 11, MUTED, 500))
    side.addSpacing(13)
    for text, active in [("↗     转账", True), ("◷     交易记录", False), ("⚙     设置", False)]:
        button = QPushButton(text)
        button.setFixedHeight(44)
        button.setStyleSheet(
            "QPushButton{border:0;border-radius:9px;text-align:left;padding-left:15px;"
            f"font-size:14px;font-weight:{600 if active else 400};"
            f"color:{GREEN if active else '#68766D'};background:{'#E3EBE0' if active else 'transparent'};}}"
        )
        side.addWidget(button)
        side.addSpacing(7)
    side.addStretch()
    wallet = QFrame()
    wallet.setObjectName("wallet")
    wallet.setStyleSheet("QFrame#wallet{background:#FBFCF9;border:1px solid #E1E7DC;border-radius:12px;}")
    wb = layout(wallet, margins=(14, 15, 14, 15), spacing=8)
    wb.addWidget(label("●  本地钱包已解锁", 11, GREEN, 500))
    wb.addWidget(label("0x7E5F…5Bdf", 14, INK, 600))
    wb.addWidget(label("使用本地钱包签名", 10, MUTED))
    side.addWidget(wallet)
    side.addSpacing(19)
    side.addWidget(label("●  网络连接正常", 11, "#81907F"))
    bodybox.addWidget(sidebar)

    main = QWidget()
    mb = layout(main, margins=(39, 30, 39, 24))
    mb.addWidget(row(label("单次转账", 28, INK, 650), None,
                     label("界面预览 · 演示数据", 11, MUTED)))
    mb.addSpacing(8)
    mb.addWidget(label("将资产发送到另一个钱包，简单几步即可完成。", 13, "#7E8982"))
    mb.addSpacing(30)
    steps = row(label("①  填写转账信息", 12, GREEN, 600),
                label("  ──────  ", 11, "#D5DDD4"),
                label("②  钱包确认", 12, "#A1AAA3"),
                label("  ──────  ", 11, "#D5DDD4"),
                label("③  转账结果", 12, "#A1AAA3"), None)
    mb.addWidget(steps)
    mb.addSpacing(29)

    columns = QWidget()
    cb = layout(columns, QHBoxLayout, spacing=27)
    form = QWidget()
    fb = layout(form)
    fb.addWidget(label("转账网络", 12, "#59695F", 550))
    fb.addSpacing(9)
    fb.addWidget(field(row(Mark("eth", 27), label("Ethereum", 14, INK, 550),
                           None, label("主网", 11, MUTED), label("⌄", 16, "#7F8D83"))))
    fb.addSpacing(16)
    fb.addWidget(row(label("转账资产", 12, "#59695F", 550), None,
                     label("余额  1.2841 ETH", 11, MUTED)))
    fb.addSpacing(9)
    fb.addWidget(field(row(Mark("eth", 29), label("ETH", 14, INK, 550),
                           label("Ethereum", 12, MUTED), None, label("⌄", 16, "#7F8D83"))))
    fb.addSpacing(16)
    fb.addWidget(row(label("收款地址", 12, "#59695F", 550), None,
                     label("粘贴地址", 11, GREEN, 550)))
    fb.addSpacing(9)
    fb.addWidget(field(row(label("0x2B5AD5c4795c026514f8317c7a215E218DcCD6cF", 11, "#46594E"),
                           None, label("▣", 18, "#86958A"))))
    fb.addSpacing(7)
    fb.addWidget(label("✓  地址格式正确", 10, "#71907A"))
    fb.addSpacing(20)
    fb.addWidget(label("转账数量", 12, "#59695F", 550))
    fb.addSpacing(9)
    fb.addWidget(field(row(label("0.025", 28, INK, 500), None,
                           label("ETH", 13, "#738278", 500)), 76))
    fb.addSpacing(12)
    fb.addWidget(label("网络手续费将从 ETH 余额中扣除。", 11, MUTED))
    fb.addSpacing(21)
    next_button = QPushButton("下一步：确认转账     →")
    next_button.setFixedHeight(51)
    next_button.setStyleSheet(
        "QPushButton{background:#226B53;color:white;border:none;border-radius:11px;"
        "font-size:14px;font-weight:600;}"
    )
    fb.addWidget(next_button)
    fb.addSpacing(11)
    fb.addWidget(label("下一步将在你的钱包中完成签名确认", 10, MUTED))
    fb.addStretch()
    cb.addWidget(form, 5)

    summary = QFrame()
    summary.setFixedWidth(300)
    summary.setObjectName("summary")
    summary.setStyleSheet("QFrame#summary{background:#F5F7F2;border:1px solid #E7ECE2;border-radius:15px;}")
    sb = layout(summary, margins=(23, 25, 23, 23))
    sb.addWidget(row(label("转账预览", 14, INK, 600), None, label("↗", 18, "#899B8B")))
    sb.addSpacing(29)
    sb.addWidget(label("你将发送", 11, MUTED))
    sb.addSpacing(5)
    sb.addWidget(label("0.025 ETH", 30, INK, 550))
    sb.addSpacing(24)
    sb.addWidget(line())
    sb.addSpacing(22)
    for title, value in [("网络", "Ethereum"), ("收款地址", "0x2B5A…D6cF"),
                         ("预计网络费用", "0.00021 ETH")]:
        sb.addWidget(row(label(title, 11, "#859082"), None, label(value, 12, "#44574A", 500)))
        sb.addSpacing(19)
    sb.addWidget(line())
    sb.addSpacing(19)
    sb.addWidget(label("预计总支出", 11, MUTED))
    sb.addSpacing(6)
    sb.addWidget(label("0.02521 ETH", 23, GREEN, 550))
    sb.addSpacing(5)
    sb.addWidget(label("实际网络费用以钱包确认为准", 10, MUTED))
    sb.addStretch()
    sb.addWidget(line())
    sb.addSpacing(18)
    sb.addWidget(label("签名由你的钱包完成", 12, "#5F7564", 550))
    sb.addSpacing(6)
    note = label("Chainloom 不接触你的私钥。\n发送前，请再次核对收款地址及网络。", 10, "#8A9788")
    note.setWordWrap(True)
    sb.addWidget(note)
    cb.addWidget(summary)
    mb.addWidget(columns, 1)
    mb.addSpacing(25)
    mb.addWidget(line())
    mb.addSpacing(15)
    mb.addWidget(row(label("本地运行 · 钱包签名", 10, MUTED), None,
                     label("需要帮助？", 10, "#77917D")))
    bodybox.addWidget(main, 1)
    outer.addWidget(body, 1)
    return canvas


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render a macOS interface concept to a PNG file.")
    parser.add_argument("output", type=Path, help="PNG output path; no default file is generated")
    args = parser.parse_args()
    destination = args.output.expanduser().resolve()
    app = QApplication([])
    app.setFont(QFont("PingFang SC", 12))
    canvas = build()
    canvas.show()
    app.processEvents()
    image = QPixmap(canvas.size() * 2)
    image.setDevicePixelRatio(2)
    image.fill(QColor("#E8EDE8"))
    canvas.render(image)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(destination)):
        raise RuntimeError("Could not save preview")
    print(destination.resolve())
