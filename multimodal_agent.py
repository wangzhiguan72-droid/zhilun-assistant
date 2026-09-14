"""
多模态图表解读（v2.9 · 规划 §三 ④）
=====================================
上传一张**统计图表**（截图 / 从 PDF 里抠出来的图）+ 论文里对应的一句**结论**，
让多模态模型"读图"，判断「这张图配得上它的结论吗」。

设计铁律（与 llm_audit / audit_chat 一脉相承，改前必读）：
    1. **只读图，不改结论**：模型输出的是"这张图能看出什么 / 和结论是否相符 /
       有哪些说不清的地方"，最终判定权仍在用户与规则引擎手里。
    2. **铁律②（最小化输入）**：只传「图片 + 结论句文本」，**绝不传原始数据 df**、
       绝不传整篇论文。图片本身也只以 base64 一次性送进请求体，**不落盘**。
    3. **冻结前缀**：system 字节级固定，为前缀缓存铺路（同 audit_chat 模式）。
    4. **失败静默降级**：任何异常 / 无 Key 都不抛，返回 (None, error, meta)，
       端点回 `llm_error` + 手动核对提示，前端不白屏。
    5. **脱敏**：错误出口过 secrets_guard（这段文案会回浏览器）。
    6. **图不持久化**：图片字节只在本函数栈内存活，请求结束即释放；
       不写 uploads/、不进缓存 key（缓存 key 用图片内容的哈希）。

接口：
    audit_image(image_bytes, mime, claim, *, force=False)
        -> (result | None, error | None, meta dict)
        result: {
            "matches_conclusion": bool | None,
            "issues": [str],
            "caption": str,          # 模型对这张图的客观描述（坐标轴/组别/趋势）
            "raw": str,              # 模型原始输出（Markdown，便于人工复核）
        }
        meta: {"cached": bool, "model": str}
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from llm_cache import llm_cache

# ---------------------------------------------------------------------------
# 冻结前缀：字节级不变，为前缀缓存铺路
# ⚠️ 改任何措辞 = 改 prompt → 必须 bump PROMPT_VERSION
# ---------------------------------------------------------------------------
PROMPT_VERSION = "image-v1"

# 送进多模态请求的图片上限（base64 后的字节）。免费档模型对图片体积敏感，
# 太大既慢又容易被平台拒；超限由调用方在入口拦截（端点先查 Content-Length）。
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

# 认得出来的图片类型（多模态接口只吃这几种）
ALLOWED_IMAGE_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

FROZEN_SYSTEM = (
    "你是「智论助手」的统计图表核查助手。用户会给你一张**统计图表**（柱状图 / 折线图 / "
    "箱线图 / 散点图 / 误差棒图等）以及论文里与这张图对应的一句**结论**。\n"
    "你的任务：客观描述这张图，并判断它与结论是否相符。\n"
    "硬性规则：\n"
    "1. 先**只描述你实际看到的**：坐标轴标签、单位、组别名称、系列数量、趋势方向、"
    "有没有误差棒 / 显著性标记 / 样本量标注。看不清就明说「图中无法辨认」，禁止脑补。\n"
    "2. 再判断结论：图里的信息**支不支持**这句话。三种取值——\n"
    "   true = 图支持结论；false = 图与结论矛盾；null = 图信息不足以判断。\n"
    "   注意：**看不出显著性 ≠ 显著**。图中若没有误差棒 / 星号 / p 值，不要断言显著或不显著。\n"
    "3. 指出具体问题，例如：结论说「显著升高」但图无误差棒；坐标轴被截断夸大了差异；"
    "组别数量与结论表述对不上；单位缺失；样本量未标注。没有问题时 issues 留空数组。\n"
    "4. 不要重新计算任何统计量、不要编造 p 值或样本量——你没有原始数据。\n"
    "5. 用规范学术语气，避免口语化和 AI 腔。\n"
    "6. **只输出一个 JSON 对象**，不要代码块围栏，不要任何解释文字。格式：\n"
    '{"caption": "对图表的客观描述", "matches_conclusion": true/false/null, '
    '"issues": ["问题1", "问题2"]}'
)

_FROZEN_USER_PREFIX = "论文里与这张图对应的结论："


def audit_image(image_bytes: bytes, mime: str, claim: str,
                *, force: bool = False) -> tuple[dict[str, Any] | None, str | None,
                                                  dict[str, Any]]:
    """读图 + 比对结论（带缓存 + 静默降级）。

    返回 (result, error, meta)；成功时 error 为 None，result 见模块文档。

    缓存 key 用**图片内容的 sha256**（不是文件名 / 路径）：
    同一张图重复核查直接命中，且图片本体不进缓存——只存模型输出的文本。
    """
    claim = (claim or "").strip()
    if not image_bytes:
        return None, "图片为空，无法解读", {"cached": False, "model": ""}
    mime = (mime or "").strip().lower()
    if mime not in ALLOWED_IMAGE_MIME:
        return None, (f"不支持的图片格式：{mime or '未知'}。"
                      f"仅支持 {'/'.join(sorted(set(ALLOWED_IMAGE_MIME)))}"), {
            "cached": False, "model": ""}
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return None, (f"图片过大（{len(image_bytes) / 1024 / 1024:.1f} MB），"
                      f"请压缩到 {MAX_IMAGE_BYTES // 1024 // 1024} MB 以内"), {
            "cached": False, "model": ""}

    img_hash = hashlib.sha256(image_bytes).hexdigest()

    # ---- 查缓存（key = 图哈希 + 结论句；换结论句 = 换问题，各自成键）----
    model_name = ""
    try:
        from agents import get_router
        model_name = _router_model_name(get_router(), "audit_image")
    except Exception:  # noqa: BLE001
        pass

    cache_key = llm_cache.make_key(
        model_name, PROMPT_VERSION, "audit_image",
        {"img": img_hash, "claim": claim},
    )
    if not force:
        entry = llm_cache.get(cache_key)
        if entry is not None:
            parsed = _parse_result(entry["text"])
            if parsed is not None:
                return parsed, None, {"cached": True, "model": model_name}
            # 缓存里的文本解析不出来（旧版本 / 模型抽风）→ 丢弃这条，重新真调
            llm_cache.discard(cache_key)

    # ---- 组装多模态消息（图片在前、文字在后，符合主流 VLM 习惯）----
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    user_text = (
        f"{_FROZEN_USER_PREFIX}{claim or '（用户未提供结论句）'}\n\n"
        "请按 JSON 格式输出你的读图结果。"
    )
    messages = [
        {"role": "system", "content": FROZEN_SYSTEM},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": user_text},
        ]},
    ]

    # ---- 真调（走 Router 的 audit_image 容灾链）----
    try:
        text = _call_router(messages)
    except Exception as e:  # noqa: BLE001
        # 兜底：真调失败时回看缓存（LLM 挂了，旧结果仍可用；peek 不污染统计）
        entry = llm_cache.peek(cache_key) if not force else None
        if entry is not None:
            parsed = _parse_result(entry["text"])
            if parsed is not None:
                return parsed, None, {"cached": True, "model": model_name}
        from agents import AgentError
        kind = "LLM 不可用" if isinstance(e, AgentError) else "LLM 调用异常"
        return None, f"{kind}：{_short(str(e))}", {"cached": False, "model": model_name}

    text = (text or "").strip()
    if not text:
        return None, "LLM 返回空内容", {"cached": False, "model": model_name}

    parsed = _parse_result(text)
    if parsed is None:
        # 模型没按 JSON 输出：仍把原文交给用户（人工可读），只是结构化字段缺失
        parsed = {
            "matches_conclusion": None,
            "issues": [],
            "caption": "",
            "raw": text,
        }
        return parsed, "模型输出未能解析为结构化结果（已附原文，请人工核对）", {
            "cached": False, "model": model_name}

    # ---- 写缓存（容灾切换时按实际模型补写一份）----
    try:
        from agents import get_router
        actual = _router_model_name(get_router(), "audit_image")
    except Exception:  # noqa: BLE001
        actual = model_name
    llm_cache.put(cache_key, text)
    if actual and actual != model_name:
        llm_cache.put(llm_cache.make_key(
            actual, PROMPT_VERSION, "audit_image",
            {"img": img_hash, "claim": claim}), text)
        model_name = actual
    return parsed, None, {"cached": False, "model": model_name}


def _call_router(messages: list[dict[str, Any]]) -> str:
    """走 Router 的 audit_image 状态真调（容灾链 + 冷却由 Router 负责）。

    多模态请求体与纯文本不同（content 是数组），所以不能走 router.complete()
    的 `prompt: str` 签名——直接取该状态的 Agent 调 SDK，并在异常时把
    AgentError 透传给 Router 的冷却逻辑不适用，故这里自己实现一次冷却记录。
    """
    from agents import AgentError, get_router
    router = get_router()
    agent = router._get_agent("audit_image")
    try:
        resp = agent.client.chat.completions.create(
            model=agent.model_name,
            messages=messages,
            max_tokens=800,
            temperature=0.2,
        )
        # usage 观测（前缀缓存证据链；多模态的 usage 同样可看 cached_tokens）
        try:
            agent.last_usage = agent._extract_usage(resp)
            router._last_agent = agent
            router._last_usage = agent.last_usage
            router._last_state = "audit_image"
            router._remember_model("audit_image", agent)
        except Exception:  # noqa: BLE001
            pass
        content = resp.choices[0].message.content or ""
        if not content.strip():
            raise AgentError(
                f"{agent.model_name} 返回空内容（多模态模型可能拒答此图，"
                f"请换一张更清晰的图或稍后重试）。")
        return content
    except AgentError:
        raise
    except Exception as e:  # noqa: BLE001
        # 把非 AgentError 的底层异常（限流等）统一成 AgentError，交给上层文案
        raise AgentError(f"多模态调用失败：{e}") from e


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------
def _parse_result(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 结果。宽容解析（模型常带围栏 / 前后缀）。

    解析不出来返回 None（调用方决定是降级还是报错）。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    candidate = fence.group(1) if fence else raw
    # 尝试直接解析；失败则取第一个 { ... } 平衡块
    obj = _try_json(candidate)
    if obj is None:
        brace = _first_json_object(candidate)
        if brace is not None:
            obj = _try_json(brace)
    if not isinstance(obj, dict):
        return None

    caption = obj.get("caption")
    caption = caption.strip() if isinstance(caption, str) else ""

    mc = obj.get("matches_conclusion")
    if isinstance(mc, str):
        low = mc.strip().lower()
        if low in ("true", "yes", "是", "符合"):
            mc = True
        elif low in ("false", "no", "否", "不符", "不符合", "矛盾"):
            mc = False
        else:
            mc = None
    elif not isinstance(mc, bool):
        mc = None

    issues_raw = obj.get("issues")
    issues: list[str] = []
    if isinstance(issues_raw, list):
        for it in issues_raw:
            if isinstance(it, str) and it.strip():
                issues.append(it.strip())
            elif isinstance(it, dict):  # 有的模型会包一层 {"issue": "..."}
                v = it.get("issue") or it.get("text") or it.get("description")
                if isinstance(v, str) and v.strip():
                    issues.append(v.strip())
    elif isinstance(issues_raw, str) and issues_raw.strip():
        issues = [issues_raw.strip()]

    if not caption and not issues and mc is None:
        return None
    return {
        "matches_conclusion": mc,
        "issues": issues,
        "caption": caption,
        "raw": raw,
    }


def _try_json(s: str) -> Any:
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None


def _first_json_object(s: str) -> str | None:
    """取第一个括号平衡的 { ... } 块（容忍 JSON 前后有解释文字）。"""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def _router_model_name(router, state: str = "audit_image") -> str:
    """稳定模型名（进缓存 key），容灾切换不漂移。"""
    try:
        fn = getattr(router, "cache_model_name", None)
        if callable(fn):
            return fn(state) or ""
        agent = router._get_agent(state)   # 兼容旧接口
        return agent.model_name
    except Exception:  # noqa: BLE001
        return ""


def _short(msg: str, limit: int = 160) -> str:
    """错误截断 + 凭据擦除（这段会进 jsonify(error=...) 回浏览器）。"""
    try:
        from agents.secrets_guard import redact
        msg = redact(msg)
    except Exception:  # noqa: BLE001
        pass
    msg = msg.replace("\n", " ").strip()
    return msg if len(msg) <= limit else msg[:limit] + "…"
