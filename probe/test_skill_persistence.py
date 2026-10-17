# -*- coding: utf-8 -*-
"""settings 技能持久化回归：普通保存不得洗掉技能列表（用户实测炸过两次）；
老的单数 skill 字段自动迁移成 skills 列表。临时 config + 封死 env/注册表写入。"""
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

A = {"name": "tong-jincheng-perspective", "description": "d", "body": "正文A",
     "source": "github.com/hotcoffeeshake/tong-jincheng-skill", "enabled": True}
B = {"name": "create-partner", "description": "d", "body": "正文B",
     "source": "github.com/NatalieCao323/partner-skill", "enabled": False}

# 旧格式迁移：手工写一份老版 config（单数 skill + skill_enabled）
Path(s._CONFIG).write_text(json.dumps({"skill": {k: A[k] for k in ("name", "description", "body", "source")},
                                       "skill_enabled": False,
                                       "relationship": "friends"}, ensure_ascii=False), encoding="utf-8")
legacy = s.skills_list()
assert len(legacy) == 1 and legacy[0]["name"] == A["name"] and legacy[0]["enabled"] is False

# 一次普通保存：迁移正式落成 skills 列表，内容不丢
s.save(lang_text="ko")
disk = json.loads(Path(s._CONFIG).read_text(encoding="utf-8"))
assert [x["name"] for x in disk["skills"]] == [A["name"]] and disk["skills"][0]["enabled"] is False

# 多技能：装两个、开关只动一条、普通保存原样保留
s.save(skills_data=[dict(A), dict(B)])
assert [x["name"] for x in s.skills_list()] == [A["name"], B["name"]]
now = s.skills_list()
now[0]["enabled"] = False
s.save(skills_data=now)
s.save(debug_view_on=False)  # 普通保存：技能列表必须原样保留
s.save(lang_text="en")
got = s.skills_list()
assert got[0]["enabled"] is False and got[1]["enabled"] is False, got
assert s.skill_text() == "", "全停用后技能段应为空"

# 磁盘上的 JSON 真的带着列表
assert len(json.loads(Path(s._CONFIG).read_text(encoding="utf-8"))["skills"]) == 2

# 启用一条：skill_text 必须拼出正文（NameError 回归——v0.1.32 漏 import，装了技能一生成必炸）
s.save(skills_data=[dict(A, enabled=True)])
text = s.skill_text()
assert A["body"] in text and A["name"] in text, text[:120]

# 备注跟着条目走：普通保存不洗掉（界面给英文技能起的中文名）
s.save(skills_data=[dict(A, enabled=True, note="现任技能")])
s.save(debug_view_on=False)  # 普通保存
assert s.skills_list()[0]["note"] == "现任技能"

# 清空：列表落成 []
s.save(skills_data=[])
assert s.skills_list() == [] and json.loads(Path(s._CONFIG).read_text(encoding="utf-8"))["skills"] == []
print("settings skills persistence ok")
