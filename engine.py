# Signal engine: filtering, detection, IMU and gamepad output on a dedicated thread.
#
# BLE packets are handed straight from the BLE thread to this thread (with their
# arrival time), so UI work (repaints, clicks, dialogs) can't hold them up.
# The UI never touches engine state directly: it sends an immutable EngineConfig
# and commands through the same queue, so changes apply between packets, in order.
#
# Stale-data guard: every sample always goes through the filters (keeps them
# continuous), but a packet that waited longer than STALE_S inside the app
# can't trigger detection or gamepad output — only fresh data drives the controller.

import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

from config import MAX_CHANNELS, EMG_SCALE, BLINK_SCALE, EYE_SCALE, JAW_SCALE, ECG_SCALE, clamp100
from channel_processor import ChannelProcessor
from Algorithms.BlinkDetector import BlinkDetector
from Algorithms.JawClenchDetector import JawClenchDetector
from Algorithms.IMUProcessor import IMUProcessor
from gamepad import MODE_DS4, DS4_ANALOG_KEYS

STALE_S = 0.050          # packets older than this (time spent inside the app) don't drive outputs
IDLE_EVAL_S = 0.050      # re-evaluate if no packet for this long (releases timed presses, etc.)
FLASH_S = 0.500          # press length for double/triple blink and double jaw clench

# Fixed signal rows: (bar name, combo name) in mapping order
FIXED_ROWS = [
    ('pbFocus', 'cmbFocus'), ('pbBlink', 'cmbBlink'), ('pbLeftEye', 'cmbLeftEye'),
    ('pbRightEye', 'cmbRightEye'), ('pbJaw', 'cmbJaw'), ('pbECG', 'cmbECG'),
    ('pbEMG1', 'cmbEMG1'), ('pbEMG2', 'cmbEMG2'), ('pbEMG3', 'cmbEMG3'),
    ('pbEMG4', 'cmbEMG4'), ('pbEMG5', 'cmbEMG5'), ('pbEMG6', 'cmbEMG6'),
]
EMG_BARS = ['pbEMG1', 'pbEMG2', 'pbEMG3', 'pbEMG4', 'pbEMG5', 'pbEMG6']


@dataclass(frozen=True)
class EngineConfig:
    """Snapshot of the UI settings the engine needs. Replaced as a whole on change."""
    connected: bool = False
    num_channels: int = 0
    active_chs: tuple = ()                 # ticked channel indices
    mode: str = 'xbox'
    rows: tuple = ()                       # (bar, key, visible, threshold) for FIXED_ROWS
    combo_rows: tuple = ()                 # (ch_a, ch_b, key, visible, threshold)
    double_blink: tuple = (False, 'None')  # (enabled, key)
    triple_blink: tuple = (False, 'None')
    double_jaw: tuple = (False, 'None')
    imu_prefix: str = None                 # 'LS' / 'RS' / None
    blink_threshold: int = 0               # pbBlink threshold (drives the blink detector)
    jaw_threshold: int = 0                 # pbJaw threshold (drives the jaw detector)


def _raise_thread_priority():
    """Ask Windows to schedule the engine thread ahead of normal threads, so other
    programs can't preempt signal processing. No-op elsewhere."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentThread.restype = wintypes.HANDLE
        k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        k32.SetThreadPriority.restype = wintypes.BOOL
        THREAD_PRIORITY_HIGHEST = 2
        if not k32.SetThreadPriority(k32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST):
            print("Note: could not raise signal engine thread priority")
    except Exception as e:
        print(f"Note: could not raise signal engine thread priority: {e}")


def analog_value(value, thresh):
    """Bar value -> 0..1 for an analog control. Range = threshold +/- 50%."""
    if thresh <= 0:
        return 0.0
    low = thresh * 0.5
    high = min(100.0, thresh * 1.5)
    return max(0.0, min(1.0, (value - low) / (high - low)))


class SignalEngine:
    def __init__(self):
        self.processors = [ChannelProcessor() for _ in range(MAX_CHANNELS)]
        self.blink_detector = BlinkDetector()
        self.jaw_detector = JawClenchDetector()
        self.imu = IMUProcessor()
        self.cfg = EngineConfig()

        # Outputs published for the UI (each replaced atomically, read-only for the UI)
        self.bar_values = {name: 0 for name, _ in FIXED_ROWS}
        self.combo_values = {}                 # (ch_a, ch_b) -> 0..100
        self.key_values = {}                   # last values sent to the gamepad
        self.lost_events = deque()             # (lost, total) for the UI to report
        self.lost_total = 0
        self.stale_skipped = 0                 # packets that only went through the filters

        self._flash = {}                       # key -> expiry time (perf_counter)
        self._signal_keys = {}
        self._last_counter = None
        self._last_eval = 0.0

        self._pad = None
        self._pad_lock = threading.Lock()

        self._q = queue.SimpleQueue()
        self._thread = None
        self._running = False

    # ---- thread control ----

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, name="SignalEngine", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=2)

    # ---- called from other threads ----

    def on_data(self, samples, num_channels, t_arrival):
        """BLE thread: a data packet arrived."""
        self._q.put(('data', samples, num_channels, t_arrival))

    def on_imu(self, samples, t_arrival):
        """BLE thread: an IMU packet arrived."""
        self._q.put(('imu', samples, t_arrival))

    def set_config(self, cfg):
        """UI thread: new settings snapshot."""
        self._q.put(('cfg', cfg))

    def call(self, fn, *args):
        """UI thread: run fn(*args) on the engine thread, between packets."""
        self._q.put(('call', fn, args))

    def set_pad(self, pad):
        """UI thread: hand over a new virtual controller (or None)."""
        with self._pad_lock:
            self._pad = pad

    def take_pad(self):
        """UI thread: remove the virtual controller; engine stops using it at once."""
        with self._pad_lock:
            pad, self._pad = self._pad, None
        return pad

    # ---- engine thread ----

    def _run(self):
        _raise_thread_priority()
        while self._running:
            item = self._get(self._timeout())
            if item is None:
                if not self._running:
                    break
                self.idle(time.perf_counter())
            else:
                self.handle(item)

    def _get(self, timeout):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _timeout(self):
        now = time.perf_counter()
        t = IDLE_EVAL_S - (now - self._last_eval)
        if self._flash:
            t = min(t, min(self._flash.values()) - now)
        return max(0.001, t)

    def handle(self, item):
        kind = item[0]
        if kind == 'data':
            self._handle_data(item[1], item[2], item[3])
        elif kind == 'imu':
            self._handle_imu(item[1], item[2])
        elif kind == 'cfg':
            self.cfg = item[1]
        elif kind == 'call':
            item[1](*item[2])

    def idle(self, now):
        """No packet for a while: keep outputs current (timed presses, timeouts)."""
        if not self.cfg.connected:
            return
        if now - self._last_eval >= IDLE_EVAL_S - 1e-4:
            self._evaluate(now)
        elif self._flash and min(self._flash.values()) <= now:
            # A timed press ended between packets: release it right away
            self._map_keys(self.bar_values, self.combo_values, now)

    def _handle_data(self, samples, num_channels, t_arrival):
        cfg = self.cfg
        if not cfg.connected:
            return
        self._check_counters(samples)

        limit = min(num_channels, MAX_CHANNELS)
        active = [(ch, self.processors[ch]) for ch in cfg.active_chs if ch < limit]
        for sample in samples:
            vals = sample['channels']
            for ch, proc in active:
                proc.process(vals[ch])

        if time.perf_counter() - t_arrival > STALE_S:
            self.stale_skipped += 1          # filters stay continuous, but no output from old data
            return
        self._evaluate(t_arrival)

    def _handle_imu(self, samples, t_arrival):
        self.imu.process(samples)            # always: orientation needs every sample
        if (self.cfg.connected and self._imu_keys()
                and time.perf_counter() - t_arrival <= STALE_S):
            self._send()

    def _check_counters(self, samples):
        if not samples:
            return
        first = samples[0]['counter']
        if self._last_counter is not None:
            expected = (self._last_counter + 1) & 0xFF
            if first != expected:
                lost = (first - expected) & 0xFF
                self.lost_total += lost
                self.lost_events.append((lost, self.lost_total))
        self._last_counter = samples[-1]['counter']

    def reset_stream(self):
        """New connection: forget counters, presses and outputs."""
        self._last_counter = None
        self.lost_total = 0
        self.stale_skipped = 0
        self.clear_outputs()

    def clear_outputs(self):
        """Mode switch / disconnect: drop timed presses and mappings, zero the bars."""
        self._flash.clear()
        self._signal_keys = {}
        self.bar_values = {name: 0 for name, _ in FIXED_ROWS}
        self.combo_values = {}
        self._send()

    # ---- evaluation (ported 1:1 from the previous main-thread code) ----

    def _evaluate(self, now):
        self._last_eval = time.perf_counter()
        cfg = self.cfg
        values = dict(self.bar_values)
        active = [ch for ch in cfg.active_chs if ch < cfg.num_channels]

        focus_set = blink_set = jaw_set = ecg_set = False
        left_eye_set = right_eye_set = False
        emg_env = {}

        jaw_owner = None
        for ch in active:
            if self.processors[ch].filter_type in ('eeg', 'eog'):
                jaw_owner = ch
                break

        for ch in active:
            p = self.processors[ch]
            if p.filter_type == 'eeg':
                if not focus_set:
                    values['pbFocus'] = clamp100(p.val_beta_pct, 100.0); focus_set = True
                if not blink_set:
                    values['pbBlink'] = clamp100(p.val_blink_envelope, BLINK_SCALE); blink_set = True
                if ch == jaw_owner and not jaw_set:
                    values['pbJaw'] = clamp100(p.val_jaw_envelope, JAW_SCALE); jaw_set = True
            elif p.filter_type == 'eog':
                if not left_eye_set:
                    values['pbLeftEye'] = clamp100(max(0.0, p.val_eye_deviation), EYE_SCALE); left_eye_set = True
                if not right_eye_set:
                    values['pbRightEye'] = clamp100(max(0.0, -p.val_eye_deviation), EYE_SCALE); right_eye_set = True
                if ch == jaw_owner and not jaw_set:
                    values['pbJaw'] = clamp100(p.val_jaw_envelope, JAW_SCALE); jaw_set = True
            elif p.filter_type == 'emg':
                values[EMG_BARS[ch]] = clamp100(p.val_emg_envelope, EMG_SCALE)
                emg_env[ch + 1] = p.val_emg_envelope
            elif p.filter_type == 'ecg' and not ecg_set:
                values['pbECG'] = clamp100(p.val_ecg, ECG_SCALE); ecg_set = True

        if not focus_set:     values['pbFocus'] = 0
        if not blink_set:     values['pbBlink'] = 0
        if not left_eye_set:  values['pbLeftEye'] = 0
        if not right_eye_set: values['pbRightEye'] = 0
        if not jaw_set:       values['pbJaw'] = 0
        if not ecg_set:       values['pbECG'] = 0
        for i in range(6):
            if (i + 1) not in emg_env:
                values[EMG_BARS[i]] = 0

        combos = dict(self.combo_values)
        for ch_a, ch_b, _, visible, _ in cfg.combo_rows:
            if visible:
                combos[(ch_a, ch_b)] = clamp100(emg_env.get(ch_a, 0.0) + emg_env.get(ch_b, 0.0), EMG_SCALE * 2)

        self.bar_values = values
        self.combo_values = combos

        self._detect(active, now)
        self._map_keys(values, combos, now)

    def _detect(self, active, now):
        cfg = self.cfg
        now_ms = int(now * 1000)
        self.blink_detector.threshold = (cfg.blink_threshold / 100.0) * BLINK_SCALE
        self.jaw_detector.threshold = (cfg.jaw_threshold / 100.0) * JAW_SCALE

        blink_sample = jaw_sample = None
        for ch in active:
            p = self.processors[ch]
            if p.filter_type == 'eeg' and blink_sample is None:
                blink_sample = p.val_blink_envelope
                if jaw_sample is None:
                    jaw_sample = p.val_jaw_envelope
            elif p.filter_type == 'eog' and jaw_sample is None:
                jaw_sample = p.val_jaw_envelope

        if blink_sample is not None:
            event = self.blink_detector.process(blink_sample, now_ms)
            if event == 'double' and cfg.double_blink[0]:
                self._flash_key(cfg.double_blink[1], now)
            elif event == 'triple' and cfg.triple_blink[0]:
                self._flash_key(cfg.triple_blink[1], now)
        if jaw_sample is not None:
            event = self.jaw_detector.process(jaw_sample, now_ms)
            if event == 'double' and cfg.double_jaw[0]:
                self._flash_key(cfg.double_jaw[1], now)

    def _flash_key(self, key, now):
        if key == 'None':
            return
        self._flash[key] = now + FLASH_S
        print(f"Triggered detection for '{key}'")

    def _expire_flash(self, now):
        for k in [k for k, t in self._flash.items() if t <= now]:
            del self._flash[k]

    def _map_keys(self, values, combos, now):
        cfg = self.cfg
        ds4 = cfg.mode == MODE_DS4
        keys = {}
        rows = [(values[bar], key, vis, thr) for bar, key, vis, thr in cfg.rows]
        rows += [(combos.get((a, b), 0), key, vis, thr) for a, b, key, vis, thr in cfg.combo_rows]
        for value, key, visible, thresh in rows:
            if not visible or key == 'None':
                continue
            if ds4 and key in DS4_ANALOG_KEYS:
                val = analog_value(value, thresh)
            else:
                val = 1.0 if (thresh > 0 and value >= thresh) else 0.0
            keys[key] = max(keys.get(key, 0.0), val)

        self._expire_flash(now)
        for key in self._flash:
            keys[key] = 1.0
        self._signal_keys = keys
        self._send()

    def _imu_keys(self):
        cfg = self.cfg
        if cfg.mode != MODE_DS4 or cfg.imu_prefix is None or not self.imu.calibrated:
            return {}
        x, y = self.imu.stick
        p = cfg.imu_prefix
        return {f"{p} Right": max(0.0, x), f"{p} Left": max(0.0, -x),
                f"{p} Up": max(0.0, y), f"{p} Down": max(0.0, -y)}

    def _send(self):
        keys = dict(self._signal_keys)
        for k, v in self._imu_keys().items():
            keys[k] = max(keys.get(k, 0.0), v)
        with self._pad_lock:
            if self._pad is not None:
                self._pad.apply(keys)
        self.key_values = keys
