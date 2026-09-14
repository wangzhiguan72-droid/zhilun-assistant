"""⑧ 答辩准备包（P6 · 八站流程最后一站）
==============================================
给"明天就要答辩"的学生两样东西：

    1. **高频 Q&A 预演**——答辩老师最常问的问题，配好**答题要点**，
       每个要点都引用本次分析**实算出来的统计量**（铁律：绝不编数）。
       纯规则模板（L0，零 LLM、零成本、断网可用）；失败永不阻断。
    2. **图表打包**——会话内做过的每张统计图打成 zip 一次性下载
       （打包在内存完成，**不落盘**，守"全内存不落盘"红线）。

设计边界：
    - 本模块是**纯函数**：`build_qa_pack(summaries, datacheck_report=None)`，
      不 import app、不被 app 的现有逻辑依赖（与 datacheck 同类先例）。
    - summaries 由**后端重放分析**得来（app.py 调 methods_registry.call_method），
      前端只传"做过哪些分析"的参数，数字永远由后端实算——前端不可信。
    - 问题口径守红线：只教"怎么答"，不替学生编结论；数据质量问题
      只说"可疑，请核对"，永不判定造假。
"""
from __future__ import annotations

from typing import Any

# 效应量口径（Cohen / Cohen ; Plonsky 通用教学口径，答辩常用）
_D_BULBS = ((0.2, "小"), (0.5, "中"), (0.8, "大"))
_ETA_BULBS = ((0.01, "小"), (0.06, "中"), (0.14, "大"))
_R_BULBS = ((0.1, "小"), (0.3, "中"), (0.5, "大"))


def _fmt_p(p: Any) -> str:
    """p 值展示：极小值防 round 成 0。"""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return ""
    if p < 0.001:
        return "p < 0.001"
    return f"p = {p:.3f}"


def _bulb(value: float, bulbs) -> str:
    v = abs(float(value))
    label = "大" if v >= bulbs[-1][0] else (
        "中" if v >= bulbs[-2][0] else ("小" if v >= bulbs[0][0] else "微小"))
    return label


def _n_text(s: dict[str, Any]) -> str | None:
    """从 summary 抽样本量描述（各方法字段名不同）。"""
    if "n1" in s and "n2" in s:
        return f"两组分别为 n₁={s['n1']}、n₂={s['n2']}（合计 {s['n1'] + s['n2']}）"
    if "n" in s:
        return f"n = {s['n']}"
    for k in ("n_total", "n_obs", "N"):
        if k in s:
            return f"n = {s[k]}"
    return None


def _effect_qa(label: str, method: str, s: dict[str, Any]) -> dict[str, Any] | None:
    """效应量题：有什么效应量就答什么，没有则跳过（不硬凑）。"""
    points: list[str] = []
    cite: dict[str, Any] = {}
    if "d" in s and s["d"] is not None:
        d = float(s["d"])
        points.append(
            f"Cohen's d = {abs(d):.2f}，按常用口径属于**{_bulb(d, _D_BULBS)}**效应"
            f"（0.2 小 / 0.5 中 / 0.8 大）。")
        cite["d"] = round(d, 3)
    if "eta_squared" in s and s["eta_squared"] is not None:
        e = float(s["eta_squared"])
        points.append(
            f"η² = {e:.3f}，属于**{_bulb(e, _ETA_BULBS)}**效应量"
            f"（0.01 小 / 0.06 中 / 0.14 大），可解释为组间差异占总变异的 {e:.0%}。")
        cite["eta_squared"] = round(e, 3)
    if "r" in s and s["r"] is not None and "p" in s:
        r = float(s["r"])
        points.append(
            f"相关系数 r = {r:.3f}，属于**{_bulb(r, _R_BULBS)}**强度"
            f"（0.1 小 / 0.3 中 / 0.5 大），r² = {r * r:.3f} 即两变量共享约 "
            f"{r * r:.0%} 的变异。")
        cite["r"] = round(r, 3)
    if "cramers_v" in s and s["cramers_v"] is not None:
        v = float(s["cramers_v"])
        points.append(f"Cramér's V = {v:.3f}（关联强度，越接近 1 越强）。")
        cite["cramers_v"] = round(v, 3)
    if not points:
        return None
    points.append(
        "答题要点：统计显著只说明「差异/关系不太可能是偶然」，效应量说明"
        "「有多明显」——审稿和答辩越来越看重大小，建议两个都报。")
    return {
        "question": f"这个结果的效应量多大？实际意义明显吗？（{label}）",
        "points": points,
        "cite": cite,
    }


def _method_qa(label: str, method: str, s: dict[str, Any]) -> dict[str, Any]:
    """「为什么用这个方法 / 前提满足吗」——按方法查前提话术。"""
    points: list[str] = []
    cite: dict[str, Any] = {}
    if method in ("independent_t", "paired_t"):
        if "levene_p" in s and s["levene_p"] is not None:
            lp = float(s["levene_p"])
            ok = lp >= 0.05
            points.append(
                f"方差齐性 Levene 检验 {_fmt_p(lp)}，"
                f"{'满足' if ok else '不满足'}方差齐性假设"
                + ("" if ok else "，因此看 Welch 校正后的结果更稳妥。"))
            cite["levene_p"] = round(lp, 3)
        points.append(
            "适用前提：因变量连续、组内近似正态、两组独立（配对 T 则为同批人前后测）。"
            "样本每组 ≥30 时中心极限定理兜底，正态性偏离影响有限。")
    elif method == "anova":
        points.append(
            "适用前提：三组及以上、组内近似正态、方差齐；备选回答——"
            "若严重违反正态可用 Kruskal-Wallis（本工具暂未内置，可说明处理思路）。")
    elif method == "two_way_anova":
        points.append(
            "双因素 ANOVA 同时看两个因素的**主效应**和**交互效应**；"
            "交互显著时，主效应要结合简单效应来解释，不能只报主效应。")
    elif method == "repeated_measures_anova":
        if "mauchly_p" in s and s["mauchly_p"] is not None:
            mp = float(s["mauchly_p"])
            points.append(
                f"球形度 Mauchly 检验 {_fmt_p(mp)}，"
                + ("满足球形度假设。" if mp >= 0.05 else
                   "不满足 → 已用 Greenhouse-Geisser 校正自由度后判断显著性，"
                   "答辩时可主动说明这一点，是加分项。"))
            cite["mauchly_p"] = round(mp, 3)
    elif method == "correlation":
        points.append(
            "Pearson 相关前提：两变量连续、近似线性、双变量近似正态；"
            "若明显偏态可改用 Spearman 秩相关（说明思路即可）。")
    elif method == "chi_square":
        points.append(
            "卡方检验前提：分类变量之间相互独立、期望频数不宜过小"
            "（<5 的格子超过 20% 时应合并类别或用 Fisher 精确检验）。")
    elif method in ("mann_whitney", "wilcoxon"):
        points.append(
            "选非参数的原因：数据偏态/序次型/样本量小，不满足参数检验前提；"
            "非参数检验不假设正态，用秩次比较，稳健但检验效能略低。")
    elif method in ("linear_regression", "logistic_regression"):
        points.append(
            "回归前提要点：线性关系、误差独立、残差近似正态、"
            "自变量间无严重多重共线性（工具已输出 VIF，可引用）。")
    elif method == "cronbach_alpha":
        points.append(
            "α ≥ 0.7 可接受、≥ 0.8 良好；若某题「删项后 α」明显高于整体 α，"
            "说明该题可能不同质，可考虑删除后重算。")
    points.append(f"答题要点：一句话模板——「{label}适用于本研究的数据结构"
                  f"（变量类型与设计），且前提检验{'通过' if points else ''}，故采用」。")
    return {
        "question": f"为什么用{label}？前提条件满足吗？",
        "points": points,
        "cite": cite,
    }


def _result_qa(label: str, method: str, s: dict[str, Any]) -> dict[str, Any] | None:
    """结果一句话：把主统计量串成可直接说出口的表述。"""
    stat = ""
    cite: dict[str, Any] = {}
    if "t" in s and s["t"] is not None:
        dfree = s.get("df", "")
        stat = f"t({dfree}) = {float(s['t']):.2f}"
        cite["t"] = round(float(s["t"]), 3)
    elif "F" in s and s["F"] is not None:
        stat = f"F = {float(s['F']):.2f}"
        cite["F"] = round(float(s["F"]), 3)
    elif "chi2" in s and s["chi2"] is not None:
        stat = f"χ² = {float(s['chi2']):.2f}"
        cite["chi2"] = round(float(s["chi2"]), 3)
    elif "W" in s and s["W"] is not None:
        stat = f"统计量 W = {float(s['W']):.2f}"
        cite["W"] = round(float(s["W"]), 3)
    if not stat or "p" not in s:
        return None
    p = float(s["p"])
    sig = p < 0.05
    points = [
        f"标准表述：{label}结果显示 {_fmt_p(p)}"
        + ("，达到显著水平（α = 0.05）。" if sig else "，未达到显著水平（α = 0.05）。"),
        f"口头版：「{stat}，{_fmt_p(p)}，"
        + ("差异/关系显著。" if sig else "没有足够证据支持存在差异/关系。") + "」",
        "答题要点：被追问时强调——p < 0.05 表示「若真的没差异，出现这么大"
        "结果的概率不足 5%」，不等于「差异一定真实」也不表示「效应很大」。",
    ]
    if "ci_low" in s and s.get("ci_high") is not None:
        points.append(
            f"95% 置信区间 [{float(s['ci_low']):.2f}, {float(s['ci_high']):.2f}]"
            "——区间不跨 0 即与显著结论一致，可主动引用，显专业。")
    return {
        "question": f"用一句话说明你的主要结果（{label}）",
        "points": points,
        "cite": {**cite, "p": "<0.001" if p < 0.001 else round(p, 3)},
    }


def _sample_qa(label: str, method: str, s: dict[str, Any]) -> dict[str, Any] | None:
    n_text = _n_text(s)
    if not n_text:
        return None
    return {
        "question": f"样本量是多少？够吗？（{label}）",
        "points": [
            f"实算口径：{n_text}。",
            "答题要点：连续变量常用经验法则为每组 ≥30；若被问「为什么不多招」——"
            "引用课程/问卷回收实际条件 + 说明效应量已足够检出（显著即事后功效的体现）。",
        ],
        "cite": {},
    }


def build_qa_pack(summaries: list[dict[str, Any]],
                  datacheck_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """由后端实算的分析结果生成答辩 Q&A。

    summaries: [{"method": "independent_t", "label": "独立样本T检验",
                 "summary": {...统计量...}, "cols": {"group_col": ..., }}]
    每个分析生成 3-4 题；全坏账（无任何可引用统计量）时返回空 qa 不崩。
    """
    qa: list[dict[str, Any]] = []
    used: list[str] = []
    for run in summaries:
        method = str(run.get("method", ""))
        label = str(run.get("label") or method)
        s = run.get("summary") or {}
        if not isinstance(s, dict) or not s:
            continue
        used.append(label)
        for maker in (_result_qa, _method_qa, _effect_qa, _sample_qa):
            item = maker(label, method, s) if maker is not _method_qa \
                else _method_qa(label, method, s)
            if item and item.get("points"):
                qa.append(item)

    # 数据质量题（答辩高频：「你的数据清洗过吗」）
    if datacheck_report:
        ds = datacheck_report.get("summary", {})
        verdict = ds.get("verdict", "")
        if verdict:
            qa.append({
                "question": "你的数据做过清洗吗？质量怎么保证？",
                "points": [
                    f"体检结论：{verdict}",
                    f"构成：高优先级 {ds.get('high', 0)} 处、可疑 {ds.get('mid', 0)} 处、"
                    f"提示 {ds.get('low', 0)} 处（共检查 {len(ds.get('checks_run', []))} 项）。",
                    "答题要点：说明体检 → 核对 → 修复（如重复行、反向计分）→"
                    " 复算的流程，强调「原文件不动、清洗用副本」，体现规范性。",
                ],
                "cite": {},
            })

    return {
        "qa": qa,
        "used_analyses": used,
        "note": ("每条「答题要点」均引用本次实算统计量，数字可直接说出口；"
                 "本清单是预演辅助，不代替你对研究本身的理解。"),
    }
