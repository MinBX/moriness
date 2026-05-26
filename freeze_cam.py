import cv2
import pyvirtualcam
from pynput import keyboard
import threading
import time
import sys
import os
import argparse
import re
import shutil
from collections import deque
import ctypes
from ctypes import wintypes


def parse_args():
    parser = argparse.ArgumentParser(description='Moriness virtual camera tool')
    parser.add_argument('cam_index', nargs='?', type=int, default=0,
                        help='Physical camera index (default: 0). Ignored when --video is set.')
    parser.add_argument('--video', type=str, default=None,
                        help='Play a saved video file instead of opening a physical camera.')
    parser.add_argument('--output-dir', type=str, default='.',
                        help='Directory where recordings are saved (default: current directory).')
    parser.add_argument('--cam-off', nargs='+', default=[], metavar='HH:MM',
                        help='Schedule camera OFF at these times (e.g. 14:30 15:00)')
    parser.add_argument('--cam-on', nargs='+', default=[], metavar='HH:MM',
                        help='Schedule camera ON at these times (e.g. 14:35 15:05)')
    return parser.parse_args()


def _parse_times(time_strs):
    result = []
    for s in time_strs:
        try:
            h, m = s.split(':')
            h, m = int(h), int(m)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            result.append((h, m))
        except (ValueError, AttributeError):
            print(f"Invalid time format: '{s}' (expected HH:MM)")
            sys.exit(1)
    return sorted(result)


args = parse_args()

# State variables
paused = False
should_quit = False
recording = False
loop_playing = False
recorded_frames = deque()
max_record_frames = 300
save_pending = False
video_mode = False

current_keys = set()

# Schedule (global – shared between TUI, scheduler, and input handler)
schedule_off_times = []
schedule_on_times = []


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

class TUI:
    _ANSI_RE = re.compile(r'\033\[[^m]*m')

    def __init__(self, device="", is_video_mode=False):
        self._device = device
        self._video_mode = is_video_mode
        self._logs = deque(maxlen=200)
        self._lock = threading.Lock()
        self._input_mode = False
        self._input_buf = ''
        self._input_prompt = ''
        self._input_callback = None
        self._setup_console()

    def _setup_console(self):
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value |= 0x0004
        kernel32.SetConsoleMode(handle, mode)
        sys.stdout.write('\033[2J\033[H\033[?25l')
        sys.stdout.flush()

    def cleanup(self):
        sys.stdout.write('\033[?25h\n')
        sys.stdout.flush()

    def _vis_len(self, s):
        return len(self._ANSI_RE.sub('', s))

    def _pad(self, text, width):
        return text + ' ' * max(width - self._vis_len(text), 0)

    def _clip(self, text, width):
        if width <= 0:
            return ''
        if self._vis_len(text) <= width:
            return text
        if width <= 3:
            limit = width
            suffix = ''
        else:
            limit = width - 3
            suffix = '...'

        out = []
        visible = 0
        i = 0
        while i < len(text) and visible < limit:
            if text[i] == '\033':
                match = self._ANSI_RE.match(text, i)
                if match:
                    out.append(match.group(0))
                    i = match.end()
                    continue
            out.append(text[i])
            visible += 1
            i += 1

        clipped = ''.join(out) + suffix
        if '\033[' in clipped:
            clipped += '\033[0m'
        return clipped

    def _plain_len(self, text):
        return self._vis_len(text)

    def _status(self):
        if loop_playing:
            return '\033[34m▶ Loop\033[0m'
        if recording:
            return '\033[31m⏺ Recording\033[0m'
        if paused:
            return '\033[33m⏸ Frozen\033[0m'
        if self._video_mode and not loop_playing:
            return '\033[33m⏸ Paused\033[0m'
        return '\033[32m● Live\033[0m'

    # ── input mode ──

    def start_input(self, prompt, callback):
        self._input_mode = True
        self._input_buf = ''
        self._input_prompt = prompt
        self._input_callback = callback
        self.draw()

    def cancel_input(self):
        self._input_mode = False
        self._input_buf = ''
        self._input_callback = None
        self.log("Input cancelled")

    def handle_input_key(self, key):
        if not self._input_mode:
            return False

        if key == keyboard.Key.enter:
            text = self._input_buf
            cb = self._input_callback
            self._input_mode = False
            self._input_buf = ''
            self._input_callback = None
            if cb:
                cb(text)
            else:
                self.draw()
            return True

        if key == keyboard.Key.esc:
            self.cancel_input()
            return True

        if key == keyboard.Key.backspace:
            if self._input_buf:
                self._input_buf = self._input_buf[:-1]
                self.draw()
            return True

        if key == keyboard.Key.space:
            self._input_buf += ' '
            self.draw()
            return True

        if hasattr(key, 'char') and key.char:
            self._input_buf += key.char
            self.draw()
            return True

        return True

    # ── drawing ──

    def draw(self):
        with self._lock:
            cols, rows = shutil.get_terminal_size()
            # Leave one spare column on the right. Some terminals auto-wrap or
            # clear oddly when the border lands on the last column.
            w = max(cols - 5, 1)
            HL = '─'

            if self._video_mode:
                hotkeys = [
                    ("Ctrl+F10", "Pause/Resume Loop"),
                    ("Ctrl+F11", "Toggle Meet Camera"),
                    ("Ctrl+F12", "Set Schedule"),
                    ("Ctrl+C", "Exit"),
                ]
            else:
                hotkeys = [
                    ("Ctrl+F8", "Freeze/Resume"),
                    ("Ctrl+F9", "Record (max 10s)"),
                    ("Ctrl+F10", "Play/Stop Loop"),
                    ("Ctrl+F11", "Toggle Meet Camera"),
                    ("Ctrl+F12", "Set Schedule"),
                    ("Ctrl+C", "Exit"),
                ]

            has_sched = bool(schedule_off_times or schedule_on_times)
            hdr_h = 3 + 1 + len(hotkeys) + 1
            if has_sched:
                hdr_h += 2
            if self._input_mode:
                hdr_h += 2
            log_h = max(rows - hdr_h - 2, 1)

            buf = ['\033[H']

            # ── top border ──
            buf.append('  ┌' + HL * w + '┐\033[K\n')

            # ── title + status ──
            title = '\033[1mMORINESS\033[0m'
            status = self._status()
            gap = w - self._vis_len(title) - self._vis_len(status) - 2
            buf.append('  │ ' + title + ' ' * max(gap, 1) + status + ' │\033[K\n')

            # ── device ──
            dev = self._clip(self._device or '--', max(w - 1, 1))
            buf.append('  │ ' + dev + ' ' * max(w - 1 - self._plain_len(dev), 0) + '│\033[K\n')

            # ── hotkey separator ──
            buf.append('  ├' + HL * w + '┤\033[K\n')

            for key, desc in hotkeys:
                line = self._clip(' ' + key.ljust(12) + desc, w)
                buf.append('  │' + line + ' ' * max(w - self._plain_len(line), 0) + '│\033[K\n')

            # ── schedule section ──
            if has_sched:
                buf.append('  ├' + HL * w + '┤\033[K\n')
                parts = []
                if schedule_off_times:
                    ts = ', '.join(f'{h:02d}:{m:02d}' for h, m in schedule_off_times)
                    parts.append('\033[31mOFF\033[0m ' + ts)
                if schedule_on_times:
                    ts = ', '.join(f'{h:02d}:{m:02d}' for h, m in schedule_on_times)
                    parts.append('\033[32mON\033[0m  ' + ts)
                sched = self._clip(' Schedule: ' + '   '.join(parts), w)
                buf.append('  │' + self._pad(sched, w) + '│\033[K\n')

            # ── log separator ──
            buf.append('  ├' + HL * w + '┤\033[K\n')

            # ── logs ──
            logs = list(self._logs)[-log_h:]
            for msg in logs:
                msg = self._clip(msg, max(w - 1, 1))
                buf.append('  │ ' + msg + ' ' * max(w - 1 - self._plain_len(msg), 0) + '│\033[K\n')
            for _ in range(log_h - len(logs)):
                buf.append('  │' + ' ' * w + '│\033[K\n')

            # ── input section ──
            if self._input_mode:
                buf.append('  ├' + HL * w + '┤\033[K\n')
                prompt = ' ' + self._input_prompt + self._input_buf + '█'
                prompt = self._clip(prompt, w)
                buf.append('  │' + prompt + ' ' * max(w - self._plain_len(prompt), 0) + '│\033[K\n')

            # ── bottom border ──
            buf.append('  └' + HL * w + '┘\033[K\n')
            buf.append('\033[J')

            sys.stdout.write(''.join(buf))
            sys.stdout.flush()

    def log(self, msg):
        ts = time.strftime('%H:%M:%S')
        self._logs.append(ts + '  ' + msg)
        self.draw()

    def update(self):
        self.draw()


tui = None


def ui_log(msg):
    if tui:
        tui.log(msg)
    else:
        print(msg)


# ---------------------------------------------------------------------------
# Schedule input handler
# ---------------------------------------------------------------------------

def _apply_schedule_input(text):
    global schedule_off_times, schedule_on_times

    text = text.strip()
    if not text:
        return

    if text.lower() == 'clear':
        schedule_off_times = []
        schedule_on_times = []
        ui_log("Schedule cleared")
        return

    new_off = list(schedule_off_times)
    new_on = list(schedule_on_times)
    tokens = text.lower().split()
    mode = None

    for token in tokens:
        if token == 'off':
            mode = 'off'
        elif token == 'on':
            mode = 'on'
        elif ':' in token:
            if mode is None:
                ui_log("Specify 'off' or 'on' before time")
                return
            try:
                p = token.split(':')
                h, m = int(p[0]), int(p[1])
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except (ValueError, IndexError):
                ui_log(f"Invalid time: {token}")
                return
            t = (h, m)
            target = new_off if mode == 'off' else new_on
            if t not in target:
                target.append(t)
        elif token.startswith('-') and ':' in token:
            if mode is None:
                ui_log("Specify 'off' or 'on' before -HH:MM")
                return
            try:
                p = token[1:].split(':')
                h, m = int(p[0]), int(p[1])
            except (ValueError, IndexError):
                ui_log(f"Invalid time: {token}")
                return
            t = (h, m)
            target = new_off if mode == 'off' else new_on
            if t in target:
                target.remove(t)
        else:
            ui_log(f"Unknown: '{token}' — use: off/on HH:MM | clear")
            return

    schedule_off_times = sorted(new_off)
    schedule_on_times = sorted(new_on)

    parts = []
    if schedule_off_times:
        parts.append("OFF " + ", ".join(f"{h:02d}:{m:02d}" for h, m in schedule_off_times))
    if schedule_on_times:
        parts.append("ON " + ", ".join(f"{h:02d}:{m:02d}" for h, m in schedule_on_times))
    ui_log("Schedule: " + (" | ".join(parts) if parts else "empty"))


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def save_video_async(frames, width, height, fps, output_dir):
    if not frames:
        ui_log("No frames to save")
        return

    os.makedirs(output_dir, exist_ok=True)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    filename = os.path.join(output_dir, f'recording_{timestamp}.mp4')

    def _write():
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(filename, fourcc, fps, (width, height))
        if not writer.isOpened():
            ui_log(f"Error: Could not open video writer for {filename}")
            return
        try:
            for frame_rgb in frames:
                writer.write(frame_rgb[..., ::-1])
            ui_log(f"Saved {len(frames)} frames ({len(frames)/fps:.1f}s) -> {filename}")
        finally:
            writer.release()

    threading.Thread(target=_write, daemon=True).start()


def load_video_file(path):
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
        frames.append(frame[..., ::-1].copy())
    cap.release()

    if not frames:
        raise RuntimeError(f"No frames could be read from {path}")

    print(f"Loaded {len(frames)} frames, {width}x{height} @ {src_fps:.1f}fps")
    return frames, width, height


# ---------------------------------------------------------------------------
# Google Meet camera control via Windows API
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32
_KEYEVENTF_KEYUP = 0x0002
_VK_MENU = 0x12
_VK_CONTROL = 0x11
_VK_E = 0x45
_SW_RESTORE = 9
_meet_lock = threading.Lock()


def _find_meet_hwnd():
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def _cb(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value.lower()
            if 'meet' in title and 'edge' in title:
                found.append(hwnd)
        return True

    _user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else None


def toggle_meet_camera(from_hotkey=True):
    if from_hotkey:
        time.sleep(0.3)

    with _meet_lock:
        hwnd = _find_meet_hwnd()
        if not hwnd:
            ui_log("Meet window not found in Edge (ensure Meet is the active tab)")
            return

        prev = _user32.GetForegroundWindow()

        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)
            time.sleep(0.1)

        _user32.keybd_event(_VK_MENU, 0, 0, 0)
        _user32.SetForegroundWindow(hwnd)
        _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.2)

        _user32.keybd_event(_VK_CONTROL, 0, 0, 0)
        _user32.keybd_event(_VK_E, 0, 0, 0)
        time.sleep(0.05)
        _user32.keybd_event(_VK_E, 0, _KEYEVENTF_KEYUP, 0)
        _user32.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)

        time.sleep(0.2)

        if prev and prev != hwnd:
            _user32.keybd_event(_VK_MENU, 0, 0, 0)
            _user32.SetForegroundWindow(prev)
            _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)

    ui_log("Meet camera toggled")


# ---------------------------------------------------------------------------
# Camera schedule
# ---------------------------------------------------------------------------

def _scheduler_loop():
    triggered = set()
    while not should_quit:
        now = time.localtime()
        today = (now.tm_year, now.tm_mon, now.tm_mday)
        hm = (now.tm_hour, now.tm_min)

        for t in list(schedule_off_times):
            key = (today, t, 'off')
            if hm == t and key not in triggered:
                triggered.add(key)
                ui_log(f"Schedule: camera OFF ({t[0]:02d}:{t[1]:02d})")
                toggle_meet_camera(from_hotkey=False)

        for t in list(schedule_on_times):
            key = (today, t, 'on')
            if hm == t and key not in triggered:
                triggered.add(key)
                ui_log(f"Schedule: camera ON ({t[0]:02d}:{t[1]:02d})")
                toggle_meet_camera(from_hotkey=False)

        triggered = {k for k in triggered if k[0] == today}
        time.sleep(1)


def _resize_loop():
    last_size = shutil.get_terminal_size()
    while not should_quit:
        time.sleep(0.1)
        size = shutil.get_terminal_size()
        if size != last_size:
            last_size = size
            if tui:
                tui.draw()


# ---------------------------------------------------------------------------
# Hotkey handlers
# ---------------------------------------------------------------------------

def on_press(key):
    global paused, should_quit, recording, loop_playing, recorded_frames, current_keys, save_pending

    # Route to TUI input mode if active
    if tui and tui.handle_input_key(key):
        return

    current_keys.add(key)

    try:
        ctrl_pressed = (keyboard.Key.ctrl in current_keys or
                        keyboard.Key.ctrl_l in current_keys or
                        keyboard.Key.ctrl_r in current_keys)

        if ctrl_pressed and keyboard.Key.f8 in current_keys:
            if video_mode:
                ui_log("Ctrl+F8 disabled in --video mode")
                return
            paused = not paused
            if paused:
                loop_playing = False
            ui_log('Frozen' if paused else 'Resumed')
            return

        if ctrl_pressed and keyboard.Key.f9 in current_keys:
            if video_mode:
                ui_log("Ctrl+F9 disabled in --video mode")
                return
            if not recording:
                recording = True
                recorded_frames.clear()
                loop_playing = False
                paused = False
                ui_log(f"Recording started (max {max_record_frames // 30}s)")
            else:
                recording = False
                save_pending = True
                ui_log(f"Recording stopped, {len(recorded_frames)} frames ({len(recorded_frames)/30:.1f}s)")
            return

        if ctrl_pressed and keyboard.Key.f10 in current_keys:
            if len(recorded_frames) == 0:
                ui_log("No recorded video, record first with Ctrl+F9")
            else:
                loop_playing = not loop_playing
                if loop_playing:
                    recording = False
                    paused = False
                    ui_log(f"Playing loop ({len(recorded_frames)/30:.1f}s)")
                else:
                    ui_log("Loop stopped")
            return

        if ctrl_pressed and keyboard.Key.f11 in current_keys:
            threading.Thread(target=toggle_meet_camera, daemon=True).start()
            return

        if ctrl_pressed and keyboard.Key.f12 in current_keys:
            if tui:
                tui.log("off/on HH:MM | off -HH:MM (remove) | clear | Esc=cancel")
                tui.start_input('> ', _apply_schedule_input)
            return

    except Exception:
        pass


def on_release(key):
    global current_keys
    try:
        current_keys.discard(key)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global paused, should_quit, recording, loop_playing, recorded_frames
    global save_pending, video_mode, tui
    global schedule_off_times, schedule_on_times

    schedule_off_times = _parse_times(args.cam_off)
    schedule_on_times = _parse_times(args.cam_on)

    cap = None
    video_mode = args.video is not None

    if video_mode:
        frames, width, height = load_video_file(args.video)
        recorded_frames.extend(frames)
        loop_playing = True
        last_frame_rgb = frames[0]
    else:
        print(f"Opening physical camera index={args.cam_index}...")
        cap = cv2.VideoCapture(args.cam_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open physical camera index={args.cam_index}")

        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            raise RuntimeError("Cannot read frame from physical camera")
        height, width = frame.shape[:2]
        last_frame_rgb = frame[..., ::-1]
        print(f"Camera resolution: {width}x{height}")

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    print("Starting virtual camera...")
    backends_to_try = ["obs", "unitycapture"]
    vcam = None
    last_error = None

    for backend in backends_to_try:
        try:
            print(f"Trying '{backend}' backend...")
            vcam = pyvirtualcam.Camera(width=width, height=height, fps=30, backend=backend)
            break
        except Exception as e:
            print(f"'{backend}' failed: {e}")
            last_error = e

    if vcam is None:
        if cap is not None:
            cap.release()
        listener.stop()
        raise last_error

    try:
        with vcam:
            tui = TUI(device=vcam.device, is_video_mode=video_mode)
            if video_mode:
                tui.log(f"Playback mode: looping {args.video}")
            else:
                tui.log("Virtual camera started")

            # Always start scheduler (handles both CLI and TUI-set schedules)
            threading.Thread(target=_scheduler_loop, daemon=True).start()
            threading.Thread(target=_resize_loop, daemon=True).start()
            if schedule_off_times or schedule_on_times:
                tui.log("Camera schedule active")

            t_frame = time.time()
            loop_frame_index = 0
            loop_direction = 1

            while not should_quit:
                if save_pending:
                    save_pending = False
                    save_video_async(list(recorded_frames), width, height, 30, args.output_dir)

                frame_to_send = None

                if loop_playing and len(recorded_frames) > 0:
                    frame_to_send = recorded_frames[loop_frame_index]
                    last_frame_rgb = frame_to_send

                    loop_frame_index += loop_direction
                    if loop_frame_index >= len(recorded_frames) - 1:
                        loop_direction = -1
                        loop_frame_index = len(recorded_frames) - 1
                    elif loop_frame_index <= 0:
                        loop_direction = 1
                        loop_frame_index = 0

                elif paused or video_mode:
                    frame_to_send = last_frame_rgb

                else:
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        time.sleep(0.01)
                        continue

                    frame_rgb = frame[..., ::-1]
                    last_frame_rgb = frame_rgb

                    if recording:
                        recorded_frames.append(frame_rgb.copy())
                        if len(recorded_frames) >= max_record_frames:
                            recording = False
                            save_pending = True
                            ui_log(f"Max recording time ({max_record_frames // 30}s), auto-stopped")

                    frame_to_send = frame_rgb

                if frame_to_send is not None:
                    vcam.send(frame_to_send)

                now = time.time()
                dt = now - t_frame
                wait = max(0.0, (1.0 / 30.0) - dt)
                time.sleep(wait)
                t_frame = time.time()

    except Exception as e:
        if tui:
            tui.cleanup()
        print(f"Error: {e}")
        if cap is not None:
            cap.release()
        listener.stop()
        raise

    if tui:
        tui.cleanup()
    listener.join()
    if cap is not None:
        cap.release()
    print("Exited.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if tui:
            tui.cleanup()
        print("\nUser interrupted")
    except Exception as e:
        if tui:
            tui.cleanup()
        print(f"\nProgram exited with error: {e}")
        import traceback
        traceback.print_exc()
