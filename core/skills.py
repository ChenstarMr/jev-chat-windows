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
    """用户填的一行 → ("github"|"gitee"|"url"|"local", 值)。

    认五种写法：
    - github.com/o/r（主页 / .git / tree / blob / raw 直链都行，blob/raw 直达某个 .md 的
      会带上文件路径，装的就是那个文件，不再只认根目录 SKILL.md）；
    - gitee.com/o/r（同上，国内网络 GitHub 时好时坏，Gitee 是备胎）；
    - 任何 http(s) 直链 .md（自建仓库、图床、gist raw 之类，抓下来就解析）；
    - 本地路径（.md 文件 / 含 SKILL.md 的目录 / 嵌套目录一次装全部 / .zip 压缩包）。
    其余一律当本地路径。"""
    s = (text or "").strip().strip("`\"' ")
    if not s:
        raise SkillError("先填 GitHub/Gitee 仓库地址、.md 直链或本地路径")
    if re.match(r"https?://", s, re.I) or re.match(r"(?:[\w-]+\.)?(?:github\.com|gitee\.com)/", s, re.I):
        m = (re.search(r"(?:raw\.githubusercontent|raw)\.gitee\.com/([^/\s]+)/([^/\s#]+)"
                      r"(?:/([^/\s]+))?(/[^\s#]+)", s, re.I)
             or re.search(r"raw\.githubusercontent\.com/([^/\s]+)/([^/\s#]+)/([^\s/]+)(/[^\s#]+)?", s, re.I)
             or re.search(r"github\.com/([^/\s]+)/([^/\s#]+?)(?:\.git)?"
                          r"(?:/(?:tree|blob|raw)/([^/\s]+)(/[^\s#]+)?)?(?:\.md)?/?$", s, re.I)
             or re.search(r"gitee\.com/([^/\s]+)/([^/\s#]+?)(?:\.git)?"
                          r"(?:/(?:tree|blob|raw)/([^/\s]+)(/[^\s#]+)?)?(?:\.md)?/?$", s, re.I))
        if not m:
            # 认不出仓库形状的 http(s) 链接：指向 .md 的直链直接抓，别的当错误
            if re.search(r"\.md($|[?#])", s, re.I):
                return "url", s
            raise SkillError("认不出这个链接：要 仓库地址（github.com/用户/仓库）、.md 直链或本地路径")
        host, owner, repo = m.group(0).split("/")[0] if "//" in m.group(0) else "", m.group(1), m.group(2)
        kind = "gitee" if ("gitee" in s.lower()) else "github"
        branch, path = m.group(3), m.group(4)
        value = f"{owner}/{repo}"
        if branch or path:  # 直达某个分支/文件的：带上，装的就是那个文件
            value += f"#{branch or ''}#{path or ''}"
        return kind, value
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


def _fetch_repo_md(host: str, slug: str, branch: str = "", path: str = "") -> str:
    """从 GitHub/Gitee 拉一个 .md 的文本。优先级：用户直指的分支+路径 → 根 SKILL.md
    （main/master）→ API 兜底 → 整棵仓库树里找任意深度的 SKILL.md（子目录布局的仓库）。"""
    scheme = "https://"
    if host == "gitee":
        raws = [f"{scheme}gitee.com/{slug}/raw/"]
        api = f"{scheme}gitee.com/api/v5/repos/{slug}"
        tree_api = f"{scheme}gitee.com/api/v5/repos/{slug}/git/trees/"
    else:
        raws = [f"{scheme}raw.githubusercontent.com/{slug}/", f"{scheme}raw.githubusercontent.com/{slug}/"]
        api = f"{scheme}api.github.com/repos/{slug}"
        tree_api = f"{scheme}api.github.com/repos/{slug}/git/trees/"
    branches = ([branch] if branch else []) + [b for b in ("main", "master") if b != branch]
    # 用户点了某个具体文件：branches × 那个路径，一个 404 都不认输（分支猜错很常见）
    if path:
        p = path.lstrip("/")
        for b in branches:
            for raw in raws:
                text = _fetch_url(f"{raw}{b}/{p}")
                if text:
                    return text
        for b in branches:  # contents 端点兜底（base64），GitHub/Gitee 形状一致
            text = _fetch_url(f"{api}/contents/{p}?ref={b}")
            if text:
                got = _b64_or_download(text)
                if got:
                    return got
    else:
        for b in branches:
            for raw in raws:
                text = _fetch_url(f"{raw}{b}/SKILL.md")
                if text:
                    return text
        text = _fetch_url(f"{api}/contents/SKILL.md")
        if text:
            got = _b64_or_download(text)
            if got:
                return got
        # 根目录没有：整棵树里找 SKILL.md（任意深度，最深也认）；取最浅的那个
        text = _fetch_url(tree_api + (branch or "HEAD") + "?recursive=1")
        if text:
            try:
                paths = [e["path"] for e in json.loads(text).get("tree", [])
                         if e.get("type") == "blob" and e.get("path", "").upper().endswith("/SKILL.MD")]
            except (ValueError, KeyError):
                paths = []
            if paths:
                best = min(paths, key=lambda p: (p.count("/"), p))
                b = branch or "main"
                for br in (b, "main", "master"):
                    for raw in raws:
                        got = _fetch_url(f"{raw}{br}/{best}")
                        if got:
                            return got
    return ""


def _b64_or_download(content_json_text: str) -> str:
    """contents 端点返回的 JSON：base64 就地解，download_url 就再抓。解析不出返回空。"""
    try:
        data = json.loads(content_json_text)
        if data.get("encoding") == "base64" and data.get("content"):
            import base64
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        if isinstance(data.get("download_url"), str) and data["download_url"]:
            return _fetch_url(data["download_url"])
    except (ValueError, KeyError):
        pass
    return ""


def fetch_github(slug: str) -> dict:
    """github owner/repo[#branch#path] → SKILL.md 文本 → 单条技能。找不到抛 SkillError。"""
    branch, _, path = slug.partition("#")
    slug = slug.split("#")[0]
    text = _fetch_repo_md("github", slug, branch=branch.strip("/"), path=path)
    if not text:
        raise SkillError("这个仓库里没找到 SKILL.md（根目录和所有子目录都翻了）")
    return parse_skill(text, fallback_name=slug.split("/")[-1])


def fetch_gitee(slug: str) -> dict:
    """gitee 同款。"""
    branch, _, path = slug.partition("#")
    slug = slug.split("#")[0]
    text = _fetch_repo_md("gitee", slug, branch=branch.strip("/"), path=path)
    if not text:
        raise SkillError("这个 Gitee 仓库里没找到 SKILL.md（根目录和所有子目录都翻了）")
    return parse_skill(text, fallback_name=slug.split("/")[-1])


def fetch_url(url: str) -> dict:
    """任意 http(s) .md 直链。"""
    text = _fetch_url(url.split("#")[0])
    if not text:
        raise SkillError("这个链接拉不下来（404 或网络不通）")
    name = url.rstrip("/?#").split("/")[-1] or "skill"
    return parse_skill(text, fallback_name=name.removesuffix(".md"))


def _zip_skills(p: Path) -> list[dict]:
    """zip 压缩包：所有 SKILL.md 条目各算一个技能。"""
    import zipfile

    out = []
    try:
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist()
                     if not n.endswith("/") and n.split("/")[-1].upper() == "SKILL.MD"
                     and "__MACOSX" not in n]
            if not names:
                # 整个包里没有 SKILL.md，但只有一个 .md 的话，拿它当技能正文
                md = [n for n in z.namelist() if not n.endswith("/") and n.lower().endswith(".md")
                      and "__MACOSX" not in n]
                names = md[:1]
            for n in names:
                text = z.read(n).decode("utf-8", errors="replace")
                stem = n.split("/")[-1].removesuffix(".md") or p.stem
                out.append(parse_skill(text, fallback_name=(n.split("/")[-2] + "-" + stem)
                                                       if len(n.split("/")) > 1 and stem.upper() == "SKILL.MD"
                                                       else stem))
    except (zipfile.BadZipFile, OSError) as exc:
        raise SkillError(f"zip 读不了: {str(exc)[:120]}") from None
    if not out:
        raise SkillError("压缩包里没找到 SKILL.md")
    return out


def fetch_local(path_text: str) -> list[dict]:
    """本地来源，能装多少装多少（返回列表）：
    .md 文件 → 一个；.zip → 包里所有 SKILL.md；目录 → 根 SKILL.md 一个，
    根没有就递归找全部 SKILL.md（嵌套目录布局一次装齐），全没有但只有一个 .md 就拿它。"""
    p = Path(path_text)
    if p.suffix.lower() == ".zip":
        return _zip_skills(p)
    if p.suffix.lower() == ".md":
        if not p.is_file():
            raise SkillError(f"路径不存在：{path_text[:120]}")
        return [parse_skill(_read_local(p), fallback_name=p.stem.removesuffix(".md"))]
    if p.is_dir():
        hit = next((p / n for n in ("SKILL.md", "skill.md", "Skill.md") if (p / n).is_file()), None)
        if hit:
            return [parse_skill(_read_local(hit), fallback_name=p.name)]
        found = sorted(q for q in p.rglob("*.md") if q.name.upper() == "SKILL.MD")
        if found:
            return [parse_skill(_read_local(q), fallback_name=q.parent.name) for q in found]
        only = [q for q in p.rglob("*.md") if "__MACOSX" not in str(q)]
        if len(only) == 1:
            return [parse_skill(_read_local(only[0]), fallback_name=only[0].stem)]
        raise SkillError("这个文件夹里没找到 SKILL.md（根目录和子目录都翻了）")
    raise SkillError(f"路径不存在：{path_text[:120]}")


def _read_local(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SkillError(f"读不了文件: {str(exc)[:120]}") from None


def install(text: str) -> list[dict]:
    """设置页「安装」入口：一行来源 → 归一化 → 拉取 → 技能列表（至少一条，否则抛 SkillError）。"""
    kind, value = normalize_source(text)
    if kind == "github":
        return [fetch_github(value)]
    if kind == "gitee":
        return [fetch_gitee(value)]
    if kind == "url":
        return [fetch_url(value)]
    return fetch_local(value)


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
    输出格式在它后面（_OUT_*），夹在中间的技能只许管「怎么说、往哪个视角想」。
    「说了算」是刻意写死的：装了技能时起草那边的「模仿 me」风格规则已给它让路，这里再钉一次优先级，
    不然两条口吻指令打架，技能永远被压住（用户实测反馈「感觉不到技能在起作用」就是这么来的）。"""
    return ("\n\n技能（用户安装的「" + str(data.get("name") or "技能") + "」人设/口吻包，"
            "三条候选的口吻、视角、用词以它为准——它说了算；它只管怎么说，输出格式仍按最后一段执行，"
            "上面的身份设定和安全规则照旧压着它，转账红包借钱照旧绝不提）：\n"
            + str(data.get("body") or ""))


if __name__ == "__main__":
    # ponytail: 全离线。链接归一化、front-matter 解析、截断、拼接块是唯一会坏的四块。
    assert normalize_source("github.com/hotcoffeeshake/tong-jincheng-skill") == ("github", "hotcoffeeshake/tong-jincheng-skill")
    assert normalize_source("https://github.com/a/b.git") == ("github", "a/b")
    assert normalize_source("https://github.com/a/b/tree/dev") == ("github", "a/b#dev#")
    assert normalize_source("https://github.com/a/b/blob/main/SKILL.md") == ("github", "a/b#main#/SKILL.md")
    assert normalize_source("https://raw.githubusercontent.com/a/b/main/SKILL.md") == ("github", "a/b#main#/SKILL.md")
    # 子目录布局：blob 直链带路径 → 装的就是那个文件
    assert normalize_source("github.com/a/b/blob/main/docs/skills/x/SKILL.md") == ("github", "a/b#main#/docs/skills/x/SKILL.md")
    # Gitee（raw 与仓库页两种形状）
    assert normalize_source("https://gitee.com/a/b/raw/master/SKILL.md") == ("gitee", "a/b#master#/SKILL.md")
    assert normalize_source("gitee.com/a/b") == ("gitee", "a/b")
    # 任意 http(s) .md 直链
    assert normalize_source("https://example.com/skills/my.md") == ("url", "https://example.com/skills/my.md")
    assert normalize_source("https://gist.githubusercontent.com/x/raw/y/note.md") == ("url", "https://gist.githubusercontent.com/x/raw/y/note.md")
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
    # 本地多技能：目录里嵌套若干 SKILL.md 一次装齐；zip 同理（内存里造一个）
    import tempfile
    import zipfile

    root = Path(tempfile.mkdtemp())
    (root / "a").mkdir()
    (root / "a" / "SKILL.md").write_text("---\nname: 甲\n---\n甲正文", encoding="utf-8")
    (root / "b" / "c").mkdir(parents=True)
    (root / "b" / "c" / "SKILL.md").write_text("---\nname: 乙\n---\n乙正文", encoding="utf-8")
    many = fetch_local(str(root))
    assert [s["name"] for s in many] == ["甲", "乙"], many
    zpath = root / "pack.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("pack/a/SKILL.md", "---\nname: 甲zip\n---\n正文")
        z.writestr("pack/b/SKILL.md", "---\nname: 乙zip\n---\n正文")
    assert [s["name"] for s in fetch_local(str(zpath))] == ["甲zip", "乙zip"]
    # 单文件 / 根目录 SKILL.md
    (root / "solo.md").write_text("纯正文技能", encoding="utf-8")
    solo = fetch_local(str(root / "solo.md"))
    assert len(solo) == 1 and solo[0]["name"] == "solo"
    print("skills ok")
