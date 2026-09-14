"""论文副驾驶 · LLM 润色层（可选下游步骤）

把 paper_writer 产出的「证据约束初稿」交给 LLM 做**学术语言润色**：
只润色行文流畅度，不增删 claim、不改数字、不破锚点。

设计铁律（与 llm_enhance 同脉，遵循「计算 + 生成解耦」）：
    1. 计算与生成解耦：LLM 只拿到已成稿的文本 + claim 元信息，绝不碰原始数据。
    2. 失败静默回退：LLM 任何报错 / 无 Key → 直接返回原稿（llm_used=False）。
    3. 证据闸门约束：润色稿必须仍通过 lint_draft；若 LLM「污染」了初稿
       （丢失 (claim-xxx) 锚点、改了 / 加了数字）→ 判定为污染稿，**丢弃**，
       保留原模板稿，并标记 llm_rejected（不让幻觉溜进正文）。
    4. 复用 llm_cache：相同初稿重复润色 → 命中缓存，0 token。
    5. 冻结前缀：FROZEN_POLISH_SYSTEM 字节级固定，改措辞须 bump POLISH_PROMPT_VERSION。

本模块是「可选的润色」——paper_writer 始终产出零幻觉底稿；
没有 Key / LLM 挂了 / 润色被污染，用户拿到的仍是干净的底稿。

接口：
    polish_paper(text, claims, template="latex"|"thesis", *, force=False)
        -> (best_text, meta)
    polish_bundle(bundle, claims, template, *, force=False)
        -> (new_bundle, meta)
"""
from __future__ import annotations

import re
from typing import Any

from llm_cache import llm_cache
from paper_writer import lint_draft, Claim

# ---------------------------------------------------------------------------
# 冻结前缀（v0.5 前缀缓存关键：字节级不变）
# ⚠️ 修改任何措辞 = 改变 prompt → 必须 bump POLISH_PROMPT_VERSION（缓存失效）
# ---------------------------------------------------------------------------
POLISH_PROMPT_VERSION = "v1"

FROZEN_POLISH_SYSTEM = (
    "你是一位严谨的学术论文语言编辑。下面是一篇论文初稿，你的任务："
    "**只润色中文行文流畅度**，让表达更符合学术写作规范。\n\n"
    "硬性约束（违反任一条，你的输出会被直接丢弃，绝不可妥协）：\n"
    "1. 必须原样保留每一处「（claim-xxx）」结论锚点标记，"
    "不得删改、不得移动它所在的句子、不得改变括号内的编号。\n"
    "2. 必须原样保留所有统计量与数字：t、F、p、r、R²、α、χ²、均值、效应量 η²、"
    "样本量等，以及其比较符号（=、<、>）。不得修改任何数字，也不得新增任何数字。\n"
    "3. 不得新增任何结论、claim、比较方向、效果判断、显著性表述或文献引用。\n"
    "4. 不得改变章节结构、标题层级与（若有）LaTeX 命令环境。\n"
    "5. 只做语言层面润色（衔接、措辞、语序、去口语化），不改变任何事实与定量陈述。\n\n"
    "输出：仅返回润色后的完整正文（保持原 Markdown / LaTeX 结构），"
    "不要代码块包裹，不要任何解释或前言后语。"
)


# ---------------------------------------------------------------------------
# 实际 LLM 调用（测试可 monkeypatch 本函数，避免真实网络）
# ---------------------------------------------------------------------------

def _router_model_name(router, state: str = "write_text") -> str:
    """拿到指定状态稳定的模型名（进缓存 key）。"""
    try:
        fn = getattr(router, "cache_model_name", None)
        if callable(fn):
            return fn(state) or ""
        agent = router._get_agent(state)   # 兼容旧接口
        return agent.model_name
    except Exception:  # noqa: BLE001
        return ""


def _call_llm(user_prompt: str, system: str,
              max_tokens: int = 3000) -> tuple[str | None, str | None, str]:
    """调用 Router(write_text) 做润色。

    返回 (text, error, model)。任何异常都不抛出——调用方负责降级。
    """
    try:
        from agents import AgentError, get_router
    except Exception as e:  # noqa: BLE001
        return None, f"无法加载 Router：{e}", ""

    model_name = ""
    try:
        model_name = _router_model_name(get_router(), "write_text")
    except Exception:  # noqa: BLE001
        pass

    try:
        router = get_router()
        text = router.complete(
            "write_text",
            prompt=user_prompt,
            system=system,
            max_tokens=max_tokens,
        )
    except AgentError as e:
        return None, f"LLM 不可用：{_short(str(e))}", model_name
    except Exception as e:  # noqa: BLE001
        return None, f"LLM 调用异常：{_short(str(e))}", model_name

    text = (text or "").strip()
    if not text:
        return None, "LLM 返回空内容", model_name
    return text, None, model_name


# ---------------------------------------------------------------------------
# 后校验：保证润色稿没有「污染」底稿
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\d.])")


def _numset(text: str) -> set[str]:
    """提取文本中所有数字 token（保留符号），用于「数字保真」校验。

    精心的边界断言避免把 `14.093` 拆成 `14`+`093`、也避免把 `p<0.001` 漏掉。
    """
    return {m.group(0) for m in _NUM_RE.finditer(text)}


def _validate_polish(orig: str, polished: str, claims: list[Claim]) -> tuple[bool, str]:
    """润色稿是否仍然「证据安全」。

    返回 (ok, reason)。任一硬约束被打破即 ok=False：
      - 任一 claim_id 锚点丢失
      - 数字集合与底稿不一致（丢失或新增数字 = 改/编统计量）
    """
    ids = [c.claim_id for c in claims]
    for cid in ids:
        if cid not in polished:
            return False, f"丢失结论锚点 {cid}"

    o_nums = _numset(orig)
    p_nums = _numset(polished)
    if o_nums != p_nums:
        missing = sorted(o_nums - p_nums)
        extra = sorted(p_nums - o_nums)
        miss_s = "、".join(missing[:6]) + ("…" if len(missing) > 6 else "")
        extra_s = "、".join(extra[:6]) + ("…" if len(extra) > 6 else "")
        return False, f"数字不符：丢失[{miss_s}] 新增[{extra_s}]"

    return True, ""


def _strip_fence(text: str) -> str:
    """去掉 LLM 可能加上的 ```tex / ``` 代码围栏。"""
    t = text.strip()
    if t.startswith("```"):
        # 去掉首行围栏（含语言标识）与末行围栏
        t = re.sub(r"^```[a-zA-Z]*\s*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
        t = t.strip()
    return t


def _short(msg: str, limit: int = 120) -> str:
    msg = msg.replace("\n", " ").strip()
    return msg if len(msg) <= limit else msg[:limit] + "…"


# ---------------------------------------------------------------------------
# 顶层：润色单篇文本
# ---------------------------------------------------------------------------

def polish_paper(text: str, claims: list[Claim], template: str = "latex",
                 *, force: bool = False) -> tuple[str, dict[str, Any]]:
    """润色一篇论文初稿。

    返回 (best_text, meta)：
      best_text —— 润色稿（通过闸门）或原稿（LLM 失败 / 被污染 / 无 Key）。
      meta —— {"llm_used","cached","model","rejected","reject_reason",
               "lint_before","lint_after"}。

    任何异常都不会抛出；最坏情况返回 (原稿, llm_used=False)。
    """
    meta: dict[str, Any] = {
        "llm_used": False,
        "cached": False,
        "model": "",
        "rejected": False,
        "reject_reason": "",
        "lint_before": 0,
        "lint_after": 0,
    }
    if not text.strip():
        return text, meta

    orig_issues = lint_draft(text, claims)
    meta["lint_before"] = len(orig_issues)

    # 模型名（进缓存 key；拿不到也能继续，只命中率略降）
    try:
        from agents import get_router
        model_name = _router_model_name(get_router(), "write_text")
    except Exception:  # noqa: BLE001
        model_name = ""

    cache_key = llm_cache.make_key(model_name, POLISH_PROMPT_VERSION, template, {"t": text})

    # ---- 查缓存（force 时绕过）----
    if not force:
        entry = llm_cache.get(cache_key)
        if entry is not None:
            polished = _strip_fence(entry["text"])
            ok, reason = _validate_polish(text, polished, claims)
            meta.update(llm_used=True, cached=True, model=model_name,
                        lint_after=len(lint_draft(polished, claims)))
            if ok:
                return polished, meta
            # 命中但已污染（底稿本身变了）：弃用缓存，回退原稿
            meta.update(rejected=True, reject_reason="cached_invalid:" + reason)
            return text, meta

    # ---- 真调 ----
    polished_raw, err, model_name = _call_llm(text, FROZEN_POLISH_SYSTEM)
    if err or not polished_raw:
        # 静默回退原稿（LLM 不可用 / 无 Key）
        meta.update(llm_used=False, reject_reason=err or "empty")
        return text, meta

    polished = _strip_fence(polished_raw)
    ok, reason = _validate_polish(text, polished, claims)
    if not ok:
        # 污染稿 → 丢弃，保留原底稿，标记 rejected
        meta.update(llm_used=True, rejected=True, reject_reason=reason, model=model_name)
        return text, meta

    # 通过闸门 → 写缓存 + 返回润色稿
    llm_cache.put(cache_key, polished)
    meta.update(llm_used=True, cached=False, model=model_name,
                lint_after=len(lint_draft(polished, claims)))
    return polished, meta


# ---------------------------------------------------------------------------
# 顶层：润色整个 paper/ bundle
# ---------------------------------------------------------------------------

def polish_bundle(bundle: dict[str, str], claims: list[Claim], template: str,
                  *, force: bool = False) -> tuple[dict[str, str], dict[str, Any]]:
    """对 bundle 里的结果正文文件做润色，返回新 bundle + meta。

    只润色「可读正文」文件，绝不碰 claim_inventory.md / figures_manifest.md
    （它们是证据真源，润色会破坏 YAML 结构）。
    """
    new_bundle = dict(bundle)
    meta: dict[str, Any] = {"llm_used": False}

    # 主展示文件
    primary_key = "paper/manuscript.tex" if template == "latex" else "paper/thesis.md"
    if primary_key in new_bundle:
        polished, m1 = polish_paper(new_bundle[primary_key], claims,
                                    template=template, force=force)
        new_bundle[primary_key] = polished
        meta["primary"] = m1
        meta["llm_used"] = meta["llm_used"] or m1.get("llm_used", False)

    # 可读副本 draft.md（始终为 thesis 体例）同步润色，保持评审一致
    draft_key = "paper/draft.md"
    if draft_key in new_bundle:
        d_polished, m2 = polish_paper(new_bundle[draft_key], claims,
                                      template="thesis", force=force)
        new_bundle[draft_key] = d_polished
        meta["draft"] = m2
        meta["llm_used"] = meta["llm_used"] or m2.get("llm_used", False)

    return new_bundle, meta
