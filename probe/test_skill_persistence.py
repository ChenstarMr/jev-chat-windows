# -*- coding: utf-8 -*-
"""settings 技能持久化回归：普通保存不得洗掉 skill / skill_enabled（用户实测炸过两次）。
临时 config + 封死 env/注册表写入，不碰真机配置。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.settings as s

s._CONFIG = str(Path(tempfile.mkdtemp()) / "config.json")
s._read_env = lambda name: ""       # 不读进程环境/注册表
s._set_key = lambda name, value: None
s._notify_env = lambda: None

SKILL = {"name": "童锦程视角", "description": "d", "body": "正文", "source": "github.com/a/b"}

s.save(skill_data=dict(SKILL), skill_enabled_on=True)
assert s.skill_installed() == SKILL and s.skill_enabled() is True

# 各种「普通保存」——曾经的洞：每一次都会把 skill 整个抹掉
s.save(lang_text="ko")
s.save(debug_view_on=False)
s.save(use_jev_on=False)
s.save(relationship_text="朋友")
assert s.skill_installed() == SKILL, "普通保存洗掉了技能！"
assert s.skill_enabled() is True, "普通保存洗掉了开关！"

# 磁盘上的 JSON 也得真的带着它
assert json.loads(Path(s._CONFIG).read_text(encoding="utf-8"))["skill"] == SKILL

# 只关开关：技能体留着；再开回来
s.save(skill_enabled_on=False)
assert s.skill_installed() == SKILL and s.skill_enabled() is False
s.save(skill_enabled_on=True)

# 显式删除：键从磁盘上消失
s.save(skill_data={})
assert s.skill_installed() == {} and json.loads(Path(s._CONFIG).read_text(encoding="utf-8")).get("skill") is None

# 重装 + 普通保存，再守一遍
s.save(skill_data=dict(SKILL), skill_enabled_on=True)
s.save()
assert s.skill_installed() == SKILL
print("settings skill persistence ok")
