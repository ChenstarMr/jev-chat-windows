# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR 头部会话名和消息 → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。"""
import ctypes
import time
import traceback
from queue import Empty

import numpy as np

from app import chatapps
from app.capture import Capture, chat_area, unminimize
from app.ocr import Reader, read_title


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


def _confirm_title(title, name, readers, pending):
    """OCR 读出的会话名要不要认（防抖 + 防串门）。返回 (title, pending, switched)。
    pending = (待确认的名字, 已读次数)，None = 没有在确认中的新名字。

    - 读回来的和当前一致：抖回去了（「小分认」下一帧又读成「小分队」），清掉待确认；
    - 跟上一帧读的一致，或恰好是某个已知会话的全名：真切换，立刻认——读到 A 的像素不会
      恰好拼出 B 的完整名字，全名一致只有真切换说得通。旧版在这里按相似度去全表匹配，
      「项目A群」↔「项目B群」这种名字相近的真会话会被并成一个，消息全串门；
    - 首见的新名字：先沿用当前名、进入待确认，下一帧再读一次——真切换的标题是稳定的，
      抖动很难连着两帧给出同一个错名。连续 6 次都读不稳（动头像、加载动画）就采用最新的，
      别永远重读。
    确认期间调用方要跳过消息识别：新会话的内容不能记到旧会话头上。"""
    if name == title:
        return title, None, False
    if (pending and name == pending[0]) or name in readers:
        return name, None, True
    tries = pending[1] + 1 if pending else 1
    if tries >= 6:
        return name, None, True
    return title, (name, tries), False


def _handle_verify(verify_q, cap, app, q):
    """「填入」前的现场核对：父进程问「现在开的是哪个会话」，拿最新一帧重新认一遍
    会话名和消息区，从 q 回 ("verified", id, 想填的会话名, 现读的会话名, 现在的消息区)。
    title=None = 暂停中/没法核对（父进程按老样子直接填）；title="" = 在采但读不出（父进程不敢填）。
    核对不等停稳：宁可毛糙也要「现在」，等停稳恰恰会放过刚切走的那零点几秒。"""
    while True:
        try:
            vid, want = verify_q.get_nowait()
        except Empty:
            return
        title, area4 = (None, None) if cap is None else ("", None)
        full = cap.latest() if cap is not None else None
        if full is not None:
            area = chat_area(full)
            if area is not None:
                x0, y0t, x1, y1, bg, y_pane = area
                title = read_title(full[y_pane:y0t, x0:x1], app)
                area4 = (int(x0), int(y0t), int(x1), int(y1))
        q.put(("verified", vid, want, title, area4))


def _packet(full, area, title, reader, lines):
    """调试视图的一帧：整帧缩到长边 ≤1100 再走队列（原帧 2560 宽裸传要 20MB），只在内存里传，不落盘。
    整数步长切片够用，不引新依赖；框和坐标照发原始值，画的那边按 scale 折算。"""
    k = max(1, -(-max(full.shape[:2]) // 1100))
    small = np.ascontiguousarray(full[::k, ::k])
    return {"w": small.shape[1], "h": small.shape[0], "rgb": small.tobytes(), "scale": k,
            "area": tuple(int(v) for v in area[:4]) if area else None,
            "pane_top": int(area[5]) if area else 0, "title": title,
            "boxes": reader.last_boxes if reader else [],
            "lines": [(w, n, t) for w, n, t, _ in lines],
            "ocr_ms": reader.last_ms if reader else 0, "ts": time.time()}


def run(q, hwnd, enabled, debug_on, app_key=None, verify_q=None):
    """enabled 置位=采集，清掉=暂停。暂停时停掉 WGC 会话（Windows 那圈黄色采集边框也跟着没了），
    恢复时重开一个；readers 一直留着，去重状态不丢，恢复后不会把屏幕上的旧消息再报一遍。
    debug_on 置位才往队列里送整帧（一帧 2~3MB），关着一点额外活都不干。
    verify_q 是「填入前的现场核对」请求队列：每圈开头先答掉，别让父进程干等。"""
    ctypes.windll.user32.SetProcessDPIAware()
    app = chatapps.get(app_key)
    cap = None
    readers = {}  # {会话名: Reader}，一个会话一套去重状态
    title, head = "", None  # 当前会话名 / 上一帧的头部像素
    last_area = None  # 上次发给父进程的 4 元组，变了才再发一次
    warned = False  # 消息区识别失败是否已经报过，拖窗口时别每帧刷一条
    back = {}  # {会话名: 上次是不是停在旧内容上}，翻上去/翻回来各报一次，别每帧刷
    pending = None  # (待确认的会话名, 已读次数)：首见的新名字要下一帧再读到一致才认
    err_sig, err_at = "", 0.0  # 上一条帧错误的指纹/时刻：同一个错别每帧刷，10 秒催一次
    while True:
        if verify_q is not None:
            try:
                _handle_verify(verify_q, cap, app, q)  # cap=None（暂停中）也答：父进程按老路直接填
            except Exception:
                _err(q)  # 一次核对出错不能带垮整个采集子进程，下一圈接着答
        if not enabled.is_set():
            if cap is not None:
                cap.stop()
                cap = None
                q.put(("paused",))
            enabled.wait()
            continue
        if cap is None:
            try:
                cap = Capture(hwnd)
            except Exception as e:
                q.put(("dead", "无法开始采集：" + (" ".join(str(e).split())[:120] or type(e).__name__)))
                enabled.clear()  # 自己清掉，下一圈就去等着，别一秒重试几十次
                continue
            q.put(("resumed",))
        if not cap.alive():
            break
        try:
            unminimize(hwnd)
            full = cap.settled()
            if full is not None:
                reader, lines = None, []  # 调试视图要用，消息区没认出来时就是空的
                area = chat_area(full)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死
                if area is None:
                    if not warned:
                        q.put(("status", "消息区认不出来（窗口太小？）"))
                        warned = True
                else:
                    warned = False
                    cap.area = area  # 采集线程拿它做 diff
                    x0, y0, x1, y1, bg, y_pane = area
                    rect = (x0, y0, x1, y1)
                    if rect != last_area:
                        q.put(("area", rect))
                        last_area = rect
                    crop = full[y_pane:y0, x0:x1]  # 头部：会话名在这里
                    if head is None or not np.array_equal(crop, head) or pending is not None:
                        # 名字没动就别白跑一次 OCR；但有待确认的新名字时每帧都重读——
                        # 真切换之后头部就不再变了，光等下一处像素变化可能永远等不到第二次确认
                        head = crop
                        name = read_title(crop, app)
                        # ponytail: 认不出就沿用上次；开头就认不出给个占位名，总比把消息全丢了强
                        name = name or title or "当前会话"
                        title, pending, switched = _confirm_title(title, name, readers, pending)
                        if switched:
                            q.put(("chat", title))
                    if pending is None:  # 名字在确认中这帧不认消息：新会话的内容不能记到旧会话头上
                        reader = readers.setdefault(title, Reader(app))
                        lines = reader.read(full[y0:y1, x0:x1], bg)
                        new = reader.new_lines(lines)
                        if new:
                            q.put(("lines", title, new, rect))
                        if back.setdefault(title, False) != reader.view_back:
                            # 翻上去看历史时只记录、不生成回复；翻回底部再恢复。给个说明，
                            # 免得用户以为程序不动了（这条只进聊天记录，不动状态行）
                            back[title] = reader.view_back
                            q.put(("note", "back" if reader.view_back else "front"))
                if debug_on.is_set():
                    q.put(("debug", _packet(full, area, title, reader, lines)))
        except Exception:
            # 一帧出错不退出；同一个错别以每秒 20 条的频率刷状态行，换错了/隔 10 秒才再报
            sig = " ".join(traceback.format_exc().split())[-200:]
            if sig != err_sig or time.time() - err_at > 10:
                err_sig, err_at = sig, time.time()
                q.put(("status", sig))
        time.sleep(0.05)
    q.put(("dead", "采集停了（聊天窗口关了？）"))
    try:
        cap.wait()  # 采集线程若是报错死的，这里把错抛出来
    except Exception:
        _err(q)


if __name__ == "__main__":
    # ponytail: 只测会话名确认（不截图、不 OCR）。这是切会话的唯一关口：
    # 抖动不能分裂出新会话，名字相近的两个真会话也不能被并成一个。
    assert _confirm_title("甲", "甲", {}, None) == ("甲", None, False)
    # 首见新名：沿用旧名、进入待确认（这帧不认消息，等下一帧再读）
    assert _confirm_title("甲", "丙", {}, None) == ("甲", ("丙", 1), False)
    # 连续两次读到一致：真切换
    assert _confirm_title("甲", "丙", {}, ("丙", 1)) == ("丙", None, True)
    # 抖回当前名：清掉待确认，不切
    assert _confirm_title("甲", "甲", {}, ("丙", 3)) == ("甲", None, False)
    # 恰好是已知会话的全名：真切换——「项目A群」→「项目B群」必须切过去，不能按相似并掉
    assert _confirm_title("项目A群", "项目B群", {"项目B群": None}, None) == ("项目B群", None, True)
    # 陌生的首见名照旧走待确认
    assert _confirm_title("甲", "陌生会话", {"乙": None}, None) == ("甲", ("陌生会话", 1), False)
    # 一直读不稳（动头像/加载动画）：到上限采用最新的，别永远重读
    assert _confirm_title("甲", "丁", {}, ("丙", 5)) == ("丁", None, True)
    # 填入前的现场核对：暂停中回 None（父进程按老路直接填）、在采但没帧回空串（父进程不敢填）
    import queue as _queue

    class _Cap:
        def __init__(self, frame):
            self._f = frame

        def latest(self):
            return self._f

    vq, oq = _queue.Queue(), _queue.Queue()
    _handle_verify(vq, None, chatapps.DEFAULT, oq)
    assert oq.empty()  # 没请求就别吭声
    vq.put((7, "某会话"))
    _handle_verify(vq, None, chatapps.DEFAULT, oq)
    assert oq.get_nowait() == ("verified", 7, "某会话", None, None)
    vq.put((8, "某会话"))
    _handle_verify(vq, _Cap(None), chatapps.DEFAULT, oq)
    assert oq.get_nowait() == ("verified", 8, "某会话", "", None)
    print("worker._confirm_title ok")
