# -*- coding: utf-8 -*-
"""技能效果的 A/B 实测：同一段对话，装技能 vs 不装技能各起草一次，对比口吻。
key 从注册表读（跟 app 同一套 _api_key），只调两次起草接口，费用几厘级。
    python probe/test_skill_ab.py [技能来源，默认 tong-jincheng-skill 仓库]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import skills
from core.engine import _draft_only

SOURCE = sys.argv[1] if len(sys.argv) > 1 else "github.com/hotcoffeeshake/tong-jincheng-skill"

# 按用户 exe 的真实配置走（E:\Downloads\Compressed\jev-chat-windows\config.json）：
# 起草走本地 magpie 网关的 workbuddy/deepseek-v4.1-flash，key 就是网关口令，
# 跟 app 同一套 _api_key 读取（注册表 LLM_API_KEY）。
from core.providers import DRAFT_PROVIDERS
PROVIDER = "custom_openai"
BASE_URL = "http://127.0.0.1:3425/v1"
MODEL = "workbuddy/deepseek-v4.1-flash"

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

print("== 拉取技能:", SOURCE)
data = None
for attempt in range(3):
    try:
        kind, value = skills.normalize_source(SOURCE)
        data = skills.fetch_github(value) if kind == "github" else skills.fetch_local(value)
        break
    except skills.SkillError as e:
        print(f"  attempt {attempt}: {e}")
        time.sleep(2)
if not data:
    print("技能拉不下来，A/B 做不了")
    raise SystemExit(1)
print(f"技能: {data['name']} · 描述: {data['description'][:60]} · 正文 {len(data['body'])} 字")

print("\n== 不装技能，起草中…")
plain = _draft_only(CONV, "romantic partners", provider=PROVIDER, model=MODEL,
                    base_url=BASE_URL, timeout=90, context=10, reply_to=None,
                    style="", thinking=False, skill=None)
for i, c in enumerate(plain["candidates"], 1):
    print(f"  {i}. {c}")

print("\n== 装技能，起草中…")
skilled = _draft_only(CONV, "romantic partners", provider=PROVIDER, model=MODEL,
                      base_url=BASE_URL, timeout=90, context=10, reply_to=None,
                      style="", thinking=False, skill=skills.skill_block(data))
for i, c in enumerate(skilled["candidates"], 1):
    print(f"  {i}. {c}")
