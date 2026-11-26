"""
IMU calibration dialog: guides the user through the tilt poses and shows a
live analog stick dial. After calibration the dial shows the real stick output.
"""

import math

from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QPolygonF

from Algorithms.IMUProcessor import CAL_ROUNDS, MIN_TILT_DEG


class StickDial(QWidget):
    """Analog stick circle with direction arrows and a hold-progress ring."""

    _DIRS = {"LEFT": (-1, 0), "RIGHT": (1, 0), "UP": (0, -1), "DOWN": (0, 1)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)
        self.dot = (0.0, 0.0)       # screen units -1..1 (y down)
        self.target = None          # highlighted direction
        self.captured = {}          # direction -> captures this calibration
        self.progress = 0.0         # hold progress 0..1
        self.live = False           # True once calibrated (dot = stick output)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#0a0a0a"))

        size = min(self.width(), self.height()) - 24
        c = QPointF(self.width() / 2, self.height() / 2)
        R = size / 2 - 26           # stick travel radius

        # Hold progress ring
        ring = QRectF(c.x() - R - 14, c.y() - R - 14, 2 * R + 28, 2 * R + 28)
        p.setPen(QPen(QColor("#1e1e1e"), 6))
        p.drawEllipse(ring)
        if self.progress > 0:
            p.setPen(QPen(QColor("#00cc66"), 6, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(ring, 90 * 16, int(-self.progress * 360 * 16))

        # Well + crosshair
        p.setPen(QPen(QColor("#3a3a3a"), 2))
        p.setBrush(QColor("#141414"))
        p.drawEllipse(c, R, R)
        p.setPen(QPen(QColor("#262626"), 1))
        p.drawLine(QPointF(c.x() - R, c.y()), QPointF(c.x() + R, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - R), QPointF(c.x(), c.y() + R))

        # Direction arrows: white = target, green = captured, grey = pending
        for name, (dx, dy) in self._DIRS.items():
            tip = QPointF(c.x() + dx * (R - 6), c.y() + dy * (R - 6))
            base = QPointF(c.x() + dx * (R - 26), c.y() + dy * (R - 26))
            perp = QPointF(-dy * 10, dx * 10)
            tri = QPolygonF([tip, base + perp, base - perp])
            if name == self.target:
                color = QColor("#ffffff")
            elif self.captured.get(name, 0) > 0:
                color = QColor("#00cc66")
            else:
                color = QColor("#333333")
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawPolygon(tri)

        # Stick dot
        x, y = self.dot
        d = QPointF(c.x() + x * R, c.y() + y * R)
        if self.live and (abs(x) > 0.001 or abs(y) > 0.001):
            p.setPen(QPen(QColor("#ffffff"), 2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(c, d)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.setBrush(QColor("#ffffff") if self.live else QColor("#2a2a2a"))
        p.drawEllipse(d, 16, 16)
        p.end()


class IMUCalibrationDialog:
    def __init__(self, imu, parent=None):
        self.imu = imu
        self.calibrator = None

        self.dialog = QDialog(parent)
        self.dialog.setWindowTitle("IMU CALIBRATION")
        self.dialog.resize(520, 600)
        self.dialog.setStyleSheet("""
            QDialog { background-color: #0a0a0a; }
            QLabel { color: #e8e8e8; background: transparent; }
            QPushButton {
                background-color: #1a1a1a; border: 2px solid #2a2a2a;
                border-radius: 10px; color: #ffffff; font-size: 14px;
                font-weight: 900; letter-spacing: 2px; padding: 10px 18px;
            }
            QPushButton:hover { border: 2px solid #00ff66; color: #00ff66; }
        """)

        layout = QVBoxLayout(self.dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self.lbl_main = QLabel()
        self.lbl_main.setAlignment(Qt.AlignCenter)
        self.lbl_main.setWordWrap(True)
        self.lbl_main.setStyleSheet("font-size: 18px; font-weight: 900;")
        layout.addWidget(self.lbl_main)

        self.lbl_sub = QLabel()
        self.lbl_sub.setAlignment(Qt.AlignCenter)
        self.lbl_sub.setWordWrap(True)
        self.lbl_sub.setStyleSheet("font-size: 12px; color: #9a9a9a;")
        layout.addWidget(self.lbl_sub)

        self.dial = StickDial()
        layout.addWidget(self.dial, 1)

        row = QHBoxLayout()
        self.btn_recal = QPushButton("RECALIBRATE")
        self.btn_recal.setFixedHeight(44)
        self.btn_recal.clicked.connect(self.start)
        btn_close = QPushButton("CLOSE")
        btn_close.setFixedHeight(44)
        btn_close.clicked.connect(self.dialog.accept)
        row.addWidget(self.btn_recal)
        row.addWidget(btn_close)
        layout.addLayout(row)

        self.dialog.finished.connect(self._on_closed)

        self._timer = QTimer(self.dialog)
        self._timer.setInterval(16)     # ~60 fps
        self._timer.timeout.connect(self._refresh)

    def show(self):
        self.start()
        self.dialog.show()
        self.dialog.raise_()

    def start(self):
        """Begin (or restart) calibration."""
        self.calibrator = self.imu.start_calibration()
        self._timer.start()
        self._refresh()

    def close(self):
        self.dialog.reject()

    def _on_closed(self, _result):
        self._timer.stop()
        if self.imu.calibrator is not None:
            self.imu.cancel_calibration()   # keeps the previous calibration, if any

    def _refresh(self):
        cal = self.calibrator
        dial = self.dial

        if not self.imu.receiving:
            self.lbl_main.setText("Waiting for IMU data...")
            self.lbl_sub.setText("Check that the MPU6050 is connected (NPG Lite LED 2 should be green).")
            dial.target, dial.progress, dial.live, dial.dot = None, 0.0, False, (0.0, 0.0)
            dial.update()
            return

        self.lbl_main.setText(cal.instruction())
        dial.captured = cal.captured_counts()

        if cal.done:
            self.lbl_sub.setText("The stick follows your tilt. Press RECALIBRATE to redo.")
            x, y = self.imu.stick
            dial.target, dial.progress, dial.live = None, 0.0, True
            dial.dot = (x, -y)
        elif cal.error:
            self.lbl_sub.setText("")
            dial.target, dial.progress, dial.live, dial.dot = None, 0.0, False, (0.0, 0.0)
        else:
            if cal.step == "NEUTRAL":
                self.lbl_sub.setText("Keep the IMU where it will rest while playing.")
            elif cal.phase == "return":
                self.lbl_sub.setText(f"Tilt from centre: {cal.tilt_deg:.0f}°")
            else:
                self.lbl_sub.setText(
                    f"Tilt {cal.tilt_deg:.0f}° (at least {MIN_TILT_DEG:.0f}°). "
                    f"Each direction is recorded {CAL_ROUNDS} times.")
            dial.live = False
            dial.progress = cal.progress
            hold_dir = cal.step if (cal.step != "NEUTRAL" and cal.phase == "hold") else None
            dial.target = hold_dir
            if hold_dir:
                dx, dy = StickDial._DIRS[hold_dir]
                k = min(1.0, cal.tilt_deg / (MIN_TILT_DEG * 2))
                dial.dot = (dx * k, dy * k)
            else:
                dial.dot = (0.0, 0.0)
        dial.update()
