# -*- coding: utf-8 -*-
"""填入几何探针：完全复刻 app/fill.fill 的坐标推导，但只截图、只算、只存图——
不点击、不写剪贴板、不动前台。用来回答「点填入之后那一下到底点在微信的哪里」。

    .venv/Scripts/python.exe probe/fill_probe.py

输出 probe/fill_probe_out/ 下三张图 + 一行诊断：
- frame_full.png   整帧（WGC 抓的微信窗口，和采集侧同一来源）
- crop_click.png   以「将要点击的那一点」为中心的裁剪，红十字 = 点击点
- crop_header.png  标题条裁剪（核对链路 read_title 吃的就是这一条）
"""
import ctypes
import ctypes.wintypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ctypes.windll.user32.SetProcessDPIAware()  # 和 main/worker 一致，先于任何窗口 API

from app import chatapps  # noqa: E402
from app.capture import Capture, chat_area, unminimize, find_chat_hwnd  # noqa: E402
from app.ocr import read_title  # noqa: E402

OUT = Path(__file__).parent / "fill_probe_out"
OUT.mkdir(exist_ok=True)

u32 = ctypes.windll.user32


def save_rgb(img, name):
    import numpy as np
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtGui import QGuiApplication, QImage

    img = np.ascontiguousarray(img)
    h, w = img.shape[:2]
    qimg = QImage(img.data, w, h, w * 3, QImage.Format_RGB888)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    qimg.save(buf, "PNG")
    (OUT / name).write_bytes(ba.data())


def main():
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication([])  # QImage.save 需要一个 platform 插件

    hwnd, app = find_chat_hwnd()
    print(f"target: {app.key} hwnd={hwnd:#x}")

    r = ctypes.wintypes.RECT()
    ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r))
    gr = ctypes.wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(gr))
    print(f"ext_bounds=({r.left},{r.top},{r.right},{r.bottom}) hr={ok}"
          f"  win_rect=({gr.left},{gr.top},{gr.right},{gr.bottom})"
          f"  iconic={bool(u32.IsIconic(hwnd))}")

    if u32.IsIconic(hwnd):
        unminimize(hwnd)  # 不激活、不抢焦点，跟采集侧行为一致
        time.sleep(0.4)

    cap = Capture(hwnd)
    time.sleep(1.0)
    full = cap.last_frame
    cap.stop()
    if full is None:
        print("PROBE-FAIL: 一帧都没抓到")
        return 1
    print(f"frame: {full.shape[1]}x{full.shape[0]}")

    save_rgb(full, "frame_full.png")

    area = chat_area(full)
    if area is None:
        print("PROBE-FAIL: chat_area 认不出消息区")
        return 1
    x0, y_top, x1, y_in, bg, y_pane = area
    print(f"chat_area: x0={x0} y_top={y_top} x1={x1} y_in={y_in} y_pane={y_pane}")

    # —— 复刻 fill.py 的点击点推导 ——
    cx, cy = r.left + x0 + 60, r.top + y_in + 40
    inside = r.left <= cx < r.right and r.top <= cy < r.bottom
    print(f"click point: ({cx},{cy})  in_window={inside}")
    print(f"input strip height below y_in: {r.bottom - (r.top + y_in)}px")

    # 点击点画红十字再裁剪——画在「帧坐标」里：帧原点 = ext_bounds 原点，
    # 屏幕 click point (r.left + x0 + 60, r.top + y_in + 40) 对应帧内 (x0 + 60, y_in + 40)
    fx, fy = x0 + 60, y_in + 40
    full[fy - 2: fy + 3, fx - 14: fx + 15] = (255, 0, 0)
    full[fy - 14: fy + 15, fx - 2: fx + 3] = (255, 0, 0)
    save_rgb(full[max(0, fy - 90): fy + 160, max(0, fx - 300): fx + 300], "crop_click.png")

    header = full[y_pane:y_top, x0:x1]
    save_rgb(header, "crop_header.png")
    title = read_title(header, app)
    print(f"read_title → {title!r}")

    print("done:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
