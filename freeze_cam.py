import cv2
import pyvirtualcam
from pynput import keyboard
import threading
import time
import sys
import os
import argparse
from collections import deque


def parse_args():
    parser = argparse.ArgumentParser(description='Moriness virtual camera tool')
    parser.add_argument('cam_index', nargs='?', type=int, default=0,
                        help='Physical camera index (default: 0). Ignored when --video is set.')
    parser.add_argument('--video', type=str, default=None,
                        help='Play a saved video file instead of opening a physical camera '
                             '(use this on machines without a webcam).')
    parser.add_argument('--output-dir', type=str, default='.',
                        help='Directory where recordings are saved (default: current directory).')
    return parser.parse_args()


args = parse_args()

# State variables
paused = False              # Freeze current frame
should_quit = False         # Exit program
recording = False           # Is recording
loop_playing = False        # Is playing loop
recorded_frames = deque()   # Recorded frame buffer (RGB)
max_record_frames = 300     # Max recorded frames (10s @ 30fps)
save_pending = False        # Set when recording stops; main loop flushes to disk
video_mode = False          # Set in main() when --video is supplied

# Combo key state tracking
current_keys = set()        # Currently pressed keys


def save_video_async(frames, width, height, fps, output_dir):
    """Write the recorded frames to an mp4 file on a background thread."""
    if not frames:
        print("[SAVE] No frames to save")
        return

    os.makedirs(output_dir, exist_ok=True)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    filename = os.path.join(output_dir, f'recording_{timestamp}.mp4')

    def _write():
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(filename, fourcc, fps, (width, height))
        if not writer.isOpened():
            print(f"[SAVE] Error: Could not open video writer for {filename}")
            return
        try:
            for frame_rgb in frames:
                writer.write(frame_rgb[..., ::-1])  # RGB -> BGR for OpenCV writer
            print(f"[SAVE] Saved {len(frames)} frames ({len(frames)/fps:.1f}s) -> {filename}")
        finally:
            writer.release()

    threading.Thread(target=_write, daemon=True).start()


def load_video_file(path):
    """Load every frame of a video file into memory as RGB ndarrays."""
    print(f"Loading video file: {path}")
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video file: {path}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    frames = []
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        frames.append(frame[..., ::-1].copy())  # BGR -> RGB
    cap.release()

    if not frames:
        raise RuntimeError(f"No frames could be read from {path}")

    print(f"Loaded {len(frames)} frames, {width}x{height} @ {src_fps:.1f}fps")
    return frames, width, height


def on_press(key):
    global paused, should_quit, recording, loop_playing, recorded_frames, current_keys, save_pending

    # Add to current key set
    current_keys.add(key)

    try:
        # Check if Ctrl key is pressed (left or right)
        ctrl_pressed = (keyboard.Key.ctrl in current_keys or
                       keyboard.Key.ctrl_l in current_keys or
                       keyboard.Key.ctrl_r in current_keys)

        # Ctrl+F8: Freeze/Resume (disabled in video playback mode — no live source)
        if ctrl_pressed and keyboard.Key.f8 in current_keys:
            if video_mode:
                print("[CTRL+F8] Disabled in --video mode")
                return
            paused = not paused
            if paused:
                loop_playing = False
            print(f"[CTRL+F8] {'Frozen' if paused else 'Resumed'}")
            return

        # Ctrl+F9: Start/Stop recording (disabled in video playback mode — would wipe loaded clip)
        if ctrl_pressed and keyboard.Key.f9 in current_keys:
            if video_mode:
                print("[CTRL+F9] Disabled in --video mode")
                return
            if not recording:
                recording = True
                recorded_frames.clear()
                loop_playing = False
                paused = False
                print(f"[CTRL+F9] Recording started... (max {max_record_frames//30}s)")
            else:
                recording = False
                save_pending = True
                print(f"[CTRL+F9] Recording stopped, {len(recorded_frames)} frames ({len(recorded_frames)/30:.1f}s)")
            return

        # Ctrl+F10: Play/Stop loop
        if ctrl_pressed and keyboard.Key.f10 in current_keys:
            if len(recorded_frames) == 0:
                print("[CTRL+F10] No recorded video, please press CTRL+F9 to record first")
            else:
                loop_playing = not loop_playing
                if loop_playing:
                    recording = False
                    paused = False
                    print(f"[CTRL+F10] Playing ({len(recorded_frames)/30:.1f}s)")
                else:
                    print("[CTRL+F10] Stopped")
            return

    except Exception:
        pass


def on_release(key):
    """Remove key from set when released"""
    global current_keys
    try:
        current_keys.discard(key)
    except Exception:
        pass


def main():
    global paused, should_quit, recording, loop_playing, recorded_frames, save_pending, video_mode

    cap = None
    video_mode = args.video is not None

    if video_mode:
        # Playback-only mode: no physical camera, auto-start the loop on the loaded clip
        frames, width, height = load_video_file(args.video)
        recorded_frames.extend(frames)
        loop_playing = True
        last_frame_rgb = frames[0]
    else:
        print(f"Opening physical camera index={args.cam_index}...")
        cap = cv2.VideoCapture(args.cam_index, cv2.CAP_DSHOW)  # DirectShow faster and more stable
        if not cap.isOpened():
            print(f"Error: Cannot open physical camera index={args.cam_index}")
            raise RuntimeError(f"Cannot open physical camera index={args.cam_index}")

        print("Camera opened, reading frame...")
        ok, frame = cap.read()
        if not ok or frame is None:
            print("Error: Cannot read frame from physical camera")
            cap.release()
            raise RuntimeError("Cannot read frame from physical camera")
        height, width = frame.shape[:2]
        last_frame_rgb = frame[..., ::-1]
        print(f"Camera resolution: {width}x{height}")

    # Start global hotkey listener
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    print("Starting virtual camera...")
    # Try different virtual camera backends
    backends_to_try = ["obs", "unitycapture"]
    vcam = None
    last_error = None

    for backend in backends_to_try:
        try:
            print(f"Trying '{backend}' backend...")
            vcam = pyvirtualcam.Camera(width=width, height=height, fps=30, backend=backend)
            print(f"Successfully using '{backend}' backend")
            break
        except Exception as e:
            print(f"'{backend}' backend failed: {e}")
            last_error = e
            continue

    if vcam is None:
        print("\n" + "=" * 60)
        print("Error: Cannot start virtual camera")
        print("=" * 60)
        if cap is not None:
            cap.release()
        listener.stop()
        raise last_error

    try:
        with vcam:
            print("=" * 60)
            print("Virtual camera started:", vcam.device)
            print("=" * 60)
            if video_mode:
                print(f"Playback mode: looping {args.video}")
                print("Hotkeys:")
                print("  CTRL+F10 - Pause/Resume loop playback")
            else:
                print("Hotkeys:")
                print("  CTRL+F8  - Freeze/Resume current frame")
                print("  CTRL+F9  - Start/Stop recording (auto-saved as mp4 on stop)")
                print("  CTRL+F10 - Play recorded video")
            print("\nExit: Press Ctrl+C or close window")
            print("=" * 60)

            t_frame = time.time()
            loop_frame_index = 0      # Current frame index for loop playback
            loop_direction = 1        # Loop direction: 1=forward, -1=backward

            while not should_quit:
                # Flush any finished recording to disk
                if save_pending:
                    save_pending = False
                    save_video_async(list(recorded_frames), width, height, 30, args.output_dir)

                frame_to_send = None

                if loop_playing and len(recorded_frames) > 0:
                    # Ping-pong loop playback mode
                    frame_to_send = recorded_frames[loop_frame_index]
                    last_frame_rgb = frame_to_send  # remember current frame so pause freezes here

                    # Ping-pong: reverse playback when reaching end
                    loop_frame_index += loop_direction

                    # Reached end, reverse direction
                    if loop_frame_index >= len(recorded_frames) - 1:
                        loop_direction = -1
                        loop_frame_index = len(recorded_frames) - 1
                    # Reached start, reverse direction
                    elif loop_frame_index <= 0:
                        loop_direction = 1
                        loop_frame_index = 0

                elif paused or video_mode:
                    # Freeze mode (camera) or paused playback (video mode)
                    frame_to_send = last_frame_rgb

                else:
                    # Normal mode - read from camera
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        time.sleep(0.01)
                        continue

                    frame_rgb = frame[..., ::-1]  # BGR->RGB
                    last_frame_rgb = frame_rgb

                    # If recording, save frame
                    if recording:
                        recorded_frames.append(frame_rgb.copy())
                        if len(recorded_frames) >= max_record_frames:
                            recording = False
                            save_pending = True
                            print(f"[CTRL+F9] Reached max recording time ({max_record_frames//30}s), auto-stopped")

                    frame_to_send = frame_rgb

                # Send frame to virtual camera
                if frame_to_send is not None:
                    vcam.send(frame_to_send)

                # Throttle to target frame rate
                now = time.time()
                dt = now - t_frame
                wait = max(0.0, (1.0 / 30.0) - dt)
                time.sleep(wait)
                t_frame = time.time()

    except Exception as e:
        print(f"Error: Virtual camera startup failed - {e}")
        if cap is not None:
            cap.release()
        listener.stop()
        raise

    listener.join()
    if cap is not None:
        cap.release()
    print("Exited.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nUser interrupted")
    except Exception as e:
        print(f"\nProgram exited with error: {e}")
        import traceback
        traceback.print_exc()
