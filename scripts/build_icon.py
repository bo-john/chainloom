"""Create the app icon using Qt, without adding an image tool dependency."""

import os
import subprocess
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

app = QGuiApplication([])
root = Path(__file__).resolve().parents[1]
assets = root / "src/chainloom/desktop/assets"
image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
image.fill(Qt.GlobalColor.transparent)
painter = QPainter(image)
painter.setRenderHint(QPainter.RenderHint.Antialiasing)
painter.setPen(Qt.PenStyle.NoPen)
painter.setBrush(QColor("#226B53"))
painter.drawRoundedRect(64, 64, 896, 896, 210, 210)
painter.setPen(QPen(QColor("#F7FBF5"), 58))
painter.setBrush(Qt.BrushStyle.NoBrush)
painter.drawRoundedRect(275, 250, 282, 360, 130, 130)
painter.drawRoundedRect(460, 422, 282, 360, 130, 130)
painter.end()
image.save(str(assets / "icon.png"))
iconset = root / "build/Chainloom.iconset"
iconset.mkdir(parents=True, exist_ok=True)
for size in (16, 32, 128, 256, 512):
    for ratio in (1, 2):
        name = f"icon_{size}x{size}" + ("@2x" if ratio == 2 else "") + ".png"
        image.scaled(
            size * ratio,
            size * ratio,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).save(str(iconset / name))
subprocess.run(
    ["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(assets / "icon.icns")], check=True
)
print(assets / "icon.icns")
