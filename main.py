"""
NPG Gamepad Emulator — Main Application
========================================
Loads NPG-Controller.ui, connects to NPG Lite via BLE,
processes signals through configurable filter chains,
and displays live signal inputs on progress bars.

Usage:  python main.py
"""

import sys
import os
import asyncio
import threading

from PySide6.QtWidgets import (
    QApplication, QInputDialog, QMessageBox, QButtonGroup, QDialog
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import QObject, Signal, QFile, QTimer

from ble_connection import NPGConnection, NPGDevice
from widgets.ThresholdBar import ThresholdBar
from widgets.ControllerViewer import ControllerViewer

try:
    import vgamepad as vg
    HAS_VGAMEPAD = True
except ImportError:
    HAS_VGAMEPAD = False
    print("⚠️  vgamepad not installed — virtual gamepad output disabled")

# Map SNES key names (from combo boxes) → vgamepad XUSB_BUTTON constants
SNES_TO_XUSB = {}
if HAS_VGAMEPAD:
    SNES_TO_XUSB = {
        "A":          vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
        "B":          vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
        "X":          vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
        "Y":          vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
        "Dpad Up":    vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
        "Dpad Down":  vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
        "Dpad Left":  vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        "Dpad Right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        "Start":      vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        "L":          "LT", # Mapped conceptually, triggered as an axis
        "R":          "RT", # Mapped conceptually, triggered as an axis
    }

# Filters
from filters.BS50 import BS50
from filters.BS60 import BS60
from filters.HP70 import HP70
from filters.HP5 import HP5
from filters.LP45 import LP45
from filters.BPECG import BPECG
from filters.BP1To10 import BP1To10
from filters.EnvelopeDetector import EnvelopeDetector
from filters.BaselineTracker import BaselineTracker
from filters.FFTBandpower import FFTBandpower

MAX_CHANNELS = 6
FILTER_MAP = {0: 'emg', 1: 'eeg', 2: 'eog', 3: 'ecg'}

# Progress bar scaling (raw → 0-100)
EMG_SCALE = 500.0
BLINK_SCALE = 300.0
EYE_SCALE = 300.0
JAW_SCALE = 500.0
ECG_SCALE = 500.0


def clamp100(val, scale):
    return max(0, min(100, int(val / scale * 100)))


# ═══════════════════════════════════════════════════════════════════════════════
# BLE Manager — async BLE in a background thread, Qt signals for communication
# ═══════════════════════════════════════════════════════════════════════════════

class BLEManager(QObject):
    scan_result         = Signal(list)
    device_connected    = Signal(int)
    device_disconnected = Signal()
    data_received       = Signal(list, int)
    battery_updated     = Signal(int)
    error               = Signal(str)

    def __init__(self):
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._conn = None

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start_scan(self):
        asyncio.run_coroutine_threadsafe(self._scan(), self._loop)

    def connect_to(self, device):
        asyncio.run_coroutine_threadsafe(self._connect(device), self._loop)

    def disconnect(self):
        asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)

    def shutdown(self):
        try:
            if self._conn:
                asyncio.run_coroutine_threadsafe(
                    self._disconnect(), self._loop
                ).result(timeout=3)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

    async def _scan(self):
        try:
            devices = await NPGConnection.scan(timeout=10.0)
            self.scan_result.emit(devices)
        except Exception as e:
            self.error.emit(f"Scan failed: {e}")

    async def _connect(self, npg_device):
        try:
            self._conn = NPGConnection()
            await self._conn.connect(npg_device)
            self._conn.on_data(lambda s, n: self.data_received.emit(s, n))
            self._conn.on_battery(lambda p: self.battery_updated.emit(p))
            await self._conn.start_streaming()
            self.device_connected.emit(self._conn.num_channels)
        except Exception as e:
            self._conn = None
            self.error.emit(f"Connection failed: {e}")

    async def _disconnect(self):
        try:
            if self._conn:
                await self._conn.disconnect()
                self._conn = None
            self.device_disconnected.emit()
        except Exception as e:
            self.error.emit(f"Disconnect error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Channel Processor — per-channel notch + signal filter chain
# ═══════════════════════════════════════════════════════════════════════════════

class ChannelProcessor:
    def __init__(self):
        self.notch = None
        self.filter_type = 'emg'
        self._init_emg()

    # ── Pipeline initialisers ────────────────────────────────────────────

    def _init_emg(self):
        self.hp70 = HP70()
        self.emg_env = EnvelopeDetector(64)
        self.val_emg_envelope = 0.0

    def _init_eeg(self):
        self.lp45 = LP45()
        self.hp5 = HP5()
        self.fft = FFTBandpower(fft_size=512, sample_rate=500)
        self.blink_env = EnvelopeDetector(50)
        self.jaw_hp70 = HP70()
        self.jaw_env = EnvelopeDetector(50)
        self.val_beta_pct = 0.0
        self.val_blink_envelope = 0.0
        self.val_jaw_envelope = 0.0

    def _init_eog(self):
        self.bp1to10 = BP1To10()
        self.baseline = BaselineTracker(256)
        self.jaw_hp70 = HP70()
        self.jaw_env = EnvelopeDetector(50)
        self.val_eye_deviation = 0.0
        self.val_jaw_envelope = 0.0

    def _init_ecg(self):
        self.ecg_filter = BPECG()
        self.val_ecg = 0.0

    # ── Configuration ────────────────────────────────────────────────────

    def set_notch(self, setting):
        if setting == '50':   self.notch = BS50()
        elif setting == '60': self.notch = BS60()
        else:                 self.notch = None

    def set_filter(self, ftype):
        self.filter_type = ftype
        if ftype == 'emg':   self._init_emg()
        elif ftype == 'eeg': self._init_eeg()
        elif ftype == 'eog': self._init_eog()
        elif ftype == 'ecg': self._init_ecg()

    # ── Per-sample processing ────────────────────────────────────────────

    def process(self, raw):
        v = float(raw)
        if self.notch:
            v = self.notch.process(v)

        if self.filter_type == 'emg':
            f = self.hp70.process(v)
            self.val_emg_envelope = self.emg_env.get_envelope(abs(f))

        elif self.filter_type == 'eeg':
            lp = self.lp45.process(v)
            if self.fft.add_sample(lp):
                self.val_beta_pct = self.fft.get_band_percentages()['beta']
            hp = self.hp5.process(lp)
            self.val_blink_envelope = self.blink_env.get_envelope(abs(hp))
            j = self.jaw_hp70.process(v)
            self.val_jaw_envelope = self.jaw_env.get_envelope(abs(j))

        elif self.filter_type == 'eog':
            bp = self.bp1to10.process(v)
            self.baseline.update(bp)
            self.val_eye_deviation = bp - self.baseline.get_baseline()
            j = self.jaw_hp70.process(v)
            self.val_jaw_envelope = self.jaw_env.get_envelope(abs(j))

        elif self.filter_type == 'ecg':
            f = self.ecg_filter.process(v)
            self.val_ecg = abs(f)


# ═══════════════════════════════════════════════════════════════════════════════
# Controller Test Dialog
# ═══════════════════════════════════════════════════════════════════════════════

class ControllerTestDialog:
    def __init__(self, parent=None):
        from PySide6.QtWidgets import QVBoxLayout, QPushButton

        self.dialog = QDialog(parent)
        self.dialog.setWindowTitle("LIVE CONTROLLER DIAGNOSTICS")
        self.dialog.resize(760, 480)
        self.dialog.setStyleSheet("""
            QDialog { background-color: #0a0a0a; }
            QPushButton {
                background-color: #1a1a1a; border: 2px solid #2a2a2a;
                border-radius: 10px; color: #ffffff; font-size: 14px;
                font-weight: 900; letter-spacing: 2px; padding: 10px 18px;
            }
            QPushButton:hover { border: 2px solid #00ff66; color: #00ff66; }
        """)

        layout = QVBoxLayout(self.dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        self.viewer = ControllerViewer()
        layout.addWidget(self.viewer, 1)

        btn_close = QPushButton("CLOSE TESTER")
        btn_close.setFixedHeight(44)
        btn_close.clicked.connect(self.dialog.accept)
        layout.addWidget(btn_close)

    def show(self):
        self.dialog.show()

    def update_input(self, action_name, value):
        self.viewer.update_button(action_name, value)


# ═══════════════════════════════════════════════════════════════════════════════
# Main Controller — rewritten for the new UI layout
# ═══════════════════════════════════════════════════════════════════════════════

class NPGController:
    def __init__(self):
        self.app = QApplication(sys.argv)

        # Load UI
        script_dir = os.path.dirname(os.path.abspath(__file__))
        loader = QUiLoader()
        ui_file = QFile(os.path.join(script_dir, "NPG-Controller.ui"))
        ui_file.open(QFile.ReadOnly)
        self.ui = loader.load(ui_file)
        ui_file.close()

        # State
        self.num_channels = 0
        self.is_connected = False
        self.processors = [ChannelProcessor() for _ in range(MAX_CHANNELS)]
        self.selected_input = 0  # 0=All, 1-6=specific channel

        # Virtual gamepad
        self.gamepad = None
        self._pressed_buttons = set()  # currently pressed XUSB buttons

        # Test Controller Window
        self.test_dialog = None

        # BLE
        self.ble = BLEManager()

        # 30Hz UI refresh timer
        self._ui_timer = QTimer()
        self._ui_timer.setInterval(33)
        self._ui_timer.timeout.connect(self._update_progress_bars)
        self._ui_timer.start()

        # Setup
        self._init_button_groups()
        self._init_threshold_bars()
        self._init_keybindings()
        self._fix_groupbox_styles()
        self._connect_signals()
        self._set_channel_enabled(0)
        # Uncheck "All" so it doesn't look selected at startup
        self.ui.btnSel_Input_All.setChecked(False)
        self._update_input_visibility()

    # ── Threshold Bars ────────────────────────────────────────────────────────

    def _init_threshold_bars(self):
        """Replace QProgressBars with ThresholdBars (draggable threshold + green detect)."""
        # Defaults derived from Arduino algo code:
        #   Blink   = 50 / BLINK_SCALE(300) * 100 ≈ 17
        #   Jaw     = 160 / JAW_SCALE(500) * 100  ≈ 32
        #   Eye L/R = 150 / EYE_SCALE(300) * 100  ≈ 50
        defaults = {
            'pbFocus':    50,
            'pbBlink':    17,
            'pbLeftEye':  50,
            'pbRightEye': 50,
            'pbJaw':      32,
            'pbECG':      50,
            'pbEMG1':     40,
            'pbEMG2':     40,
            'pbEMG3':     40,
            'pbEMG4':     40,
        }
        for name, thresh in defaults.items():
            old_pb = getattr(self.ui, name, None)
            if old_pb is None:
                continue
            bar = ThresholdBar(threshold=thresh)
            bar.setObjectName(name)
            layout = self._find_layout_of(old_pb)
            if layout:
                layout.replaceWidget(old_pb, bar)
                old_pb.hide()
                old_pb.deleteLater()
                setattr(self.ui, name, bar)

        # Sync Left/Right eye thresholds (single detector threshold)
        self.ui.pbLeftEye.thresholdChanged.connect(self.ui.pbRightEye.setThreshold)
        self.ui.pbRightEye.thresholdChanged.connect(self.ui.pbLeftEye.setThreshold)

    def _find_layout_of(self, widget):
        """Recursively find the QLayout that directly contains *widget*."""
        parent = widget.parentWidget()
        if not parent or not parent.layout():
            return None
        return self._search_layout(parent.layout(), widget)

    def _search_layout(self, layout, widget):
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is widget:
                return layout
            sub = item.layout()
            if sub:
                r = self._search_layout(sub, widget)
                if r:
                    return r
        return None

    def _init_keybindings(self):
        """Populate combo boxes with SNES controller keys."""
        snes_keys = [
            "None", "A", "B", "X", "Y", "Dpad Up", "Dpad Down", 
            "Dpad Left", "Dpad Right", "L", "R", "Start"
        ]
        
        cmb_list = [
            self.ui.cmbFocus, self.ui.cmbBlink, self.ui.cmbLeftEye, 
            self.ui.cmbRightEye, self.ui.cmbJaw, self.ui.cmbECG,
            self.ui.cmbEMG1, self.ui.cmbEMG2, self.ui.cmbEMG3, self.ui.cmbEMG4
        ]
        
        for cmb in cmb_list:
            cmb.addItems(snes_keys)

    # ── Button Groups ────────────────────────────────────────────────────────

    def _init_button_groups(self):
        # (Notch on/off is now a QCheckBox — no button group needed)

        # Notch frequency
        self.grp_notch_freq = QButtonGroup(self.ui)
        self.grp_notch_freq.setExclusive(True)
        self.grp_notch_freq.addButton(self.ui.btnNotch50Hz, 0)
        self.grp_notch_freq.addButton(self.ui.btnNotch60Hz, 1)

        # Per-channel filter type (EMG=0, EEG=1, EOG=2, ECG=3)
        self.grp_filter_ch = []
        for ch in range(1, 7):
            g = QButtonGroup(self.ui)
            g.setExclusive(True)
            g.addButton(getattr(self.ui, f'btnFilterCh{ch}EMG'), 0)
            g.addButton(getattr(self.ui, f'btnFilterCh{ch}EEG'), 1)
            g.addButton(getattr(self.ui, f'btnFilterCh{ch}EOG'), 2)
            g.addButton(getattr(self.ui, f'btnFilterCh{ch}ECG'), 3)
            self.grp_filter_ch.append(g)

        # Signal input channel selector (All=0, Ch1=1..Ch6=6)
        self.grp_input_sel = QButtonGroup(self.ui)
        self.grp_input_sel.setExclusive(True)
        self.grp_input_sel.addButton(self.ui.btnSel_Input_All, 0)
        for ch in range(1, 7):
            self.grp_input_sel.addButton(
                getattr(self.ui, f'btnSel_Input_Ch{ch}'), ch
            )

    # ── Signal Wiring ────────────────────────────────────────────────────────

    def _connect_signals(self):
        # Bottom bar
        self.ui.btnConnect.clicked.connect(self._on_connect_clicked)
        self.ui.btnKeybinds.clicked.connect(self._on_keybinds_clicked)

        # Notch
        self.ui.grpNotch.toggled.connect(self._on_notch_toggle)
        self.grp_notch_freq.idClicked.connect(self._on_notch_freq)

        # Per-channel filter
        for i, g in enumerate(self.grp_filter_ch):
            g.idClicked.connect(lambda id_, ch=i: self._on_filter_ch(ch, id_))

        # Per-channel checkbox (now QGroupBox)
        for i in range(MAX_CHANNELS):
            getattr(self.ui, f'grpCh{i + 1}').toggled.connect(
                lambda state, ch=i: self._on_channel_toggled(ch, state)
            )

        # Gamepad icon → select channel in Signal Inputs
        for i in range(MAX_CHANNELS):
            getattr(self.ui, f'btnChIcon{i + 1}').clicked.connect(
                lambda _, ch=i + 1: self._select_input_channel(ch)
            )

        # Input selector
        self.grp_input_sel.idClicked.connect(self._on_input_selection)

        # BLE
        self.ble.scan_result.connect(self._on_scan_result)
        self.ble.device_connected.connect(self._on_connected)
        self.ble.device_disconnected.connect(self._on_disconnected)
        self.ble.data_received.connect(self._on_data)
        self.ble.battery_updated.connect(self._on_battery)
        self.ble.error.connect(self._on_error)

    # ── Channel Enable / Disable ─────────────────────────────────────────────

    def _set_channel_enabled(self, n):
        """Enable channels 1..n, disable n+1..6. All remain visible."""
        any_active = n > 0

        # Notch section
        self.ui.grpNotch.setEnabled(any_active)

        # Input selector buttons
        self.ui.btnSel_Input_All.setEnabled(any_active)
        for ch in range(1, 7):
            getattr(self.ui, f'btnSel_Input_Ch{ch}').setEnabled(ch <= n)

        for ch_idx in range(MAX_CHANNELS):
            ch = ch_idx + 1
            in_range = ch <= n
            checked = (ch == 1) and in_range  # Only ch1 on by default

            # Disable entire group box for out-of-range (greys title too)
            getattr(self.ui, f'grpCh{ch}').setEnabled(in_range)

            cb = getattr(self.ui, f'grpCh{ch}')
            cb.blockSignals(True)
            cb.setEnabled(in_range)
            cb.setChecked(checked)
            cb.blockSignals(False)

            self._set_channel_controls_enabled(ch_idx, checked)

    def _set_channel_controls_enabled(self, ch_idx, enabled):
        """Enable/disable filter buttons + icon for one channel. Reset on disable."""
        ch = ch_idx + 1
        for suffix in ['EMG', 'EEG', 'EOG', 'ECG']:
            getattr(self.ui, f'btnFilterCh{ch}{suffix}').setEnabled(enabled)
        getattr(self.ui, f'btnChIcon{ch}').setEnabled(enabled)

        if not enabled:
            # Reset to EMG (default)
            getattr(self.ui, f'btnFilterCh{ch}EMG').setChecked(True)
            self.processors[ch_idx].set_filter('emg')
            self.processors[ch_idx].set_notch('off')

    def _fix_groupbox_styles(self):
        """Enforce perfect badge-style titles and checkboxes on the channel groupboxes, 
        mirroring the Notch filter styles, and fix the clipping top border bug."""
        
        for i in range(1, 7):
            grp = getattr(self.ui, f'grpCh{i}')
            inline_css = f"""
            QGroupBox#grpCh{i} {{
                background-color: transparent;
                border: 1px solid #ffffff;  /* Box outer line bright white when active */
                border-radius: 8px;
                margin-top: 14px;
                margin-bottom: 6px; /* Stop vertical clipping / overlaps */
                padding-top: 14px; 
                padding-bottom: 6px;
                padding-left: 8px;
                padding-right: 8px;
            }}
            QGroupBox#grpCh{i}:disabled, QGroupBox#grpCh{i}:unchecked {{
                border: 1px solid #2a2a2a; /* Greyed out box when inactive or unchecked */
            }}
            /* The title is styled as a badge */
            QGroupBox#grpCh{i}::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 4px 8px;
                left: 12px;
                top: 0px;
                background-color: #0a0a0a;  /* Hides the top border line natively without Qt clipping bugs */
                border: 1px solid #ffffff;
                border-radius: 5px;
                color: #ffffff;
                font-size: 11px;
            }}
            QGroupBox#grpCh{i}::title:disabled, QGroupBox#grpCh{i}::title:unchecked {{
                border: 1px solid #333333;
                color: #333333;
            }}
            /* The check box in the title */
            QGroupBox#grpCh{i}::indicator {{
                width: 16px; 
                height: 16px;
                border-radius: 4px;
                background-color: transparent;
                border: 2px solid #3a3a3a;
                margin-right: 6px;
                margin-top: 1px;
            }}
            QGroupBox#grpCh{i}::indicator:checked {{
                background-color: #0a0a0a;
                border: 2px solid #ffffff;
                image: url(icons/check.svg);
            }}
            QGroupBox#grpCh{i}::indicator:disabled, QGroupBox#grpCh{i}::indicator:unchecked {{
                border: 2px solid #222222;
                background-color: transparent;
                image: none;
            }}
            """
            grp.setStyleSheet(inline_css)
            
        # Ensure grpNotch safely adopts correct title disabled/enabled pseudo-states
        notch_css = """
        QGroupBox#grpNotch {
            background-color: transparent;
            border: 1px solid #ffffff;  /* Box outer line bright white when active */
            border-radius: 8px;
            margin-top: 14px;
            margin-bottom: 6px;
            padding-top: 14px; 
            padding-bottom: 6px;
            padding-left: 8px;
            padding-right: 8px;
        }
        QGroupBox#grpNotch:disabled, QGroupBox#grpNotch:unchecked {
            border: 1px solid #2a2a2a; /* Greyed out box when inactive or unchecked */
        }
        QGroupBox#grpNotch::title {
            background-color: #0a0a0a;
            border: 1px solid #ffffff;
            color: #ffffff;
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 5px;
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 12px;
        }
        QGroupBox#grpNotch::title:disabled, QGroupBox#grpNotch::title:unchecked {
            border: 1px solid #333333;
            color: #333333;
        }
        QGroupBox#grpNotch::indicator {
            width: 16px; 
            height: 16px;
            border-radius: 4px;
            background-color: transparent;
            border: 2px solid #3a3a3a;
            margin-right: 6px;
            margin-top: 1px;
        }
        QGroupBox#grpNotch::indicator:checked {
            background-color: #0a0a0a;
            border: 2px solid #ffffff;
            image: url(icons/check.svg);
        }
        QGroupBox#grpNotch::indicator:disabled, QGroupBox#grpNotch::indicator:unchecked {
            border: 2px solid #222222;
            background-color: transparent;
            image: none;
        }
        """
        self.ui.grpNotch.setStyleSheet(notch_css)

    # ── Handlers: Connect / Disconnect ───────────────────────────────────────

    def _on_connect_clicked(self):
        if self.ui.btnConnect.isChecked():
            self.ui.btnConnect.setText("SCANNING...")
            self.ui.btnConnect.setEnabled(False)
            self.ui.statusbar.showMessage("Scanning for NPG devices...")
            self.ble.start_scan()
        else:
            self.ble.disconnect()

    def _on_scan_result(self, devices):
        self.ui.btnConnect.setEnabled(True)
        if not devices:
            self.ui.btnConnect.setChecked(False)
            self.ui.btnConnect.setText("CONNECT")
            self.ui.statusbar.showMessage("No NPG devices found", 5000)
            QMessageBox.information(
                self.ui, "No Devices",
                "No NPG devices found.\nMake sure your device is powered on.")
            return

        items = [str(d) for d in devices]
        if len(devices) == 1:
            chosen = 0
        else:
            item, ok = QInputDialog.getItem(
                self.ui, "Select NPG Device", "Found devices:", items, 0, False)
            if not ok:
                self.ui.btnConnect.setChecked(False)
                self.ui.btnConnect.setText("CONNECT")
                self.ui.statusbar.showMessage("Cancelled", 3000)
                return
            chosen = items.index(item)

        self.ui.btnConnect.setText("CONNECTING...")
        self.ui.statusbar.showMessage(f"Connecting to {devices[chosen].name}...")
        self.ble.connect_to(devices[chosen])

    def _on_connected(self, num_channels):
        self.is_connected = True
        self.num_channels = num_channels
        self.ui.btnConnect.setText("DISCONNECT")
        self.ui.btnConnect.setChecked(True)
        self.ui.btnConnect.setEnabled(True)
        self.ui.statusbar.showMessage(
            f"Connected — {num_channels} channels @ 500Hz", 5000)
        self._set_channel_enabled(num_channels)

        # Apply initial filter settings to processors
        for ch in range(num_channels):
            filter_id = self.grp_filter_ch[ch].checkedId()
            self.processors[ch].set_filter(FILTER_MAP.get(filter_id, 'emg'))
        self._apply_notch_to_all()

        # Create virtual gamepad
        if HAS_VGAMEPAD and self.gamepad is None:
            try:
                self.gamepad = vg.VX360Gamepad()
                print("🎮 Virtual gamepad created")
            except Exception as e:
                print(f"⚠️  Could not create gamepad: {e}")
                self.gamepad = None

        # Select Ch1 in input selector and show its bars
        self.ui.btnSel_Input_Ch1.setChecked(True)
        self.selected_input = 1
        self._update_input_visibility()

    def _on_disconnected(self):
        self.is_connected = False
        self.num_channels = 0
        self.ui.btnConnect.setChecked(False)
        self.ui.btnConnect.setText("CONNECT")
        self.ui.btnConnect.setEnabled(True)
        self.ui.statusbar.showMessage("Disconnected", 3000)
        self._set_channel_enabled(0)
        self._reset_progress_bars()

        # Release all gamepad buttons and destroy
        if self.gamepad:
            try:
                self.gamepad.reset()
                self.gamepad.update()
                del self.gamepad
            except Exception:
                pass
            self.gamepad = None
            self._pressed_buttons.clear()
            print("🎮 Virtual gamepad released")

        # Reset controller viewer
        if self.test_dialog:
            self.test_dialog.viewer.reset_all()

        # Reset input selector to All
        self.ui.btnSel_Input_All.setChecked(True)
        self.selected_input = 0
        self._update_input_visibility()

    def _on_error(self, msg):
        self.ui.statusbar.showMessage(f"Error: {msg}", 5000)
        if not self.is_connected:
            self.ui.btnConnect.setChecked(False)
            self.ui.btnConnect.setText("CONNECT")
            self.ui.btnConnect.setEnabled(True)

    def _on_battery(self, pct):
        self.ui.statusbar.showMessage(f"Battery: {pct}%", 3000)

    def _on_keybinds_clicked(self):
        if not self.test_dialog:
            self.test_dialog = ControllerTestDialog(self.ui)
        self.test_dialog.show()

    # ── Handlers: Notch ──────────────────────────────────────────────────────

    def _on_notch_toggle(self, state):
        notch_on = bool(state)
        self.ui.btnNotch50Hz.setEnabled(notch_on)
        self.ui.btnNotch60Hz.setEnabled(notch_on)
        self._apply_notch_to_all()

    def _on_notch_freq(self, id_):
        self._apply_notch_to_all()

    def _apply_notch_to_all(self):
        """Apply the global notch setting to all enabled+checked channels."""
        if self.ui.grpNotch.isChecked():
            setting = '50' if self.ui.btnNotch50Hz.isChecked() else '60'
        else:
            setting = 'off'
        for ch_idx in range(self.num_channels):
            cb = getattr(self.ui, f'grpCh{ch_idx + 1}')
            if cb.isChecked():
                self.processors[ch_idx].set_notch(setting)

    # ── Handlers: Filter & Channel ───────────────────────────────────────────

    def _on_filter_ch(self, ch, id_):
        self.processors[ch].set_filter(FILTER_MAP.get(id_, 'emg'))
        self._update_input_visibility()

    def _on_channel_toggled(self, ch_idx, state):
        enabled = bool(state)
        self._set_channel_controls_enabled(ch_idx, enabled)
        if enabled:
            self._apply_notch_to_all()
            filter_id = self.grp_filter_ch[ch_idx].checkedId()
            self.processors[ch_idx].set_filter(FILTER_MAP.get(filter_id, 'emg'))
        self._update_input_visibility()

    # ── Signal Input Selector ────────────────────────────────────────────────

    def _on_input_selection(self, id_):
        self.selected_input = id_
        self._update_input_visibility()

    def _select_input_channel(self, ch):
        """Called by gamepad icon button — select channel in Signal Inputs."""
        btn = getattr(self.ui, f'btnSel_Input_Ch{ch}')
        if btn.isEnabled():
            btn.setChecked(True)
            self.selected_input = ch
            self._update_input_visibility()

    def _update_input_visibility(self):
        """Show/hide signal input rows based on the input selector."""
        sel = self.selected_input

        # Fixed rows (non-EMG)
        fixed = {
            'focus':    (self.ui.lblFocus,    self.ui.pbFocus,    self.ui.cmbFocus),
            'blink':    (self.ui.lblBlink,    self.ui.pbBlink,    self.ui.cmbBlink),
            'leftEye':  (self.ui.lblLeftEye,  self.ui.pbLeftEye,  self.ui.cmbLeftEye),
            'rightEye': (self.ui.lblRightEye, self.ui.pbRightEye, self.ui.cmbRightEye),
            'jaw':      (self.ui.lblJaw,      self.ui.pbJaw,      self.ui.cmbJaw),
            'ecg':      (self.ui.lblECG,      self.ui.pbECG,      self.ui.cmbECG),
        }
        emg_slots = [
            (self.ui.lblEMG1, self.ui.pbEMG1, self.ui.cmbEMG1),
            (self.ui.lblEMG2, self.ui.pbEMG2, self.ui.cmbEMG2),
            (self.ui.lblEMG3, self.ui.pbEMG3, self.ui.cmbEMG3),
            (self.ui.lblEMG4, self.ui.pbEMG4, self.ui.cmbEMG4),
        ]

        # Helper: hide everything
        def hide_all():
            for lbl, pb, cmb in fixed.values():
                lbl.setVisible(False)
                pb.setVisible(False)
                cmb.setVisible(False)
            for lbl, pb, cmb in emg_slots:
                lbl.setVisible(False)
                pb.setVisible(False)
                cmb.setVisible(False)

        # Not connected — hide everything
        if not self.is_connected:
            hide_all()
            return

        # Collect active filter types + EMG channel list
        active_types = set()
        emg_chs = []
        for ch_idx in range(self.num_channels):
            cb = getattr(self.ui, f'grpCh{ch_idx + 1}')
            if not cb.isChecked():
                continue
            ft = self.processors[ch_idx].filter_type
            active_types.add(ft)
            if ft == 'emg':
                emg_chs.append(ch_idx + 1)

        if sel == 0:
            # "All" — only show rows whose filter type has an active channel
            has_eeg = 'eeg' in active_types
            has_eog = 'eog' in active_types
            has_ecg = 'ecg' in active_types
            has_jaw = has_eeg or has_eog

            fixed['focus'][0].setVisible(has_eeg)
            fixed['focus'][1].setVisible(has_eeg)
            fixed['focus'][2].setVisible(has_eeg)
            fixed['blink'][0].setVisible(has_eeg)
            fixed['blink'][1].setVisible(has_eeg)
            fixed['blink'][2].setVisible(has_eeg)
            fixed['leftEye'][0].setVisible(has_eog)
            fixed['leftEye'][1].setVisible(has_eog)
            fixed['leftEye'][2].setVisible(has_eog)
            fixed['rightEye'][0].setVisible(has_eog)
            fixed['rightEye'][1].setVisible(has_eog)
            fixed['rightEye'][2].setVisible(has_eog)
            fixed['jaw'][0].setVisible(has_jaw)
            fixed['jaw'][1].setVisible(has_jaw)
            fixed['jaw'][2].setVisible(has_jaw)
            fixed['ecg'][0].setVisible(has_ecg)
            fixed['ecg'][1].setVisible(has_ecg)
            fixed['ecg'][2].setVisible(has_ecg)

            for i, (lbl, pb, cmb) in enumerate(emg_slots):
                if i < len(emg_chs):
                    lbl.setText(f' EMG(Ch{emg_chs[i]})')
                    lbl.setVisible(True)
                    pb.setVisible(True)
                    cmb.setVisible(True)
                else:
                    lbl.setVisible(False)
                    pb.setVisible(False)
                    cmb.setVisible(False)
        else:
            # Specific channel — only show if the channel is actually checked
            hide_all()
            ch_idx = sel - 1
            if ch_idx < self.num_channels:
                cb = getattr(self.ui, f'grpCh{sel}')
                if cb.isChecked():
                    ftype = self.processors[ch_idx].filter_type
                    if ftype == 'eeg':
                        for k in ('focus', 'blink', 'jaw'):
                            fixed[k][0].setVisible(True)
                            fixed[k][1].setVisible(True)
                            fixed[k][2].setVisible(True)
                    elif ftype == 'eog':
                        for k in ('leftEye', 'rightEye', 'jaw'):
                            fixed[k][0].setVisible(True)
                            fixed[k][1].setVisible(True)
                            fixed[k][2].setVisible(True)
                    elif ftype == 'ecg':
                        fixed['ecg'][0].setVisible(True)
                        fixed['ecg'][1].setVisible(True)
                        fixed['ecg'][2].setVisible(True)
                    elif ftype == 'emg':
                        lbl, pb, cmb = emg_slots[0]
                        lbl.setText(f' EMG(Ch{sel})')
                        lbl.setVisible(True)
                        pb.setVisible(True)
                        cmb.setVisible(True)

    # ── Data Processing ──────────────────────────────────────────────────────

    def _on_data(self, samples, num_channels):
        if not self.is_connected:
            return
        for sample in samples:
            for ch_idx in range(min(num_channels, MAX_CHANNELS)):
                cb = getattr(self.ui, f'grpCh{ch_idx + 1}')
                if not cb.isChecked():
                    continue
                self.processors[ch_idx].process(sample['channels'][ch_idx])

    def _update_progress_bars(self):
        """Route processor outputs to progress bars, run detection, and
        trigger gamepad buttons (called at 30Hz by QTimer)."""
        if not self.is_connected:
            return

        focus_set = blink_set = jaw_set = ecg_set = False
        left_eye_set = right_eye_set = False
        emg_bars = [self.ui.pbEMG1, self.ui.pbEMG2, self.ui.pbEMG3, self.ui.pbEMG4]
        emg_idx = 0

        # Determine which channel owns jaw clench (lowest EEG or EOG)
        jaw_owner = None
        for ch in range(self.num_channels):
            p = self.processors[ch]
            cb = getattr(self.ui, f'grpCh{ch + 1}')
            if cb.isChecked() and p.filter_type in ('eeg', 'eog'):
                jaw_owner = ch
                break

        for ch in range(self.num_channels):
            p = self.processors[ch]
            cb = getattr(self.ui, f'grpCh{ch + 1}')
            if not cb.isChecked():
                continue

            if p.filter_type == 'eeg':
                if not focus_set:
                    self.ui.pbFocus.setValue(clamp100(p.val_beta_pct, 100.0))
                    focus_set = True
                if not blink_set:
                    self.ui.pbBlink.setValue(clamp100(p.val_blink_envelope, BLINK_SCALE))
                    blink_set = True
                if ch == jaw_owner and not jaw_set:
                    self.ui.pbJaw.setValue(clamp100(p.val_jaw_envelope, JAW_SCALE))
                    jaw_set = True

            elif p.filter_type == 'eog':
                if not left_eye_set:
                    left_val = max(0.0, p.val_eye_deviation)
                    self.ui.pbLeftEye.setValue(clamp100(left_val, EYE_SCALE))
                    left_eye_set = True
                if not right_eye_set:
                    right_val = max(0.0, -p.val_eye_deviation)
                    self.ui.pbRightEye.setValue(clamp100(right_val, EYE_SCALE))
                    right_eye_set = True
                if ch == jaw_owner and not jaw_set:
                    self.ui.pbJaw.setValue(clamp100(p.val_jaw_envelope, JAW_SCALE))
                    jaw_set = True

            elif p.filter_type == 'emg':
                if emg_idx < 4:
                    emg_bars[emg_idx].setValue(clamp100(p.val_emg_envelope, EMG_SCALE))
                    emg_idx += 1

            elif p.filter_type == 'ecg' and not ecg_set:
                self.ui.pbECG.setValue(clamp100(p.val_ecg, ECG_SCALE))
                ecg_set = True

        if not focus_set:     self.ui.pbFocus.setValue(0)
        if not blink_set:     self.ui.pbBlink.setValue(0)
        if not left_eye_set:  self.ui.pbLeftEye.setValue(0)
        if not right_eye_set: self.ui.pbRightEye.setValue(0)
        if not jaw_set:       self.ui.pbJaw.setValue(0)
        if not ecg_set:       self.ui.pbECG.setValue(0)
        for i in range(emg_idx, 4):
            emg_bars[i].setValue(0)

        # ── Detection → key mapping → gamepad ────────────────────────────
        self._process_key_mappings()

    def _process_key_mappings(self):
        """Check each ThresholdBar's detected state, read its combo box mapping,
        and press/release the corresponding gamepad button."""
        # Pairs of (ThresholdBar, QComboBox)
        bar_cmb_pairs = [
            (self.ui.pbFocus,    self.ui.cmbFocus),
            (self.ui.pbBlink,    self.ui.cmbBlink),
            (self.ui.pbLeftEye,  self.ui.cmbLeftEye),
            (self.ui.pbRightEye, self.ui.cmbRightEye),
            (self.ui.pbJaw,      self.ui.cmbJaw),
            (self.ui.pbECG,      self.ui.cmbECG),
            (self.ui.pbEMG1,     self.ui.cmbEMG1),
            (self.ui.pbEMG2,     self.ui.cmbEMG2),
            (self.ui.pbEMG3,     self.ui.cmbEMG3),
            (self.ui.pbEMG4,     self.ui.cmbEMG4),
        ]

        # Collect which SNES keys should be pressed this frame
        keys_to_press = set()
        for bar, cmb in bar_cmb_pairs:
            if not bar.isVisible():
                continue
            key_name = cmb.currentText()
            if key_name == "None":
                continue
            if bar.detected:
                keys_to_press.add(key_name)

        # Update virtual gamepad
        if self.gamepad:
            # Press newly detected keys
            for key_name in keys_to_press:
                xusb = SNES_TO_XUSB.get(key_name)
                if xusb == "LT":
                    if "LT" not in self._pressed_buttons:
                        self.gamepad.left_trigger_float(value_float=1.0)
                        self._pressed_buttons.add("LT")
                elif xusb == "RT":
                    if "RT" not in self._pressed_buttons:
                        self.gamepad.right_trigger_float(value_float=1.0)
                        self._pressed_buttons.add("RT")
                elif xusb and xusb not in self._pressed_buttons:
                    self.gamepad.press_button(xusb)
                    self._pressed_buttons.add(xusb)

            # Release keys that are no longer detected
            active_xusb = {SNES_TO_XUSB.get(k) for k in keys_to_press
                           if k in SNES_TO_XUSB}
            for xusb in list(self._pressed_buttons):
                if xusb not in active_xusb:
                    if xusb == "LT":
                        self.gamepad.left_trigger_float(value_float=0.0)
                    elif xusb == "RT":
                        self.gamepad.right_trigger_float(value_float=0.0)
                    else:
                        self.gamepad.release_button(xusb)
                    self._pressed_buttons.discard(xusb)

            self.gamepad.update()

        # Update controller viewer in test dialog
        if self.test_dialog:
            all_keys = ["A", "B", "X", "Y", "Dpad Up", "Dpad Down",
                        "Dpad Left", "Dpad Right", "L", "R", "Start"]
            for key_name in all_keys:
                self.test_dialog.viewer.update_button(
                    key_name, key_name in keys_to_press)

    def _reset_progress_bars(self):
        for bar in [self.ui.pbFocus, self.ui.pbBlink,
                    self.ui.pbLeftEye, self.ui.pbRightEye,
                    self.ui.pbJaw, self.ui.pbECG,
                    self.ui.pbEMG1, self.ui.pbEMG2, self.ui.pbEMG3, self.ui.pbEMG4]:
            bar.setValue(0)

    # ── Run ──────────────────────────────────────────────────────────────────

    def run(self):
        self.ui.show()
        try:
            return self.app.exec()
        finally:
            self.ble.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    controller = NPGController()
    sys.exit(controller.run())
