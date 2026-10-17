# -*- coding: utf-8 -*-
"""A/B 补充：用用户实际安装的 create-partner 技能起草同一对话。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import skills
from core.engine import _draft_only

CONV = [
    ("her", "在忙吗"),
    ("me", "刚忙完 怎么了"),
    ("her", "我跟你讲 我今天在地铁上看到一个人 巨像我前任"),
    ("me", "哈哈哈哈然后呢 你没多看两眼？"),
    ("her", "看了啊 越看越像 然后我差点叫出来了"),
    ("me", "幸好没叫 出社死了"),
    ("her", "就是说 你说这世界上真的有这么像的两个人吗"),
    ("her", "搞得我心里怪怪的 有点想他了"),
]
kind, value = skills.normalize_source("github.com/NatalieCao323/partner-skill")
data = skills.fetch_github(value)
print("== 装你自己装的那个技能（create-partner）起草：")
r = _draft_only(CONV, "romantic partners", provider="custom_openai",
                model="workbuddy/deepseek-v4.1-flash", base_url="http://127.0.0.1:3425/v1",
                timeout=90, context=10, reply_to=None, style="", thinking=False,
                skill=skills.skill_block(data))
for i, c in enumerate(r["candidates"], 1):
    print(f"  {i}. {c}")
