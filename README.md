# NPG Lite SNES

**Play video games using your mind and body.**

This app connects to the Neuro Playground (NPG) Lite over Bluetooth to read your body's electrical signals (EMG, EEG, EOG, ECG). It records these bio-potential signals like flexing a muscle, blinking your eyes, clenching your jaw, or focusing and converts them into standard Xbox controller button presses. This lets you play almost any PC game using just your mind and body.

## Features

- **Wireless:** Connects wirelessly to the NPG Lite device via Bluetooth Low Energy (BLE).
- **Up to 6 Bio-potential Signals:** Supports up to 6 channels processing muscles (EMG), brainwaves and blinks (EEG), eye movements (EOG), and heartbeats (ECG).
- **Custom Mapping:** A simple UI that lets you choose which action (like a "Double Jaw Clench") triggers which button (like the "A" button or "Right Trigger").
- **Zero Config:** The app automatically handles and installs the required virtual Xbox controller driver (`ViGEmBus`) on Windows for you.
- **Built-in Tester:** See your body signals light up in real-time and test your virtual controller before jumping into a game.

## How to use it

The easiest way to use this is to grab the pre-built standalone `.exe` file.

1. Download the latest `NPG Lite SNES.exe` from the Releases / GitHub Actions tab.
2. Double-click the file to open it.
3. If it's your first time running the app, it will ask for admin permissions to quickly install the `ViGEmBus` driver. This allows Windows to see your body as a real Xbox controller.
4. Turn on your NPG Lite device using the On/Off switch and click the "CONNECT" button on the app.
5. Check the boxes for the channels you are using, select the type of signal and map it to gamepad buttons using the dropdowns, and launch your game!

## Running from source (For developers)

If you want to view or edit the code directly:

1. Clone this repository to your PC.
2. Make sure you have Python installed.
3. Open a terminal in the project folder and create a virtual environment:
   ```bash
   python -m venv .venv
   ```
4. Activate the virtual environment:
   - **Windows:** `.venv\Scripts\activate`
   - **Mac/Linux:** `source .venv/bin/activate`
5. Install the required libraries:
   ```bash
   pip install -r requirements.txt
   ```
6. Run the main script:
   ```bash
   python main.py
   ```

### Building the .exe yourself
If you want to package your own version of the `.exe`:
```bash
pyinstaller build.spec
```
