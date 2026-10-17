# -*- coding: utf-8 -*-
"""「技能」= 用户安装的人设/口吻包（SKILL.md 形态，如 github.com/hotcoffeeshake/tong-jincheng-skill）。

这类 skill 本质是一份结构化大 prompt（人设 + 思维框架 + 语录 + 表达风格），没有任何可执行代码。
这里只做四件事：取下来（GitHub raw / 本地文件）→ 解析（front-matter + 正文）→ 截断 → 拼成
起草 system prompt 的一段。只影响起草的口吻与视角：安全硬规则照旧压着它（见 core/draft._system），
判断题口径不碰，采集与发送不碰。装好的技能存 config.json 的 skill 字段，key 绝不落这里。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

MAX_BODY = 4000   # 字符。SKILL.md 普遍几万字，全塞等于每次起草多烧一两个千 token，第一版截断
_TIMEOUT = 15     # 拉取超时（秒）
_UA = {"User-Agent": "jev-chat-windows"}   # GitHub 的接口吃 UA，裸 urllib 默认头可能被拒


class SkillError(Exception):
    """安装/解析失败，一句话原因直接给设置页显示。"""


def normalize_source(text: str) -> tuple[str, str]:
    """用户填的一行 → ("github", owner/repo) 或 ("local", 路径)。

    GitHub 侧认四种写法：仓库主页（github.com/o/r）、带 .git、tree/分支、blob/raw 直达
    SKILL.md 的完整链接。其余一律当本地路径（文件夹或 .md 文件都行）。"""
    s = (text or "").strip().strip("`\"' ")
    if not s:
        raise SkillError("先填 GitHub 仓库地址或本地 SKILL.md 路径")
    if re.match(r"https?://", s, re.I) or re.match(r"(?:github\.com|raw\.githubusercontent\.com)/", s, re.I):
        # 认四种写法：仓库主页、带 .git、tree/分支、blob/raw 直达 SKILL.md 的完整链接（可裸域名无协议）。
        # raw 主机名的 URL 是 /o/r/分支/路径（没有 tree|blob 字样），单独一条分支解析
        m = (re.search(r"raw\.githubusercontent\.com/([^/\s]+)/([^/\s#]+)/([^\s/]+)(/[^\s]+)?", s, re.I)
             or re.search(r"github\.com/([^/\s]+)/([^/\s#]+?)(?:\.git)?"
                          r"(?:/(?:tree|blob)/([^/\s]+)(/[^\s]+)?)?(?:\.md)?/?$", s, re.I))
        if not m:
            raise SkillError("认不出这个 GitHub 链接：要 github.com/用户/仓库 这样的仓库地址")
        owner, repo = m.group(1), m.group(2)
        return "github", f"{owner}/{repo}"
    return "local", s


def _fetch_url(url: str) -> str:
    """GET 一个文本，404 返回 None 之外的状态/网络错误转 SkillError（带人话）。"""
    req = urllib.request.Request(url, headers={**_UA, "Accept": "text/plain, application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ""
        raise SkillError(f"拉取失败 HTTP {exc.code}: {exc.reason}") from None
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise SkillError(f"拉取失败: {str(exc)[:120]}") from None


def fetch_github(slug: str) -> dict:
    """owner/repo → 拉 SKILL.md。分支按 main → master 顺序试；raw 404 且没有分支信息时
    也试 api.github.com 的 contents 端点（有些网关两个 host 通的不一样）。"""
    branches = ("main", "master")
    text = ""
    for branch in branches:
        text = _fetch_url(f"https://raw.githubusercontent.com/{slug}/{branch}/SKILL.md")
        if text:
            break
    if not text:
        text = _fetch_url(f"https://api.github.com/repos/{slug}/contents/SKILL.md")
        if text:  # contents 端点返回 JSON，内容在 base64 字段里
            try:
                data = json.loads(text)
                text = ""
                if data.get("encoding") == "base64" and data.get("content"):
                    import base64
                    text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                elif isinstance(data.get("download_url"), str) and data["download_url"]:
                    text = _fetch_url(data["download_url"])
            except (ValueError, KeyError):
                text = ""
    if not text:
        raise SkillError("这个仓库里没找到 SKILL.md（根目录，main/master 分支）")
    return parse_skill(text, fallback_name=slug.split("/")[-1])


def fetch_local(path_text: str) -> dict:
    """本地来源：文件夹（找 SKILL.md/skill.md）或直接一个 .md 文件。"""
    p = Path(path_text)
    if p.is_dir():
        hit = next((p / n for n in ("SKILL.md", "skill.md", "Skill.md") if (p / n).is_file()), None)
        if hit is None:
            raise SkillError("这个文件夹里没有 SKILL.md")
        p = hit
    if not p.is_file():
        raise SkillError(f"路径不存在：{path_text[:120]}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SkillError(f"读不了文件: {str(exc)[:120]}") from None
    return parse_skill(text, fallback_name=p.stem)


def parse_skill(text: str, fallback_name: str = "") -> dict:
    """SKILL.md 文本 → {name, description, body, source}。front-matter（开头的 --- 块）里的
    name/description 优先；description 的 YAML 块标量（`|`/`>` 换行段）按多行文本取；
    正文截到 MAX_BODY，尽量在段落边界断开。"""
    text = text.replace("\r\n", "\n").strip()
    name, description = "", ""
    if text.startswith("---"):
        head, _, rest = text[3:].partition("\n---")
        if rest:  # 真有 front-matter；没有就把开头当正文
            lines = head.splitlines()
            for i, line in enumerate(lines):
                kv = re.match(r"\s*(name|description)\s*:\s*(.*)", line)
                if not kv:
                    continue
                value = kv.group(2).strip()
                if value in ("|", ">"):  # YAML 块标量：取缩进更深的后续行拼起来
                    parts = []
                    for cont in lines[i + 1:]:
                        if not cont.strip() or not cont[0].isspace():
                            break
                        parts.append(cont.strip())
                    value = " ".join(parts)
                elif value.startswith(("\"", "'")):
                    value = value.strip("\"'")
                if kv.group(1) == "name":
                    name = value
                else:
                    description = value
            text = rest.partition("\n")[2] if "\n" in rest else ""
    body = text.strip()
    if not body:
        raise SkillError("SKILL.md 里没有正文")
    if len(body) > MAX_BODY:
        cut = body.rfind("\n\n", 0, MAX_BODY)
        body = body[:cut if cut > MAX_BODY // 2 else MAX_BODY].rstrip()
        body += "\n\n（技能太长，只装了开头这部分）"
    return {"name": name or fallback_name or "未命名技能",
            "description": description or body.strip().splitlines()[0][:80],
            "body": body, "source": ""}


def skill_block(data: dict) -> str:
    """装好的技能 → 拼进起草 system 的那段。身份与安全规则在它前面（core/draft._RULES），
    输出格式在它后面（_OUT_*），夹在中间的技能只许管「怎么说、往哪个视角想」。"""
    return ("\n\n技能（用户安装的「" + str(data.get("name") or "技能") + "」人设/口吻包，"
            "写候选时借它的视角和口吻；它只管怎么说，输出格式仍按最后一段执行，"
            "上面的身份设定和安全规则照旧压着它，转账红包借钱照旧绝不提）：\n"
            + str(data.get("body") or ""))


if __name__ == "__main__":
    # ponytail: 全离线。链接归一化、front-matter 解析、截断、拼接块是唯一会坏的四块。
    assert normalize_source("github.com/hotcoffeeshake/tong-jincheng-skill") == ("github", "hotcoffeeshake/tong-jincheng-skill")
    assert normalize_source("https://github.com/a/b.git") == ("github", "a/b")
    assert normalize_source("https://github.com/a/b/tree/dev") == ("github", "a/b")
    assert normalize_source("https://github.com/a/b/blob/main/SKILL.md") == ("github", "a/b")
    assert normalize_source("https://raw.githubusercontent.com/a/b/main/SKILL.md") == ("github", "a/b")
    assert normalize_source(r"E:\skills\我的技能") == ("local", r"E:\skills\我的技能")
    try:
        normalize_source("")
        raise SystemExit("应当抛错")
    except SkillError:
        pass

    md = "---\nname: 童锦程视角\ndescription: 深情祖师爷的思维框架\n---\n\n## 他是谁\n\n正文……\n"
    data = parse_skill(md, fallback_name="tong-jincheng-skill")
    assert data["name"] == "童锦程视角" and data["description"] == "深情祖师爷的思维框架"
    assert data["body"].startswith("## 他是谁")
    assert "童锦程视角" in skill_block(data) and data["body"] in skill_block(data)
    # YAML 块标量（`|` 多行 description，真实仓库就是这个形状）
    block = parse_skill("---\nname: n\ndescription: |\n  第一行\n  第二行\n---\n\n正文")
    assert block["description"] == "第一行 第二行", block["description"]
    # 没有 front-matter：名字退 fallback，description 退正文第一行
    plain = parse_skill("# 纯正文\n第二行", fallback_name="plain")
    assert plain["name"] == "plain" and plain["description"].startswith("# 纯正文")
    # 截断：超长在段落边界断，带提示
    long_md = "段落一。\n\n" + ("很长的段落。" * 2000)
    got = parse_skill(long_md)
    assert len(got["body"]) <= MAX_BODY + 40 and "只装了开头" in got["body"]
    try:
        parse_skill("---\nname: x\n---\n正文占位")
        parse_skill("")  # 空文本没有正文，应当抛错
        raise SystemExit("空正文应当抛错")
    except SkillError:
        pass
    print("skills ok")
