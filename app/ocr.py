# -*- coding: utf-8 -*-
"""消息区截图 → 谁说了什么。RapidOCR 吃 numpy，不落盘。"""
import difflib
import re
import time

import numpy as np

from app.chatapps import DEFAULT, ChatApp

_ENGINES: dict = {}   # backend name -> engine, shared per process
_FELL_BACK = set()    # backends that failed once; don't retry every frame


def _engine(app: ChatApp = DEFAULT):
    """OCR 引擎全进程共用：一个实例 ~40MB，每个会话一个 Reader，不能各带一个。
    det_limit_type 默认 'min' 会把小图放大到短边 736，裁小反而更慢；必须 'max'。

    RapidOCR 的内置模型只有中英文，韩日文全是乱码，所以这类界面走 Windows 自带 OCR
    （离线、随系统语言包装好，见 app/ocr_windows.py）；没有对应识别器就退回 RapidOCR。
    引擎在函数里 import：打包时 jev.spec 用 collect_all 把整个包收进去，不靠静态分析，
    而这样跑「不加载引擎」的自测就不用先装它（跟 core/llm.py 里 SDK 的处理一个道理）。"""
    want = app.ocr if app.ocr not in _FELL_BACK else "rapidocr"
    if want not in _ENGINES:
        if want == "windows":
            try:
                from app.ocr_windows import WindowsOcr
                _ENGINES[want] = WindowsOcr()
            except Exception:
                _FELL_BACK.add("windows")
                return _engine(app)
        else:
            from rapidocr_onnxruntime import RapidOCR

            _ENGINES[want] = RapidOCR(intra_op_num_threads=4, det_limit_type="max",
                                      det_limit_side_len=4000)
    return _ENGINES[want]


def read_title(header, app: ChatApp = DEFAULT):
    """面板头部那一条截图 → 会话名（numpy RGB）。取最靠上的一行，同一行里取最左的
    （右边是图标按钮，OCR 不出字；下面那行是公告）。群聊的成员数「(422)」去掉，只留名字当 key。
    认不出返回 ""。一次约 60ms，所以调用方只在头部像素变了时才问。"""
    res, _ = _engine(app)(header, use_cls=False)
    if not res:
        return ""
    first = min(res, key=lambda r: r[0][0][1])
    row = first[0][0][1] + (first[0][2][1] - first[0][0][1])  # 框底：顶在这之上的算同一行
    text = min((r for r in res if r[0][0][1] < row), key=lambda r: r[0][0][0])[1]
    return re.sub(r"\s*[（(]\d+[)）]\s*$", "", text.strip())


def who_said(chat, box, app: ChatApp = DEFAULT):
    """按 OCR 框里的颜色分类，不看 x 坐标。返回 (谁, 底色, 墨高)：
    先看底色平不平：框里众数颜色占比 <45% 就是图片（头像/照片/表情包）里的字 → None 丢掉。
    绿底 → me；非绿且文字对底色对比度 ≥150 → her；其余（引用块、群里的发言人名、时间戳、系统提示、
    链接卡片描述——都是灰字，对比度 80~95）→ "gray"。
    实测：气泡正文对比度 178~208，me 绿泡 142~150，灰字 ≤ 93。深浅主题都靠这套。
    哪种底色算 "me" 由 app.is_me 决定（微信绿 / KakaoTalk 黄）。
    墨高 = 框里最长一段连续有字的行数（OCR 框对小字有固定 padding、还会蹭到上下行，不能拿框高比大小）。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    reg = chat[int(min(ys)):int(max(ys)), int(min(xs)):int(max(xs))].astype(int)
    if reg.size == 0:
        return None, None, 0
    vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    if cnt.max() / reg.shape[0] / reg.shape[1] < 0.45:
        # 文字必须落在平底色上：WGC 帧是精确像素，气泡/面板里众数颜色占 0.56~0.82，
        # 头像/照片/表情包里只有 0.1~0.3——那是图片里的字（头像上的「借仲夏夜之梦」之类），不是消息。
        # ponytail: 只对精确像素的帧成立；缩放/压缩过的截图（比如拿预览窗再截一次的图）底色会糊成几百种颜色，全会被当图片。
        return None, bg, 0
    diff = np.abs(reg @ [0.299, 0.587, 0.114] - bg @ [0.299, 0.587, 0.114])
    ink_h = best = 0
    for r in (diff > 60).any(axis=1):
        best = best + 1 if r else 0
        ink_h = max(ink_h, best)
    if app.is_me(bg):  # 自己的气泡：微信是绿色，KakaoTalk 是 #FEE500（见 app/chatapps.py）
        return "me", bg, ink_h
    return ("her" if diff.max() >= 150 else "gray"), bg, ink_h


def similar(a, b):
    """同一段像素挪个位置 OCR 会抖（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」），按相似度判同一条。"""
    if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1  # 短句错一个字


class Reader:
    """一个会话一个 Reader：lh/seen 各自算各自的，切走再切回来不会把旧消息当新的重报一遍。"""

    def __init__(self, app: ChatApp = DEFAULT):
        self.app = app
        self.ocr = _engine(app)
        self.lh = None  # 正常气泡字高，头一帧定
        self.seen = []  # [(who, name, text)]，累计，封顶 500
        self.last_lines = []  # 上一帧的 [(who, text, y)]，只用来判画面往哪边挪
        self.view_back = False  # 画面停在这条会话的旧内容上（用户往上翻了）：这段不报新消息
        self.anchor = None  # 最后报出去的那条 (who, text)：整段对不上时用它认「回到最新了没」
        self.last_boxes = []  # 调试视图用：[(x0,y0,x1,y1,kind,text)]，消息区裁剪坐标
        self.last_ms = 0  # 上一帧 OCR 耗时

    def read(self, chat, pane_bg):
        """→ [(who, name, text, y)]，同一气泡的多行已合并。who ∈ me/her；name 群聊里是发言人，单聊 None。
        顺带把每个框的分类记进 self.last_boxes（调试视图画框用，几十个 tuple，不开也不亏）。"""
        skip = self.app.trim_top(chat)   # 群聊置顶公告等：不是消息，整块跳过
        if skip:
            chat = chat[skip:]
        t0 = time.perf_counter()
        res, _ = self.ocr(chat, use_cls=False)
        self.last_ms = int((time.perf_counter() - t0) * 1000)
        self.last_boxes = []
        W = chat.shape[1]
        # 群聊：每条 her 气泡上方一行灰色发言人名（靠左、短、不带冒号、印在面板底色上），从上往下扫，名字带给后面的气泡。
        # 引用块/时间戳/公告带冒号，链接卡片灰字印在气泡底色上，都不会被当成名字。
        # ponytail: 名字行被 OCR 漏掉时会挂到上一个人头上。
        name, raw = None, []
        # 按框顶排序：四个点不保证从左上角开始（斜着认出来的字会换个起点），取 min y 最稳
        for box, text, _ in sorted(res or [], key=lambda r: min(p[1] for p in r[0])):
            kind, bg, h = who_said(chat, box, self.app)
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            rect = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
            # 印在面板底色上的字都不是消息：气泡自带底色。微信的发言人名是灰字，
            # KakaoTalk 的是深色（对比度跟正文一样），所以不能只看 kind == "gray"。
            on_pane = np.abs(bg - pane_bg).sum() <= 6
            if kind == "gray" or (on_pane and kind == "her"):
                taken = bool(on_pane and box[0][0] < 0.25 * W and len(text) <= 16
                             and not re.search("[:：]", text))
                if taken:
                    name = text
                self.last_boxes.append(rect + ("name" if taken else "gray", text))
                continue
            if kind is None or (self.lh and h < 0.6 * self.lh):
                # 字比正常气泡小得多 = 图片消息（截图/表情包）里的字，不是气泡
                self.last_boxes.append(rect + ("image" if kind is None else "tiny", text))
                continue
            self.last_boxes.append(rect + (kind, text))
            raw.append((kind, name if kind == "her" else None, text, min(ys), max(ys), h))
        if not self.lh and len(raw) >= 3:
            self.lh = float(np.median([r[5] for r in raw]))
        # 同一气泡的多行合并：同人、上一行底到这一行顶的间距不到半个字高（不同气泡之间至少隔一个字高）
        lines = []
        for who, nm, text, top, bottom, h in raw:
            if lines and lines[-1][0] == who and lines[-1][1] == nm and top - lines[-1][4] < 0.6 * (self.lh or h):
                lines[-1][2] += self.app.join + text
                lines[-1][4] = bottom
            else:
                lines.append([who, nm, text, top, bottom])
        return [(w, n, t, y) for w, n, t, y, _ in lines]

    def _align(self, lines, prev):
        """本帧的行按顺序跟上一帧对上 → [(prev_y, cur_y)]。没配上的行不前进指针：
        只在真配上时才挪，免得前面一条认错就把后面全带偏；重复文本按出现顺序一一对上。"""
        out, j = [], 0
        for w, n, t, y in lines:
            k = j
            while k < len(prev):
                pw, pt, py = prev[k]
                if pw == w and similar(pt, t):
                    out.append((py, y))
                    j = k + 1
                    break
                k += 1
        return out

    def _track_view(self, lines):
        """画面往哪边挪：整段内容上移 = 在往新的方向走（新消息把旧的顶上去）；
        下移 = 用户往上翻，画面停在旧内容上。中位数抹掉个别行的抖动，±3px 以内当没动
        （动图表情每帧都在变，但同一行不会跳位置）。整段对不上（切会话、拖滚动条跳了很远）
        就沿用上次的判断——只有一种情况要纠正：最新那条又出现在画面里，说明已经回到最新了。"""
        shifts = sorted(cy - py for py, cy in self._align(lines, self.last_lines))
        if shifts:
            shift = shifts[len(shifts) // 2]
            if shift > 3:
                self.view_back = True
            elif shift < -3:
                self.view_back = False
        elif self.anchor and any(w == self.anchor[0] and similar(t, self.anchor[1])
                                 for w, n, t, y in lines):
            self.view_back = False
        self.last_lines = [(w, t, y) for w, n, t, y in lines]

    def new_lines(self, lines):
        """去重（滚动不重复）→ 这一帧里真正新出现的 [(who, name, text)]。
        本帧有已知行时只要已知行下方的：往上滚翻出来的旧消息在已知行上方，不算。
        本帧一行已知的都没有（大图把旧文字全顶出去了、切了聊天、滚远了）：全算，宁可多算不能漏。

        画面停在旧内容上（往上翻看历史）时不报：那段既不是新消息，报出去还会按到达顺序
        塞进记录和上下文，顺序跟聊天软件里对不上；但看到的照样记进 seen，翻回底部不重报。
        ponytail: 同一人连发两句一模一样的会吞一句——对触发分析无害。"""
        self._track_view(lines)
        known_y = [y for w, n, t, y in lines if self._seen(w, n, t)]
        floor = max(known_y) if known_y else -1
        new = [(w, n, t) for w, n, t, y in lines if y > floor and not self._seen(w, n, t)]
        self.seen.extend((w, n, t) for w, n, t, _ in lines if not self._seen(w, n, t))
        del self.seen[:-500]
        if self.view_back:
            return []
        if new:  # 最新报出去的那条：下次认「回到最新了没」
            self.anchor = (new[-1][0], new[-1][2])
        return new

    def _seen(self, who, name, text):
        # 名字不参与判重：名字行滚出画面后同一条消息会从 her(LO) 变成 her，不能算新消息
        return any(w == who and similar(t, text) for w, _, t in self.seen)


if __name__ == "__main__":
    # ponytail: 只测「哪一帧算新消息」这段（不加载 OCR 引擎、不截图）。
    # 顺序错乱和「翻上去看历史被当成新消息」都出在这一段，其余部分是像素活。
    def _lines(*specs):
        return [(w, None, t, y) for w, t, y in specs]

    def _fresh():
        r = Reader.__new__(Reader)  # 绕开 __init__：不建引擎
        r.app, r.seen, r.last_lines, r.view_back, r.anchor = DEFAULT, [], [], False, None
        return r

    r = _fresh()
    # 头一帧：屏幕上看到的全算（不报就等于永远不分析）
    assert r.new_lines(_lines(("her", "在吗", 100), ("me", "在的", 200))) == \
        [("her", None, "在吗"), ("me", None, "在的")]
    # 来新消息：内容整体上移 → 只报下面那条新的
    assert r.new_lines(_lines(("me", "在的", 80), ("her", "晚上吃啥", 180))) == \
        [("her", None, "晚上吃啥")]
    assert r.view_back is False
    # 往上翻看历史：内容整体下移 → 一条都不报（报了就会按到达顺序塞进记录，顺序全乱）
    assert r.new_lines(_lines(("her", "早", 200), ("me", "在的", 300),
                              ("her", "晚上吃啥", 400))) == []
    assert r.view_back is True
    # 停在旧内容上时继续翻：还是不报，但看到的都记进 seen 了
    assert r.new_lines(_lines(("her", "昨天那事", 150), ("her", "早", 250))) == []
    # 翻回底部：整段上移 → 恢复上报，翻过的那几条不重报
    assert r.new_lines(_lines(("me", "在的", 80), ("her", "晚上吃啥", 180))) == []
    assert r.view_back is False
    # 回到最新之后来新消息：照报
    assert r.new_lines(_lines(("me", "在的", 60), ("her", "晚上吃啥", 160),
                              ("her", "七点行吗", 260))) == [("her", None, "七点行吗")]
    # 对不上（切会话、滚远了）：沿用上次的方向，不在旧内容上就照报，宁可多算不能漏
    assert r.new_lines(_lines(("her", "八点见", 100), ("me", "好", 200))) == \
        [("her", None, "八点见"), ("me", None, "好")]
    r.view_back = True  # 翻着的时候画面整个换了一批（大图顶出去）：别把旧内容当新的
    assert r.new_lines(_lines(("her", "前天", 120), ("me", "嗯", 220))) == []
    assert r.view_back is True
    # 同一人连发两句一样的：第二句被吞（跟原来一样，对触发分析无害）
    r2 = _fresh()
    r2.new_lines(_lines(("her", "在吗", 100), ("her", "在吗", 200)))
    assert r2.new_lines(_lines(("her", "在吗", 90))) == []
    # 轻微抖动（OCR 抖一两个像素、动图表情）：方向不该乱跳
    r3 = _fresh()
    r3.new_lines(_lines(("her", "在吗", 100), ("me", "在的", 200)))
    r3.new_lines(_lines(("her", "在吗", 102), ("me", "在的", 198)))
    assert r3.view_back is False
    print("ocr.new_lines ok")
