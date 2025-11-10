# Moriness 

Virtual camera tool for freezing frames, recording, and looping video.

## Requirements

- Python 3.7+
- OBS Studio (with Virtual Camera)

## Installation

Install required Python packages:

```bash
pip install opencv-python pyvirtualcam pynput
```

## Usage

1. Start OBS Virtual Camera in OBS Studio
2. Run the script:
   ```bash
   python freeze_cam.py
   ```

## Hotkeys

- **Ctrl+F8** - Freeze/Resume current frame
- **Ctrl+F9** - Start/Stop recording (max 10s)
- **Ctrl+F10** - Play recorded video (ping-pong loop)
- **Ctrl+C** - Exit

---

Just Fun