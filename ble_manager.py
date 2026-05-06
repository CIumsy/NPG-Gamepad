# Qt wrapper around ble_connection — runs BLE in a background thread,
# emits Qt signals so the UI can react safely from the main thread.

import asyncio
import threading

from PySide6.QtCore import QObject, Signal
from ble_connection import NPGConnection


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
