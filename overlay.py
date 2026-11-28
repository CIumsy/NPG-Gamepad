# On-screen signal overlay. Runs as its own process (own Python interpreter and
# GIL), so drawing can never delay the signal engine or the gamepad output.
#
# Sweep display (like a patient monitor): a pen writes the newest samples from
# left to right and wraps around, overwriting the oldest data; a small gap in
# front of the pen marks "now". Only the columns that received new samples are
# computed and repainted each frame, so the cost is tiny at any overlay size.
#
# Quality: every pixel column shows the exact min and max of the samples it
# covers (no spike is hidden), joined to its neighbour so the trace is
# continuous, with anti-aliased ends, rendered at the screen's real pixel
# resolution (sharp on scaled displays).

import sys
import time
from collections import deque

import numpy as np
from PySide6.QtCore import Qt, QTimer, QRect, QRectF
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from config import MAX_CHANNELS
from win_perf import disable_power_throttling, set_normal_priority
from overlay_shm import (SignalRing, FILTER_NAMES, RING_LEN, H_COUNT, H_HEARTBEAT, H_STOP, H_WINDOW_S,
                         H_SCALE_MILLI, H_SIZE_PCT, H_CORNER, H_OPACITY_PCT, H_THICK_TENTHS)

SAMPLE_RATE = 500

FRAME_MS = 16                 # poll for new samples ~60x/s (they arrive every ~20 ms)
LANE_H_AT_35 = 64             # lane height at 35% width; scales with the size setting
LABEL_W = 70
PAD = 10
MARGIN = 24                   # from the screen edges
GAP_FRAC = 0.02               # blank gap in front of the pen, fraction of the plot width
HEARTBEAT_TIMEOUT_MS = 3000   # app gone -> close
NO_SIGNAL_S = 1.0

COLORS = {'EMG': QColor('#00ff66'), 'EEG': QColor('#5b9eff'),
          'EOG': QColor('#ffd55a'), 'ECG': QColor('#ff6b6b')}

# Fixed half-lane range per signal type at scale 1x (filtered ADC units).
# The global scale slider multiplies all of them, so every type stays readable.
BASE_RANGE = {'EMG': 1000.0, 'EEG': 200.0, 'EOG': 300.0, 'ECG': 500.0}


DC_R = 1.0 - 2 * np.pi * 0.5 / SAMPLE_RATE      # 0.5 Hz DC blocker (EEG only, see DisplaySignal)


class DisplaySignal:
    """The overlay's own copy of the stream, as displayed.

    EMG, EOG and ECG arrive band-pass filtered (no constant offset) and are shown
    exactly as filtered. EEG arrives low-pass only and carries the ADC's constant
    offset, so it goes through a continuous 0.5 Hz DC blocker, sample by sample:
    the offset is removed without ever shifting already-drawn parts of the trace.
    """

    def __init__(self, ring):
        self.ring = ring
        self.data = np.zeros((MAX_CHANNELS, RING_LEN), dtype=np.float32)
        self.count = 0
        self._x1 = np.zeros(MAX_CHANNELS)     # DC blocker state per channel
        self._y1 = np.zeros(MAX_CHANNELS)

    def update(self, count, eeg_chs):
        """Ingest samples up to count. eeg_chs: channels that need the DC blocker."""
        start = max(self.count, count - RING_LEN)
        if start >= count:
            return
        if start > self.count:                # fell behind by more than the ring: restart blockers
            self._x1[:] = 0.0
            self._y1[:] = 0.0
        new = self.ring.read_range(start, count)
        for ch in range(MAX_CHANNELS):          # a channel that stops being EEG restarts settled
            if ch not in eeg_chs:
                self._x1[ch] = self._y1[ch] = 0.0
        for ch in eeg_chs:
            x = new[ch].astype(np.float64)
            y = np.empty_like(x)
            x1, y1 = self._x1[ch], self._y1[ch]
            if x1 == 0.0 and y1 == 0.0 and len(x):
                x1 = x[0]                     # start settled on the first value
            for i in range(len(x)):
                y1 = x[i] - x1 + DC_R * y1
                x1 = x[i]
                y[i] = y1
            self._x1[ch], self._y1[ch] = x1, y1
            new[ch] = y
        a = start % RING_LEN
        n = count - start
        if a + n <= RING_LEN:
            self.data[:, a:a + n] = new
        else:
            k = RING_LEN - a
            self.data[:, a:] = new[:, :k]
            self.data[:, :n - k] = new[:, k:]
        self.count = count

    def read_range(self, start, end):
        n = end - start
        a = start % RING_LEN
        if a + n <= RING_LEN:
            return self.data[:, a:a + n].copy()
        k = RING_LEN - a
        return np.concatenate([self.data[:, a:], self.data[:, :n - k]], axis=1)


class Playout:
    """Smooth pen position (in samples).

    Samples arrive in bursts (a BLE packet every ~20 ms, and Windows sometimes
    bunches several packets together). Drawing them as they arrive makes the pen
    jump. Instead the pen moves at a steady 500 samples/s on every display frame,
    a small delay behind the newest sample; the delay adapts to the actual arrival
    jitter (largest recent gap), so the pen never has to wait for data.
    Display only: the gamepad output never waits for this.
    """

    GAIN = 0.02            # fraction of the position error corrected per frame
    MIN_DELAY = 0.040      # s
    MAX_DELAY = 0.250      # s
    MARGIN = 0.015         # s on top of the largest recent gap

    def __init__(self):
        self.pos = None
        self.t = None
        self.delay = self.MIN_DELAY
        self.arrivals = deque()

    def step(self, now, count, arrived):
        if arrived:
            self.arrivals.append(now)
            while self.arrivals and now - self.arrivals[0] > 2.0:
                self.arrivals.popleft()
        a = self.arrivals
        gap = max((y - x for x, y in zip(a, list(a)[1:])), default=0.02)
        target = min(self.MAX_DELAY, max(self.MIN_DELAY, gap + self.MARGIN))
        # Grow at once (avoid stalls), shrink slowly (no visible speed changes)
        self.delay = target if target > self.delay else self.delay + (target - self.delay) * 0.01

        goal = count - self.delay * SAMPLE_RATE
        if self.pos is None or abs(goal - self.pos) > SAMPLE_RATE:     # start / resync after a long break
            self.pos, self.t = goal, now
        else:
            self.pos += SAMPLE_RATE * (now - self.t)                   # steady speed
            self.pos += (goal - self.pos) * self.GAIN                  # gently stay on target
            self.t = now
        self.pos = min(self.pos, count)                                # never beyond received data
        return max(0, int(self.pos))


def color_lut(color):
    """alpha (0-255) -> premultiplied ARGB32 pixel of the given colour."""
    a = np.arange(256, dtype=np.uint32)
    return ((a << 24) | ((a * color.red() // 255) << 16)
            | ((a * color.green() // 255) << 8) | (a * color.blue() // 255)).astype(np.uint32)


def raster_columns(tops, bots, lane_h, luts, thickness):
    """Draw pixel columns for all lanes.

    tops/bots: (lanes, k) float pixel rows within a lane. Each column is filled from
    top to bot: rows fully inside are opaque, the two end rows get their exact
    fractional coverage (anti-aliased). Returns (lanes, lane_h, k) uint32 ARGB.
    """
    lanes, k = tops.shape
    top = tops - np.maximum(0.0, thickness - (bots - tops)) * 0.5     # minimum stroke thickness
    bot = top + np.maximum(bots - tops, thickness)
    np.clip(top, 0.0, lane_h - 1e-3, out=top)
    np.clip(bot, 0.0, lane_h - 1e-3, out=bot)
    t_i = top.astype(np.int32)
    b_i = bot.astype(np.int32)

    rows = np.arange(lane_h, dtype=np.int32)[None, :, None]
    a = ((rows > t_i[:, None, :]) & (rows < b_i[:, None, :])).astype(np.uint8)
    a *= 255
    li, ci = np.indices((lanes, k))
    same = t_i == b_i
    a[li, t_i, ci] = np.where(same, bot - top, (t_i + 1) - top) * 255 + 0.5
    d = ~same
    a[li[d], b_i[d], ci[d]] = (bot - b_i)[d] * 255 + 0.5

    out = np.empty((lanes, lane_h, k), dtype=np.uint32)
    for lane in range(lanes):
        out[lane] = luts[lane][a[lane]]
    return out


class Overlay(QWidget):
    def __init__(self, ring):
        super().__init__()
        self.ring = ring
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("NPG Signal Overlay")

        self._frame = (5, 1.0, 35, 3, 70, 1.2)     # resolved settings
        self._active, self._names = [], []
        self._layout_key = None
        self._lane_h = LANE_H_AT_35
        self._last_count = -1
        self._last_new = time.monotonic()
        self._stalled = False
        self._luts = {}
        self._static = None                       # cached background + labels
        self._static_key = None

        # Sweep canvas (device pixels): one row block per lane
        self._canvas = None
        self._canvas_img = None
        self._sweep_key = None
        self._drawn = 0                           # absolute sample count already drawn
        self._disp = DisplaySignal(ring)          # displayed values (EEG offset removed)
        self._playout = Playout()                 # smooth pen position

        self._font = QFont("Segoe UI", 9)
        self._font.setBold(True)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start(FRAME_MS)

    # ---- settings / layout ----

    def _settings(self):
        h = self.ring.header
        return (max(1, min(10, int(h[H_WINDOW_S]) or 5)),
                max(0.1, min(10.0, (int(h[H_SCALE_MILLI]) or 1000) / 1000)),
                max(20, min(80, int(h[H_SIZE_PCT]) or 35)),
                max(0, min(3, int(h[H_CORNER]))),
                max(0, min(100, int(h[H_OPACITY_PCT]))),
                max(1.0, min(3.0, (int(h[H_THICK_TENTHS]) or 12) / 10)))

    def _place(self, lanes, size_pct, corner):
        """Chosen corner of the primary screen, height fitted to the lanes."""
        key = (lanes, size_pct, corner)
        if key == self._layout_key:
            return
        self._layout_key = key
        area = QGuiApplication.primaryScreen().availableGeometry()
        w = int(area.width() * size_pct / 100)
        self._lane_h = int(LANE_H_AT_35 * size_pct / 35)
        h = PAD * 2 + self._lane_h * max(1, lanes)
        x = area.left() + MARGIN if corner in (0, 2) else area.right() - w - MARGIN
        y = area.top() + MARGIN if corner in (0, 1) else area.bottom() - h - MARGIN
        self.setGeometry(x, y, w, h)

    def _geometry(self):
        """Plot geometry in device pixels for the current layout."""
        dpr = self.devicePixelRatioF()
        lanes = max(1, len(self._active))
        cols = max(10, int(round((self.width() - PAD * 2 - LABEL_W) * dpr)))
        lane_px = max(8, int(round(self._lane_h * dpr)))
        return dpr, lanes, cols, lane_px

    # ---- per frame ----

    def _tick(self):
        hdr = self.ring.header
        now = time.monotonic()
        if hdr[H_STOP] or (int(now * 1000) - int(hdr[H_HEARTBEAT]) > HEARTBEAT_TIMEOUT_MS):
            QApplication.quit()
            return

        # Settings and geometry are resolved here, never inside paintEvent
        # (resizing a window while it paints is unsafe)
        frame = self._settings()
        meta = self.ring.meta
        active = [ch for ch in range(MAX_CHANNELS) if meta[0, ch]]
        names = [FILTER_NAMES.get(int(meta[1, ch]), '') for ch in active]
        layout_changed = (frame, active, names) != (self._frame, self._active, self._names)
        self._frame, self._active, self._names = frame, active, names
        self._place(max(1, len(active)), frame[2], frame[3])

        count = int(hdr[H_COUNT])
        arrived = count != self._last_count
        if arrived:
            self._last_count = count
            self._last_new = now
            self._disp.update(count, [ch for ch, nm in zip(active, names) if nm == 'EEG'])
        stalled = now - self._last_new > NO_SIGNAL_S
        if stalled != self._stalled:
            self._stalled = stalled
            layout_changed = True

        # Smooth pen: how far to draw this frame (steady speed, small adaptive delay)
        show = self._playout.step(time.perf_counter(), count, arrived)

        dpr, lanes, cols, lane_px = self._geometry()
        sweep_key = (frame[0], frame[1], frame[5], tuple(active), tuple(names), dpr, cols, lane_px)
        if sweep_key != self._sweep_key or layout_changed:
            self._sweep_key = sweep_key
            self._rebuild_canvas(show, lanes, cols, lane_px, dpr)
            self.update()                                         # full repaint (rare)
        elif show > self._drawn and active and not stalled:
            dirty = self._draw_new(show)                          # only new columns
            for c0, c1 in dirty:
                x0 = PAD + LABEL_W + int(c0 / dpr) - 1
                x1 = PAD + LABEL_W + int(np.ceil(c1 / dpr)) + 1
                self.update(QRect(x0, PAD, x1 - x0, self.height() - PAD * 2))

    def showEvent(self, e):
        self._tick()
        super().showEvent(e)

    # ---- sweep canvas ----

    def _rebuild_canvas(self, count, lanes, cols, lane_px, dpr):
        self._canvas = np.zeros((lanes * lane_px, cols), dtype=np.uint32)
        self._canvas_img = QImage(self._canvas.data, cols, lanes * lane_px, cols * 4,
                                  QImage.Format_ARGB32_Premultiplied)
        self._canvas_img.setDevicePixelRatio(dpr)
        window_n = self._frame[0] * SAMPLE_RATE
        # Columns map to positions within one sweep: column c covers sweep positions
        # [starts[c], starts[c+1])
        self._starts = (np.arange(cols + 1) * window_n + cols - 1) // cols
        self._window_n = window_n
        # Redraw the whole last window (current sweep + the older part ahead of the pen)
        self._drawn = max(0, count - window_n)
        if count and self._active and not self._stalled:
            self._draw_new(count)

    def _draw_new(self, count):
        """Draw columns that received samples in [self._drawn, count). Returns dirty column ranges."""
        n, cols = self._window_n, self._canvas.shape[1]
        start = max(self._drawn, count - n)                      # never more than one full sweep
        self._drawn = count

        # Columns touched by the new samples (in the sweep each sample lives in)
        s = np.arange(start, count)
        pos = s % n
        col_of = np.searchsorted(self._starts, pos, side='right') - 1
        touched = np.unique(col_of)

        cyc = count - 1 - ((count - 1) % n)                      # start of the current sweep
        pen_col = int(np.searchsorted(self._starts, (count - 1) % n, side='right') - 1)

        # Sample range covering every touched column (+1 earlier sample to join columns)
        spans = []
        for c in touched:
            # The sweep this column belongs to: current one if at/behind the pen, else previous
            c_cyc = cyc if c <= pen_col else cyc - n
            spans.append((c_cyc + self._starts[c], min(c_cyc + self._starts[c + 1], count)))
        base = max(0, count - n, min(a for a, _ in spans) - 1)
        data = self._disp.read_range(base, count)[self._active]

        lo_list, hi_list = [], []
        for a, b in spans:
            a_idx = max(a - 1, base) - base                      # include previous sample: joins columns
            b_idx = b - base
            if b_idx <= a_idx:
                lo_list.append(np.full(len(self._active), np.nan, dtype=np.float32))
                hi_list.append(lo_list[-1])
                continue
            seg = data[:, a_idx:b_idx]
            lo_list.append(seg.min(axis=1))
            hi_list.append(seg.max(axis=1))
        lo = np.stack(lo_list, axis=1)
        hi = np.stack(hi_list, axis=1)

        dpr, lanes, _, lane_px = self._geometry()
        window_s, scale, _, _, _, thickness = self._frame
        half = lane_px / 2 - 2 * dpr
        k = np.array([half * scale / BASE_RANGE.get(nm, 500.0) for nm in self._names],
                     dtype=np.float32)[:, None]
        mid = lane_px / 2
        valid = ~np.isnan(lo)
        lo = np.where(valid, lo, 0.0)
        hi = np.where(valid, hi, 0.0)
        cols_img = raster_columns(mid - hi * k, mid - lo * k, lane_px,
                                  [self._lut(COLORS.get(nm, QColor('#ffffff'))) for nm in self._names],
                                  thickness * dpr)
        cols_img[~np.broadcast_to(valid[:, None, :], cols_img.shape)] = 0

        canvas = self._canvas.reshape(lanes, lane_px, cols)
        canvas[:, :, touched] = cols_img

        # Gap in front of the pen
        gap = max(3, int(cols * GAP_FRAC))
        gap_cols = (pen_col + 1 + np.arange(gap)) % cols
        canvas[:, :, gap_cols] = 0

        dirty_cols = np.unique(np.concatenate([touched, gap_cols]))
        return _ranges(dirty_cols)

    # ---- painting ----

    def _static_layer(self, w, h, dpr, lane_h, opacity_pct):
        """Background, labels and lane separators: drawn once per layout change."""
        key = (w, h, dpr, lane_h, opacity_pct, tuple(self._active), tuple(self._names))
        if key == self._static_key:
            return self._static
        img = QImage(int(round(w * dpr)), int(round(h * dpr)), QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, int(40 * max(opacity_pct, 30) / 100)), 1))
        p.setBrush(QColor(10, 10, 10, int(255 * opacity_pct / 100)))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)
        p.setFont(self._font)
        for lane, (ch, name) in enumerate(zip(self._active, self._names)):
            y0 = PAD + lane * lane_h
            p.setPen(COLORS.get(name, QColor('#ffffff')))
            p.drawText(QRectF(PAD + 4, y0, LABEL_W - 8, lane_h), Qt.AlignVCenter | Qt.AlignLeft,
                       f"CH{ch + 1}\n{name}")
            if lane:
                p.setPen(QPen(QColor(255, 255, 255, 25), 1))
                p.drawLine(PAD + LABEL_W, int(y0), w - PAD, int(y0))
        p.end()
        self._static, self._static_key = img, key
        return img

    def paintEvent(self, e):
        opacity_pct = self._frame[4]
        lane_h = self._lane_h
        w, h = self.width(), self.height()
        if lane_h * max(1, len(self._active)) + PAD * 2 > h + 1:
            return      # geometry for a new lane count not applied yet; next frame draws
        dpr = self.devicePixelRatioF()

        p = QPainter(self)
        p.setClipRegion(e.region())                              # only the dirty columns
        p.setCompositionMode(QPainter.CompositionMode_Source)   # replace, don't blend old frame
        p.drawImage(0, 0, self._static_layer(w, h, dpr, lane_h, opacity_pct))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)

        if not self._active or self._stalled:
            p.setFont(self._font)
            p.setPen(QColor('#9a9a9a'))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter,
                       "No signal" if self._active or self._last_count > 0 else "Waiting for signal...")
        elif self._canvas_img is not None:
            p.drawImage(PAD + LABEL_W, PAD, self._canvas_img)
        p.end()

    def _lut(self, color):
        key = color.rgb()
        lut = self._luts.get(key)
        if lut is None:
            lut = self._luts[key] = color_lut(color)
        return lut


def _ranges(cols):
    """Sorted column indices -> list of contiguous [start, end) ranges."""
    if len(cols) == 0:
        return []
    breaks = np.nonzero(np.diff(cols) > 1)[0]
    starts = np.concatenate([[cols[0]], cols[breaks + 1]])
    ends = np.concatenate([cols[breaks], [cols[-1]]]) + 1
    return list(zip(starts.tolist(), ends.tolist()))


def main(shm_name):
    # Normal priority + no power throttling: a busy PC (browser, recorder, game)
    # must not starve the overlay's frames. Its CPU use is small either way.
    set_normal_priority()
    disable_power_throttling()
    app = QApplication(sys.argv[:1])
    try:
        ring = SignalRing(shm_name)
    except FileNotFoundError:
        print("Overlay: signal buffer not found")
        return 1
    win = Overlay(ring)
    win.show()
    code = app.exec()
    win._timer.stop()
    ring.close()
    return code
