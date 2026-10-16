# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。发送永远手动。静默期零调用。
上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import itertools
import multiprocessing
import queue
import threading
import time
import traceback
from collections import deque

from app import chatapps, settings, update, worker
from app.capture import find_chat_hwnd
from app.fill import fill
from app.overlay import Overlay
from app.version import VERSION
from core.engine import analyze
from app.i18n import T

# {会话名: {history, result, rev, target, senders}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
state = {"area": None, "busy": False, "rerun": {}, "retry_at": 0.0, "hwnd": None, "chat": ""}
results = queue.Queue()
verify_q = multiprocessing.Queue()  # 填入前的现场核对请求：父进程问「现在开的是谁」，子进程拿最新帧答
cmd_q = multiprocessing.Queue()  # 控制命令（清空记录、重新收集）：父进程发，子进程丢掉去重状态后回执
pending_fills = {}  # {核对 id: (要填的文本, 发起时刻)}：核对通过才真填；对不上/超时就作废
pending_reset = {}  # {会话名: 发起时刻}：等子进程确认丢掉去重状态再清记录，免得清完又被旧帧填回来
_fill_seq = itertools.count(1)
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": []})


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def _fill_now(text):
    """真正去点输入框粘贴：核对通过的、和没法核对（暂停中）的都从这儿走。
    成功/失败的状态也从这儿出——核对是异步的，「已填入」不能在发起时就说。"""
    try:
        fill(state["hwnd"], state["area"], text)
    except Exception as e:
        import traceback
        ov.set_status("未能填入，请确认聊天窗口可用后重试，或复制回复。", "error")
        ov.log(f"[填入失败] {type(e).__name__}: {e}")
        ov.log(f"[填入失败堆栈] {' '.join(traceback.format_exc().split())[:300]}")
        return
    ov.set_status("已尝试填入，请确认内容后发送。", "success")


def fill_reply(text):
    """点「填入」。先让子进程拿最新一帧现场核对微信当前开着的会话，对上了才真的点输入框——
    微信刚切走、界面还没跟上的那零点几秒里照旧粘贴，是「填进别的窗口」的主要来源。
    核对只要一次标题 OCR（约百毫秒），期间按钮禁掉防连点；采集没在跑时核对不了，按老样子直接填。"""
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError(T("未找到聊天窗口，请确认已经打开"))
    if state["area"] is None:
        raise RuntimeError(T("输入区域尚不可用，请确认聊天窗口可见（不要最小化）"))
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target:
            text = f"@{target} " + text  # 纯文本，微信不认成真正的 @，只是让群里看得出在跟谁说
    if capture_on.is_set() and child is not None:
        vid = next(_fill_seq)
        pending_fills[vid] = (text, time.time())
        verify_q.put((vid, ov.current_chat()))
        ov.set_fills_enabled(False)  # 核对这零点几秒里先禁用填入/复制，防连点重复填
        return
    _fill_now(text)


def clear_chat(title):
    """把这个会话的记录清干净：历史、候选、发言人、待重跑，界面上那份也跟着清。
    只动这一个会话，别的会话的记录不受影响。rev 加一：清空前发出去的分析回来也贴不上来了。"""
    chat = chat_of(title)
    chat["history"].clear()
    chat["result"] = None
    chat["rev"] += 1
    chat["target"] = None
    chat["senders"].clear()
    state["rerun"].pop(title, None)
    ov.clear_chat(title)


def reset_chat(title):
    """「重新收集」：清掉这个会话的记录，再让采集侧丢掉去重状态，下一帧把画面上现有的消息
    当新的重收一遍——滚太快记乱了时手动重来一次（不重启、不重开窗口）。
    子进程活着就先问一声、等它的回执再清：队列先入先出，回执排在旧帧后面，不会清完又被旧帧填回来。
    没在采（暂停/没窗口/子进程死了）就直接清，等窗口回来自己重新收。"""
    title = title or ov.current_chat()
    if not title:
        ov.set_status("还没识别到会话，等聊天窗口的会话名读出来再试", "warning")
        return
    if child is not None:
        pending_reset[title] = time.time()
        cmd_q.put(("reset", title))
        ov.set_status("正在清空这个会话的记录，重新读一遍当前画面…", "busy")
        return
    clear_chat(title)
    ov.set_status("记录已清空；采集没在跑，等窗口回来会自动重新收集", "warning")


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。"""
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on, state.get("app"),
                                      verify_q, cmd_q),
                                daemon=True)
    p.start()
    return p


def want_app():
    """设置里指定要盯的聊天应用；auto = None，按 app/chatapps.APPS 顺序先到先得（微信优先）。"""
    want = settings.app()
    return None if want == "auto" else chatapps.APPS.get(want)


def set_debug(on):
    """调试视图开关：开 → 开窗 + 置位（子进程这才开始送帧，一帧 2~3MB）；关 → 清掉 + 收窗。"""
    global dbg
    if not on:
        debug_on.clear()
        if dbg is not None:
            dbg.hide()
        return
    if dbg is None:
        from app.debugwin import DebugWindow

        dbg = DebugWindow(on_close=on_debug_closed)
    dbg.show()
    debug_on.set()


def on_debug_closed():
    """用户直接关了调试窗 = 把开关也关了，否则设置页显示开着但没窗。"""
    debug_on.clear()
    ov.set_debug_switch(False)
    settings.save(debug_view_on=False)


def on_language_changed():
    """主界面切换语言后，同步刷新已经打开过的调试窗。"""
    if dbg is not None:
        dbg.retranslate()


def on_toggle_capture(on):
    """标题栏开关。窗口还没开也照常接下开关：tick 里定期找一次，开了自动接上。"""
    global child
    if not on:
        capture_on.clear()
        return
    if child is None:
        try:
            state["hwnd"], found = find_chat_hwnd(want=want_app())
            state["app"] = found.key
        except RuntimeError:
            state["retry_at"] = time.time() + 3  # 别每帧都枚举一遍窗口
            ov.set_status("未找到聊天窗口，打开后自动开始采集", "warning")
            capture_on.set()  # 开关保持开：窗口一开 tick 里自动接上
            return
        child = spawn_worker()
    capture_on.set()


def on_app_changed():
    """设置里换了要盯的聊天应用：正在采集就立刻换窗口重开，没在采就等下次开启。"""
    global child
    was_on = capture_on.is_set()
    capture_on.clear()
    if child is not None:
        child.terminate()
        child.join()
        child = None
    state["area"] = None
    state["hwnd"] = None
    state["retry_at"] = 0.0
    pending_fills.clear()  # 换目标的瞬间，没核对完的填入请求别再落到新子进程头上
    pending_reset.clear()  # 同上：没回执的「重新收集」也跟着作废
    ov.set_fills_enabled(True)
    if was_on:
        on_toggle_capture(True)


def analyze_bg(msgs, title, revision, reply_to=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。"""
    try:
        results.put(("ok", analyze(msgs, settings.relationship(), context=settings.context(),
                                   model=settings.draft_model() or None,
                                   provider=settings.draft_provider(),
                                   base_url=settings.draft_base_url() or None,
                                   reply_to=reply_to, style=settings.style(),
                                   thinking=settings.thinking(),
                                   jev_provider=settings.jev_provider(),
                                   jev_model=settings.jev_model() or None,
                                   jev_base_url=settings.jev_base_url() or None,
                                   use_jev=settings.use_jev()),
                     title, revision))
    except Exception as e:
        results.put(("err", f"{T('分析失败: ')}{e}", title, revision))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs):
    if settings.use_jev() and not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(lambda name=settings.draft_provider_name():
                      f"{T('起草来源 ')}{name}{T(' 没填密钥，去设置里补上')}", "warning")
        return
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, chat_of(title)["rev"], reply_to),
                     daemon=True).start()


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        state["rerun"][title] = msgs  # 按会话各存一份：别的会话顶不掉它
        ov.set_busy(True)
    else:
        start_analyze(title, msgs)


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            state["chat"] = msg[1]
            ov.set_chat(msg[1])
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1], chat=state["chat"])  # 记到采着的那个会话头上，别串到正在看的屏里
            continue
        if kind == "note":  # 采集侧的一句说明（往上翻看历史之类），只进聊天记录，不动状态行
            ov.log(T("聊天窗口翻到上面了：这一段只记录、不生成回复") if msg[1] == "back"
                   else T("回到最新消息，继续生成回复"), chat=state["chat"])
            continue
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "verified":  # 填入前的现场核对回来了：会话对上才真的填
            _, vid, want, title, area = msg
            entry = pending_fills.pop(vid, None)
            ov.set_fills_enabled(True)
            if entry is None:
                continue  # 早作废了（超时/采集死了），别填两遍
            if title is None:  # 采集没在跑（暂停/没窗口）：核对不了，按老样子直接填
                _fill_now(entry[0])
                continue
            if area:
                state["area"] = area  # 核对顺手把此刻的消息区坐标也带来了，填的时候用它
            if not title or area is None:
                ov.set_status("没敢填入：核对时读不到当前会话，请再点一次填入。", "warning")
            elif title != want:
                ov.set_status(lambda w=want, t=title:
                              T("没敢填入：微信现在开的是「{t}」，候选是给「{w}」写的").format(t=t, w=w),
                              "warning")
            else:
                _fill_now(entry[0])
            continue
        if kind == "reset_done":  # 采集侧已经丢掉去重状态了：这会儿清记录，接着收进来的就是干净的
            _, title = msg
            pending_reset.pop(title, None)
            if title:
                clear_chat(title)
            else:  # 会话名没读出来时发的（全清）
                for name in list(chats):
                    clear_chat(name)
            if capture_on.is_set():
                ov.set_status("记录已清空，正在重新读当前画面…", "success")
            else:  # 暂停中：重读要等采集恢复（子进程那会儿才看得到画面）
                ov.set_status("记录已清空；继续采集后会重新读一遍当前画面", "success")
            continue
        if kind == "dead":  # 采集彻底停了（聊天窗口关了之类），这才是真的要清状态
            state["area"] = None
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
            state["rerun"].clear()
            pending_fills.clear()  # 没核对完的填入请求跟着作废，别在死掉的子进程上等到超时
            pending_reset.clear()  # 子进程没了：等不到回执了，别再按旧会话清（窗口回来时子进程本来就会重读一遍）
            ov.set_fills_enabled(True)
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.log(msg[1], chat=state["chat"])
            if child is not None:  # 子进程已经不干活了，收掉引用
                child.terminate()
                child.join()
                child = None
            if capture_on.is_set():  # 开关还开着：等窗口回来自动接上，不用人再拨一次
                state["retry_at"] = time.time() + 3
                ov.set_status("聊天窗口关了，等它重新打开后自动继续", "warning")
            else:
                ov.set_capture(False, msg[1])
            continue
        _, title, new, area = msg
        state["area"] = area
        chat = chat_of(title)
        chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for who, name, text in new:
            chat["history"].append((who, text, name))
            ov.log_message(who, text, name, chat=title)
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if new[-1][0] == "her":  # 只有对方最新说话才值得分析
            msgs = list(chat["history"])
            if state["busy"]:
                state["rerun"][title] = msgs  # 按会话各存一份，别的会话的新消息顶不掉它
                ov.set_busy(True)
            else:
                start_analyze(title, msgs)
        else:  # 这个会话轮到我说话了：它的待重跑作废（已经回了，不用再生成）
            state["rerun"].pop(title, None)
            if title == ov.current_chat():  # 别的会话你回了话，别动这边正在看的状态行
                ov.set_busy(False)
                ov.set_status("你已回复，等待对方的新消息")


def tick():
    global child
    try:
        drain()
        now = time.time()
        for vid in [v for v, (_, t0) in pending_fills.items() if now - t0 > 2.0]:
            # 子进程一直没答（卡死了/队列丢了）：作废，别让按钮永远禁着
            pending_fills.pop(vid, None)
            ov.set_fills_enabled(True)
            ov.set_status("核对会话超时（采集没响应），没敢填入；请再试一次。", "warning")
        for title in [t for t, t0 in pending_reset.items() if now - t0 > 3.0]:
            # 回执一直没来（子进程卡在某一帧里/队列丢了）：记录照清，重新收集等它缓过来自己来。
            # 3 秒是按子进程最慢的一圈给的：等画面停稳最多 1 秒 + OCR 最多 0.8 秒 + 50ms 的圈间隔。
            pending_reset.pop(title, None)
            clear_chat(title)
            ov.set_status("采集没响应，记录已清空；采集缓过来后会自动重新收集。", "warning")
        if capture_on.is_set() and child is None and time.time() >= state["retry_at"]:
            # 开关开着但子进程没了（聊天窗口关了/还没开）：定期找一次，回来就自动接上
            state["retry_at"] = time.time() + 3
            try:
                state["hwnd"], found = find_chat_hwnd(want=want_app())
            except RuntimeError:
                found = None  # 还没开：三秒后再看
            if found is not None:
                state["app"] = found.key
                child = spawn_worker()
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not results.empty():
            kind, r, title, revision = results.get()
            state["busy"] = False
            pending = state["rerun"].pop(title, None)
            if pending:  # 这个会话分析期间又来了新消息：旧结果直接作废，接着跑最新的
                start_analyze(title, pending)
                continue
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
            elif kind == "ok":
                r["at"] = time.strftime("%H:%M")  # 生成时刻：切回旧会话看缓存时别盖成当前时间
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                if title == ov.current_chat():
                    ov.show(r)
                else:
                    ov.set_busy(False)
            else:
                ov.set_busy(False)
                ov.set_status("生成失败，请检查网络和服务设置；新消息到来后会重试。", "error")
                ov.log(r, chat=title)  # 哪个会话失败的记到哪个会话头上
        if state["rerun"] and not state["busy"]:
            # 别的会话还压着重跑没跑：现在闲下来了，按会话接着跑
            t = next(iter(state["rerun"]))
            start_analyze(t, state["rerun"].pop(t))
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    debug_on = multiprocessing.Event()  # 同上，置位=子进程往队列里送整帧给调试窗
    child = dbg = None
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_language_changed=on_language_changed, on_app_changed=on_app_changed,
                 on_reset_chat=reset_chat,
                 result_of=lambda t: chats.get(t, {}).get("result"))
    try:
        state["hwnd"], found = find_chat_hwnd(want=want_app())
        state["app"] = found.key
    except RuntimeError:
        capture_on.set()  # 开关先开着：窗口一开 tick 里自动接上，不用人管
        ov.set_status("未找到聊天窗口，打开后自动开始采集", "warning")
    else:
        capture_on.set()
        child = spawn_worker()
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_key():  # 起草那把永远要；用 Jev 判断时判断那把也要（见 settings.has_key）
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            child.terminate()
