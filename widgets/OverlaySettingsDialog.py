"""
Overlay settings: model (saved between launches) and the popup dialog.
Every change is applied to the running overlay immediately.
"""

import math
from dataclasses import dataclass, asdict

from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                               QSlider, QComboBox, QPushButton)

CORNERS = ["Top left", "Top right", "Bottom left", "Bottom right"]


@dataclass
class OverlaySettings:
    window_s: int = 5            # 1-10 s
    scale: float = 1.0           # 0.1x-10x, on top of each signal type's base range
    size_pct: int = 35           # width, % of screen width
    corner: int = 3              # index into CORNERS
    opacity_pct: int = 70        # background opacity
    thickness: float = 1.2       # trace width, px

    @staticmethod
    def _store():
        return QSettings(QSettings.IniFormat, QSettings.UserScope, "UpsideDownLabs", "NPG-Lite-SNES")

    @classmethod
    def load(cls):
        s, d = cls._store(), cls()
        def get(key, typ, lo, hi):
            try:
                return max(lo, min(hi, typ(s.value(f"overlay/{key}", getattr(d, key)))))
            except (TypeError, ValueError):
                return getattr(d, key)
        return cls(window_s=get('window_s', int, 1, 10), scale=get('scale', float, 0.1, 10.0),
                   size_pct=get('size_pct', int, 20, 80), corner=get('corner', int, 0, 3),
                   opacity_pct=get('opacity_pct', int, 0, 100), thickness=get('thickness', float, 1.0, 3.0))

    def save(self):
        s = self._store()
        for k, v in asdict(self).items():
            s.setValue(f"overlay/{k}", v)
        s.sync()


class OverlaySettingsDialog:
    # Scale slider: 0..200 -> 0.1x..10x (logarithmic, 100 = 1x)
    @staticmethod
    def _scale_from_slider(v):
        return 10 ** ((v - 100) / 100)

    @staticmethod
    def _slider_from_scale(s):
        return int(round(100 + 100 * math.log10(s)))

    def __init__(self, settings, on_change, parent=None):
        """on_change(settings) is called after every edit."""
        self.settings = settings
        self._on_change = on_change

        self.dialog = QDialog(parent)
        self.dialog.setWindowTitle("OVERLAY SETTINGS")
        self.dialog.setMinimumWidth(460)
        self.dialog.setStyleSheet("""
            QDialog { background-color: #0a0a0a; }
            QLabel { color: #e8e8e8; background: transparent; font-size: 13px; font-weight: 700; }
            QLabel#value { color: #9a9a9a; font-weight: 600; min-width: 64px; }
            QSlider::groove:horizontal { height: 6px; background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #ffffff; border-radius: 3px; }
            QSlider::handle:horizontal { background: #ffffff; width: 14px; margin: -5px 0; border-radius: 7px; }
            QComboBox { background: #141414; border: 1px solid #3a3a3a; border-radius: 6px; color: #e8e8e8; padding: 4px 8px; }
            QPushButton {
                background-color: #1a1a1a; border: 2px solid #2a2a2a; border-radius: 10px;
                color: #ffffff; font-size: 13px; font-weight: 900; letter-spacing: 2px; padding: 8px 16px;
            }
            QPushButton:hover { border: 2px solid #00ff66; color: #00ff66; }
        """)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        self._rows = 0

        def slider(label, lo, hi, value, fmt):
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(value)
            val = QLabel(fmt(value))
            val.setObjectName("value")
            s.valueChanged.connect(lambda v: (val.setText(fmt(v)), self._changed()))
            grid.addWidget(QLabel(label), self._rows, 0)
            grid.addWidget(s, self._rows, 1)
            grid.addWidget(val, self._rows, 2)
            self._rows += 1
            return s

        st = settings
        self.sl_window = slider("Time window", 1, 10, st.window_s, lambda v: f"{v} s")
        self.sl_scale = slider("Scale", 0, 200, self._slider_from_scale(st.scale),
                               lambda v: f"{self._scale_from_slider(v):.2f}x")
        self.sl_size = slider("Size", 20, 80, st.size_pct, lambda v: f"{v}%")
        self.sl_opacity = slider("Background", 0, 100, st.opacity_pct, lambda v: f"{v}%")
        self.sl_thick = slider("Line thickness", 10, 30, int(round(st.thickness * 10)),
                               lambda v: f"{v / 10:.1f} px")

        self.cmb_corner = QComboBox()
        self.cmb_corner.addItems(CORNERS)
        self.cmb_corner.setCurrentIndex(st.corner)
        self.cmb_corner.currentIndexChanged.connect(self._changed)
        grid.addWidget(QLabel("Position"), self._rows, 0)
        grid.addWidget(self.cmb_corner, self._rows, 1, 1, 2)

        btns = QHBoxLayout()
        reset = QPushButton("RESET")
        reset.clicked.connect(self._reset)
        close = QPushButton("CLOSE")
        close.clicked.connect(self.dialog.accept)
        btns.addWidget(reset)
        btns.addStretch(1)
        btns.addWidget(close)

        layout = QVBoxLayout(self.dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(18)
        layout.addLayout(grid)
        layout.addLayout(btns)

    def show(self):
        self.dialog.show()
        self.dialog.raise_()

    def _changed(self, *_):
        st = self.settings
        st.window_s = self.sl_window.value()
        st.scale = self._scale_from_slider(self.sl_scale.value())
        st.size_pct = self.sl_size.value()
        st.opacity_pct = self.sl_opacity.value()
        st.thickness = self.sl_thick.value() / 10
        st.corner = self.cmb_corner.currentIndex()
        self._on_change(st)

    def _reset(self):
        d = OverlaySettings()
        for w, v in [(self.sl_window, d.window_s), (self.sl_scale, self._slider_from_scale(d.scale)),
                     (self.sl_size, d.size_pct), (self.sl_opacity, d.opacity_pct),
                     (self.sl_thick, int(round(d.thickness * 10)))]:
            w.blockSignals(True); w.setValue(v); w.blockSignals(False)
        self.cmb_corner.blockSignals(True); self.cmb_corner.setCurrentIndex(d.corner); self.cmb_corner.blockSignals(False)
        # Refresh value labels
        for w in (self.sl_window, self.sl_scale, self.sl_size, self.sl_opacity, self.sl_thick):
            w.valueChanged.emit(w.value())
