# -*- coding: utf-8 -*-
"""多技能 UI 逻辑回归（离屏）：技能列表渲染 N 条、开关只动那一条、删除只删那一条、
安装追加（同 source 替换）。app.settings 整体换成假对象，不碰真机配置。"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

demo = {"skills": [
    {"name": "tong-jincheng-perspective", "description": "d1", "body": "b1",
     "source": "github.com/hotcoffeeshake/tong-jincheng-skill", "enabled": True},
    {"name": "create-partner", "description": "d2", "body": "b2",
     "source": "github.com/NatalieCao323/partner-skill", "enabled": False}],
    "relationship": "friends", "context": 10, "style": "", "lang": "zh",
    "reply_target": False, "thinking": False, "check_update": True, "debug_view": False,
    "app": "auto", "use_jev": True, "jev_provider": "openrouter", "jev_model": "",
    "jev_base_url": "", "draft_provider": "deepseek", "draft_model": "", "draft_base_url": ""}

saved = {}


def fake_save(*_a, **kw):
    if kw.get("skills_data") is not None:
        demo["skills"] = [dict(s) for s in kw["skills_data"]]
        saved["calls"] = saved.get("calls", 0) + 1


fake = SimpleNamespace(
    relationship=lambda: demo["relationship"], context=lambda: demo["context"],
    style=lambda: demo["style"], language=lambda: demo["lang"],
    reply_target=lambda: demo["reply_target"], thinking=lambda: demo["thinking"],
    check_update=lambda: demo["check_update"], debug_view=lambda: demo["debug_view"],
    app=lambda: demo["app"], use_jev=lambda: demo["use_jev"],
    jev_provider=lambda: demo["jev_provider"], jev_model=lambda: demo["jev_model"],
    jev_base_url=lambda: demo["jev_base_url"], draft_provider=lambda: demo["draft_provider"],
    draft_model=lambda: demo["draft_model"], draft_base_url=lambda: demo["draft_base_url"],
    skills_list=lambda: [dict(s) for s in demo["skills"]], skill_text=lambda: "",
    has_key=lambda: True, has_jev_key=lambda: True, has_llm_key=lambda: True,
    jev_key=lambda: "k", llm_key=lambda: "k", save=fake_save)

app = QApplication([])
import app.overlay  # noqa: E402  先把 app.settings 绑定进模块命名空间，patch 才有落点
from unittest.mock import patch  # noqa: E402

with patch("app.overlay.settings", fake):
    from app.overlay import Overlay

    ov = Overlay(on_fill=lambda t: None, result_of=lambda t: None)
    ov.open_settings()
    assert ov.skillBars.count() == 2, ov.skillBars.count()

    # 开关只落盘、不重铺：重铺会把刚拨的开关拆了重建，滑动动画从头再播（用户实测抽搐）
    orig_reload, reload_calls = ov._reload_skills, []
    ov._reload_skills = lambda *a, **k: reload_calls.append(1)
    ov._skill_toggled(1, True)  # 只动第二条
    assert demo["skills"][1]["enabled"] is True and demo["skills"][0]["enabled"] is True
    ov._skill_toggled(0, False)
    assert demo["skills"][0]["enabled"] is False and demo["skills"][1]["enabled"] is True
    assert not reload_calls, "开关不应触发列表重铺"
    ov._reload_skills = orig_reload

    ov._delete_skill(0)  # 只删第一条
    assert [s["name"] for s in demo["skills"]] == ["create-partner"]
    assert ov.skillBars.count() == 1

    # 安装追加：source 相同的旧条目被替换，不重复
    ov._skill_installed({"ok": True, "msg": "", "data": [
        {"name": "create-partner", "description": "d2b", "body": "b2b",
         "source": "github.com/NatalieCao323/partner-skill"},
        {"name": "new-one", "description": "d3", "body": "b3",
         "source": "github.com/a/new-skill"}]})
    names = [s["name"] for s in demo["skills"]]
    assert names == ["create-partner", "new-one"], names
    assert all(s.get("enabled") is True for s in demo["skills"]), demo["skills"]
    assert ov.skillBars.count() == 2

    # 异源追加：装第三个不动前两个（v0.1.32 的洞：source 恒空，装新的洗掉全部旧的）
    ov._skill_installed({"ok": True, "msg": "", "data": [
        {"name": "create-ex", "description": "d4", "body": "b4",
         "source": "therealXiaomanChu/ex-skill"}]})
    assert [s["name"] for s in demo["skills"]] == ["create-partner", "new-one", "create-ex"]
    assert ov.skillBars.count() == 3

    # 老版本装的条目没盖 source：重装同款按名字认亲替换，不出双份
    demo["skills"].append({"name": "legacy-one", "description": "d0", "body": "b0",
                           "source": "", "enabled": False})
    ov._reload_skills()
    ov._skill_installed({"ok": True, "msg": "", "data": [
        {"name": "legacy-one", "description": "d0b", "body": "b0b",
         "source": "someone/legacy-one"}]})
    names = [s["name"] for s in demo["skills"]]
    assert names.count("legacy-one") == 1, names
    assert names == ["create-partner", "new-one", "create-ex", "legacy-one"], names
    assert ov.skillBars.count() == 4

    # 备注：_set_note 只改那一条 + 原地刷新标题，不重铺列表
    orig_reload, reload_calls = ov._reload_skills, []
    ov._reload_skills = lambda *a, **k: reload_calls.append(1)
    ov._set_note(0, "现任技能")
    assert demo["skills"][0]["note"] == "现任技能"
    assert not reload_calls, "备注保存不应重铺列表"
    assert ov.skillBars.itemAt(0).widget()._title._full == "现任技能"
    ov._reload_skills = orig_reload
    ov._reload_skills()  # 重铺后标题仍显示备注（note 优先于英文原名）
    assert ov.skillBars.itemAt(0).widget()._title._full == "现任技能"

    # 重装同款：备注跟过去（同 source 替换时继承）
    ov._skill_installed({"ok": True, "msg": "", "data": [
        {"name": "create-partner", "description": "d2c", "body": "b2c",
         "source": "github.com/NatalieCao323/partner-skill"}]})
    assert [s["name"] for s in demo["skills"]].count("create-partner") == 1
    assert demo["skills"][0].get("note") == "现任技能", demo["skills"][0]

    # 留空 = 清备注，恢复显示原名
    ov._set_note(0, "")
    assert demo["skills"][0]["note"] == ""
    assert ov.skillBars.itemAt(0).widget()._title._full == "create-partner"

    # 老条目（没 source）按名字认亲替换时，备注也跟过去
    demo["skills"].append({"name": "old-bare", "description": "d", "body": "b",
                           "source": "", "note": "老备注", "enabled": True})
    ov._skill_installed({"ok": True, "msg": "", "data": [
        {"name": "old-bare", "description": "d2", "body": "b2",
         "source": "someone/old-bare"}]})
    got = [s for s in demo["skills"] if s["name"] == "old-bare"]
    assert len(got) == 1 and got[0]["note"] == "老备注", got
    print("overlay multi-skill ok:", [s["name"] for s in demo["skills"]])
