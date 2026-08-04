"""Tray icons drawn with QPainter: a microphone on a colored state dot."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

STATE_COLORS = {
    "idle": "#9b59b6",       # purple
    "recording": "#e93d3d",  # red
    "busy": "#e9a13d",       # orange
    "disabled": "#777777",   # gray
}


def make_icon(state: str) -> QIcon:
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    p.setPen(Qt.NoPen)
    p.setBrush(QColor(STATE_COLORS.get(state, "#777777")))
    p.drawEllipse(2, 2, size - 4, size - 4)

    fg = QColor("#ffffff")
    # mic capsule
    p.setBrush(fg)
    p.drawRoundedRect(QRectF(26, 12, 12, 24), 6, 6)
    # pickup arc
    pen = QPen(fg, 4)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(20, 20, 24, 24), 180 * 16, 180 * 16)
    # stem and base
    p.drawLine(32, 44, 32, 50)
    p.drawLine(25, 51, 39, 51)

    if state == "disabled":
        p.setPen(QPen(QColor("#2a2a2a"), 6))
        p.drawLine(12, 52, 52, 12)

    p.end()
    return QIcon(pm)
