import cv2
import pyvirtualcam
from pynput import keyboard
import threading
import time
import sys

# 可选：命令行指定物理摄像头索引，默认 0
cam_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0

paused = False
should_quit = False

def on_press(key):
    global paused, should_quit
    try:
        if key == keyboard.Key.f8:
            paused = not paused
            print("[F8] 切换：", "已暂停(冻结当前画面)" if paused else "已恢复")
        elif key == keyboard.Key.esc:
            should_quit = True
            return False  # 停止监听
    except Exception:
        pass

def main():
    global paused, should_quit

    cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)  # DirectShow 更快更稳
    if not cap.isOpened():
        raise RuntimeError(f"无法打开物理摄像头 index={cam_index}")

    # 先读一帧，确定分辨率
    ok, frame = cap.read()
    if not ok or frame is None:
        raise RuntimeError("无法从物理摄像头读取画面")
    height, width = frame.shape[:2]

    # 启动全局热键监听（F8 切换冻结，Esc 退出）
    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    # 使用 OBS Virtual Camera 后端；不指定 device 时自动选第一个虚拟设备
    # 如果你装了多个虚拟摄像头，也可传 device="OBS Virtual Camera"
    with pyvirtualcam.Camera(width=width, height=height, fps=30, backend="obs") as vcam:
        print("虚拟摄像头已启动：", vcam.device, " | 热键：F8 冻结/恢复，Esc 退出")

        last_frame_rgb = frame[..., ::-1]  # BGR->RGB
        t_frame = time.time()

        while not should_quit:
            if not paused:
                ok, frame = cap.read()
                if not ok or frame is None:
                    # 读不到就稍等一下再试
                    time.sleep(0.01)
                    continue
                last_frame_rgb = frame[..., ::-1]

            # 发送上一帧（若暂停则维持不变）
            vcam.send(last_frame_rgb)

            # 节流到目标帧率
            # 用 sleep 而不是 vcam.sleep_until_next_frame()，避免长时间暂停时积累偏差
            now = time.time()
            dt = now - t_frame
            wait = max(0.0, (1.0 / 30.0) - dt)
            time.sleep(wait)
            t_frame = time.time()

    listener.join()
    cap.release()
    print("已退出。")

if __name__ == "__main__":
    main()
