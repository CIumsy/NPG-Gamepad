"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
Copyright (c) 2026 Krishnanshu Mittal - krishnanshu@upsidedownlabs.tech
Copyright (c) 2026 Upside Down Labs - contact@upsidedownlabs.tech

At Upside Down Labs, we create open-source DIY neuroscience hardware and software.
Our mission is to make neuroscience affordable and accessible for everyone.
By supporting us with your purchase, you help spread innovation and open science.
Thank you for being part of this journey with us!
"""

import sys
import os
import time

# Essential for PyInstaller one-file mode so PySide CSS/UI can find relative asset paths like 'icons/*.svg'
if hasattr(sys, '_MEIPASS'):
    os.chdir(sys._MEIPASS)

from PySide6.QtWidgets import (
    QApplication, QInputDialog, QMessageBox, QButtonGroup, QDialog,
    QScrollArea, QWidget, QVBoxLayout
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import QFile, QTimer, Qt, qInstallMessageHandler
from PySide6.QtGui import QIcon

def qt_message_handler(mode, context, message):
    # Ignore unfixable SVG and Font warnings from Qt
    lower_msg = message.lower()
    if "invalid path data" in lower_msg or "path truncated" in lower_msg:
        return
    if "setpointsize: point size <= 0" in lower_msg:
        return
    if "unknown property transition" in lower_msg:
        return
    sys.stderr.write(f"Qt: {message}\n")

qInstallMessageHandler(qt_message_handler)

from gamepad import (
    resource_path, HAS_VGAMEPAD, ensure_vigembus, create_pad,
    MODE_XBOX, MODE_DS4, KEYS_FOR_MODE, CONTROLLER_NAMES, DS4_ANALOG_KEYS,
    IMU_STICK_PREFIX,
)
from config import MAX_CHANNELS, FILTER_MAP, EMG_SCALE, BLINK_SCALE, EYE_SCALE, JAW_SCALE, ECG_SCALE, clamp100, DEFAULT_THRESHOLDS
from ble_manager import BLEManager
from channel_processor import ChannelProcessor
from widgets.ThresholdBar import ThresholdBar
from widgets.ControllerViewer import ControllerViewer
from widgets.DualSenseViewer import DualSenseViewer
from widgets.IMUCalibrationDialog import IMUCalibrationDialog
from Algorithms.IMUProcessor import IMUProcessor


# Controller Test Dialog

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

        # One viewer per controller mode
        from PySide6.QtWidgets import QStackedWidget
        self._viewers = {
            MODE_XBOX: ControllerViewer(),
            MODE_DS4: DualSenseViewer(),
        }
        self._stack = QStackedWidget()
        for v in self._viewers.values():
            self._stack.addWidget(v)
        layout.addWidget(self._stack, 1)
        self.mode = MODE_XBOX
        self.viewer = self._viewers[MODE_XBOX]

        btn_close = QPushButton("CLOSE TESTER")
        btn_close.setFixedHeight(44)
        btn_close.clicked.connect(self.dialog.accept)
        layout.addWidget(btn_close)

    def show(self):
        self.dialog.show()

    def set_mode(self, mode):
        """Show the controller drawing for the given mode."""
        self.mode = mode
        self.viewer = self._viewers[mode]
        self._stack.setCurrentWidget(self.viewer)
        for v in self._viewers.values():
            v.reset_all()

    def update_values(self, key_values):
        """key_values: {key name: 0..1} for the current mode."""
        if self.mode == MODE_DS4:
            self.viewer.set_values(key_values)
        else:
            for key_name in KEYS_FOR_MODE[MODE_XBOX]:
                self.viewer.update_button(key_name, key_values.get(key_name, 0.0) > 0)


# Main

class NPGLiteSNES:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setWindowIcon(QIcon(resource_path("icons/app_icon.png")))

        # Load UI
        loader = QUiLoader()
        ui_file = QFile(resource_path("NPG-Controller.ui"))
        ui_file.open(QFile.ReadOnly)
        self.ui = loader.load(ui_file)
        ui_file.close()

        # Make both panels scrollable so content never disappears
        self._make_panel_scrollable(self.ui.configGroup)
        self._make_panel_scrollable(self.ui.inputsGroup)

        # State
        self.num_channels = 0
        self.is_connected = False
        self.processors = [ChannelProcessor() for _ in range(MAX_CHANNELS)]
        self.selected_input = 0  # 0=All, 1-6=specific channel
        self._active_chs = []        # ticked channel indices (cached for the per-sample loop)
        self._last_eval = 0.0        # perf_counter() of the last signal evaluation
        self._last_counter = None    # last firmware sample counter seen
        self._lost_samples = 0

        # Virtual gamepad
        self.gamepad = None
        self._gamepad_gen = 0   # bumps on every create/destroy to cancel stale retries
        self._detection_flash_keys = set()

        # Controller mode (Xbox / DualShock). Each mode keeps its own key mappings.
        self.controller_mode = MODE_XBOX
        self._mode_selections = {MODE_XBOX: {}, MODE_DS4: {}}

        # Test Controller Window
        self.test_dialog = None

        # IMU (MPU6050) motion -> analog stick
        self.imu = IMUProcessor()
        self.has_imu = False
        self.imu_dialog = None
        self._signal_key_values = {}     # latest signal mappings (30 Hz), merged with the IMU stick
        self._imu_stick_choice = {MODE_XBOX: "None", MODE_DS4: "None"}

        # Blink / Jaw Clench detectors
        from Algorithms.BlinkDetector import BlinkDetector
        from Algorithms.JawClenchDetector import JawClenchDetector
        self.blink_detector = BlinkDetector()
        self.jaw_detector = JawClenchDetector()
        self.last_blink_event = None
        self.last_jaw_event = None
        self._blink_action_timer = 0
        self._jaw_action_timer = 0

        # BLE
        self.ble = BLEManager()

        # 30Hz UI refresh timer
        self._ui_timer = QTimer()
        self._ui_timer.setInterval(33)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

        # Setup
        self._init_mode_tabs()
        self._init_button_groups()
        self._init_threshold_bars()
        self._init_keybindings()
        self._fix_groupbox_styles()
        self._connect_signals()
        self._set_channel_enabled(0)
        # Uncheck "All" so it doesn't look selected at startup
        self.ui.btnSel_Input_All.setChecked(False)
        # EMG combination dynamic rows: list of (lbl, bar, cmb, ch_a, ch_b)
        self._emg_combo_rows = []

        self._init_imu_row()

        # Hide detection sub-rows initially
        self.ui.grpDoubleBlink.setVisible(False)
        self.ui.grpTripleBlink.setVisible(False)
        self.ui.grpDoubleJawClench.setVisible(False)
        self._update_input_visibility()

    # Scrollable panels

    @staticmethod
    def _make_panel_scrollable(group_box):
        """Wrap a QGroupBox's content in a QScrollArea so it scrolls
        instead of crushing/hiding widgets on small windows."""
        old_layout = group_box.layout()
        if old_layout is None:
            return

        # Create a container widget and move all existing items into it
        container = QWidget()
        new_layout = QVBoxLayout(container)
        new_layout.setContentsMargins(
            old_layout.contentsMargins().left(),
            old_layout.contentsMargins().top(),
            old_layout.contentsMargins().right(),
            old_layout.contentsMargins().bottom(),
        )
        new_layout.setSpacing(old_layout.spacing())

        while old_layout.count():
            item = old_layout.takeAt(0)
            if item.widget():
                new_layout.addWidget(item.widget())
            elif item.layout():
                new_layout.addLayout(item.layout())
            elif item.spacerItem():
                new_layout.addItem(item.spacerItem())

        # Create scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background:transparent;}"
                             "QScrollArea>QWidget>QWidget{background:transparent;}")

        # Put scroll area into the group box
        old_layout.addWidget(scroll)

    # Threshold Bars

    def _init_threshold_bars(self):
        """Replace QProgressBars with ThresholdBars (draggable threshold + green detect)."""
        for name, thresh in DEFAULT_THRESHOLDS.items():
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

    def _key_list(self):
        """Drop-down entries for the current controller mode."""
        return ["None"] + KEYS_FOR_MODE[self.controller_mode]

    def _static_combos(self):
        """Key mapping combo boxes defined in the .ui file."""
        return [
            self.ui.cmbFocus, self.ui.cmbBlink, self.ui.cmbLeftEye,
            self.ui.cmbRightEye, self.ui.cmbJaw, self.ui.cmbECG,
            self.ui.cmbEMG1, self.ui.cmbEMG2, self.ui.cmbEMG3,
            self.ui.cmbEMG4, self.ui.cmbEMG5, self.ui.cmbEMG6,
            self.ui.cmbDoubleBlink, self.ui.cmbTripleBlink,
            self.ui.cmbDoubleJawClench,
        ]

    def _all_combos(self):
        """Static combos plus the dynamic EMG combination combos."""
        return self._static_combos() + [row[2] for row in self._emg_combo_rows]

    def _init_keybindings(self):
        """Populate combo boxes with the current mode's controller keys."""
        for cmb in self._static_combos():
            cmb.addItems(self._key_list())

    # Controller Mode Tabs

    def _init_mode_tabs(self):
        """Add the Xbox / DualShock tabs above the main content."""
        from PySide6.QtWidgets import QTabBar, QHBoxLayout

        self.mode_tabs = QTabBar()
        self.mode_tabs.setObjectName("modeTabs")
        self.mode_tabs.setDrawBase(False)
        self.mode_tabs.setExpanding(False)
        self.mode_tabs.addTab("XBOX")
        self.mode_tabs.addTab("DUALSHOCK")
        self.mode_tabs.setStyleSheet("""
            QTabBar#modeTabs { background: transparent; }
            QTabBar#modeTabs::tab {
                background-color: #141414;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                color: #e8e8e8;
                font-size: 13px;
                font-weight: 900;
                letter-spacing: 2px;
                padding: 8px 28px;
                margin-right: 8px;
            }
            QTabBar#modeTabs::tab:hover:!selected {
                border: 1px solid #ffffff;
                color: #ffffff;
            }
            QTabBar#modeTabs::tab:selected {
                background-color: #ffffff;
                border: 1px solid #ffffff;
                color: #000000;
            }
        """)
        self.mode_tabs.currentChanged.connect(self._on_mode_changed)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.mode_tabs)
        row.addStretch(1)
        self.ui.centralwidget.layout().insertLayout(0, row)

    def _on_mode_changed(self, index):
        new_mode = MODE_DS4 if index == 1 else MODE_XBOX
        if new_mode == self.controller_mode:
            return

        # Remember this mode's mappings, then swap every drop-down's key list
        self._save_mode_selections()
        self.controller_mode = new_mode
        self._detection_flash_keys.clear()
        for cmb in self._all_combos():
            self._fill_combo(cmb)
        self._fill_imu_combo()
        self._signal_key_values = {}

        # Swap the virtual controller live if we are connected
        self._destroy_gamepad()
        if self.is_connected:
            self._create_gamepad()

        if self.test_dialog:
            self.test_dialog.set_mode(new_mode)

    def _save_mode_selections(self):
        sel = self._mode_selections[self.controller_mode]
        for cmb in self._all_combos():
            sel[cmb.objectName()] = cmb.currentText()
        self._imu_stick_choice[self.controller_mode] = self.cmb_imu.currentText()

    def _fill_combo(self, cmb):
        """Reload a combo with the current mode's keys and restore its
        saved choice for this mode (or None)."""
        saved = self._mode_selections[self.controller_mode].get(cmb.objectName(), "None")
        cmb.blockSignals(True)
        cmb.clear()
        cmb.addItems(self._key_list())
        idx = cmb.findText(saved)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)
        cmb.blockSignals(False)

    # Button Groups

    def _init_button_groups(self):

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

    # Signal Wiring

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

        # Per-channel checkbox 
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
        self.ble.imu_available.connect(self._on_imu_available)
        self.ble.imu_received.connect(self._on_imu_samples)
        self.ble.error.connect(self._on_error)

    # Channel Enable / Disable

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
            checked = (ch == 1) and in_range

            getattr(self.ui, f'grpCh{ch}').setEnabled(in_range)

            cb = getattr(self.ui, f'grpCh{ch}')
            cb.blockSignals(True)
            cb.setEnabled(in_range)
            cb.setChecked(checked)
            cb.blockSignals(False)

            self._set_channel_controls_enabled(ch_idx, checked)

        self._refresh_active_channels()
        self._update_filter_button_states()

    def _set_channel_controls_enabled(self, ch_idx, enabled):
        """Enable/disable filter buttons + icon for one channel. Reset on disable."""
        ch = ch_idx + 1
        for suffix in ['EMG', 'EEG', 'EOG', 'ECG']:
            getattr(self.ui, f'btnFilterCh{ch}{suffix}').setEnabled(enabled)
        getattr(self.ui, f'btnChIcon{ch}').setEnabled(enabled)

        if not enabled:
            getattr(self.ui, f'btnFilterCh{ch}EMG').setChecked(True)
            self.processors[ch_idx].set_filter('emg')
            self.processors[ch_idx].set_notch('off')

    def _fix_groupbox_styles(self):
        """Enforce badge-style titles and checkboxes on the channel groupboxes."""
        
        for i in range(1, 7):
            grp = getattr(self.ui, f'grpCh{i}')
            inline_css = f"""
            QGroupBox#grpCh{i} {{
                background-color: transparent;
                border: 1px solid #ffffff;
                border-radius: 8px;
                margin-top: 14px;
                margin-bottom: 6px;
                padding-top: 14px; 
                padding-bottom: 6px;
                padding-left: 8px;
                padding-right: 8px;
            }}
            QGroupBox#grpCh{i}:disabled, QGroupBox#grpCh{i}:unchecked {{
                border: 1px solid #2a2a2a;
            }}
            QGroupBox#grpCh{i}::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 4px 8px;
                left: 12px;
                top: 0px;
                background-color: #0a0a0a;
                border: 1px solid #ffffff;
                border-radius: 5px;
                color: #ffffff;
                font-size: 11px;
            }}
            QGroupBox#grpCh{i}::title:disabled, QGroupBox#grpCh{i}::title:unchecked {{
                border: 1px solid #333333;
                color: #333333;
            }}
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

        # Style for Notch and detection sub-groupboxes (shared template)
        def _badge_css(obj_name):
            return f"""
            QGroupBox#{obj_name} {{
                background-color: transparent;
                border: 1px solid #ffffff;
                border-radius: 8px;
                margin-top: 14px;
                margin-bottom: 6px;
                padding-top: 14px;
                padding-bottom: 6px;
                padding-left: 8px;
                padding-right: 8px;
            }}
            QGroupBox#{obj_name}:disabled, QGroupBox#{obj_name}:unchecked {{
                border: 1px solid #2a2a2a;
            }}
            QGroupBox#{obj_name}::title {{
                background-color: #0a0a0a;
                border: 1px solid #ffffff;
                color: #ffffff;
                font-size: 11px;
                padding: 3px 8px;
                border-radius: 5px;
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
            }}
            QGroupBox#{obj_name}::title:disabled, QGroupBox#{obj_name}::title:unchecked {{
                border: 1px solid #333333;
                color: #333333;
            }}
            QGroupBox#{obj_name}::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 4px;
                background-color: transparent;
                border: 2px solid #3a3a3a;
                margin-right: 6px;
                margin-top: 1px;
            }}
            QGroupBox#{obj_name}::indicator:checked {{
                background-color: #0a0a0a;
                border: 2px solid #ffffff;
                image: url(icons/check.svg);
            }}
            QGroupBox#{obj_name}::indicator:disabled, QGroupBox#{obj_name}::indicator:unchecked {{
                border: 2px solid #222222;
                background-color: transparent;
                image: none;
            }}
            """

        self.ui.grpNotch.setStyleSheet(_badge_css('grpNotch'))

    # Handlers: Connect / Disconnect

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
        self._last_counter = None
        self._lost_samples = 0
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

        # Create virtual gamepad for the selected mode
        if HAS_VGAMEPAD and self.gamepad is None:
            ensure_vigembus()
            self._create_gamepad()

        # Select Ch1 in input selector and show its bars
        self.ui.btnSel_Input_Ch1.setChecked(True)
        self.selected_input = 1
        self._rebuild_emg_combo_rows()
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

        self._destroy_gamepad()

        if self.test_dialog:
            self.test_dialog.viewer.reset_all()

        if self.imu_dialog:
            self.imu_dialog.close()
        self.imu.reset()
        self.has_imu = False
        self._signal_key_values = {}
        self._update_imu_row()

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

    # IMU

    def _init_imu_row(self):
        """IMU row pinned to the bottom of Signal Inputs (shown when the firmware has the IMU stream)."""
        from PySide6.QtWidgets import QLabel, QPushButton, QHBoxLayout

        self.lbl_imu = QLabel(' IMU')
        self.lbl_imu.setMinimumWidth(100)
        self.lbl_imu.setStyleSheet('background-color: transparent;')

        self.lbl_imu_status = QLabel()
        self.lbl_imu_status.setStyleSheet(
            'background-color: transparent; color: #9a9a9a; font-size: 12px;')

        self.btn_imu_cal = QPushButton('CALIBRATE')
        self.btn_imu_cal.clicked.connect(self._on_imu_calibrate)

        from PySide6.QtWidgets import QComboBox
        self.cmb_imu = QComboBox()
        self.cmb_imu.setObjectName('cmbIMU')
        self.cmb_imu.setMinimumWidth(110)
        self._fill_imu_combo()

        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.lbl_imu)
        row.addWidget(self.lbl_imu_status, 1)
        row.addWidget(self.btn_imu_cal)
        row.addWidget(self.cmb_imu)
        self._imu_row_widgets = [self.lbl_imu, self.lbl_imu_status, self.btn_imu_cal, self.cmb_imu]

        # After the spacer, so it stays at the bottom of the panel
        parent_layout = self._find_layout_of_spacer('inputsSpacer')
        parent_layout.addLayout(row)
        self._update_imu_row()

    def _fill_imu_combo(self):
        """IMU can only drive an analog stick, so it's DualShock-only."""
        cmb = self.cmb_imu
        cmb.blockSignals(True)
        cmb.clear()
        if self.controller_mode == MODE_DS4:
            cmb.addItems(["None"] + list(IMU_STICK_PREFIX))
            idx = cmb.findText(self._imu_stick_choice[MODE_DS4])
            cmb.setCurrentIndex(max(0, idx))
            cmb.setEnabled(True)
            cmb.setToolTip("Analog stick driven by the IMU tilt")
        else:
            cmb.addItem("None")
            cmb.setEnabled(False)
            cmb.setToolTip("The IMU can only drive an analog stick (DualShock mode)")
        cmb.blockSignals(False)

    def _on_imu_available(self, available):
        self.has_imu = available
        self._update_imu_row()

    def _on_imu_calibrate(self):
        if not self.imu_dialog:
            self.imu_dialog = IMUCalibrationDialog(self.imu, self.ui)
        self.imu_dialog.show()

    def _update_imu_row(self):
        visible = self.is_connected and self.has_imu
        for w in self._imu_row_widgets:
            w.setVisible(visible)
        if not visible:
            return
        if not self.imu.receiving:
            text = 'No data (is the MPU6050 connected?)'
        elif self.imu.calibrator is not None:
            text = 'Calibrating...'
        elif self.imu.calibrated:
            text = 'Calibrated'
        else:
            text = 'Not calibrated'
        if self.lbl_imu_status.text() != text:
            self.lbl_imu_status.setText(text)

    def _on_keybinds_clicked(self):
        if not self.test_dialog:
            self.test_dialog = ControllerTestDialog(self.ui)
            self.test_dialog.set_mode(self.controller_mode)
        self.test_dialog.show()

    # Handlers: Notch

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

    # Handlers: Filter & Channel

    def _on_filter_ch(self, ch, id_):
        self.processors[ch].set_filter(FILTER_MAP.get(id_, 'emg'))
        self._update_filter_button_states()
        self._rebuild_emg_combo_rows()
        self._update_input_visibility()

    def _on_channel_toggled(self, ch_idx, state):
        enabled = bool(state)
        self._refresh_active_channels()
        self._set_channel_controls_enabled(ch_idx, enabled)
        if enabled:
            self._apply_notch_to_all()
            filter_id = self.grp_filter_ch[ch_idx].checkedId()
            self.processors[ch_idx].set_filter(FILTER_MAP.get(filter_id, 'emg'))
        self._update_filter_button_states()
        self._rebuild_emg_combo_rows()
        self._update_input_visibility()

    def _update_filter_button_states(self):
        """Ensure EEG, EOG, and ECG can only be selected by one active channel at a time."""
        active_filters = {}
        
        for ch_idx in range(MAX_CHANNELS):
            cb = getattr(self.ui, f'grpCh{ch_idx + 1}')
            if cb.isChecked():
                filter_id = self.grp_filter_ch[ch_idx].checkedId()
                if filter_id in (1, 2, 3):
                    if filter_id in active_filters:
                        getattr(self.ui, f'btnFilterCh{ch_idx + 1}EMG').setChecked(True)
                        self.processors[ch_idx].set_filter('emg')
                    else:
                        active_filters[filter_id] = ch_idx
        
        for ch_idx in range(MAX_CHANNELS):
            cb = getattr(self.ui, f'grpCh{ch_idx + 1}')
            if not cb.isEnabled() or not cb.isChecked():
                continue
                
            btn_eeg = getattr(self.ui, f'btnFilterCh{ch_idx + 1}EEG')
            btn_eog = getattr(self.ui, f'btnFilterCh{ch_idx + 1}EOG')
            btn_ecg = getattr(self.ui, f'btnFilterCh{ch_idx + 1}ECG')
            
            btn_eeg.setEnabled(1 not in active_filters or active_filters[1] == ch_idx)
            btn_eog.setEnabled(2 not in active_filters or active_filters[2] == ch_idx)
            btn_ecg.setEnabled(3 not in active_filters or active_filters[3] == ch_idx)

    def _rebuild_emg_combo_rows(self):
        """Destroy old combo rows and create new ones for every pair of active
        EMG channels.  Each row is (QLabel, ThresholdBar, QComboBox, ch_a, ch_b)."""
        from PySide6.QtWidgets import QLabel, QComboBox, QHBoxLayout
        from itertools import combinations

        parent_layout = self._find_layout_of_spacer('inputsSpacer')
        if parent_layout is None:
            return

        if self._emg_combo_rows:
            for lbl, bar, cmb, ch_a, ch_b in self._emg_combo_rows:
                # Save previous combination keys (per controller mode)
                self._mode_selections[self.controller_mode][cmb.objectName()] = cmb.currentText()
                for i in range(parent_layout.count() - 1, -1, -1):
                    item = parent_layout.itemAt(i)
                    sub = item.layout() if item else None
                    if sub:
                        for j in range(sub.count()):
                            w = sub.itemAt(j).widget() if sub.itemAt(j) else None
                            if w is lbl:
                                parent_layout.takeAt(i)
                                break
                lbl.setParent(None)
                lbl.deleteLater()
                bar.setParent(None)
                bar.deleteLater()
                cmb.setParent(None)
                cmb.deleteLater()
            self._emg_combo_rows.clear()

        active_emg = []
        for ch in range(self.num_channels):
            cb = getattr(self.ui, f'grpCh{ch + 1}')
            if cb.isChecked() and self.processors[ch].filter_type == 'emg':
                active_emg.append(ch + 1)

        if len(active_emg) < 2:
            return

        spacer_idx = None
        for i in range(parent_layout.count()):
            item = parent_layout.itemAt(i)
            if item.spacerItem():
                spacer_idx = i
                break

        if spacer_idx is None:
            spacer_idx = parent_layout.count()

        insert_at = spacer_idx

        for ch_a, ch_b in combinations(active_emg, 2):
            row_layout = QHBoxLayout()
            row_layout.setSpacing(12)

            lbl = QLabel(f' EMG(Ch{ch_a}+Ch{ch_b})')
            lbl.setMinimumWidth(100)
            lbl.setStyleSheet('background-color: transparent;')

            bar = ThresholdBar(threshold=40)
            bar.setObjectName(f'pbEMGCombo_{ch_a}_{ch_b}')

            cmb = QComboBox()
            cmb.setObjectName(f'cmbEMGCombo_{ch_a}_{ch_b}')
            cmb.setMinimumWidth(80)
            self._fill_combo(cmb)

            row_layout.addWidget(lbl)
            row_layout.addWidget(bar)
            row_layout.addWidget(cmb)

            parent_layout.insertLayout(insert_at, row_layout)
            insert_at += 1

            self._emg_combo_rows.append((lbl, bar, cmb, ch_a, ch_b))

    def _find_layout_of_spacer(self, spacer_name):
        """Find the parent vertical QLayout that contains all signal input rows."""
        parent_widget = self.ui.pbEMG6.parentWidget()
        if parent_widget and parent_widget.layout():
            return parent_widget.layout()
        return None

    # Signal Input Selector

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
            (self.ui.lblEMG5, self.ui.pbEMG5, self.ui.cmbEMG5),
            (self.ui.lblEMG6, self.ui.pbEMG6, self.ui.cmbEMG6),
        ]

        def hide_all():
            for lbl, pb, cmb in fixed.values():
                lbl.setVisible(False)
                pb.setVisible(False)
                cmb.setVisible(False)
            for lbl, pb, cmb in emg_slots:
                lbl.setVisible(False)
                pb.setVisible(False)
                cmb.setVisible(False)
            for lbl, bar, cmb, _, _ in self._emg_combo_rows:
                lbl.setVisible(False)
                bar.setVisible(False)
                cmb.setVisible(False)
            self.ui.grpDoubleBlink.setVisible(False)
            self.ui.cmbDoubleBlink.setVisible(False)
            self.ui.grpTripleBlink.setVisible(False)
            self.ui.cmbTripleBlink.setVisible(False)
            self.ui.grpDoubleJawClench.setVisible(False)
            self.ui.cmbDoubleJawClench.setVisible(False)

        if not self.is_connected:
            hide_all()
            return

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

            self.ui.grpDoubleBlink.setVisible(has_eeg)
            self.ui.cmbDoubleBlink.setVisible(has_eeg)
            self.ui.grpTripleBlink.setVisible(has_eeg)
            self.ui.cmbTripleBlink.setVisible(has_eeg)
            self.ui.grpDoubleJawClench.setVisible(has_jaw)
            self.ui.cmbDoubleJawClench.setVisible(has_jaw)

            for i, (lbl, pb, cmb) in enumerate(emg_slots):
                ch_num = i + 1
                if ch_num in emg_chs:
                    lbl.setText(f' EMG(Ch{ch_num})')
                    lbl.setVisible(True)
                    pb.setVisible(True)
                    cmb.setVisible(True)
                else:
                    lbl.setVisible(False)
                    pb.setVisible(False)
                    cmb.setVisible(False)

            for lbl, bar, cmb, ch_a, ch_b in self._emg_combo_rows:
                vis = (ch_a in emg_chs) and (ch_b in emg_chs)
                lbl.setVisible(vis)
                bar.setVisible(vis)
                cmb.setVisible(vis)
        else:
            hide_all()
            self.ui.grpDoubleBlink.setVisible(False)
            self.ui.cmbDoubleBlink.setVisible(False)
            self.ui.grpTripleBlink.setVisible(False)
            self.ui.cmbTripleBlink.setVisible(False)
            self.ui.grpDoubleJawClench.setVisible(False)
            self.ui.cmbDoubleJawClench.setVisible(False)
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
                        self.ui.grpDoubleBlink.setVisible(True)
                        self.ui.cmbDoubleBlink.setVisible(True)
                        self.ui.grpTripleBlink.setVisible(True)
                        self.ui.cmbTripleBlink.setVisible(True)
                        self.ui.grpDoubleJawClench.setVisible(True)
                        self.ui.cmbDoubleJawClench.setVisible(True)
                    elif ftype == 'eog':
                        for k in ('leftEye', 'rightEye', 'jaw'):
                            fixed[k][0].setVisible(True)
                            fixed[k][1].setVisible(True)
                            fixed[k][2].setVisible(True)
                        self.ui.grpDoubleJawClench.setVisible(True)
                        self.ui.cmbDoubleJawClench.setVisible(True)
                    elif ftype == 'ecg':
                        fixed['ecg'][0].setVisible(True)
                        fixed['ecg'][1].setVisible(True)
                        fixed['ecg'][2].setVisible(True)
                    elif ftype == 'emg':
                        lbl, pb, cmb = emg_slots[ch_idx]
                        lbl.setText(f' EMG(Ch{sel})')
                        lbl.setVisible(True)
                        pb.setVisible(True)
                        cmb.setVisible(True)
                        for lbl2, bar2, cmb2, ch_a, ch_b in self._emg_combo_rows:
                            if sel == ch_a or sel == ch_b:
                                lbl2.setVisible(True)
                                bar2.setVisible(True)
                                cmb2.setVisible(True)

    # Data Processing

    def _refresh_active_channels(self):
        """Cache which channels are ticked so the per-sample loop needs no Qt calls.
        Called synchronously wherever a channel checkbox changes."""
        self._active_chs = [i for i in range(MAX_CHANNELS)
                            if getattr(self.ui, f'grpCh{i + 1}').isChecked()]

    def _on_data(self, samples, num_channels):
        if not self.is_connected:
            return
        self._check_sample_counters(samples)

        limit = min(num_channels, MAX_CHANNELS)
        active = [(ch, self.processors[ch]) for ch in self._active_chs if ch < limit]
        for sample in samples:
            vals = sample['channels']
            for ch, proc in active:
                proc.process(vals[ch])

        # Detection + gamepad output on every packet (every 20 ms), not on the UI timer
        self._evaluate_signals()

    def _check_sample_counters(self, samples):
        """Count samples lost in transit using the firmware's 0-255 sample counter."""
        if not samples:
            return
        first = samples[0]['counter']
        if self._last_counter is not None:
            expected = (self._last_counter + 1) & 0xFF
            if first != expected:
                lost = (first - expected) & 0xFF
                self._lost_samples += lost
                msg = f"Warning: {lost} samples lost in transit (total {self._lost_samples})"
                print(msg)
                self.ui.statusbar.showMessage(msg, 5000)
        self._last_counter = samples[-1]['counter']

    def _refresh_ui(self):
        """30 Hz UI tick: repaint bars that changed, refresh the IMU row."""
        if not self.is_connected:
            return
        # If packets stop arriving, keep evaluating like the old 30 Hz tick did
        # (releases timed presses, lets blink/jaw timeouts fire)
        if time.perf_counter() - self._last_eval > 0.05:
            self._evaluate_signals()
        for bar in self._all_bars():
            bar.flush()
        self._update_imu_row()

    def _all_bars(self):
        ui = self.ui
        bars = [ui.pbFocus, ui.pbBlink, ui.pbLeftEye, ui.pbRightEye, ui.pbJaw, ui.pbECG,
                ui.pbEMG1, ui.pbEMG2, ui.pbEMG3, ui.pbEMG4, ui.pbEMG5, ui.pbEMG6]
        bars += [row[1] for row in self._emg_combo_rows]
        return bars

    def _evaluate_signals(self):
        """Route processor outputs to the bars (repaint deferred to the UI tick),
        run detection, and send the gamepad output."""
        self._last_eval = time.perf_counter()

        focus_set = blink_set = jaw_set = ecg_set = False
        left_eye_set = right_eye_set = False
        emg_bars = [self.ui.pbEMG1, self.ui.pbEMG2, self.ui.pbEMG3,
                    self.ui.pbEMG4, self.ui.pbEMG5, self.ui.pbEMG6]
        emg_ch_envelopes = {}
        active = [ch for ch in self._active_chs if ch < self.num_channels]

        jaw_owner = None
        for ch in active:
            if self.processors[ch].filter_type in ('eeg', 'eog'):
                jaw_owner = ch
                break

        for ch in active:
            p = self.processors[ch]

            if p.filter_type == 'eeg':
                if not focus_set:
                    self.ui.pbFocus.setValue(clamp100(p.val_beta_pct, 100.0), repaint=False)
                    focus_set = True
                if not blink_set:
                    self.ui.pbBlink.setValue(clamp100(p.val_blink_envelope, BLINK_SCALE), repaint=False)
                    blink_set = True
                if ch == jaw_owner and not jaw_set:
                    self.ui.pbJaw.setValue(clamp100(p.val_jaw_envelope, JAW_SCALE), repaint=False)
                    jaw_set = True

            elif p.filter_type == 'eog':
                if not left_eye_set:
                    left_val = max(0.0, p.val_eye_deviation)
                    self.ui.pbLeftEye.setValue(clamp100(left_val, EYE_SCALE), repaint=False)
                    left_eye_set = True
                if not right_eye_set:
                    right_val = max(0.0, -p.val_eye_deviation)
                    self.ui.pbRightEye.setValue(clamp100(right_val, EYE_SCALE), repaint=False)
                    right_eye_set = True
                if ch == jaw_owner and not jaw_set:
                    self.ui.pbJaw.setValue(clamp100(p.val_jaw_envelope, JAW_SCALE), repaint=False)
                    jaw_set = True

            elif p.filter_type == 'emg':
                emg_bars[ch].setValue(clamp100(p.val_emg_envelope, EMG_SCALE), repaint=False)
                emg_ch_envelopes[ch + 1] = p.val_emg_envelope

            elif p.filter_type == 'ecg' and not ecg_set:
                self.ui.pbECG.setValue(clamp100(p.val_ecg, ECG_SCALE), repaint=False)
                ecg_set = True

        if not focus_set:     self.ui.pbFocus.setValue(0, repaint=False)
        if not blink_set:     self.ui.pbBlink.setValue(0, repaint=False)
        if not left_eye_set:  self.ui.pbLeftEye.setValue(0, repaint=False)
        if not right_eye_set: self.ui.pbRightEye.setValue(0, repaint=False)
        if not jaw_set:       self.ui.pbJaw.setValue(0, repaint=False)
        if not ecg_set:       self.ui.pbECG.setValue(0, repaint=False)
        for i in range(6):
            if (i + 1) not in emg_ch_envelopes:
                emg_bars[i].setValue(0, repaint=False)

        # EMG combination rows update
        for lbl, bar, cmb, ch_a, ch_b in self._emg_combo_rows:
            if not bar.isVisible():
                continue
            val_a = emg_ch_envelopes.get(ch_a, 0.0)
            val_b = emg_ch_envelopes.get(ch_b, 0.0)
            combined = val_a + val_b
            bar.setValue(clamp100(combined, EMG_SCALE * 2), repaint=False)

        self._process_blink_jaw_detection()
        self._process_key_mappings()

    def _process_key_mappings(self):
        """Check each ThresholdBar's detected state, read its combo box mapping,
        and press/release the corresponding gamepad button."""
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
            (self.ui.pbEMG5,     self.ui.cmbEMG5),
            (self.ui.pbEMG6,     self.ui.cmbEMG6),
        ]
        for lbl, bar, cmb, _, _ in self._emg_combo_rows:
            bar_cmb_pairs.append((bar, cmb))

        # Key name -> 0..1. When several signals drive the same key, the strongest wins.
        key_values = {}
        for bar, cmb in bar_cmb_pairs:
            if not bar.isVisible():
                continue
            key_name = cmb.currentText()
            if key_name == "None":
                continue
            if self.controller_mode == MODE_DS4 and key_name in DS4_ANALOG_KEYS:
                val = self._analog_value(bar)
            else:
                val = 1.0 if bar.detected else 0.0
            key_values[key_name] = max(key_values.get(key_name, 0.0), val)

        # Double/triple blink and double jaw clench: full press for the flash time
        for key_name in self._detection_flash_keys:
            key_values[key_name] = 1.0

        self._signal_key_values = key_values
        self._send_outputs()

    def _send_outputs(self):
        """Merge the latest signal mappings with the IMU stick and send them to the
        gamepad and tester. Runs at 30 Hz for signals and on every IMU packet."""
        key_values = dict(self._signal_key_values)
        for key_name, val in self._imu_key_values().items():
            key_values[key_name] = max(key_values.get(key_name, 0.0), val)

        if self.gamepad:
            self.gamepad.apply(key_values)

        if self.test_dialog:
            self.test_dialog.update_values(key_values)

    def _imu_key_values(self):
        """IMU stick as stick-direction key values, e.g. {'LS Left': 0.4, 'LS Up': 0.7}."""
        if self.controller_mode != MODE_DS4 or not self.imu.calibrated:
            return {}
        prefix = IMU_STICK_PREFIX.get(self.cmb_imu.currentText())
        if prefix is None:
            return {}
        x, y = self.imu.stick
        return {
            f"{prefix} Right": max(0.0, x), f"{prefix} Left": max(0.0, -x),
            f"{prefix} Up": max(0.0, y), f"{prefix} Down": max(0.0, -y),
        }

    def _on_imu_samples(self, samples):
        self.imu.process(samples)
        # Push the stick straight away instead of waiting for the 30 Hz tick
        if self.is_connected and self._imu_key_values():
            self._send_outputs()

    @staticmethod
    def _analog_value(bar):
        """Map a bar onto 0..1 for an analog control. The range is the
        threshold +/- 50%: threshold 40 -> 20 is 0.0 and 60 is 1.0."""
        thresh = bar.threshold()
        if thresh <= 0:
            return 0.0      # threshold 0 disables the signal, same as for buttons
        low = thresh * 0.5
        high = min(100.0, thresh * 1.5)
        return max(0.0, min(1.0, (bar.value() - low) / (high - low)))

    def _reset_progress_bars(self):
        for bar in [self.ui.pbFocus, self.ui.pbBlink,
                    self.ui.pbLeftEye, self.ui.pbRightEye,
                    self.ui.pbJaw, self.ui.pbECG,
                    self.ui.pbEMG1, self.ui.pbEMG2, self.ui.pbEMG3,
                    self.ui.pbEMG4, self.ui.pbEMG5, self.ui.pbEMG6]:
            bar.setValue(0)
        for _, bar, _, _, _ in self._emg_combo_rows:
            bar.setValue(0)

    # Blink / Jaw multi-event detection

    def _process_blink_jaw_detection(self):
        """Feed latest envelope values into BlinkDetector / JawClenchDetector
        and trigger gamepad actions for double/triple events."""
        import time
        now_ms = int(time.time() * 1000)

        blink_thresh = (self.ui.pbBlink.threshold() / 100.0) * BLINK_SCALE
        jaw_thresh = (self.ui.pbJaw.threshold() / 100.0) * JAW_SCALE
        self.blink_detector.threshold = blink_thresh
        self.jaw_detector.threshold = jaw_thresh

        blink_sample = None
        jaw_sample = None
        for ch in range(self.num_channels):
            p = self.processors[ch]
            cb = getattr(self.ui, f'grpCh{ch + 1}')
            if not cb.isChecked():
                continue
            if p.filter_type == 'eeg' and blink_sample is None:
                blink_sample = p.val_blink_envelope
                if jaw_sample is None:
                    jaw_sample = p.val_jaw_envelope
            elif p.filter_type == 'eog' and jaw_sample is None:
                jaw_sample = p.val_jaw_envelope

        if blink_sample is not None:
            # Always process to maintain moving window state, then check if we should trigger
            event = self.blink_detector.process(blink_sample, now_ms)
            if event == 'double' and self.ui.grpDoubleBlink.isChecked():
                self._trigger_detection_action(self.ui.cmbDoubleBlink)
            elif event == 'triple' and self.ui.grpTripleBlink.isChecked():
                self._trigger_detection_action(self.ui.cmbTripleBlink)

        if jaw_sample is not None:
            event = self.jaw_detector.process(jaw_sample, now_ms)
            if event == 'double' and self.ui.grpDoubleJawClench.isChecked():
                self._trigger_detection_action(self.ui.cmbDoubleJawClench)

    def _trigger_detection_action(self, cmb):
        """Register a detection event to press a gamepad button for 500ms."""
        key_name = cmb.currentText()
        if key_name == 'None':
            return
            
        self._detection_flash_keys.add(key_name)
        print(f"Triggered detection for '{key_name}'")

        def _end_flash(k=key_name):
            self._detection_flash_keys.discard(k)
        QTimer.singleShot(500, _end_flash)

    def _create_gamepad(self):
        """Create the virtual controller for the current mode
        (staggered attempts on main thread to avoid UI freeze)."""
        if not HAS_VGAMEPAD:
            return
        import gc
        self._gamepad_gen += 1
        gen = self._gamepad_gen
        mode = self.controller_mode
        name = CONTROLLER_NAMES[mode]

        def _try_create_gamepad(attempt):
            # Stop if disconnected, or the mode changed since this attempt was scheduled
            if not self.is_connected or gen != self._gamepad_gen:
                return
            try:
                self.gamepad = create_pad(mode)
                self.ui.statusbar.showMessage(f"Virtual {name} controller connected", 4000)
                print(f"Virtual {name} gamepad created")
            except Exception as e:
                print(f"Gamepad attempt {attempt}/3 failed: {e}")
                self.gamepad = None
                gc.collect()
                if attempt < 3:
                    QTimer.singleShot(1000, lambda: _try_create_gamepad(attempt + 1))
                else:
                    self.ui.statusbar.showMessage(
                        f"Failed to create virtual {name} controller.", 6000)

        _try_create_gamepad(1)

    def _destroy_gamepad(self):
        """Safely tear down the virtual gamepad and free the ViGEmBus slot."""
        self._gamepad_gen += 1
        if self.gamepad is not None:
            import gc
            try:
                self.gamepad.release()
            except Exception as e:
                print(f"Warning: Virtual gamepad reset failed: {e}")
            try:
                del self.gamepad
            except Exception as e:
                print(f"Warning: Virtual gamepad deletion failed: {e}")
            self.gamepad = None
            gc.collect()
            print("Virtual gamepad released")

    def run(self):
        import atexit
        atexit.register(self._destroy_gamepad)
        self.ui.show()
        try:
            return self.app.exec()
        finally:
            self._destroy_gamepad()
            self.ble.shutdown()



if __name__ == "__main__":
    controller = NPGLiteSNES()
    sys.exit(controller.run())
