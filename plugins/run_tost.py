"""TOST 等价性检验（Two One-Sided Tests）· 示例插件
================================================================
**为什么是 TOST 而不是别的**

毕业论文里最常见的误用之一：p > 0.05 就被写成"两组没有差异"。
**这在统计上是错的** —— 不显著只代表"没检出差异"，不等于"差异不存在"
（可能是样本量不够）。要论证**两组等价**，标准做法是 **TOST 等价性检验**：

    H0：|μ₁ − μ₂| ≥ Δ（差异**不小于**你设的等价边界）
    H1：|μ₁ − μ₂| <  Δ（差异小到可以忽略）

方法：做**两个**单侧检验，两个都拒绝才算等价（所以 TOST 的 p 取两者的较大值）。
判定等价于"差异的 (1−2α) 置信区间完全落在 (−Δ, +Δ) 之内"——
α=0.05 时就是看 **90% CI**，这是 TOST 的标准做法，不是写错。

**等价边界 Δ（margin）怎么定**：没有万能默认值，必须由研究者按领域判断
（"多大的差异在实践中可以视为无差别"）。Lakens 建议常用 Cohen's d = 0.3~0.5
对应的原始单位。本插件默认 0.5 只是占位，**请务必替换成你自己的理由**，
并在论文里写明依据。

**用的是 Welch 版**（不假设方差齐性），自由度为 Welch–Satterthwaite 近似，
因此方差不齐时结论依然稳健（代价是略保守）。

插件契约见 `plugin_registry.py` 模块文档。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

SCHEMA = {
    "key": "tost",
    "label": "TOST 等价性检验",
    "needs": ["group_col", "value_col"],
    "params": [
        {"name": "margin", "default": 0.5},
        {"name": "alpha", "default": 0.05},
    ],
    "ui": "group_value",
    "picker_label": "TOST 等价性检验（插件）",
    "error_hint": "TOST 等价性检验需要：一个分组列（恰好 2 组）+ 一个数值列。",
    "description": "论证「两组没有差异」的正确做法（p>0.05 不能说明无差异）",
}


def run(df: pd.DataFrame, group_col: str, value_col: str,
        margin: float = 0.5, alpha: float = 0.05, **kw) -> dict:
    """对两组独立样本做 Welch-TOST 等价性检验。"""
    margin = float(margin)
    alpha = float(alpha)
    if not np.isfinite(margin) or margin <= 0:
        raise ValueError("等价边界 margin 必须是正数"
                         "（它代表「多大的差异仍可视为无差别」）。")
    if not 0 < alpha < 0.5:
        raise ValueError("alpha 必须在 0 到 0.5 之间。")

    sub = df[[group_col, value_col]].dropna()
    vals = pd.to_numeric(sub[value_col], errors="coerce")
    sub = sub.assign(_v=vals).dropna(subset=["_v"])
    levels = list(pd.unique(sub[group_col].astype(str)))
    if len(levels) != 2:
        raise ValueError(
            f"TOST 需要分组列「{group_col}」恰好有 2 个水平，"
            f"实际有 {len(levels)} 个（{('、'.join(levels[:5]))}…）。"
        )

    g1, g2 = levels
    v1 = sub.loc[sub[group_col].astype(str) == g1, "_v"].to_numpy(dtype=float)
    v2 = sub.loc[sub[group_col].astype(str) == g2, "_v"].to_numpy(dtype=float)
    n1, n2 = len(v1), len(v2)
    if n1 < 2 or n2 < 2:
        raise ValueError("每组至少需要 2 个有效观测。")

    m1, m2 = float(v1.mean()), float(v2.mean())
    s1, s2 = float(v1.std(ddof=1)), float(v2.std(ddof=1))
    diff = m1 - m2

    # Welch 标准误与自由度
    se = float(np.sqrt(s1 ** 2 / n1 + s2 ** 2 / n2))
    if se <= 0:
        raise ValueError("两组合并标准误为 0（数据无变异），无法检验。")
    num = (s1 ** 2 / n1 + s2 ** 2 / n2) ** 2
    den = ((s1 ** 2 / n1) ** 2 / (n1 - 1)) + ((s2 ** 2 / n2) ** 2 / (n2 - 1))
    dfree = float(num / den) if den > 0 else float(n1 + n2 - 2)

    # 两个单侧检验
    t_lower = (diff + margin) / se      # H01：diff ≤ −Δ
    t_upper = (diff - margin) / se      # H02：diff ≥ +Δ
    p_lower = float(stats.t.sf(t_lower, dfree))
    p_upper = float(stats.t.cdf(t_upper, dfree))
    p_tost = max(p_lower, p_upper)

    # (1−2α) 置信区间：α=0.05 → 90% CI（TOST 的标准口径）
    tcrit = float(stats.t.ppf(1 - alpha, dfree))
    ci_low = diff - tcrit * se
    ci_high = diff + tcrit * se

    # 效应量（ pooled SD 版 Cohen's d，仅作参考）
    pooled = float(np.sqrt(((n1 - 1) * s1 ** 2 + (n2 - 1) * s2 ** 2)
                           / (n1 + n2 - 2)))
    d = diff / pooled if pooled > 0 else float("nan")

    equivalent = bool(p_tost < alpha)
    ci_pct = int(round((1 - 2 * alpha) * 100))
    significant_nhst = float(2 * stats.t.sf(abs(diff / se), dfree))

    def _f(x: float, k: int = 3) -> str:
        return "—" if x is None or not np.isfinite(x) else f"{x:.{k}f}"

    verdict = ("**可以认为两组等价**（差异小于等价边界）"
               if equivalent else
               "**尚不能认为两组等价**")
    md = [
        f"## TOST 等价性检验结果",
        "",
        f"**因变量**：{value_col}　　**分组变量**：{group_col}",
        f"**等价边界 Δ**：±{_f(margin)}　　**α**：{alpha:g}",
        "",
        "### 一、描述统计",
        "",
        "| 分组 | n | 均值 | 标准差 |",
        "| --- | ---: | ---: | ---: |",
        f"| {g1} | {n1} | {_f(m1)} | {_f(s1)} |",
        f"| {g2} | {n2} | {_f(m2)} | {_f(s2)} |",
        "",
        "### 二、两个单侧检验",
        "",
        "| 检验 | t | p |",
        "| --- | ---: | ---: |",
        f"| H₀₁：差值 ≤ −Δ（t = (d+Δ)/SE） | {_f(t_lower)} | {_f(p_lower)} |",
        f"| H₀₂：差值 ≥ +Δ（t = (d−Δ)/SE） | {_f(t_upper)} | {_f(p_upper)} |",
        "",
        f"- **TOST p = max(p₁, p₂) = {_f(p_tost)}**（Welch df = {_f(dfree, 1)}）",
        f"- **{ci_pct}% 置信区间**：[{_f(ci_low)}, {_f(ci_high)}]"
        f"（等价性检验用 1−2α 区间，α=0.05 时即 90% CI，不是笔误）",
        "",
        "### 三、结论",
        "",
        f"- 均值差（{g1} − {g2}）= {_f(diff)}，Cohen's d = {_f(d)}；"
        f"常规差异性检验 p = {_f(significant_nhst)}。",
        f"- {verdict}：TOST p = {_f(p_tost)} "
        f"{'< ' if equivalent else '≥ '}{alpha:g}。",
        "",
        "> 注意：**p > 0.05 不等于「两组没有差异」**。若要论证"无差异"，
        > 必须用本检验（或等价的置信区间法），并**事先说明等价边界 Δ 的依据**。",
    ]

    return {
        "method": "tost",
        "summary": {
            "n1": n1, "n2": n2, "mean1": m1, "mean2": m2,
            "sd1": s1, "sd2": s2, "diff": diff, "se": se,
            "df": dfree, "margin": margin, "alpha": alpha,
            "t_lower": t_lower, "t_upper": t_upper,
            "p_lower": p_lower, "p_upper": p_upper,
            "p": p_tost,
            "ci_level": 1 - 2 * alpha,
            "ci_low": ci_low, "ci_high": ci_high,
            "d": d if np.isfinite(d) else None,
            "p_nhst": significant_nhst,
            "equivalent": equivalent,
        },
        "markdown": "\n".join(md),
        "groups": {"g1": g1, "g2": g2},
    }
