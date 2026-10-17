# -*- coding: utf-8 -*-
"""Jev 判断 API 客户端：OpenRouter、基元律动、OpenCode Zen、自定义、TypeSafe 直连。

五条路分两种：OpenRouter / 基元律动 / OpenCode Zen / 自定义都是同一套 SystemOne 形状的 HTTP
（POST {model, state, questions} → {answers, usage}），只是地址、key、模型 id 和固定头不同，
由 core/providers.JEV_PROVIDERS 描述，这里手写 urllib 发；TypeSafe 直连走官方 `typesafe_sdk`
（它把路径写死成 /v1/systemone，所以只有它用 SDK）。

两条路返回同一个 dict 形状，engine 不关心跑的是哪条。key 只从环境变量读，绝不打进日志。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import NoReturn

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .providers import (ENV_VARS, JEV_ENV, JEV_PROVIDERS, LEGACY, OPENCODE_JEV_MODELS,
                            OPENROUTER_DECISIONS, OPENROUTER_KEY_URL, TOKENRHYTHM_JEV_MODELS,
                            TYPESAFE_BASE)
except ImportError:
    from providers import (ENV_VARS, JEV_ENV, JEV_PROVIDERS, LEGACY, OPENCODE_JEV_MODELS,
                           OPENROUTER_DECISIONS, OPENROUTER_KEY_URL, TOKENRHYTHM_JEV_MODELS,
                           TYPESAFE_BASE)

MAX_RETRIES = 3


class JevError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def redact_secrets(text: str) -> str:
    """Strip every live key from any string before print or disk write."""
    if not isinstance(text, str):
        text = str(text)
    for env in ENV_VARS:
        key = os.environ.get(env) or ""
        if key:
            text = text.replace(key, "[REDACTED]")
    return text


def _status_of(exc: Exception) -> int | None:
    """各家 SDK 放 HTTP 状态码的属性名不一样：openai/anthropic 是 status_code，
    google-genai 是 code（它的 status 是 'NOT_FOUND' 这种字符串），typesafe 是 status。"""
    for name in ("status_code", "code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    return None


def _fail(exc: Exception, what: str) -> NoReturn:
    """SDK 抛的异常 → 一句人话的 JevError。消息过脱敏，绝不把 key 带出来。"""
    if isinstance(exc, JevError):
        raise exc
    status = _status_of(exc)
    hint = {401: "密钥被拒", 403: "没有权限", 404: "模型或地址不对", 422: "请求被拒",
            429: "被限流", 529: "服务过载"}.get(status, "")
    detail = redact_secrets(str(exc)).strip()[:300]
    head = f"{what} HTTP {status}" if status else f"{what}失败"
    raise JevError(f"{head}: {hint or detail or type(exc).__name__}", status) from None


def _api_key(env: str = JEV_ENV) -> str:
    """两把 key 之一（JEV_API_KEY / LLM_API_KEY）。新名字空着就退回老名字，老用户不用重填。"""
    key = ((os.environ.get(env) or "").strip()
           or (os.environ.get(LEGACY.get(env, "")) or "").strip())
    if not key:
        raise JevError(
            f"{env} is not set. Export it in the environment; "
            "do not put the key in a file."
        )
    return key


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    return redact_secrets(raw)[:800]


def _systemone_url(base: str) -> str:
    """自定义来源的 Base URL → SystemOne 端点。填到 /systemone 为止就照用，只填到 /v1 就补
    /systemone，只填到域名/前缀就补 /v1/systemone——这样 TypeSafe 那种 `https://api.typesafe.ai`
    和 Zen 那种 `https://opencode.ai/zen` 都能直接粘。空串返回空串（调用方据此报「要填 Base URL」）。"""
    url = (base or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/systemone"):
        return url
    return url + ("/systemone" if url.endswith("/v1") else "/v1/systemone")


def _catalog_url(endpoint: str) -> str:
    """SystemOne 端点 → 同一个网关的模型目录：把尾巴 /systemone 换成 /models。"""
    return endpoint[: -len("/systemone")] + "/models" if endpoint.endswith("/systemone") \
        else endpoint + "/models"


def _model_ids(data) -> list[str]:
    """模型目录的形状各家不完全一样：{"data":[{"id":…}]}（OpenAI 风格）/ {"models":[…]}
    / 裸数组 / 纯字符串数组。认不出就返回空，让调用方去兜底。"""
    if isinstance(data, dict):
        data = data.get("data") or data.get("models") or []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("model")
            if isinstance(value, str):
                out.append(value.removeprefix("models/"))
    return out


def ask(state: dict, questions: dict, timeout: float = 20,
        provider: str = "openrouter", model: str | None = None,
        base_url: str | None = None, retries: int = MAX_RETRIES) -> dict:
    """问 Jev 一轮判断，返回 {"answers": {名字: 答案}, "usage": {...}}。

    provider ∈ JEV_PROVIDERS（openrouter / tokenrhythm / opencode / typesafe / custom）；
    model=None 用该来源的默认模型；base_url 只有自定义来源要传。
    五条路返回的 dict 形状一模一样，429/529 都会退避重试。绝不打印或写出 key。
    retries：重试上限。判断链两处都有兜底（退回盲起草 / 按第一条推荐），engine 传 1——
    弱网下别让人在「正在整理」上干等满四轮退避，速降级到不用 Jev 的老路。
    """
    spec = JEV_PROVIDERS.get(provider) or JEV_PROVIDERS["openrouter"]
    key = _api_key(JEV_ENV)  # 各家共用同一把 key，换来源不用重填
    model = model or spec.default
    if spec.kind == "sdk":
        return _ask_typesafe(state, questions, key, model, timeout)
    url = spec.url or _systemone_url(base_url or "")
    if not url:
        raise JevError("自定义来源要填 Base URL（TypeSafe 兼容的 SystemOne 地址）")
    return _ask_http(url, state, questions, key, model, timeout, spec.headers, retries)


def _answer(answer) -> dict:
    """SDK 的答案对象 → OpenRouter 那条路 JSON 出来的同一个形状。"""
    if answer.type == "noul":
        return {"type": "noul", "noul": answer.noul}
    if answer.type == "choice":
        return {"type": "choice", "choice": answer.choice, "confidence": answer.confidence,
                "probabilities": dict(answer.probabilities)}
    # score：SDK 把概率的 key 转成了 int，这里转回字符串，跟 JSON 那条路对齐
    return {"type": "score", "score": answer.score, "confidence": answer.confidence,
            "probabilities": {str(k): v for k, v in answer.probabilities.items()}}


def _ask_typesafe(state: dict, questions: dict, key: str, model: str, timeout: float) -> dict:
    """官方 typesafe_sdk。questions 原样传：core/questions.py 里那几个 dict 本身就是 SDK 的
    NoulModel / ChoiceModel / ScoreModel（SDK 的 normalize_questions 认 dict），不用再包一层对象。
    重试用 RetryPolicy 的默认值——它本来就重试 408/429/5xx（含 529）并退避。"""
    import typesafe_sdk

    try:
        with typesafe_sdk.TypeSafeClient(api_key=key, base_url=TYPESAFE_BASE, model=model,
                                         timeout=timeout) as client:
            result = client.system_one(state, questions, model=model)
    except Exception as exc:
        _fail(exc, "Jev 判断")
    return {
        "answers": {name: _answer(a) for name, a in result.answers.items()},
        "usage": {"input_tokens": result.usage.input_tokens,
                  "output_tokens": result.usage.output_tokens},
    }


def _ask_http(url: str, state: dict, questions: dict, key: str, model: str,
              timeout: float, headers: dict | None = None,
              retries: int = MAX_RETRIES) -> dict:
    """SystemOne 形状的 POST：OpenRouter 的 /api/alpha/decisions、OpenCode Zen 和自定义来源的
    /v1/systemone 都是这一套。429/529 退避重试（上限 retries 次）。连接被拒/DNS 解析不了
    不重试——服务挂了或断网时重试也只是白等，秒级报错让上层走兜底。
    headers 是来源要求的固定头（Zen 网关的 WAF 会 403 掉 urllib 默认 UA，必须带一个 curl/browser
    的 UA，见 providers.CURL_UA）；鉴权和 Content-Type 由这里覆盖，来源表改不了它们。"""
    payload = json.dumps(
        {"model": model, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")
    request_headers = {
        **(headers or {}),
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }

    last_status: int | None = None
    last_body = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_body = _error_body(exc)
            if last_status in (429, 529) and attempt < retries:
                time.sleep(2**attempt)
                continue
            readable = {
                401: f"Jev HTTP 401: API key rejected. Check {JEV_ENV}.",
                422: f"Jev HTTP 422: request body rejected. {last_body}",
                429: f"Jev HTTP 429: rate limited after {retries} retries. {last_body}",
                529: f"Jev HTTP 529: provider overloaded after {retries} retries. {last_body}",
            }.get(last_status, f"Jev HTTP {last_status}: {last_body}")
            raise JevError(readable, last_status) from None
        except (TimeoutError, socket.timeout) as exc:
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            raise JevError(f"Jev request timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                # 连接被拒（服务没在监听）/ DNS 解析不了：重试也是白搭，立刻报
                raise JevError(f"Jev 连不上（{redact_secrets(str(reason))[:120]}）") from None
            reason = redact_secrets(reason)
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            raise JevError(f"Jev request failed: {reason}") from None

    raise JevError(
        f"Jev HTTP {last_status}: exhausted retries. {last_body}", last_status
    )


def _check_openrouter_key(key: str, timeout: float) -> None:
    """免费的 auth/key 探测：401/403 说明 key 不对，别的错（超时/断网）也如实上报。
    列表本身是写死的，key 对不对只有靠它才知道，别等第一次判断才暴露。"""
    req = urllib.request.Request(OPENROUTER_KEY_URL,
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        hint = {401: "密钥被拒", 403: "没有权限"}.get(exc.code, _error_body(exc)[:200])
        raise JevError(f"取模型列表 HTTP {exc.code}: {hint}") from None
    except (TimeoutError, socket.timeout):
        raise JevError(f"取模型列表请求超时（{timeout}s）") from None
    except urllib.error.URLError as exc:
        raise JevError(
            f"取模型列表失败: {redact_secrets(getattr(exc, 'reason', exc))}") from None


def _catalog_models(endpoint: str, key: str, timeout: float, headers: dict | None,
                    only_jev: bool, fallback: tuple[str, ...] = ()) -> list[str]:
    """问网关的模型目录（{endpoint 去掉 /systemone}/models）。only_jev 只留 id 里带 jev 的——
    Zen 那种目录里混着整本对话模型，而这一格要的是判断模型。目录能连上但没列出 Jev 时用
    fallback 兜底（官方文档写死的 id）；连不上就如实报错，别让坏 key 拖到第一次判断才暴露。"""
    url = _catalog_url(endpoint)
    try:
        req = urllib.request.Request(url, headers={
            **(headers or {}),
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        hint = {401: "密钥被拒", 403: "没有权限",
                404: "这个地址没有模型目录，直接手打模型 id"}.get(exc.code, _error_body(exc)[:200])
        raise JevError(f"取模型列表 HTTP {exc.code}: {hint}") from None
    except (TimeoutError, socket.timeout):
        raise JevError(f"取模型列表请求超时（{timeout}s）") from None
    except urllib.error.URLError as exc:
        raise JevError(f"取模型列表失败: {redact_secrets(getattr(exc, 'reason', exc))}") from None
    ids = _model_ids(data)
    if only_jev:
        ids = [i for i in ids if "jev" in i.lower()]
    return sorted(set(ids)) or list(fallback)


def list_models(provider: str, key: str, timeout: float = 10,
                base_url: str | None = None) -> list[str]:
    """某家能用的 Jev 模型 id，去重排序。失败抛 JevError（设置页直接显示这句话）。
    base_url 只有自定义来源要传。"""
    spec = JEV_PROVIDERS.get(provider) or JEV_PROVIDERS["openrouter"]
    if spec.kind == "sdk":
        import typesafe_sdk

        try:
            with typesafe_sdk.TypeSafeClient(api_key=key, base_url=TYPESAFE_BASE,
                                             timeout=timeout) as client:
                return sorted({m.name for m in client.models.list().models})
        except Exception as exc:
            _fail(exc, "取模型列表")
    if spec.url is None:  # 自定义来源：地址是用户填的，目录也照他的地址问
        endpoint = _systemone_url(base_url or "")
        if not endpoint:
            raise JevError("自定义来源要填 Base URL（TypeSafe 兼容的 SystemOne 地址）")
        return _catalog_models(endpoint, key, timeout, spec.headers, only_jev=False)
    if provider == "openrouter":
        # OpenRouter 的 Jev 是 Decisions API 专属模型，不在 /api/v1/models 目录里
        # （也没有列它的专用端点），列表按官方模型页写死，别名列排最前（永远指向最新版）。
        # key 对不对由探测兜着，别让坏密钥等到第一次判断才暴露。
        _check_openrouter_key(key, timeout)
        return ["~typesafe/jev-latest", "typesafe/jev-1.13"]
    # OpenCode Zen / 基元律动：目录里可能混着别的模型，只留 Jev；目录一个都不列时
    # 用各自文档里写死的 id 兜底（Zen 是 jev-1.13 两档，基元律动固定 NeoHorse-Jev-4B）
    fallback = OPENCODE_JEV_MODELS if provider == "opencode" else TOKENRHYTHM_JEV_MODELS
    return _catalog_models(spec.url, key, timeout, spec.headers, only_jev=True,
                           fallback=fallback)


if __name__ == "__main__":
    # ponytail: 不联网。两条路各测一次：SDK 那条在 typesafe_sdk 边界换成假客户端，
    # urllib 那条 mock urlopen。会坏的地方就一个——答案对象 → dict 的映射得跟 JSON 那条一模一样。
    import io
    import types as _t
    from unittest.mock import patch

    import typesafe_sdk

    try:
        from .providers import OPENCODE_JEV_MODELS, OPENCODE_SYSTEMONE, TOKENRHYTHM_SYSTEMONE
        from .questions import JUDGE_QUESTIONS, build_rank_question
    except ImportError:
        from providers import OPENCODE_JEV_MODELS, OPENCODE_SYSTEMONE, TOKENRHYTHM_SYSTEMONE
        from questions import JUDGE_QUESTIONS, build_rank_question

    os.environ.pop(JEV_ENV, None)
    os.environ["OPENROUTER_API_KEY"] = "or-key"  # 老名字：新名字没设时该退回它
    assert _api_key(JEV_ENV) == "or-key"
    os.environ[JEV_ENV] = "ts-key"  # 新名字在就用新的，两家来源共用这一把
    questions = dict(JUDGE_QUESTIONS)
    questions.update(build_rank_question(["甲", "乙", "丙"]))
    seen: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            seen["init"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def system_one(self, state, qs, **kw):
            seen["state"], seen["questions"], seen["kw"] = state, qs, kw
            return _t.SimpleNamespace(
                answers={
                    "literal_question": _t.SimpleNamespace(type="noul", noul=0.9),
                    "best_reply": _t.SimpleNamespace(
                        type="choice", choice="reply_b", confidence=0.7,
                        probabilities={"reply_a": 0.2, "reply_b": 0.7, "reply_c": 0.1}),
                    "danger_level": _t.SimpleNamespace(
                        type="score", score=4.0, confidence=0.6, probabilities={4: 0.6, 5: 0.4}),
                },
                usage=_t.SimpleNamespace(input_tokens=11, output_tokens=22))

        @property
        def models(self):
            return _t.SimpleNamespace(list=lambda: _t.SimpleNamespace(models=(
                _t.SimpleNamespace(name="jev-preview"), _t.SimpleNamespace(name="jev-latest"))))

    with patch.object(typesafe_sdk, "TypeSafeClient", _FakeClient):
        got = ask({"chat": {}}, questions, timeout=15, provider="typesafe", model="jev-1.13.0")
        ask_init = seen["init"]
        assert list_models("typesafe", "ts-key") == ["jev-latest", "jev-preview"]
        assert seen["init"] == {"api_key": "ts-key", "base_url": TYPESAFE_BASE, "timeout": 10}
    assert ask_init == {"api_key": "ts-key", "base_url": TYPESAFE_BASE,
                        "model": "jev-1.13.0", "timeout": 15}
    assert seen["kw"] == {"model": "jev-1.13.0"}
    # 题目原样进 SDK：它们本身就是 NoulModel / ChoiceModel / ScoreModel，不用再包一层
    assert seen["questions"] is questions
    assert seen["questions"]["danger_level"]["type"] == "score"
    assert isinstance(seen["questions"]["danger_level"]["criteria"], list)
    assert seen["questions"]["best_reply"]["criteria"] == {
        "reply_a": "甲", "reply_b": "乙", "reply_c": "丙"}
    # 映射出来的形状跟 OpenRouter 那条路的 JSON 必须一致（engine 不关心跑的是哪条）
    assert got["answers"]["literal_question"] == {"type": "noul", "noul": 0.9}
    assert got["answers"]["best_reply"] == {
        "type": "choice", "choice": "reply_b", "confidence": 0.7,
        "probabilities": {"reply_a": 0.2, "reply_b": 0.7, "reply_c": 0.1}}
    assert got["answers"]["danger_level"] == {
        "type": "score", "score": 4.0, "confidence": 0.6,
        "probabilities": {"4": 0.6, "5": 0.4}}  # score 的概率 key 转回字符串
    assert got["usage"] == {"input_tokens": 11, "output_tokens": 22}

    class _Boom(Exception):
        status = 429

    with patch.object(typesafe_sdk, "TypeSafeClient", lambda **kw: (_ for _ in ()).throw(_Boom("x"))):
        try:
            ask({"chat": {}}, questions, provider="typesafe")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.status == 429 and "被限流" in str(e)

    # OpenRouter 那条没动：还是自己拼 body、打 /api/alpha/decisions
    body = {"answers": {"best_reply": {"type": "choice", "choice": "reply_a"}}, "usage": {}}

    def _fake_urlopen(req, timeout=None):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data.decode("utf-8"))
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    with patch.object(urllib.request, "urlopen", _fake_urlopen):
        assert ask({"chat": {}}, questions) == body
    assert seen["url"] == OPENROUTER_DECISIONS
    assert seen["body"]["model"] == "typesafe/jev-1.13" and seen["body"]["questions"] == questions

    # SystemOne 地址归一化：域名 / /v1 / 完整端点 三种写法都该落到同一个端点
    assert _systemone_url("https://api.typesafe.ai") == "https://api.typesafe.ai/v1/systemone"
    assert _systemone_url("https://opencode.ai/zen/") == "https://opencode.ai/zen/v1/systemone"
    assert _systemone_url("https://x/v1") == "https://x/v1/systemone"
    assert _systemone_url(OPENCODE_SYSTEMONE) == OPENCODE_SYSTEMONE
    assert _systemone_url("") == "" and _systemone_url("   ") == ""
    assert _catalog_url(OPENCODE_SYSTEMONE) == "https://opencode.ai/zen/v1/models"
    # 各家目录的形状不一样，认不出的一律返回空（调用方去兜底）
    assert _model_ids({"data": [{"id": "models/a"}, {"id": "b"}]}) == ["a", "b"]
    assert _model_ids({"models": [{"name": "c"}]}) == ["c"]
    assert _model_ids(["d", {"model": "e"}]) == ["d", "e"]
    assert _model_ids("nope") == [] and _model_ids({}) == []

    # OpenCode Zen：同一套 body，但打 Zen 的 /v1/systemone，并且必须带上绕 WAF 的 UA
    def _fake_zen(req, timeout=None):
        seen["zen_url"], seen["zen_headers"] = req.full_url, dict(req.headers)
        seen["zen_body"] = json.loads(req.data.decode("utf-8"))
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    with patch.object(urllib.request, "urlopen", _fake_zen):
        assert ask({"chat": {}}, questions, provider="opencode") == body
    assert seen["zen_url"] == OPENCODE_SYSTEMONE
    assert seen["zen_body"]["model"] == "jev-1.13"  # 该来源的默认模型
    assert seen["zen_body"]["questions"] == questions
    assert seen["zen_headers"]["User-agent"] == "curl/8.9.1"  # urllib 会把 UA 键首字母大写
    assert seen["zen_headers"]["Authorization"] == "Bearer ts-key"
    assert seen["zen_headers"]["Content-type"] == "application/json; charset=utf-8"

    # 自定义来源：地址用户填（只填到域名也认），模型不给就用该来源的默认值
    with patch.object(urllib.request, "urlopen", _fake_zen):
        assert ask({"chat": {}}, questions, provider="custom",
                   base_url="https://my-gw.example", model="my-jev") == body
    assert seen["zen_url"] == "https://my-gw.example/v1/systemone"
    assert seen["zen_body"]["model"] == "my-jev"
    assert "User-agent" not in seen["zen_headers"]  # 那个 UA 是 Zen 网关专属，别带给别人
    with patch.object(urllib.request, "urlopen", _fake_zen):
        try:
            ask({"chat": {}}, questions, provider="custom")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "Base URL" in str(e)

    # 连接被拒 / DNS 解析不了：一次都不重试——服务挂了或断网时白等退避没有意义
    calls = {"n": 0}

    def _refused(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError(ConnectionRefusedError())

    with patch.object(urllib.request, "urlopen", _refused):
        try:
            ask({"chat": {}}, questions)
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "连不上" in str(e)
    assert calls["n"] == 1

    calls["n"] = 0

    def _dns(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError(socket.gaierror("Name or service not known"))

    with patch.object(urllib.request, "urlopen", _dns):
        try:
            ask({"chat": {}}, questions)
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "连不上" in str(e)
    assert calls["n"] == 1

    # retries 参数：判断链传 1，超时最多打两次就放手，别让用户干等满四轮
    calls["n"] = 0

    def _slow(req, timeout=None):
        calls["n"] += 1
        raise socket.timeout()

    with patch.object(urllib.request, "urlopen", _slow):
        try:
            ask({"chat": {}}, questions, retries=1)
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "timed out" in str(e)
    assert calls["n"] == 2

    # OpenRouter 路的列表是写死的，但 key 要过 auth/key 探测：mock urlopen 验两头
    def _fake_key_ok(req, timeout=None):
        seen["key_url"] = req.full_url
        assert req.headers["Authorization"] == "Bearer or-key"
        return io.BytesIO(b'{"data":{}}')

    with patch.object(urllib.request, "urlopen", _fake_key_ok):
        assert list_models("openrouter", "or-key") == [
            "~typesafe/jev-latest", "typesafe/jev-1.13"]
    assert seen["key_url"] == OPENROUTER_KEY_URL

    def _fake_key_rejected(req, timeout=None):
        raise urllib.error.HTTPError(OPENROUTER_KEY_URL, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error":{"message":"bad key or-key"}}'))

    with patch.object(urllib.request, "urlopen", _fake_key_rejected):
        try:
            list_models("openrouter", "or-key")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.status is None and "密钥被拒" in str(e) and "or-key" not in str(e)

    # 401/403 以外的状态码要把响应体带出来（别提前 read 把流吃空）
    def _fake_key_429(req, timeout=None):
        raise urllib.error.HTTPError(OPENROUTER_KEY_URL, 429, "Too Many", {},
                                     io.BytesIO(b'{"error":{"message":"rate limited"}}'))

    with patch.object(urllib.request, "urlopen", _fake_key_429):
        try:
            list_models("openrouter", "or-key")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "HTTP 429" in str(e) and "rate limited" in str(e)

    # OpenCode Zen / 自定义的模型目录：都是 {端点去掉 /systemone}/models
    def _catalog(payload):
        def _open(req, timeout=None):
            seen["cat_url"], seen["cat_auth"] = req.full_url, req.headers.get("Authorization")
            return io.BytesIO(json.dumps(payload).encode("utf-8"))
        return _open

    with patch.object(urllib.request, "urlopen",
                      _catalog({"data": [{"id": "gpt-5.5"}, {"id": "jev-1.13"}]})):
        assert list_models("opencode", "zen-key") == ["jev-1.13"]  # 只留 Jev
    assert seen["cat_url"] == "https://opencode.ai/zen/v1/models"
    assert seen["cat_auth"] == "Bearer zen-key"
    # 目录连上了但没列 Jev：用官方文档里那两个 id 兜底，别让这一格空着
    with patch.object(urllib.request, "urlopen", _catalog({"data": [{"id": "gpt-5.5"}]})):
        assert list_models("opencode", "zen-key") == list(OPENCODE_JEV_MODELS)
    # 自定义来源照用户填的地址问，不筛 Jev
    with patch.object(urllib.request, "urlopen", _catalog({"data": [{"id": "b"}, {"id": "a"}]})):
        assert list_models("custom", "k", base_url="https://my-gw.example") == ["a", "b"]
    assert seen["cat_url"] == "https://my-gw.example/v1/models"
    # 基元律动：同一个目录问法，只留 Jev；一个都不列时用文档里写死的 NeoHorse-Jev-4B 兜底
    with patch.object(urllib.request, "urlopen",
                      _catalog({"data": [{"id": "deepseek-v4-flash"}, {"id": "NeoHorse-Jev-4B"}]})):
        assert list_models("tokenrhythm", "tr-key") == ["NeoHorse-Jev-4B"]
    assert seen["cat_url"] == TOKENRHYTHM_SYSTEMONE.replace("/systemone", "/models")
    with patch.object(urllib.request, "urlopen", _catalog({"data": [{"id": "deepseek-v4-flash"}]})):
        assert list_models("tokenrhythm", "tr-key") == list(TOKENRHYTHM_JEV_MODELS)

    def _catalog_404(req, timeout=None):
        raise urllib.error.HTTPError("https://x/models", 404, "Not Found", {}, io.BytesIO(b"{}"))

    # 目录连不上就如实报错（坏 key 得当场暴露，别静默兜底），自定义再提示可以手打模型 id
    for provider, base, wanted in (("opencode", None, "404"),
                                   ("tokenrhythm", None, "404"),
                                   ("custom", "https://my-gw.example", "手打模型 id")):
        with patch.object(urllib.request, "urlopen", _catalog_404):
            try:
                list_models(provider, "k", base_url=base)
                raise SystemExit("应当抛错")
            except JevError as e:
                assert wanted in str(e), str(e)

    assert redact_secrets("key=ts-key or-key") == "key=[REDACTED] [REDACTED]"
    print("jev_client ok")
