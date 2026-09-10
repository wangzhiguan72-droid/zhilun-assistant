"""
论文深度审计 LLM 层（v0.4.2）
=============================
把 Router 的 paper_check 状态（sf/deepseek-v4-pro）接到论文排查 Tab 上。
规则引擎（audit.py）仍然先跑，LLM 在规则结果之上做"深度审计"。

设计原则（与 llm_enhance.py / ROADMAP §5 一致，外加两条实测结论）：

    1. 冻结前缀（agents/prompts.py 契约）：
       prompt = [冻结区：论文全文 + schema，字节级稳定] + [变量区：规则发现 + 指令]
       → 同一篇论文重复审计 / 追问，论文文本走前缀缓存命中（价差 10 倍，
         实测 2026-09-09：hit=2048/miss=20）
    2. 前缀对象复用：模块级 LRU 按论文哈希缓存已构建的前缀字符串，
       同篇论文绝不重建（既省 CPU，也杜绝拼接顺序意外变化导致缓存失效）
    3. 规则发现做变量区（不做冻结区）：它依赖 (论文, 数据, 指令) 三元组，
       数据换了 / 指令换了它就变——放前面会打碎前缀缓存
    4. 响应缓存（llm_cache）：相同 (论文, 规则发现, 指令) 直接命中，0 token
    5. 失败静默回退：LLM 任何报错都不阻断规则报告，只标记 llm_error
    6. LLM 只读论文文本 + 规则发现摘要，不喂原始数据行（隐私 + token）

接口：
    audit_paper_with_llm(paper_text, audit, directive, force=False)
        -> (section_md | None, error | None, meta dict)
        meta: {"cached": bool, "model": str}
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from agents.prompts import PAPER_CHECK_SYSTEM, build_paper_check_prefix
from llm_cache import llm_cache

# 改 PAPER_CHECK_SYSTEM / schema / 变量区措辞时必须 bump（旧缓存自动失效）
PROMPT_VERSION = "v2"  # v0.5.1：Ponytail 决策层优化

# v0.5.1：Ponytail 决策层优化（参考 best-practice）
_PONYTAIL_DECISION = (
    "审计前决策（参考 Ponytail 最佳实践）：\n"
    "在输出审计意见前，先快速判断：\n"
    "- 论文中声称的统计方法是否明显错误？（如样本量不足、方法不匹配）\n"
    "- 统计量差异是否可容忍？（如 2.31 vs 2.5，是否属于四舍五入误差）\n"
    "- 规则引擎的建议是否足够？（若规则已给出明确修复方向，无需额外展开）\n"
    "若以上答案倾向于「是」，则：输出简洁结论，避免过度审计。\n"
    "若你在输出中跳过了某些常规核查，可用「ponytail: 原因」标注（可选）。\n\n"
)

# V4-Pro 是推理模型：reasoning token 计入 completion，预算留足
_MAX_TOKENS = 2048

# 规则发现摘要的长度上限（变量区，别让数据侧内容淹没论文审计）
_DIGEST_LIMIT = 1500

# 论文正文进冻结前缀的长度上限（字符数）
# 背景：provider 侧有 prompt 硬上限，长论文整篇塞进去会 400。
# 2000 字 ≈ 覆盖摘要 + 研究假设 + 方法描述，够做深度审计判断。
PAPER_PREFIX_LIMIT = 2000


# ---------------------------------------------------------------------------
# 冻结前缀 LRU：同篇论文 → 同一个前缀对象（不重建、字节级不变）
# ---------------------------------------------------------------------------
_PREFIX_CACHE_MAX = 4  # 论文全文不小，只留最近 4 篇
_prefix_cache: dict[str, str] = {}  # paper_sha256 -> prefix


def get_paper_prefix(paper_text: str) -> tuple[str, str]:
    """返回 (冻结前缀, 论文哈希)。同篇论文命中缓存直接复用对象。

    v0.5.1 截断：只取前 PAPER_PREFIX_LIMIT 字符，避免 provider 侧
    prompt 长度硬限制（曾踩坑：长论文直接 400 Prompt exceeds max length）。

    注意：hash 基于截断后文本，所以缓存语义自洽；但「前 N 字相同的两篇
    论文」会共享前缀缓存——N 取值较大（默认 2000），实际碰撞概率极低。
    """
    safe_text = (paper_text or "").strip()[:PAPER_PREFIX_LIMIT]
    paper_hash = hashlib.sha256(safe_text.encode("utf-8")).hexdigest()
    prefix = _prefix_cache.get(paper_hash)
    if prefix is None:
        prefix = build_paper_check_prefix(safe_text)
        _prefix_cache[paper_hash] = prefix
        # 简单 LRU：超上限丢最旧（dict 保序，第一个即最旧）
        while len(_prefix_cache) > _PREFIX_CACHE_MAX:
            _prefix_cache.pop(next(iter(_prefix_cache)))
    return prefix, paper_hash


def _scope_note(paper_text: str) -> str:
    """审计范围说明：论文被截断时如实告知用户（别让人以为全文都审了）。"""
    total = len((paper_text or "").strip())
    if total <= PAPER_PREFIX_LIMIT:
        return ""
    return (f"> ⚠️ 本次 AI 审计基于论文前 {PAPER_PREFIX_LIMIT} 字"
            f"（全文共 {total} 字）。超长部分未纳入，"
            f"如需全文深度审计请分段核查。\n\n")


def _reset_prefix_cache() -> None:
    """测试用：清空前缀缓存。"""
    _prefix_cache.clear()


# ---------------------------------------------------------------------------
# 变量区：规则引擎发现摘要（依赖数据 + 指令，每次可变 → 永远放最后）
# ---------------------------------------------------------------------------
def build_rule_digest(audit: dict[str, Any]) -> str:
    """把 audit.py 的规则结果压成紧凑摘要，给 LLM 当"数据侧事实"。

    适配 audit.py 真实结构：
        comparisons: [{status: match|diff|unknown|no_real, paper, real, diff}, ...]
        suggestions: [str, ...]
    摘要是确定性的：同 (论文, 数据, 指令) → 字节相同（缓存 key 稳定的前提）。
    """
    comparisons = audit.get("comparisons") or []
    suggestions = audit.get("suggestions") or []
    status_map = {"match": "一致", "diff": "不一致", "unknown": "无法比对"}
    lines: list[str] = []
    for c in comparisons:
        if isinstance(c, str):
            lines.append(f"- {c}")
            continue
        if not isinstance(c, dict):
            continue
        status = c.get("status", "")
        if status == "no_real":
            lines.append(f"- 真实数据重跑失败：{c.get('reason', '未知原因')}")
        else:
            flag = status_map.get(status, status)
            lines.append(f"- 论文声称 {c.get('paper', '?')}；真实数据重跑{flag}：{c.get('real', '?')}")
    for s in suggestions:
        text = s if isinstance(s, str) else (s.get("title") or s.get("text") or "")
        if text:
            lines.append(f"- 规则建议：{text}")
    if not lines:
        return "规则引擎没有发现可比对的内容。"
    digest = "规则引擎核查发现（供参考，可与你的判断对照）：\n" + "\n".join(lines)
    if len(digest) > _DIGEST_LIMIT:
        digest = digest[:_DIGEST_LIMIT] + "…（已截断）"
    return digest


def _digest_hash(digest: str) -> str:
    return hashlib.sha256(digest.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def audit_paper_with_llm(paper_text: str, audit: dict[str, Any],
                         directive: str, *, force: bool = False
                         ) -> tuple[str | None, str | None, dict[str, Any]]:
    """调用 Router(paper_check → V4-Pro) 生成深度审计段（带响应缓存）。

    返回 (section_md, error, meta)：成功时 error 为 None。
    meta: {
        "cached": bool,
        "model": str,
        "input_tokens": int,   # 本次实际付费的 input tokens（未命中的）
        "output_tokens": int,  # 本次输出 tokens
        "cost_estimate": float,  # 预估费用（元，按硅基流动 V4-Pro 计价）
    }
    任何异常都不抛出——调用方（/api/check_paper）直接降级为纯规则报告。
    """
    paper_text = (paper_text or "").strip()
    if not paper_text:
        return None, "论文文本为空，无法 AI 审计", {"cached": False, "model": "",
                                                     "input_tokens": 0,
                                                     "output_tokens": 0,
                                                     "cost_estimate": 0.0}

    directive = (directive or "").strip()
    prefix, paper_hash = get_paper_prefix(paper_text)
    rule_digest = build_rule_digest(audit)

    # ---- 响应缓存（v0.5，复用 llm_cache 的 LRU）----
    model_name = _router_model_name()
    cache_key = llm_cache.make_key(
        model_name, PROMPT_VERSION, "paper_check",
        {"paper": paper_hash, "rules": _digest_hash(rule_digest),
         "directive": directive},
    )
    if not force:
        entry = llm_cache.get(cache_key)
        if entry is not None:
            meta = {"cached": True, "model": model_name,
                     "input_tokens": 0, "output_tokens": 0,
                     "cost_estimate": 0.0}
            return _wrap(entry["text"], paper_text), None, meta

    # ---- 变量区（规则发现 + 指令），永远在冻结前缀之后 ----
    parts: list[str] = ["数据侧核查发现：", rule_digest]
    if directive:
        parts.append(f"用户本次重点关注：{directive}（优先围绕它给审计意见）")
    parts.append("请输出深度审计。")
    user_prompt = prefix + "\n" + "\n".join(parts) + "\n"

    try:
        from agents import AgentError, get_router
        router = get_router()
        result = router._get_agent("paper_check").client.chat.completions.create(
            model=router._get_agent("paper_check").model_name,
            messages=[{"role": "system", "content": PAPER_CHECK_SYSTEM},
                     {"role": "user", "content": user_prompt}],
            max_tokens=_MAX_TOKENS,
        )
        text = result.choices[0].message.content or ""
        usage = result.usage
        # 硅基流动透传 usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
        input_paid = getattr(usage, "prompt_cache_miss_tokens",
                            getattr(usage, "prompt_tokens", 0))
        output_tokens = getattr(usage, "completion_tokens", 0)
        # V4-Pro 计价：缓存命中 ¥1/M，未命中 ¥12/M（2026-09-09 官方价）
        cost = (input_paid * 12 + output_tokens * 24) / 1_000_000
    except AgentError as e:
        # 兜底：真调失败时回看缓存（LLM 挂了，旧结果仍可用；peek 不污染统计）
        entry = llm_cache.peek(cache_key) if not force else None
        if entry is not None:
            return _wrap(entry["text"], paper_text), None, {
                "cached": True, "model": model_name,
                "input_tokens": 0, "output_tokens": 0, "cost_estimate": 0.0}
        return None, _short(f"LLM 不可用：{e}"), {"cached": False, "model": model_name,
                                                  "input_tokens": 0, "output_tokens": 0,
                                                  "cost_estimate": 0.0}
    except Exception as e:  # noqa: BLE001
        return None, _short(f"LLM 调用异常：{e}"), {"cached": False, "model": model_name,
                                                  "input_tokens": 0, "output_tokens": 0,
                                                  "cost_estimate": 0.0}

    text = text.strip()
    if not text:
        return None, "LLM 返回空内容", {"cached": False, "model": model_name,
                                         "input_tokens": 0, "output_tokens": 0,
                                         "cost_estimate": 0.0}

    llm_cache.put(cache_key, text)
    meta = {"cached": False, "model": model_name,
             "input_tokens": input_paid, "output_tokens": output_tokens,
             "cost_estimate": round(cost, 6)}  # 6 位小数，避免浮点误差
    return _wrap(text, paper_text), None, meta


def _wrap(text: str, paper_text: str = "") -> str:
    return ("### 🔬 AI 深度审计（V4-Pro，实验功能，请自行核对）\n\n"
            + _scope_note(paper_text) + text)


def _router_model_name() -> str:
    """拿到 paper_check 状态实际路由到的模型名（进缓存 key）。"""
    try:
        from agents import get_router
        agent = get_router()._get_agent("paper_check")
        return agent.model_name
    except Exception:  # noqa: BLE001
        return ""


def _short(msg: str, limit: int = 120) -> str:
    msg = msg.replace("\n", " ").strip()
    return msg if len(msg) <= limit else msg[:limit] + "…"


# ---------------------------------------------------------------------------
# 离线自测（不调 API）：python llm_audit.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _reset_prefix_cache()
    paper = "本研究采用独立样本 t 检验。" * 300  # 模拟长论文
    p1, h1 = get_paper_prefix(paper)
    p2, h2 = get_paper_prefix(paper)
    assert p1 is p2 and h1 == h2, "同篇论文必须复用同一前缀对象"
    p3, h3 = get_paper_prefix("另一篇论文" * 50)
    assert p3 is not p1 and h3 != h1
    # LRU 淘汰：塞满 4 篇后，最旧的 h1 被挤掉
    for i in range(5):
        get_paper_prefix(f"论文{i}" * 100)
    assert h1 not in _prefix_cache, "LRU 淘汰失效"
    print("[1] 前缀 LRU：复用 / 字节稳定 / 淘汰 全部 OK")

    # 缓存 key：同输入同 key，指令变 key 变
    audit = {"comparisons": [{"label": "t 值", "paper_value": "2.5", "real_value": "2.31"}],
             "suggestions": [{"title": "补报效应量"}]}
    d = build_rule_digest(audit)
    assert build_rule_digest(audit) == d, "规则摘要必须确定性"
    k1 = llm_cache.make_key("m", PROMPT_VERSION, "paper_check",
                            {"paper": h3, "rules": _digest_hash(d), "directive": "只看t检验"})
    k2 = llm_cache.make_key("m", PROMPT_VERSION, "paper_check",
                            {"paper": h3, "rules": _digest_hash(d), "directive": "只看t检验"})
    k3 = llm_cache.make_key("m", PROMPT_VERSION, "paper_check",
                            {"paper": h3, "rules": _digest_hash(d), "directive": ""})
    assert k1 == k2 and k1 != k3
    print("[2] 响应缓存 key：确定性 + 指令敏感 全部 OK")

    # 无 Key 时静默降级（不抛异常）
    import os
    os.environ.pop("SILICONFLOW_API_KEY", None)
    os.environ.pop("ZHIPU_API_KEY", None)
    from agents.router import _router as _r  # 清单例（测试隔离）
    import agents.router as _ar
    _ar._router = None
    sec, err, meta = audit_paper_with_llm("论文正文" * 100, audit, "")
    assert sec is None and err and "不可用" in err, (sec, err)
    print("[3] 无 Key 静默降级 OK →", err[:60])
    print("\n=== llm_audit 离线自测 3/3 通过 ===")
