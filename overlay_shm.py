# Shared-memory ring buffer: filtered samples from the app to the overlay process.
#
# Single writer (the signal engine thread), single reader (the overlay process).
# The writer stores the samples first and publishes the new total count last, so
# the reader only ever reads complete samples. Every sample is kept (no decimation);
# the ring holds RING_LEN samples per channel (~16 s at 500 Hz), far more than the
# overlay's window, so the reader can't be overtaken while copying.

import time
from multiprocessing import shared_memory

import numpy as np

from config import MAX_CHANNELS

RING_LEN = 8192

# int64 header slots
H_COUNT = 0          # total samples written (publish last)
H_HEARTBEAT = 1      # app heartbeat, ms (overlay exits if it stops)
H_STOP = 2           # 1 = overlay should close
# Overlay settings (written by the app, read by the overlay every frame)
H_WINDOW_S = 3       # time window, seconds (1-10)
H_SCALE_MILLI = 4    # global scale x1000 (100 = 0.1x ... 10000 = 10x)
H_SIZE_PCT = 5       # overlay width, % of screen width
H_CORNER = 6         # 0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right
H_OPACITY_PCT = 7    # background opacity, %
H_THICK_TENTHS = 8   # line thickness, tenths of a px
H_SLOTS = 16

FILTER_CODES = {'emg': 1, 'eeg': 2, 'eog': 3, 'ecg': 4}
FILTER_NAMES = {v: k.upper() for k, v in FILTER_CODES.items()}

_HDR_BYTES = H_SLOTS * 8
_META_BYTES = 2 * MAX_CHANNELS          # active flag + filter code per channel
_META_PAD = 16                          # keep the float data 8-byte aligned
_DATA_BYTES = MAX_CHANNELS * RING_LEN * 4
SIZE = _HDR_BYTES + _META_PAD + _DATA_BYTES


class SignalRing:
    def __init__(self, name=None):
        """name=None creates a new block (app side); a name attaches (overlay side)."""
        if name is None:
            self.shm = shared_memory.SharedMemory(create=True, size=SIZE)
        else:
            self.shm = shared_memory.SharedMemory(name=name)
        buf = self.shm.buf
        self.header = np.ndarray((H_SLOTS,), dtype=np.int64, buffer=buf, offset=0)
        self.meta = np.ndarray((2, MAX_CHANNELS), dtype=np.uint8, buffer=buf, offset=_HDR_BYTES)
        self.data = np.ndarray((MAX_CHANNELS, RING_LEN), dtype=np.float32, buffer=buf,
                               offset=_HDR_BYTES + _META_PAD)
        if name is None:
            self.header[:] = 0
            self.meta[:] = 0
            self.data[:] = 0
        self._count = int(self.header[H_COUNT])

    @property
    def name(self):
        return self.shm.name

    # ---- writer (engine thread) ----

    def write(self, block, active, codes):
        """block: (n, MAX_CHANNELS) float array of filtered samples, in order.
        active / codes: per-channel flags and FILTER_CODES for the labels."""
        n = len(block)
        if n == 0:
            return
        self.meta[0, :] = active
        self.meta[1, :] = codes
        start = self._count % RING_LEN
        end = start + n
        cols = np.asarray(block, dtype=np.float32).T
        if end <= RING_LEN:
            self.data[:, start:end] = cols
        else:
            k = RING_LEN - start
            self.data[:, start:] = cols[:, :k]
            self.data[:, :end - RING_LEN] = cols[:, k:]
        self._count += n
        self.header[H_COUNT] = self._count       # publish after the data

    def heartbeat(self):
        self.header[H_HEARTBEAT] = int(time.monotonic() * 1000)

    def set_settings(self, s):
        """s: OverlaySettings-like object (window_s, scale, size_pct, corner, opacity_pct, thickness)."""
        self.header[H_WINDOW_S] = int(s.window_s)
        self.header[H_SCALE_MILLI] = int(round(s.scale * 1000))
        self.header[H_SIZE_PCT] = int(s.size_pct)
        self.header[H_CORNER] = int(s.corner)
        self.header[H_OPACITY_PCT] = int(s.opacity_pct)
        self.header[H_THICK_TENTHS] = int(round(s.thickness * 10))

    def request_stop(self):
        self.header[H_STOP] = 1

    # ---- reader (overlay process) ----

    def read(self, n):
        """Latest n samples per channel -> (count, (MAX_CHANNELS, n) array).
        Before n samples exist, the missing start is zeros."""
        count = int(self.header[H_COUNT])
        n = min(n, RING_LEN)
        out = np.zeros((MAX_CHANNELS, n), dtype=np.float32)
        avail = min(n, count)
        if avail:
            end = count % RING_LEN
            start = end - avail
            if start >= 0:
                out[:, n - avail:] = self.data[:, start:end]
            else:
                k = -start
                out[:, n - avail:n - avail + k] = self.data[:, RING_LEN - k:]
                out[:, n - avail + k:] = self.data[:, :end]
        return count, out

    def read_range(self, start, end):
        """Samples with absolute indices [start, end) -> (MAX_CHANNELS, end-start).
        Indices must be within the last RING_LEN samples."""
        n = end - start
        out = np.empty((MAX_CHANNELS, n), dtype=np.float32)
        a = start % RING_LEN
        if a + n <= RING_LEN:
            out[:] = self.data[:, a:a + n]
        else:
            k = RING_LEN - a
            out[:, :k] = self.data[:, a:]
            out[:, k:] = self.data[:, :n - k]
        return out

    def close(self):
        # Drop numpy views first, or the buffer can't be released
        self.header = self.meta = self.data = None
        try:
            self.shm.close()
        except Exception:
            pass

    def unlink(self):
        try:
            self.shm.unlink()
        except Exception:
            pass
