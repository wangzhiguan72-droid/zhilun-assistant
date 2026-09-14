"""
LLM 深度解读增强层（v0.4 最终步）
================================
把 Router（智谱/硅基流动双平台）接到分析结果上：
统计计算仍由 Python 完成（准确、零成本），LLM 只负责"解释与洞察"。

设计原则（与 ROADMAP §5 一致）：
    1. 计算+生成解耦：LLM 只读统计量 summary dict，绝不喂原始数据
       （隐私 + 防幻觉 + token 省）
    2. 冻结前缀：system prompt 字节级固定，为 v0.5 前缀缓存铺路
       （天枢方案：同前缀命中 cache，成本降一个数量级）
    3. 失败静默回退：LLM 任何报错都不阻断主结果，只标记 llm_error
    4. 路由：走 Router 的 write_text 状态（V4-Flash 免费主力，
       双平台容灾；用户没配 Key 时提示而不是报错）

v0.5 缓存层（llm_cache.py）：
    - key = hash(model + PROMPT_VERSION + method + summary_canonical_json)
    - 相同统计量重复解读 → 直接命中，跳过 LLM 调用（0 token）
    - 缓存命中不需要 API Key（LLM 挂了旧结果仍可用）
    - 改 FROZEN_SYSTEM 时必须 bump PROMPT_VERSION（旧缓存自动失效）

接口：
    enhance_analysis(method, summary, label, force=False)
        -> (section_md | None, error | None, meta dict)
        meta: {"cached": bool, "model": str}（cached=True 表示命中缓存）
"""
from __future__ import annotations

import json
from typing import Any

from llm_cache import llm_cache
from tone_guide import TONE_RULES as _TONE_RULES

# ---------------------------------------------------------------------------
# 冻结前缀（v0.5 前缀缓存的关键：这部分字节级不变，永不修改措辞）
# ⚠️ 修改任何措辞 = 改变 prompt → 必须 bump PROMPT_VERSION（缓存失效）
# v1.7：注入 tone_guide.TONE_RULES（把"避免 AI 腔"讲成可判定的具体禁令）
# ---------------------------------------------------------------------------
PROMPT_VERSION = "v3"  # v1.7：注入明确语气规约（旧缓存失效）

FROZEN_SYSTEM = (
    "你是「智论助手」的学术解读引擎。你的任务：基于给定的统计检验结果（JSON），"
    "写一段 180 字以内的中文深度解读，帮助论文作者理解结果并写进论文。\n"
    "硬性规则：\n"
    "1. 只能使用 JSON 中出现的数字，禁止编造、修改或外推任何统计量\n"
    "2. 用规范学术语言，避免口语化和 AI 腔\n"
    "3. 不要复述描述统计表，直接给解释：这个结果说明什么、怎么写进论文、要注意什么\n"
    "4. 若 JSON 中有前提条件信息（如方差齐性、正态性 p 值），结合它给适用性提醒\n"
    "5. 输出纯 Markdown 正文，不要代码块，不要重复标题\n"
    "6. 最后单独一行写一条「写作提示：…」的短建议\n\n"
    + _TONE_RULES
    + "\n生成前决策（参考 Ponytail 最佳实践）：\n"
    "在输出解读前，先快速判断：\n"
    "- 统计结果是否已经一目了然？（如 p<0.001 且效应量显著，结论明确）\n"
    "- 是否需要额外解释？（用户只要结论还是需要理论依据？）\n"
    "- 能否一句话说清？（若能，不要分段展开）\n"
    "若以上答案倾向于「否」或「简单」，则：输出简洁结论，避免过度解释。\n"
    "若你在输出中跳过了某些常规说明，可用「ponytail: 原因」标注（可选）。"
)

_FROZEN_USER_PREFIX = "统计方法：{label}\n统计量 JSON：\n"


def enhance_analysis(method: str, summary: dict[str, Any], label: str,
                     *, force: bool = False) -> tuple[str | None, str | None, dict[str, Any]]:
    """调用 Router(write_text) 生成 AI 深度解读段（带 v0.5 缓存）。

    返回 (section_md, error, meta)：成功时 error 为 None。
    meta = {"cached": bool, "model": str}
    任何异常都不抛出——调用方（/api/analyze）直接降级为纯模板。
    """
    if not summary:
        return None, "没有可解读的统计量", {"cached": False, "model": ""}

    # 统计量 JSON：数字取 4 位小数，稳定输出（也利于缓存命中）
    try:
        clean = _sanitize(summary)
        stats_json = json.dumps(clean, ensure_ascii=False, indent=None)
    except (TypeError, ValueError) as e:
        return None, f"统计量序列化失败：{e}", {"cached": False, "model": ""}

    # ---- v0.5：查缓存（force=True 时绕过）----
    # 模型名从 Router 解析（进 key，双平台切换不串缓存）。
    # v1.1.1：用 cache_model_name()——它优先返回该状态**上次真正成功**的模型，
    # 避免容灾切换导致"查缓存时的候选"与"写缓存时的候选"不一致而假性 miss。
    model_name = ""
    try:
        from agents import get_router
        model_name = _router_model_name(get_router(), "write_text")
    except Exception:  # noqa: BLE001
        pass  # 拿不到模型名也能继续（Key 缺失在下面报）

    cache_key = llm_cache.make_key(model_name, PROMPT_VERSION, method, clean)
    if not force:
        entry = llm_cache.get(cache_key)
        if entry is not None:
            section = "### 六、AI 深度解读（实验功能，请自行核对数字）\n\n" + entry["text"]
            return section, None, {"cached": True, "model": model_name}

    user_prompt = (
        _FROZEN_USER_PREFIX.format(label=label)
        + stats_json
        + "\n\n请输出深度解读。"
    )

    try:
        from agents import AgentError, get_router
        router = get_router()
        text = router.complete(
            "write_text",          # → 硅基流动 V4-Flash（免费主力）
            prompt=user_prompt,
            system=FROZEN_SYSTEM,  # 冻结前缀
            max_tokens=600,
        )
    except AgentError as e:
        # 兜底：真调失败时再试一次缓存（LLM 挂了，旧结果仍可用；peek 不污染统计）
        entry = llm_cache.peek(cache_key) if not force else None
        if entry is not None:
            section = "### 六、AI 深度解读（实验功能，请自行核对数字）\n\n" + entry["text"]
            return section, None, {"cached": True, "model": model_name}
        return None, f"LLM 不可用：{_short(str(e))}", {"cached": False, "model": model_name}
    except Exception as e:  # noqa: BLE001
        return None, f"LLM 调用异常：{_short(str(e))}", {"cached": False, "model": model_name}

    text = (text or "").strip()
    if not text:
        return None, "LLM 返回空内容", {"cached": False, "model": model_name}

    # ---- v0.5：写缓存 ----
    # v1.1.1：若实际服务模型与查缓存时用的不同（发生了容灾切换），
    # 则按实际模型再写一份，保证"下次用同一模型查时能命中"。
    try:
        from agents import get_router
        actual = _router_model_name(get_router(), "write_text")
    except Exception:  # noqa: BLE001
        actual = model_name
    if actual and actual != model_name:
        llm_cache.put(llm_cache.make_key(actual, PROMPT_VERSION, method, clean), text)
        model_name = actual
    llm_cache.put(cache_key, text)
    section = "### 六、AI 深度解读（实验功能，请自行核对数字）\n\n" + text
    return section, None, {"cached": False, "model": model_name}


def _router_model_name(router, state: str = "write_text") -> str:
    """拿到指定状态**稳定**的模型名（进缓存 key）。

    v1.1.1：改用 router.cache_model_name(state)，它在"上次成功模型"与
    "当前候选"之间做稳定化处理；旧实现直接取当前候选，容灾切换时会漂移。
    """
    try:
        fn = getattr(router, "cache_model_name", None)
        if callable(fn):
            return fn(state) or ""
        agent = router._get_agent(state)   # 兼容旧接口
        return agent.model_name
    except Exception:  # noqa: BLE001
        return ""


def _sanitize(obj: Any, _depth: int = 0) -> Any:
    """把 summary 里不可 JSON 序列化的部分转成可读字符串，浮点保留 4 位。"""
    if _depth > 6:
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _sanitize(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v, _depth + 1) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        # 极小值（如 p=3e-8）round 后会变 0.0，对 LLM 有误导
        # → 转成 "<0.0001" 字符串，让 LLM 正确表述"p < 0.001"
        if 0 < abs(obj) < 0.00005:
            return "<0.0001"
        return round(obj, 4)
    if isinstance(obj, int):
        return obj
    return str(obj)


def _short(msg: str, limit: int = 120) -> str:
    """错误信息截断（避免把大段 API 报错塞给前端）。

    v0.5.4：先过一遍凭据擦除——这段文案会直接进 jsonify(error=...) 回浏览器，
    而底层 SDK 异常正文有时会回显 API Key。
    """
    try:
        from agents.secrets_guard import redact
        msg = redact(msg)
    except Exception:  # noqa: BLE001
        pass
    msg = msg.replace("\n", " ").strip()
    return msg if len(msg) <= limit else msg[:limit] + "…"
