# Windows scheduling helpers. All are no-ops on other platforms.
#
# Windows 11 "power throttling" (EcoQoS) slows down processes whose windows are
# not in focus: their timers are coarsened and they may be moved to efficiency
# cores. The app runs in the background while a game is focused, and the overlay
# never has focus, so both opt out.

import ctypes
import sys

PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
ProcessPowerThrottling = 4

NORMAL_PRIORITY_CLASS = 0x20


def _k32():
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetProcessInformation.restype = wintypes.BOOL
    return k32


def disable_power_throttling():
    """Keep full CPU speed and precise timers even when not in focus."""
    if sys.platform != 'win32':
        return False
    try:
        from ctypes import wintypes

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        k32 = _k32()
        state = PROCESS_POWER_THROTTLING_STATE(
            1, PROCESS_POWER_THROTTLING_EXECUTION_SPEED | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION, 0)
        return bool(k32.SetProcessInformation(k32.GetCurrentProcess(), ProcessPowerThrottling,
                                              ctypes.byref(state), ctypes.sizeof(state)))
    except Exception:
        return False


def set_normal_priority():
    if sys.platform != 'win32':
        return
    try:
        k32 = _k32()
        k32.SetPriorityClass(k32.GetCurrentProcess(), NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
