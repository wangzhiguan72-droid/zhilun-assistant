"""
审计对话（v1.6 · 规划 §三 ②）
==============================
用户对「一条统计比对」追问（"为什么说该用非参数？""这个 p 值够显著吗？"），
由 LLM 用**规则引擎已算出的统计量**给出解释。

设计铁律（与 llm_enhance / llm_audit 一致，改动前务必读）：
    1. **只解释，不计算**：输入只有单条 comparison 的 summary 子字典
       （方法名 / 统计量名 / 论文值 / 实算值 / 差值 / 已有结论），
       绝不传 df、绝不传原始观测、绝不传其它条目。→ 铁律②（最小化输入）。
    2. **不能改结果**：prompt 明确禁止 LLM 推翻规则引擎的 status；
       最终结论永远以规则引擎为准，LLM 只负责"讲人话"。
    3. **冻结前缀**：system 字节级固定，为前缀缓存铺路（同 llm_enhance 模式）。
    4. **失败静默降级**：任何异常/无 Key 都不抛，返回 (None, error, meta)，
       前端回退到 summary.verdict 的模板文案（规则结果仍在，不白屏）。
    5. **脱敏**：错误出口过 secrets_guard（这段文案会回浏览器）。

接口：
    explain_comparison(summary, question, *, force=False)
        -> (answer_md | None, error | None, meta dict)
        meta: {"cached": bool, "model": str}
"""
from __future__ import annotations

import json
from typing import Any

from llm_cache import llm_cache

# ---------------------------------------------------------------------------
# 冻结前缀：字节级不变，为前缀缓存铺路
# ⚠️ 改任何措辞 = 改 prompt → 必须 bump PROMPT_VERSION
# ---------------------------------------------------------------------------
# 与 llm_enhance 的 v2 独立编号：本模块首个版本，命名空间区分开避免误判缓存。
PROMPT_VERSION = "chat-v1"

FROZEN_SYSTEM = (
    "你是「智论助手」的论文统计答疑助手。用户会对**一条已经算好的统计比对结果**追问，"
    "你要用通俗、克制的中文解释清楚，帮用户判断这条结果意味着什么。\n"
    "硬性规则：\n"
    "1. 只能引用给定 JSON 中的数字与结论，禁止编造任何统计量、p 值、样本量\n"
    "2. 不要推翻已给出的 status（一致/不一致等）——那是规则引擎的结论，你只负责解释\n"
    "3. 若用户的问题超出这条比对的信息范围（比如问别的变量、问整体研究设计），"
    "明确说明「这需要回到原始数据/论文上下文核对」，不要猜\n"
    "4. 指出可能原因时，给**具体可操作的排查方向**（如缺失值处理、样本范围、四舍五入），"
    "而不是笼统的「建议检查数据」\n"
    "5. 用规范学术语气，避免口语化和 AI 腔\n"
    "6. 篇幅控制在 200 字以内；输出纯 Markdown 正文，不要代码块，不要重复标题\n"
    "7. 不提供任何代写、伪造数据或规避检测的方法"
)

_FROZEN_USER_PREFIX = "本条目比对信息（JSON）：\n"


def explain_comparison(summary: dict[str, Any], question: str,
                       *, force: bool = False) -> tuple[str | None, str | None, dict[str, Any]]:
    """对单条比对摘要回答用户追问（带缓存 + 容灾 + 静默降级）。

    返回 (answer_md, error, meta)；成功时 error 为 None。
    """
    question = (question or "").strip()
    if not question:
        return None, "请先输入你的问题", {"cached": False, "model": ""}
    if not summary:
        return None, "没有可解释的比对条目", {"cached": False, "model": ""}

    # 输入最小化：只取白名单字段，防止调用方误传整条 comparison（含多余内容）
    payload = {
        "方法": summary.get("method_cn") or summary.get("method_key") or "",
        "统计量": summary.get("kind_cn") or "",
        "论文写的值": summary.get("paper"),
        "实算的值": summary.get("real"),
        "差值": summary.get("diff"),
        "结论": summary.get("status_cn") or summary.get("status") or "",
        "规则引擎给的说明": summary.get("verdict") or "",
        "可能原因提示": summary.get("hint") or "",
    }
    try:
        payload_json = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return None, f"比对信息序列化失败：{e}", {"cached": False, "model": ""}

    # ---- 查缓存（key 含 question，不同追问各自成键）----
    model_name = ""
    try:
        from agents import get_router
        model_name = _router_model_name(get_router(), "audit_chat")
    except Exception:  # noqa: BLE001
        pass

    cache_key = llm_cache.make_key(
        model_name, PROMPT_VERSION, "audit_chat",
        {"summary": payload, "q": question},
    )
    if not force:
        entry = llm_cache.get(cache_key)
        if entry is not None:
            return entry["text"], None, {"cached": True, "model": model_name}

    user_prompt = (
        _FROZEN_USER_PREFIX
        + payload_json
        + "\n\n用户追问：" + question
        + "\n\n请直接回答这个追问。"
    )

    try:
        from agents import AgentError, get_router
        router = get_router()
        text = router.complete(
            "audit_chat",
            prompt=user_prompt,
            system=FROZEN_SYSTEM,
            max_tokens=500,
        )
    except AgentError as e:
        entry = llm_cache.peek(cache_key) if not force else None
        if entry is not None:
            return entry["text"], None, {"cached": True, "model": model_name}
        return None, f"LLM 不可用：{_short(str(e))}", {"cached": False, "model": model_name}
    except Exception as e:  # noqa: BLE001
        return None, f"LLM 调用异常：{_short(str(e))}", {"cached": False, "model": model_name}

    text = (text or "").strip()
    if not text:
        return None, "LLM 返回空内容", {"cached": False, "model": model_name}

    # ---- 写缓存（容灾切换时按实际模型补写一份）----
    try:
        from agents import get_router
        actual = _router_model_name(get_router(), "audit_chat")
    except Exception:  # noqa: BLE001
        actual = model_name
    if actual and actual != model_name:
        llm_cache.put(llm_cache.make_key(
            actual, PROMPT_VERSION, "audit_chat",
            {"summary": payload, "q": question}), text)
        model_name = actual
    llm_cache.put(cache_key, text)
    return text, None, {"cached": False, "model": model_name}


def _router_model_name(router, state: str = "audit_chat") -> str:
    """稳定模型名（进缓存 key），容灾切换不漂移。"""
    try:
        fn = getattr(router, "cache_model_name", None)
        if callable(fn):
            return fn(state) or ""
        agent = router._get_agent(state)   # 兼容旧接口
        return agent.model_name
    except Exception:  # noqa: BLE001
        return ""


def _short(msg: str, limit: int = 120) -> str:
    """错误截断 + 凭据擦除（这段会进 jsonify(error=...) 回浏览器）。"""
    try:
        from agents.secrets_guard import redact
        msg = redact(msg)
    except Exception:  # noqa: BLE001
        pass
    msg = msg.replace("\n", " ").strip()
    return msg if len(msg) <= limit else msg[:limit] + "…"
