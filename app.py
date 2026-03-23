"""
NPG Lite to Xbox Controller Bridge
Connects to NPG Lite via BLE and maps channel peaks to Xbox buttons
"""

import asyncio
from bleak import BleakClient, BleakScanner
import vgamepad as vg

# NPG Lite BLE Configuration
NPG_SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
NPG_DATA_CHAR_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"
NPG_CONTROL_CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

# Signal processing parameters
ENVELOPE_MIN = 100   # Envelope value where joystick starts moving
ENVELOPE_MAX = 700   # Envelope value where joystick reaches max
JOYSTICK_MAX = 32767 # Max positive X-axis value for Xbox joystick
WINDOW_SIZE = 50
COOLDOWN_SAMPLES = 25

# Band-Stop Butterworth IIR digital filter
# Sampling rate: 500.0 Hz, frequency: [48.0, 52.0] Hz
# Filter is order 2, implemented as second-order sections (biquads)
# Reference: https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.butter.html

class Notch:
    def __init__(self):
        # Initialize state variables for each biquad section
        self.z1_0 = 0.0
        self.z2_0 = 0.0
        self.z1_1 = 0.0
        self.z2_1 = 0.0

    def process(self, input_sample):
        """Process a single sample through the filter"""
        output = input_sample

        # Biquad section 0
        x = output - (-1.56858163 * self.z1_0) - (0.96424138 * self.z2_0)
        output = 0.96508099 * x + -1.56202714 * self.z1_0 + 0.96508099 * self.z2_0
        self.z2_0 = self.z1_0
        self.z1_0 = x

        # Biquad section 1
        x = output - (-1.61100358 * self.z1_1) - (0.96592171 * self.z2_1)
        output = 1.00000000 * x + -1.61854514 * self.z1_1 + 1.00000000 * self.z2_1
        self.z2_1 = self.z1_1
        self.z1_1 = x

        return output

    def reset(self):
        """Reset filter state variables"""
        self.z1_0 = 0.0
        self.z2_0 = 0.0
        self.z1_1 = 0.0
        self.z2_1 = 0.0
        
# High-Pass Butterworth IIR digital filter
# Sampling rate: 500.0 Hz, frequency: 70.0 Hz
# Filter is order 2, implemented as second-order sections (biquads)
# Reference: https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.butter.html

class EMG:
    def __init__(self):
        # Initialize state variables for each biquad section
        self.z1_0 = 0.0
        self.z2_0 = 0.0

    def process(self, input_sample):
        """Process a single sample through the filter"""
        output = input_sample

        # Biquad section 0
        x = output - (-0.82523238 * self.z1_0) - (0.29463653 * self.z2_0)
        output = 0.52996723 * x + -1.05993445 * self.z1_0 + 0.52996723 * self.z2_0
        self.z2_0 = self.z1_0
        self.z1_0 = x

        return output

    def reset(self):
        """Reset filter state variables"""
        self.z1_0 = 0.0
        self.z2_0 = 0.0

class Envelope:
    def __init__(self, buffer_size=WINDOW_SIZE):
        self.buffer_size = buffer_size
        self.circular_buffer = [0] * buffer_size
        self.data_index = 0
        self.sum = 0
    
    def process(self, abs_emg):
        self.sum -= self.circular_buffer[self.data_index]
        self.sum += abs_emg
        self.circular_buffer[self.data_index] = abs_emg
        self.data_index = (self.data_index + 1) % self.buffer_size
        return (self.sum / self.buffer_size) * 2

    def reset(self):
        self.circular_buffer = [0] * self.buffer_size
        self.data_index = 0
        self.sum = 0

class NPGXboxBridge:
    def __init__(self):
        self.gamepad = None
        self.client = None
        self.num_channels = 3
        self.sample_count = 0
        
        # Initialize filters for 3 channels
        self.notch_filters = [Notch() for _ in range(3)]
        self.emg_filters = [EMG() for _ in range(3)]
        self.envelopes = [Envelope() for _ in range(3)]
        
        # 3-channel mapping:
        # Ch 1 -> Left Joystick positive X-axis (analog)
        # Ch 2 -> Right Joystick positive X-axis (analog)
        # Ch 3 -> Start Button (digital, threshold-based)
        
    def envelope_to_joystick(self, envelope_val):
        """Map envelope value (200-500) to joystick X-axis (0-32767)"""
        if envelope_val < ENVELOPE_MIN:
            return 0
        if envelope_val > ENVELOPE_MAX:
            return JOYSTICK_MAX
        # Linear mapping: 200->0, 500->32767
        return int(((envelope_val - ENVELOPE_MIN) / (ENVELOPE_MAX - ENVELOPE_MIN)) * JOYSTICK_MAX)
        
    async def find_npg_device(self):
        print("🔍 Scanning for NPG Lite device...")
        devices = await BleakScanner.discover(timeout=10.0)
        
        for device in devices:
            if device.name and "NPG-Lite" in device.name:
                print(f"✅ Found NPG Lite: {device.name} ({device.address})")
                self.num_channels = 3
                print("📊 3-channel mode")
                return device.address
        
        print("❌ NPG Lite device not found")
        return None
    
    def parse_data_packet(self, data):
        try:
            if len(data) < 10:
                return None
            
            channels = []
            offset = 0
            
            while offset + 3 <= len(data):
                counter = data[offset]
                offset += 1
                
                num_bio_channels = self.num_channels
                
                for i in range(num_bio_channels):
                    if offset + 2 <= len(data):
                        high_byte = data[offset]
                        low_byte = data[offset + 1]
                        value = (high_byte << 8) | low_byte
                        channels.append(value)
                        offset += 2
                    else:
                        break
                
                if len(channels) >= num_bio_channels:
                    return channels[:num_bio_channels]
            
            return None
        except Exception as e:
            print(f"Parse error: {e}")
            return None
    
    def process_channel(self, channel_idx, value):
        """Signal chain: Raw -> Notch -> EMG -> Abs -> Envelope"""
        notch_output = self.notch_filters[channel_idx].process(value)
        emg_output = self.emg_filters[channel_idx].process(notch_output)
        abs_emg = abs(emg_output)
        envelope_val = self.envelopes[channel_idx].process(abs_emg)
        return envelope_val
    
    def update_gamepad(self, envelopes):
        """Update gamepad with analog joystick values and digital Start button"""
        # Ch1 -> Left Joystick positive X-axis (analog mapping)
        left_x = self.envelope_to_joystick(envelopes[0])
        self.gamepad.left_joystick(x_value=left_x, y_value=0)
        
        # Ch2 -> Right Joystick positive X-axis (analog mapping)
        right_x = self.envelope_to_joystick(envelopes[1])
        self.gamepad.right_joystick(x_value=right_x, y_value=0)
        
        # Ch3 -> Start Button (digital: press if envelope > ENVELOPE_MIN)
        if envelopes[2] > ENVELOPE_MIN:
            self.gamepad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_START)
        else:
            self.gamepad.release_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_START)
        
        self.gamepad.update()
        
        # Debug: print every 100 samples
        if self.sample_count % 100 == 0:
            print(f"[Ch1] Env={envelopes[0]:.1f} LX={left_x} | [Ch2] Env={envelopes[1]:.1f} RX={right_x} | [Ch3] Env={envelopes[2]:.1f} Start={'ON' if envelopes[2] > ENVELOPE_MIN else 'off'}")
    
    def notification_handler(self, sender, data):
        channels = self.parse_data_packet(data)
        if channels:
            self.sample_count += 1
            
            # Process all 3 channels through filter chain
            env_values = []
            for i, value in enumerate(channels[:3]):
                env_val = self.process_channel(i, value)
                env_values.append(env_val)
            
            # Update gamepad with analog values
            if len(env_values) == 3:
                self.update_gamepad(env_values)
    
    async def run(self):
        address = await self.find_npg_device()
        if not address:
            return
        
        print("🎮 Creating virtual Xbox 360 controller...")
        self.gamepad = vg.VX360Gamepad()
        print("✅ Virtual controller ready")
        
        print(f"📡 Connecting to NPG Lite at {address}...")
        try:
            async with BleakClient(address, timeout=20.0) as client:
                self.client = client
                print("✅ Connected to NPG Lite")
                
                print("📤 Sending START command...")
                await client.write_gatt_char(NPG_CONTROL_CHAR_UUID, b"START")
                await asyncio.sleep(0.5)
                
                await client.start_notify(NPG_DATA_CHAR_UUID, self.notification_handler)
                print("📊 Receiving data... Press Ctrl+C to stop")
                print(f"Analog mapping: Envelope {ENVELOPE_MIN}-{ENVELOPE_MAX} → Joystick 0-{JOYSTICK_MAX}")
                print(f"Ch1 → Left Joystick X+ | Ch2 → Right Joystick X+ | Ch3 → Start Button")
                print("-" * 50)
                
                while True:
                    await asyncio.sleep(1)
                    
        except KeyboardInterrupt:
            print("\n⏹️  Stopping...")
            if self.client:
                await self.client.write_gatt_char(NPG_CONTROL_CHAR_UUID, b"STOP")
        except Exception as e:
            print(f"❌ Error: {e}")
        finally:
            if self.gamepad:
                del self.gamepad
                print("✅ Virtual controller removed")

async def main():
    print("=" * 50)
    print("NPG Lite to Xbox Controller Bridge")
    print("=" * 50)
    
    bridge = NPGXboxBridge()
    await bridge.run()

if __name__ == "__main__":
    asyncio.run(main())