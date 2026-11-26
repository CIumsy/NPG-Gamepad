"""
DualSenseViewer — Simple PS5-style controller drawn with QPainter.

Shows live DualShock mode input in the tester:
  Face buttons:  PlayStation colours when pressed
  Other buttons: white when pressed
  L2 / R2:       fill with the analog trigger value
  Sticks:        cap moves smoothly with the analog stick value

All drawing uses a fixed 640x400 canvas scaled to the widget, so it can
later be swapped for an SVG without changing the public API.
"""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath, QPolygonF, QTransform, QFont


class DualSenseViewer(QWidget):

    _CANVAS_W, _CANVAS_H = 640.0, 400.0

    # Palette (matches the app's black & white theme)
    _BG        = QColor("#0a0a0a")
    _BODY      = QColor("#1c1c1c")
    _OUTLINE   = QColor("#4a4a4a")
    _PART      = QColor("#2a2a2a")
    _PART_EDGE = QColor("#555555")
    _SYMBOL    = QColor("#8a8a8a")
    _ON        = QColor("#ffffff")
    _ON_TEXT   = QColor("#000000")
    _LABEL     = QColor("#9a9a9a")

    _FACE_COLORS = {
        "Triangle": QColor("#40e0a0"),
        "Circle":   QColor("#ff6b6b"),
        "Cross":    QColor("#5b9eff"),
        "Square":   QColor("#ff8ad8"),
    }

    # Layout (canvas coordinates)
    _L2_RECT = QRectF(118, 14, 116, 36)
    _R2_RECT = QRectF(406, 14, 116, 36)
    _L1_RECT = QRectF(108, 58, 136, 20)
    _R1_RECT = QRectF(396, 58, 136, 20)
    _TOUCHPAD = QRectF(225, 92, 190, 92)
    _SHARE = QRectF(192, 98, 14, 28)
    _OPTIONS = QRectF(434, 98, 14, 28)
    _DPAD_C = QPointF(165, 158)
    _FACE_C = QPointF(475, 158)
    _FACE_GAP = 36
    _FACE_R = 16
    _LS_C = QPointF(248, 240)
    _RS_C = QPointF(392, 240)
    _STICK_WELL_R = 34
    _STICK_CAP_R = 24
    _STICK_TRAVEL = 16
    _PS_C = QPointF(320, 222)

    _BUTTONS = [
        "Cross", "Circle", "Square", "Triangle",
        "Dpad Up", "Dpad Down", "Dpad Left", "Dpad Right",
        "L1", "R1", "L3", "R3", "Share", "Options", "PS", "Touchpad",
    ]
    _ANALOG = [
        "L2", "R2",
        "LS Up", "LS Down", "LS Left", "LS Right",
        "RS Up", "RS Down", "RS Left", "RS Right",
    ]

    # Fraction of the remaining distance the display moves each frame
    _SMOOTHING = 0.6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(540, 340)
        self._target = {k: 0.0 for k in self._BUTTONS + self._ANALOG}
        self._shown = dict(self._target)
        self._body_path = self._build_body_path()

        # 60 fps easing towards the latest values for smooth analog motion
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._step_animation)
        self._anim.start()

    # Public API

    def set_values(self, values: dict):
        """values: {key name: 0..1} for the current frame. Missing keys are 0."""
        for k in self._target:
            self._target[k] = float(values.get(k, 0.0))

    def update_button(self, name: str, value):
        if name in self._target:
            self._target[name] = float(value)

    def reset_all(self):
        for k in self._target:
            self._target[k] = 0.0
            self._shown[k] = 0.0
        self.update()

    # Animation

    def _step_animation(self):
        changed = False
        for k, tgt in self._target.items():
            cur = self._shown[k]
            if k in self._BUTTONS:
                new = tgt      # buttons switch instantly
            else:
                new = cur + (tgt - cur) * self._SMOOTHING
                if abs(tgt - new) < 0.002:
                    new = tgt
            if new != cur:
                self._shown[k] = new
                changed = True
        if changed:
            self.update()

    def _on(self, name):
        return self._shown.get(name, 0.0) > 0

    def _stick(self, prefix):
        """Stick offset in -1..1 (screen y grows downwards)."""
        s = self._shown
        x = s[f"{prefix} Right"] - s[f"{prefix} Left"]
        y = s[f"{prefix} Down"] - s[f"{prefix} Up"]
        return max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))

    # Geometry

    @staticmethod
    def _build_body_path():
        body = QPainterPath()
        body.addRoundedRect(QRectF(70, 84, 500, 196), 70, 70)

        def grip(cx, cy, angle):
            p = QPainterPath()
            p.addEllipse(QPointF(0, 0), 78, 122)
            t = QTransform()
            t.translate(cx, cy)
            t.rotate(angle)
            return t.map(p)

        body = body.united(grip(150, 250, 20))
        body = body.united(grip(490, 250, -20))
        return body.simplified()

    # Paint

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self._BG)

        # Fit the canvas into the widget, keeping the aspect ratio
        pad = 16
        sx = (self.width() - 2 * pad) / self._CANVAS_W
        sy = (self.height() - 2 * pad) / self._CANVAS_H
        scale = max(0.01, min(sx, sy))
        p.translate((self.width() - self._CANVAS_W * scale) / 2,
                    (self.height() - self._CANVAS_H * scale) / 2)
        p.scale(scale, scale)

        self._draw_triggers(p)
        self._draw_bumpers(p)

        p.setPen(QPen(self._OUTLINE, 2))
        p.setBrush(self._BODY)
        p.drawPath(self._body_path)

        self._draw_touchpad(p)
        self._draw_pill(p, self._SHARE, "Share")
        self._draw_pill(p, self._OPTIONS, "Options")
        self._draw_dpad(p)
        self._draw_face_buttons(p)
        self._draw_stick(p, self._LS_C, "LS", "L3")
        self._draw_stick(p, self._RS_C, "RS", "R3")
        self._draw_ps_button(p)

        p.end()

    def _label(self, p, rect, text, color):
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(color)
        p.drawText(rect, Qt.AlignCenter, text)

    def _draw_triggers(self, p):
        for name, rect in (("L2", self._L2_RECT), ("R2", self._R2_RECT)):
            val = self._shown[name]
            p.setPen(QPen(self._PART_EDGE, 1.5))
            p.setBrush(self._PART)
            p.drawRoundedRect(rect, 12, 12)

            text = f"{name}  {int(round(val * 100))}%" if val > 0 else name
            self._label(p, rect, text, self._LABEL)

            if val > 0:
                # Fill grows from the outer edge inwards, like pulling the trigger.
                # The label is redrawn dark inside the fill so it stays readable.
                fill_w = rect.width() * val
                if name == "L2":
                    fill = QRectF(rect.left(), rect.top(), fill_w, rect.height())
                else:
                    fill = QRectF(rect.right() - fill_w, rect.top(), fill_w, rect.height())
                clip = QPainterPath()
                clip.addRoundedRect(rect, 12, 12)
                fill_path = QPainterPath()
                fill_path.addRect(fill)
                p.save()
                p.setClipPath(clip.intersected(fill_path))
                p.setPen(Qt.NoPen)
                p.setBrush(self._ON)
                p.drawRect(fill)
                self._label(p, rect, text, self._ON_TEXT)
                p.restore()

    def _draw_bumpers(self, p):
        for name, rect in (("L1", self._L1_RECT), ("R1", self._R1_RECT)):
            on = self._on(name)
            p.setPen(QPen(self._PART_EDGE, 1.5))
            p.setBrush(self._ON if on else self._PART)
            p.drawRoundedRect(rect, 9, 9)
            self._label(p, rect, name, self._ON_TEXT if on else self._LABEL)

    def _draw_touchpad(self, p):
        on = self._on("Touchpad")
        p.setPen(QPen(self._PART_EDGE, 1.5))
        p.setBrush(self._ON if on else self._PART)
        p.drawRoundedRect(self._TOUCHPAD, 14, 14)

    def _draw_pill(self, p, rect, name):
        on = self._on(name)
        p.setPen(QPen(self._PART_EDGE, 1.5))
        p.setBrush(self._ON if on else self._PART)
        p.drawRoundedRect(rect, 7, 7)

    def _draw_dpad(self, p):
        c = self._DPAD_C
        # Each arm: an arrow-shaped key pointing away from the centre
        arms = {
            "Dpad Up":    (0, -1),
            "Dpad Down":  (0, 1),
            "Dpad Left":  (-1, 0),
            "Dpad Right": (1, 0),
        }
        near, far, half = 8, 40, 12
        for name, (dx, dy) in arms.items():
            # Build in "pointing up" space then rotate
            shape = QPolygonF([
                QPointF(0, -near),
                QPointF(half, -near - 10),
                QPointF(half, -far),
                QPointF(-half, -far),
                QPointF(-half, -near - 10),
            ])
            angle = {(0, -1): 0, (1, 0): 90, (0, 1): 180, (-1, 0): 270}[(dx, dy)]
            t = QTransform()
            t.translate(c.x(), c.y())
            t.rotate(angle)
            poly = t.map(shape)

            on = self._on(name)
            p.setPen(QPen(self._PART_EDGE, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(self._ON if on else self._PART)
            p.drawPolygon(poly)

            # Small triangle hint
            tri = QPolygonF([QPointF(0, -far + 8), QPointF(5, -far + 16), QPointF(-5, -far + 16)])
            p.setPen(Qt.NoPen)
            p.setBrush(self._ON_TEXT if on else self._SYMBOL)
            p.drawPolygon(t.map(tri))

    def _draw_face_buttons(self, p):
        c, g, r = self._FACE_C, self._FACE_GAP, self._FACE_R
        spots = {
            "Triangle": QPointF(c.x(), c.y() - g),
            "Circle":   QPointF(c.x() + g, c.y()),
            "Cross":    QPointF(c.x(), c.y() + g),
            "Square":   QPointF(c.x() - g, c.y()),
        }
        for name, pt in spots.items():
            on = self._on(name)
            color = self._FACE_COLORS[name]
            p.setPen(QPen(self._PART_EDGE, 1.5))
            p.setBrush(color if on else self._PART)
            p.drawEllipse(pt, r, r)

            sym = self._ON if on else color
            p.setPen(QPen(sym, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            s = r * 0.48
            if name == "Triangle":
                p.drawPolygon(QPolygonF([
                    QPointF(pt.x(), pt.y() - s),
                    QPointF(pt.x() + s * 0.95, pt.y() + s * 0.65),
                    QPointF(pt.x() - s * 0.95, pt.y() + s * 0.65),
                ]))
            elif name == "Circle":
                p.drawEllipse(pt, s, s)
            elif name == "Cross":
                p.drawLine(QPointF(pt.x() - s, pt.y() - s), QPointF(pt.x() + s, pt.y() + s))
                p.drawLine(QPointF(pt.x() - s, pt.y() + s), QPointF(pt.x() + s, pt.y() - s))
            else:
                p.drawRect(QRectF(pt.x() - s * 0.85, pt.y() - s * 0.85, s * 1.7, s * 1.7))

    def _draw_stick(self, p, centre, prefix, click_name):
        x, y = self._stick(prefix)
        active = abs(x) > 0.001 or abs(y) > 0.001
        clicked = self._on(click_name)

        # Well
        p.setPen(QPen(self._PART_EDGE, 1.5))
        p.setBrush(QColor("#121212"))
        p.drawEllipse(centre, self._STICK_WELL_R, self._STICK_WELL_R)

        # Direction line from centre to cap
        cap = QPointF(centre.x() + x * self._STICK_TRAVEL, centre.y() + y * self._STICK_TRAVEL)
        if active:
            p.setPen(QPen(self._ON, 2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(centre, cap)

        # Cap: white ring while moved, solid white while clicked (L3/R3)
        p.setPen(QPen(self._ON if (active or clicked) else self._PART_EDGE, 2))
        p.setBrush(self._ON if clicked else self._PART)
        p.drawEllipse(cap, self._STICK_CAP_R, self._STICK_CAP_R)
        p.setPen(QPen(self._ON_TEXT if clicked else self._PART_EDGE, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(cap, self._STICK_CAP_R * 0.62, self._STICK_CAP_R * 0.62)

    def _draw_ps_button(self, p):
        on = self._on("PS")
        p.setPen(QPen(self._PART_EDGE, 1.5))
        p.setBrush(self._ON if on else self._PART)
        p.drawEllipse(self._PS_C, 12, 12)
        f = QFont(self.font())
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(self._ON_TEXT if on else self._LABEL)
        p.drawText(QRectF(self._PS_C.x() - 12, self._PS_C.y() - 12, 24, 24), Qt.AlignCenter, "PS")
