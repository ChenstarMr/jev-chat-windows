# -*- coding: utf-8 -*-
"""技能条布局回归：长名字/长描述必须单行省略，不能把启动开关和删除按钮挤出窗口。
真实事故：SKILL.md 的 description 有一整段话，QLabel 不换行时最小宽度 = 整句宽度，
开关和删除被顶出窗口右边，界面看起来就只剩一条文字。离屏跑，不碰真机。"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

from app.overlay import _ElideLabel

app = QApplication([])
win = QWidget()
win.resize(430, 80)  # 比用户截图的窄布局还紧一点
row = QHBoxLayout(win)
row.setContentsMargins(12, 8, 12, 8)
name = _ElideLabel(size=13, color="#233c2f", bold=True)
desc = _ElideLabel(size=11, color="#68776f")
row.addWidget(name, 1)
row.addWidget(desc, 1)
tail = QWidget()  # 占位代替开关+删除：只要它俩还在可见范围内就算过关
tail.setFixedSize(180, 30)
row.addWidget(tail)

LONG = "童锦程视角：以「深情祖师爷」、「情感内容创作者」的思维框架来看待人际关系、社交与表达，" * 6
name.setFull(LONG)
desc.setFull(LONG)
win.show()
app.processEvents()

for w in (name, desc):
    assert w.width() <= win.width() - 200, f"标签宽度失控：{w.width()}"
    assert len(w.text()) < len(LONG) and "…" in w.text(), "没省略，整句都画上去了"
    assert w.minimumSizeHint().width() < win.width(), "最小宽度还是整句话的宽度"
assert tail.x() + tail.width() <= win.width(), "右侧控件被挤出窗口了！"
print("skill bar elide ok")
