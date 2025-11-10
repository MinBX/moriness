import cv2
import pyvirtualcam
from pynput import keyboard
import threading
import time
import sys
from collections import deque

# Optional: specify physical camera index via command line, default 0
cam_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0

# State variables
paused = False              # Freeze current frame
should_quit = False         # Exit program
recording = False           # Is recording
loop_playing = False        # Is playing loop
recorded_frames = deque()   # Recorded frame buffer
max_record_frames = 300     # Max recorded frames (10s @ 30fps)

# Combo key state tracking
current_keys = set()        # Currently pressed keys

def on_press(key):
    global paused, should_quit, recording, loop_playing, recorded_frames, current_keys
    
    # Add to current key set
    current_keys.add(key)
    
    try:
        # Check if Ctrl key is pressed (left or right)
        ctrl_pressed = (keyboard.Key.ctrl in current_keys or 
                       keyboard.Key.ctrl_l in current_keys or 
                       keyboard.Key.ctrl_r in current_keys)
        
        # Ctrl+F8: Freeze/Resume
        if ctrl_pressed and keyboard.Key.f8 in current_keys:
            paused = not paused
            if paused:
                loop_playing = False
            print(f"[CTRL+F8] {'Frozen' if paused else 'Resumed'}")
            return
        
        # Ctrl+F9: Start/Stop recording
        if ctrl_pressed and keyboard.Key.f9 in current_keys:
            if not recording:
                recording = True
                recorded_frames.clear()
                loop_playing = False
                paused = False
                print(f"[CTRL+F9] Recording started... (max {max_record_frames//30}s)")
            else:
                recording = False
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
    global paused, should_quit, recording, loop_playing, recorded_frames

    print(f"Opening physical camera index={cam_index}...")
    cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)  # DirectShow faster and more stable
    if not cap.isOpened():
        print(f"Error: Cannot open physical camera index={cam_index}")
        raise RuntimeError(f"Cannot open physical camera index={cam_index}")

    print("Camera opened, reading frame...")
    # Read first frame to determine resolution
    ok, frame = cap.read()
    if not ok or frame is None:
        print("Error: Cannot read frame from physical camera")
        cap.release()
        raise RuntimeError("Cannot read frame from physical camera")
    height, width = frame.shape[:2]
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
        cap.release()
        listener.stop()
        raise last_error
    
    try:
        with vcam:
            print("=" * 60)
            print("Virtual camera started:", vcam.device)
            print("=" * 60)
            print("Hotkeys:")
            print("  CTRL+F8  - Freeze/Resume current frame")
            print("  CTRL+F9  - Start/Stop recording video")
            print("  CTRL+F10 - Play recorded video")
            print("\nExit: Press Ctrl+C or close window")
            print("=" * 60)

            last_frame_rgb = frame[..., ::-1]  # BGR->RGB
            t_frame = time.time()
            loop_frame_index = 0      # Current frame index for loop playback
            loop_direction = 1        # Loop direction: 1=forward, -1=backward

            while not should_quit:
                frame_to_send = None
                
                if loop_playing and len(recorded_frames) > 0:
                    # Ping-pong loop playback mode
                    frame_to_send = recorded_frames[loop_frame_index]
                    
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
                    
                elif paused:
                    # Freeze mode - use last frame
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
        cap.release()
        listener.stop()
        raise

    listener.join()
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
