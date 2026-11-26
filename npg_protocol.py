"""
NPG Lite BLE Protocol
=====================
Constants, commands, and data parsing for NeuroPlayground Lite devices.

Data Packet Format (from firmware):
    - 10 samples per BLE notification (BLOCK_COUNT = 10)
    - Each sample: 1 counter byte + num_bio-amp_channels × 2 bytes (big-endian)
    - 3CH device: 3 bio-amp channels → 7 bytes/sample → 70 bytes/packet
    - 6CH device: 6 bio-amp channels → 13 bytes/sample → 130 bytes/packet
    - Battery level is sent separately via BATTERY_CHAR_UUID (1 byte, 0-100%)
    - Counter wraps around 0-255

Device Naming:
    - 3-channel: "NPG-Lite-3CH:XX:XX"
    - 6-channel: "NPG-Lite-6CH:XX:XX"
"""

# BLE UUIDs 
SERVICE_UUID     = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
DATA_CHAR_UUID   = "beb5483e-36e1-4688-b7f5-ea07361b26a8"     # Notify: ADC data
CONTROL_CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"     # Read/Write/Notify: commands
BATTERY_CHAR_UUID = "f633d0ec-46b4-43c1-a39f-1ca06d0602e1"    # Notify: battery %
IMU_CHAR_UUID     = "5a153fa9-7be0-400c-8ef8-d84502b31c4d"    # Notify: MPU6050 accel + gyro (optional)

# Commands 
CMD_START  = b"START"
CMD_STOP   = b"STOP"
CMD_WHORU  = b"WHORU"
CMD_STATUS = b"STATUS"

# Data Format Constants 
BLOCK_COUNT = 10      # Samples per BLE notification
SAMPLE_RATE = 500     # Hz per channel
ADC_RESOLUTION = 4096 # 12-bit ADC (0–4095)

# IMU (MPU6050) stream: 1 counter byte + ax, ay, az, gx, gy, gz (int16, big-endian)
IMU_SAMPLE_LEN = 13
IMU_SAMPLE_RATE = 200          # Hz (firmware sends 2 samples per notification, 100 per second)
IMU_ACCEL_LSB_PER_G = 16384.0  # +/-2 g range
IMU_GYRO_LSB_PER_DPS = 65.5    # +/-500 deg/s range




def detect_channels_from_name(device_name: str) -> int | None:
    """
    Detect number of bio-amp channels from BLE device name.

    Returns:
        3, 6, or None if not determinable.
    """
    if not device_name:
        return None
    if "6CH" in device_name.upper():
        return 6
    if "3CH" in device_name.upper():
        return 3
    return None




def parse_packet(data: bytes, num_channels: int) -> list[dict]:
    """
    Parse a BLE notification packet into individual samples.

    Args:
        data:         Raw BLE notification bytes.
        num_channels: Number of bio-amp channels (3 or 6).

    Returns:
        List of sample dicts:
            {
                'counter':  int (0–255),
                'channels': list[int] (12-bit values, length = num_channels)
            }
    """
    sample_len = 1 + num_channels * 2
    samples = []
    offset = 0

    while offset + sample_len <= len(data):
        counter = data[offset]
        offset += 1

        channels = []
        for _ in range(num_channels):
            value = (data[offset] << 8) | data[offset + 1]
            channels.append(value)
            offset += 2

        samples.append({
            'counter': counter,
            'channels': channels,
        })

    return samples


def parse_imu_packet(data: bytes) -> list[tuple]:
    """
    Parse an IMU notification into samples.

    Returns:
        List of (counter, ax, ay, az, gx, gy, gz) tuples with signed raw values.
    """
    samples = []
    for offset in range(0, len(data) - IMU_SAMPLE_LEN + 1, IMU_SAMPLE_LEN):
        vals = [int.from_bytes(data[offset + 1 + i * 2: offset + 3 + i * 2], 'big', signed=True)
                for i in range(6)]
        samples.append((data[offset], *vals))
    return samples
