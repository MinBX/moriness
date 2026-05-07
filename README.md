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
   python freeze_cam.py                       # use default camera (index 0)
   python freeze_cam.py 1                     # use camera index 1
   python freeze_cam.py --video clip.mp4      # play a saved clip (no webcam needed)
   python freeze_cam.py --output-dir ./clips  # change where recordings are saved
   ```

Recordings are auto-saved as `recording_<YYYYMMDD_HHMMSS>.mp4` in the output directory
when you stop recording (or when the 10s cap is hit). Copy the file to another machine
and run with `--video <file>` to reproduce the same virtual-camera output without a webcam.

## Hotkeys

- **Ctrl+F8** - Freeze/Resume current frame *(camera mode only)*
- **Ctrl+F9** - Start/Stop recording, max 10s *(camera mode only; auto-saved as mp4 on stop)*
- **Ctrl+F10** - Play recorded video (ping-pong loop) / Pause loop in `--video` mode
- **Ctrl+C** - Exit

---

Just Fun