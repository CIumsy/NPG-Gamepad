# ViGEmBus driver check/install and vgamepad setup

import sys
import os


def resource_path(relative_path):
    """Get path to bundled resource. Works in both dev and PyInstaller exe."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


VIGEMBUS_FILENAME = 'ViGEmBus_1.22.0_x64_x86_arm64.exe'
VIGEMBUS_URL = f'https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/{VIGEMBUS_FILENAME}'


def _download_vigembus_installer(dest):
    import urllib.request
    print('Downloading ViGEmBus installer...')
    part_file = dest + '.part'
    try:
        urllib.request.urlretrieve(VIGEMBUS_URL, part_file)
        os.replace(part_file, dest)
    except Exception as e:
        print(f'ViGEmBus installer download failed: {e}')
        if os.path.exists(part_file):
            os.remove(part_file)


def ensure_vigembus():
    """Install ViGEmBus driver if not already installed and running."""
    import subprocess
    result = subprocess.run(['sc', 'query', 'ViGEmBus'], capture_output=True, text=True)
    if result.returncode == 0 and 'RUNNING' in result.stdout:
        return True
    installer = resource_path(VIGEMBUS_FILENAME)
    if not os.path.exists(installer) and not hasattr(sys, '_MEIPASS'):
        _download_vigembus_installer(installer)
    if not os.path.exists(installer):
        print('ViGEmBus installer not found')
        return False
    print('Installing ViGEmBus driver (admin required)...')
    try:
        import ctypes, time
        ctypes.windll.shell32.ShellExecuteW(None, "runas", installer, "/quiet /norestart", None, 1)
        # Poll for service availability (max ~30 seconds)
        for _ in range(30):
            time.sleep(1)
            result = subprocess.run(['sc', 'query', 'ViGEmBus'], capture_output=True, text=True)
            if result.returncode == 0:
                subprocess.run(['sc', 'start', 'ViGEmBus'], capture_output=True)
                time.sleep(1)
                # Verify running
                result = subprocess.run(['sc', 'query', 'ViGEmBus'], capture_output=True, text=True)
                if 'RUNNING' in result.stdout:
                    print('ViGEmBus installed and started successfully')
                    return True
        print('ViGEmBus installation timed out')
        return False
    except Exception as e:
        print(f'ViGEmBus install failed: {e}')
        return False


# Try to load vgamepad (needs ViGEmBus driver)
ensure_vigembus()
try:
    import vgamepad as vg
    HAS_VGAMEPAD = True
except Exception:
    HAS_VGAMEPAD = False
    print("vgamepad not available — virtual gamepad disabled")

# Controller modes

MODE_XBOX = 'xbox'
MODE_DS4 = 'ds4'

# Keys shown in the mapping drop-downs for each mode
XBOX_KEYS = [
    "A", "B", "X", "Y", "Dpad Up", "Dpad Down",
    "Dpad Left", "Dpad Right", "L", "R", "Start",
]

DS4_KEYS = [
    "Cross", "Circle", "Square", "Triangle",
    "Dpad Up", "Dpad Down", "Dpad Left", "Dpad Right",
    "L1", "R1", "L2", "R2", "L3", "R3",
    "Share", "Options", "PS", "Touchpad",
    "LS Up", "LS Down", "LS Left", "LS Right",
    "RS Up", "RS Down", "RS Left", "RS Right",
]

# DS4 keys driven by a 0..1 analog value (triggers and stick directions)
DS4_ANALOG_KEYS = {
    "L2", "R2",
    "LS Up", "LS Down", "LS Left", "LS Right",
    "RS Up", "RS Down", "RS Left", "RS Right",
}

KEYS_FOR_MODE = {MODE_XBOX: XBOX_KEYS, MODE_DS4: DS4_KEYS}
CONTROLLER_NAMES = {MODE_XBOX: "Xbox 360", MODE_DS4: "DualShock 4"}

# Map SNES button names to vgamepad constants
SNES_TO_XUSB = {}
DS4_BUTTON_MAP = {}
DS4_SPECIAL_MAP = {}
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
        "L":          "LT",
        "R":          "RT",
    }
    DS4_BUTTON_MAP = {
        "Cross":    vg.DS4_BUTTONS.DS4_BUTTON_CROSS,
        "Circle":   vg.DS4_BUTTONS.DS4_BUTTON_CIRCLE,
        "Square":   vg.DS4_BUTTONS.DS4_BUTTON_SQUARE,
        "Triangle": vg.DS4_BUTTONS.DS4_BUTTON_TRIANGLE,
        "L1":       vg.DS4_BUTTONS.DS4_BUTTON_SHOULDER_LEFT,
        "R1":       vg.DS4_BUTTONS.DS4_BUTTON_SHOULDER_RIGHT,
        "L3":       vg.DS4_BUTTONS.DS4_BUTTON_THUMB_LEFT,
        "R3":       vg.DS4_BUTTONS.DS4_BUTTON_THUMB_RIGHT,
        "Share":    vg.DS4_BUTTONS.DS4_BUTTON_SHARE,
        "Options":  vg.DS4_BUTTONS.DS4_BUTTON_OPTIONS,
    }
    DS4_SPECIAL_MAP = {
        "PS":       vg.DS4_SPECIAL_BUTTONS.DS4_SPECIAL_BUTTON_PS,
        "Touchpad": vg.DS4_SPECIAL_BUTTONS.DS4_SPECIAL_BUTTON_TOUCHPAD,
    }


def _axis(values, negative, positive):
    """Combine two opposite directions into one axis value (-1..1).
    Opposite directions held together cancel each other out."""
    return max(-1.0, min(1.0, values.get(positive, 0.0) - values.get(negative, 0.0)))


class XboxPad:
    """Virtual Xbox 360 controller. Every key is digital (on/off)."""

    def __init__(self):
        self.pad = vg.VX360Gamepad()

    def apply(self, values):
        """values: {key name: 0..1}. A key counts as pressed when > 0."""
        self.pad.reset()
        for key, val in values.items():
            if val <= 0:
                continue
            xusb = SNES_TO_XUSB.get(key)
            if xusb == "LT":
                self.pad.left_trigger_float(value_float=1.0)
            elif xusb == "RT":
                self.pad.right_trigger_float(value_float=1.0)
            elif xusb:
                self.pad.press_button(xusb)
        self.pad.update()

    def release(self):
        self.pad.reset()
        self.pad.update()


class DS4Pad:
    """Virtual DualShock 4 controller with all buttons, triggers and sticks."""

    # (up, down, left, right) -> hat direction
    _DPAD = {
        (True,  False, False, False): 'DS4_BUTTON_DPAD_NORTH',
        (True,  False, False, True):  'DS4_BUTTON_DPAD_NORTHEAST',
        (False, False, False, True):  'DS4_BUTTON_DPAD_EAST',
        (False, True,  False, True):  'DS4_BUTTON_DPAD_SOUTHEAST',
        (False, True,  False, False): 'DS4_BUTTON_DPAD_SOUTH',
        (False, True,  True,  False): 'DS4_BUTTON_DPAD_SOUTHWEST',
        (False, False, True,  False): 'DS4_BUTTON_DPAD_WEST',
        (True,  False, True,  False): 'DS4_BUTTON_DPAD_NORTHWEST',
    }

    def __init__(self):
        self.pad = vg.VDS4Gamepad()

    def apply(self, values):
        """values: {key name: 0..1}. Buttons count as pressed when > 0,
        triggers and stick directions use the value as the analog amount."""
        self.pad.reset()

        for key, flag in DS4_BUTTON_MAP.items():
            if values.get(key, 0.0) > 0:
                self.pad.press_button(flag)
        for key, flag in DS4_SPECIAL_MAP.items():
            if values.get(key, 0.0) > 0:
                self.pad.press_special_button(flag)

        # Triggers: analog value plus the digital L2/R2 bit, like a real DS4
        l2 = values.get("L2", 0.0)
        r2 = values.get("R2", 0.0)
        self.pad.left_trigger_float(value_float=l2)
        self.pad.right_trigger_float(value_float=r2)
        if l2 > 0:
            self.pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_TRIGGER_LEFT)
        if r2 > 0:
            self.pad.press_button(vg.DS4_BUTTONS.DS4_BUTTON_TRIGGER_RIGHT)

        # D-pad: opposite directions cancel, two neighbours make a diagonal
        up, down = values.get("Dpad Up", 0) > 0, values.get("Dpad Down", 0) > 0
        left, right = values.get("Dpad Left", 0) > 0, values.get("Dpad Right", 0) > 0
        if up and down:
            up = down = False
        if left and right:
            left = right = False
        hat = self._DPAD.get((up, down, left, right), 'DS4_BUTTON_DPAD_NONE')
        self.pad.directional_pad(getattr(vg.DS4_DPAD_DIRECTIONS, hat))

        # Sticks: on a DS4 the Y axis grows downwards
        self.pad.left_joystick_float(
            x_value_float=_axis(values, "LS Left", "LS Right"),
            y_value_float=_axis(values, "LS Up", "LS Down"))
        self.pad.right_joystick_float(
            x_value_float=_axis(values, "RS Left", "RS Right"),
            y_value_float=_axis(values, "RS Up", "RS Down"))

        self.pad.update()

    def release(self):
        self.pad.reset()
        self.pad.update()


def create_pad(mode):
    """Create the virtual controller for the given mode."""
    return DS4Pad() if mode == MODE_DS4 else XboxPad()
