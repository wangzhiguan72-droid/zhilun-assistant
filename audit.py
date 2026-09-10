"""
论文统计方法核查
================
输入：
  - paper_claims：extract_paper 输出的结构化 dict（methods / quantities / variables）
  - df：用户上传的数据（Pandas DataFrame）
  - columns：api/upload 返回的列概览（每列含 type / n_unique）
输出：
  - Markdown 报告，包含：
      ① 论文声称的方法识别结果
      ② 论文声称的统计量 vs 真实数据重跑出来的结果
      ③ 改进建议（基于规则模板）

每个生成函数都是纯函数，方便后续用 LLM 或新规则替换。

新增（v0.4）：
  - apply_user_directive(...)：根据用户自然语言指令（"只看 T 检验"、
    "只看 p<0.05"、"只看男组"……）过滤报告。
  - build_audit_report 新增可选参数 directive，传入后会被解析、过滤、并
    在 Markdown 顶部追加"应用指令"说明。
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


# -----------------------------------------------------------------------------
# 1) 把论文变量匹配到数据列
# -----------------------------------------------------------------------------
# 论文变量名 → 数据列名 常见同义词
# 兜底用：用户上传的列名如果包含同义词关键词，就匹配。
_VAR_SYNONYMS = {
    # 中文
    "性别": ["gender", "sex", "男", "女"],
    "年龄": ["age"],
    "年级": ["grade", "year", "class"],
    "专业": ["major", "subject"],
    "班级": ["class", "班级"],
    "学号": ["id", "no", "num", "编号"],
    "成绩": ["score", "grade", "mark", "result", "分数", "得分"],
    "分数": ["score", "grade", "mark", "分数", "得分"],
    "得分": ["score", "grade", "mark", "分数", "得分"],
    "学习时长": ["study_hours", "study_time", "hours", "时间"],
    "学习时间": ["study_hours", "study_time", "hours", "时间"],
    "焦虑": ["anxiety"],
    "抑郁": ["depression"],
    "压力": ["stress", "pressure"],
    "实验组": ["group", "treatment", "实验"],
    "对照组": ["group", "control", "对照"],
    "前测": ["pre", "before"],
    "后测": ["post", "after"],
    # 英文
    "gender": ["性别", "sex"],
    "age": ["年龄"],
    "score": ["成绩", "分数", "得分", "grade"],
    "grade": ["成绩", "分数", "得分"],
    "study_hours": ["学习时长", "学习时间", "时间", "hours"],
    "study_time": ["学习时长", "学习时间"],
    "group": ["组", "分组"],
}


def _match_var_to_column(var_name: str, columns: list[dict[str, Any]]) -> str | None:
    """用模糊匹配 + 同义词表把论文变量名落到数据列名上。"""
    if not var_name:
        return None
    col_names = [c["name"] for c in columns]
    var_lower = var_name.lower().strip()

    # 1) 精确
    if var_name in col_names:
        return var_name
    if var_lower in [c.lower() for c in col_names]:
        for c in col_names:
            if c.lower() == var_lower:
                return c

    # 2) 子串
    for cn in col_names:
        cn_low = cn.lower()
        if var_lower in cn_low or cn_low in var_lower:
            return cn

    # 3) 同义词表（双向）
    synonyms = _VAR_SYNONYMS.get(var_name, []) + _VAR_SYNONYMS.get(var_lower, [])
    for syn in synonyms:
        syn_low = syn.lower()
        for cn in col_names:
            cn_low = cn.lower()
            if syn_low == cn_low or syn_low in cn_low or cn_low in syn_low:
                return cn

    return None


def _auto_pick_group_value(paper_methods: list[dict], columns: list[dict],
                            matched_vars: list[dict] | None = None):
    """根据推荐 / 论文描述，挑一对 (group_col, value_col)。
    优先：
      - 论文里已匹配到的变量（matched_vars）
      - 否则：方法推荐
    排除：id 类列（学号/name/no/id 等），避免误用编号当因变量。
    """
    matched_vars = matched_vars or []
    matched_data_cols = {m["data_col"] for m in matched_vars if m.get("data_col")}

    def _is_id_like(name: str) -> bool:
        n = name.lower()
        return any(k in n for k in ("id", "no", "编号", "学号", "num", "序号", "name"))

    cont = [c for c in columns if c["type"] == "continuous" and not _is_id_like(c["name"])]
    # v1.1 修复：分类列同样要排除 id 类（否则 student_id 会被当成分组变量）
    cat = [c for c in columns if c["type"] == "categorical" and not _is_id_like(c["name"])]
    # v1.1：Likert 量表题（数值但取值少，被 type 归为 categorical）
    # 在信度分析里必须当题项，这里单独收集。
    numeric_items = [
        c["name"] for c in columns
        if c.get("is_numeric") and not _is_id_like(c["name"])
    ]

    method_keys = {m["method_key"] for m in paper_methods}
    # 优先用论文里匹配到的"连续列"做因变量、"分类列"做分组
    matched_cont = [c["name"] for c in cont if c["name"] in matched_data_cols]
    matched_cat = [c["name"] for c in cat if c["name"] in matched_data_cols]
    matched_items = [n for n in numeric_items if n in matched_data_cols]

    # v1.1 信度优先：论文若声称做了信度分析，题项 = 所有数值列
    # （含 Likert 量表题）。信度是"量表整体"分析，与 T 检验的单因变量诉求不冲突，
    # 故优先级最高；题项用 | 连接，供 _run_real_analysis 拆分。
    if "cronbach_alpha" in method_keys and len(numeric_items) >= 2:
        items = matched_items if len(matched_items) >= 2 else numeric_items
        return None, "|".join(items)

    if ("independent_t" in method_keys or "paired_t" in method_keys or "anova" in method_keys) \
            and cat and cont:
        group = (matched_cat[0] if matched_cat else sorted(cat, key=lambda c: c["n_unique"])[0]["name"])
        value = (matched_cont[0] if matched_cont else cont[0]["name"])
        return group, value
    if "correlation" in method_keys and len(cont) >= 2:
        pair = matched_cont[:2] if len(matched_cont) >= 2 else [c["name"] for c in cont[:2]]
        return None, "|".join(pair)
    if "chi_square" in method_keys and len(cat) >= 2:
        a = matched_cat[0] if matched_cat else cat[0]["name"]
        b = matched_cat[1] if len(matched_cat) >= 2 else cat[1]["name"]
        return a, b
    # v1.1 双因素：两个分类因素（group_col / value_col 位），因变量由 _run_real_analysis 挑
    if "two_way_anova" in method_keys and len(cat) >= 2:
        a = matched_cat[0] if matched_cat else cat[0]["name"]
        b = (matched_cat[1] if len(matched_cat) >= 2
             else cat[1]["name"])
        return a, b
    # v1.0 回归：因变量 = 连续列优先，自变量由 _run_real_analysis 从剩余连续列中挑
    if method_keys & {"linear_regression", "logistic_regression"} and cont:
        value = matched_cont[0] if matched_cont else cont[0]["name"]
        return None, value
    if cont and cat:
        cat_sorted = sorted(cat, key=lambda c: c["n_unique"])
        return cat_sorted[0]["name"], cont[0]["name"]
    if cont:
        return None, cont[0]["name"]
    if cat:
        return cat[0]["name"], None
    return None, None


# -----------------------------------------------------------------------------
# 2) 用数据真跑一遍 + 拿到对比用的统计量
# -----------------------------------------------------------------------------
def _run_real_analysis(df: pd.DataFrame, group_col: str | None, value_col: str | None,
                       method_key: str) -> dict[str, Any]:
    """根据方法 key 跑实际分析，返回 dict（含 t/p/F/χ²/r 等可对比字段）。"""
    out: dict[str, Any] = {"method_key": method_key, "ok": False, "error": None}

    try:
        if method_key in ("independent_t", "paired_t", "one_sample_t", "anova") \
                and group_col and value_col:
            sub = df[[group_col, value_col]].dropna()
            if not pd.api.types.is_numeric_dtype(sub[value_col]):
                raise ValueError(f"列【{value_col}】不是数值列。")
            groups = sub[group_col].astype(str).unique().tolist()
            out["n_groups"] = len(groups)
            out["group_col"] = group_col
            out["value_col"] = value_col

            if method_key == "independent_t" and len(groups) == 2:
                v1 = sub.loc[sub[group_col].astype(str) == groups[0], value_col].astype(float)
                v2 = sub.loc[sub[group_col].astype(str) == groups[1], value_col].astype(float)
                lev_stat, lev_p = stats.levene(v1, v2)
                t_stat, p_val = stats.ttest_ind(v1, v2, equal_var=bool(lev_p > 0.05))
                out.update({"ok": True, "t": float(t_stat), "p": float(p_val),
                            "df": len(v1) + len(v2) - 2,
                            "n1": int(len(v1)), "n2": int(len(v2)),
                            "mean1": float(v1.mean()), "mean2": float(v2.mean()),
                            "sd1": float(v1.std(ddof=1)), "sd2": float(v2.std(ddof=1))})
            elif method_key == "anova":
                samples = [sub.loc[sub[group_col].astype(str) == g, value_col].astype(float)
                           for g in groups]
                f_stat, p_val = stats.f_oneway(*samples)
                out.update({"ok": True, "F": float(f_stat), "p": float(p_val),
                            "df_between": len(groups) - 1,
                            "df_within": len(sub) - len(groups),
                            "group_means": [float(s.mean()) for s in samples],
                            "group_sizes": [int(len(s)) for s in samples]})
            else:
                # 配对 / 单样本 MVP 不展开，标个未实现
                out["error"] = f"方法 {method_key} 暂未实装具体计算。"
        elif method_key == "correlation":
            if not value_col or "|" not in value_col:
                raise ValueError("相关分析需要两个连续列。")
            a, b = value_col.split("|", 1)
            sub = df[[a, b]].dropna()
            if not (pd.api.types.is_numeric_dtype(sub[a])
                    and pd.api.types.is_numeric_dtype(sub[b])):
                raise ValueError("两列都必须为数值列。")
            r_val, p_val = stats.pearsonr(sub[a], sub[b])
            out.update({"ok": True, "r": float(r_val), "p": float(p_val),
                        "n": int(len(sub)),
                        "value_col": value_col})
        elif method_key == "chi_square":
            if not group_col or not value_col:
                raise ValueError("卡方检验需要两个分类列。")
            ct = pd.crosstab(df[group_col].astype(str), df[value_col].astype(str))
            chi2, p_val, dof, expected = stats.chi2_contingency(ct)
            out.update({"ok": True, "chi2": float(chi2), "p": float(p_val),
                        "df": int(dof),
                        "n": int(ct.values.sum()),
                        "value_col": value_col})
        elif method_key in ("linear_regression", "logistic_regression"):
            # v1.0 回归真跑：从 app.py 复用同一实现（import 避免代码重复）
            from app import run_linear_regression, run_logistic_regression
            # 自变量：所有数值列（排除因变量本身和 id 类列）
            _id_kws = ("id", "no", "编号", "学号", "num", "序号", "name")
            x_candidates = [c for c in df.columns if c != value_col
                            and pd.api.types.is_numeric_dtype(df[c])
                            and not any(k in c.lower() for k in _id_kws)]
            if not x_candidates:
                raise ValueError("无可用自变量列（需至少一个数值列）。")
            if method_key == "linear_regression":
                res = run_linear_regression(df, value_col, x_candidates)
                out.update({"ok": True, "r2": res["summary"]["r2"],
                            "n": res["summary"]["n"],
                            "coefficients": res["summary"]["coefficients"]})
            else:
                res = run_logistic_regression(df, value_col, x_candidates)
                out.update({"ok": True,
                            "pseudo_r2": res["summary"]["pseudo_r2"],
                            "n": res["summary"]["n"],
                            "accuracy": res["summary"]["accuracy"],
                            "coefficients": res["summary"]["coefficients"]})
        elif method_key == "cronbach_alpha":
            # v1.1 信度真跑：复用 app.py 实现。
            # value_col 由 _auto_pick_group_value 用 "|" 连接多个题项列。
            from app import run_cronbach_alpha
            if value_col and "|" in str(value_col):
                item_cols = [c.strip() for c in str(value_col).split("|") if c.strip()]
            elif value_col:
                item_cols = [value_col] + [
                    c for c in df.columns
                    if pd.api.types.is_numeric_dtype(df[c]) and c != value_col
                ]
            else:
                item_cols = [c for c in df.columns
                             if pd.api.types.is_numeric_dtype(df[c])]
            # 只保留真实存在的数值列（匹配可能失败）
            item_cols = [c for c in dict.fromkeys(item_cols)
                         if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
            if len(item_cols) < 2:
                raise ValueError("信度分析需要至少 2 个数值题项列。")
            res = run_cronbach_alpha(df, item_cols)
            out.update({"ok": True,
                        "alpha": res["summary"]["alpha"],
                        "k": res["summary"]["k"],
                        "n": res["summary"]["n"]})
        elif method_key == "two_way_anova":
            # v1.1 双因素真跑：因素 A = group_col，因素 B = value_col（沿用小语法的双列约定）
            from app import run_two_way_anova
            factor_a = group_col
            factor_b = value_col
            # 因变量：挑一个既不是 A 也不是 B 的连续列
            y_candidates = [
                c for c in df.columns
                if c not in (factor_a, factor_b)
                and pd.api.types.is_numeric_dtype(df[c])
                and not any(k in c.lower() for k in
                            ("id", "no", "编号", "学号", "num", "序号"))
            ]
            if not factor_a or not factor_b or not y_candidates:
                raise ValueError("双因素方差分析需要两个分类因素列和一个数值因变量列。")
            res = run_two_way_anova(df, factor_a, factor_b, y_candidates[0])
            s = res["summary"]
            out.update({"ok": True, "n": s["n"],
                        "f_a": s["f_a"], "f_b": s["f_b"], "f_ab": s["f_ab"],
                        "p_a": s["p_a"], "p_b": s["p_b"], "p_ab": s["p_ab"],
                        "eta2_a": s["eta2_a"], "eta2_b": s["eta2_b"],
                        "eta2_ab": s["eta2_ab"]})
        else:
            # v0.9.2：识别层认得回归/双因素/Spearman 等方法，但计算层未实装
            friendly = {
                "regression": "回归分析",
                "spearman": "Spearman 等级相关",
                "one_sample_t": "单样本 T 检验",
                "non_parametric": "非参数检验",
                "kruskal_wallis": "Kruskal-Wallis 检验",
                "repeated_measures_anova": "重复测量方差分析",
            }
            label = friendly.get(method_key, method_key)
            out["error"] = (
                f"论文使用「{label}」，该方法暂未实装自动核查"
                f"（在开发路线图上）。本次跳过重跑，但下方建议仍然有效。"
            )
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


# -----------------------------------------------------------------------------
# 3) 比对声称 vs 实际
# -----------------------------------------------------------------------------
def _compare_quantity(paper_q: dict, real: dict) -> dict[str, Any]:
    """单条声称统计量 vs 实际跑出来的同类型量。"""
    kind = paper_q["kind"]
    real_val = real.get(kind)
    paper_val = paper_q.get("value")

    if real_val is None or paper_val is None:
        return {"status": "unknown", "paper": paper_q["raw"],
                "real": "未跑出对应统计量", "diff": None}

    diff = abs(real_val - paper_val)
    # v1.0 回归统计量：R² / 伪 R² 容忍度 0.05；OR / β 容忍度 0.5
    if kind in ("r2", "pseudo_r2"):
        status = "ok" if diff < 0.05 else "minor_diff" if diff < 0.1 else "mismatch"
        return {"status": status, "kind": kind,
                "paper": paper_q["raw"], "real": f"{real_val:.3f}",
                "diff": f"{diff:.3f}"}
    if kind in ("beta", "or"):
        status = "ok" if diff < 0.5 else "minor_diff" if diff < 1.0 else "mismatch"
        return {"status": status, "kind": kind,
                "paper": paper_q["raw"], "real": f"{real_val:.3f}",
                "diff": f"{diff:.3f}"}
    # v1.1 信度 α：与 R² 同为 0~1 区间，容忍度 0.05
    if kind == "alpha":
        status = "ok" if diff < 0.05 else "minor_diff" if diff < 0.1 else "mismatch"
        return {"status": status, "kind": kind,
                "paper": paper_q["raw"], "real": f"{real_val:.3f}",
                "diff": f"{diff:.3f}"}
    # p 值容忍度：0.01 内的差视为一致（p 经常 < 0.001 → 0.000 四舍五入）
    if kind == "p":
        # 论文写 P < 0.05 的，p=0.041 / p=0.022 都算一致
        op = paper_q.get("op", "lt")
        same_side = (
            (op == "lt" and paper_val is not None and real_val <= paper_val + 1e-4)
            or (op == "eq" and diff < 0.05)
        )
        status = "ok" if same_side else "mismatch"
        return {"status": status, "kind": kind,
                "paper": paper_q["raw"], "real": f"{real_val:.4f}",
                "diff": None if status == "ok" else f"{diff:.4f}"}
    # t / F / r 等：差 0.5 之内视为一致（论文常四舍五入到 2 位）
    if diff < 0.5:
        status = "ok"
    elif diff < 1.0:
        status = "minor_diff"
    else:
        status = "mismatch"
    return {"status": status, "kind": kind,
            "paper": paper_q["raw"], "real": f"{real_val:.3f}",
            "diff": f"{diff:.3f}"}


# -----------------------------------------------------------------------------
# 4) 改进建议（规则化模板）
# -----------------------------------------------------------------------------
def _generate_suggestions(real: dict, paper_methods: list[dict],
                            paper_quantities: list[dict]) -> list[str]:
    """基于真实统计量 + 论文上下文，给出可操作的改进建议。
    每条建议形如：「建议 …」
    """
    out: list[str] = []

    # 4.0 无统计方法论文（v0.9.1）：给一条针对性说明就够，别堆无关建议
    if not paper_methods and not paper_quantities and not real.get("ok"):
        return [
            "论文未声明统计方法。若这是建模/仿真/控制/设计类研究（非假设检验范式），"
            "本工具的统计核查不适用——属于正常情况。",
            "若论文实际使用了统计方法但未被识别，请确认文中明确写出了方法全名"
            "（如「独立样本 T 检验」而非只写「检验」）。",
        ]

    # 4.1 样本量
    n = real.get("n1", 0) + real.get("n2", 0) or real.get("n", 0)
    if n and n < 30:
        out.append(
            f"样本量较小（N={n}），统计推断的稳定性有限。建议在论文局限/讨论部分说明样本量，"
            f"并考虑报告效应量（Cohen's d / η²）而非仅依赖 p 值。"
        )
    elif n and n < 100:
        out.append(
            f"样本量 N={n} 处于常见毕业论文规模，结论可参考，但仍建议报告效应量以增强说服力。"
        )

    # 4.2 p 值边缘
    for q in paper_quantities:
        if q["kind"] == "p" and q.get("value") is not None:
            v = q["value"]
            if 0.01 < v < 0.05:
                out.append(
                    f"论文中 p = {v:.3f} 处于边缘显著区间，建议同时报告精确 p 值与 95% 置信区间，"
                    f"并考虑使用重采样（bootstrap）做稳健性检验。"
                )

    # 4.3 T 检验 / 分组数不匹配
    method_keys = {m["method_key"] for m in paper_methods}
    if "independent_t" in method_keys and real.get("n_groups", 0) > 2:
        out.append(
            f"论文使用独立样本 T 检验，但【{real.get('group_col')}】实际有 "
            f"{real.get('n_groups')} 个分组。建议改用单因素方差分析（ANOVA）"
            f"或对分组两两比较并做 Bonferroni 校正。"
        )

    # 4.4 效应量
    has_d_in_paper = any(q["kind"] == "d" for q in paper_quantities)
    if "independent_t" in method_keys and real.get("ok") and not has_d_in_paper:
        n1, n2 = real.get("n1", 0), real.get("n2", 0)
        if n1 > 0 and n2 > 0:
            m1, m2 = real.get("mean1", 0), real.get("mean2", 0)
            s1, s2 = real.get("sd1", 0), real.get("sd2", 0)
            pooled = (((n1 - 1) * s1**2 + (n2 - 1) * s2**2) / (n1 + n2 - 2)) ** 0.5
            if pooled > 0:
                d = abs(m1 - m2) / pooled
                interp = (
                    "小效应" if d < 0.5 else
                    "中等效应" if d < 0.8 else "大效应"
                )
                out.append(
                    f"实际算得 Cohen's d ≈ {d:.2f}（{interp}），但论文未报告效应量。"
                    f"建议补充效应量与 95% CI，这比单纯报告 p 值更受审稿人青睐。"
                )

    # 4.5 ANOVA 后无事后检验
    if "anova" in method_keys and not any(m["method_key"] == "non_parametric"
                                            for m in paper_methods):
        out.append(
            "若 ANOVA 显著，建议补充事后多重比较（Post-hoc，如 Tukey HSD / Bonferroni），"
            "以明确是哪几组之间存在差异。"
        )

    # 4.6 正态性 / 方差齐性
    if "independent_t" in method_keys and real.get("ok"):
        out.append(
            "建议补充正态性检验（Shapiro-Wilk）与方差齐性检验（Levene）的结果；"
            "若不满足前提，应改用非参数方法（Mann-Whitney U）或 Welch 校正版本，并在文中注明。"
        )

    # 4.7 相关 / 因果
    if "correlation" in method_keys:
        out.append(
            "相关不等于因果。若仅做相关分析，论文讨论部分应避免因果表述，"
            "并考虑加入回归分析以控制混杂变量。"
        )

    # 4.8 卡方期望频数
    if "chi_square" in method_keys and real.get("ok"):
        out.append(
            "建议报告卡方检验的期望频数（Expected Count）。"
            "若任一期望频数 < 5，应改用 Fisher 精确检验。"
        )

    # 4.10 配对 T（v0.7 新增）
    if "paired_t" in method_keys and real.get("ok"):
        out.append(
            "配对 T 检验依赖「差值近似正态」的假设。建议补充 Shapiro-Wilk 检验结果；"
            "若不满足，应改用 Wilcoxon 符号秩检验作为非参数替代。"
        )
        out.append(
            "若配对数据存在大量「差值 = 0」（如前后测无变化），考虑剔除或改用 Wilcoxon。"
        )

    # 4.11 Mann-Whitney U（v0.7 新增）
    if "mann_whitney" in method_keys and real.get("ok"):
        out.append(
            "Mann-Whitney U 检验适用于不满足正态假设的场景；"
            "若数据确实近似正态，参数检验（T 检验）统计功效更高，可作为补充验证。"
        )

    # 4.12 Wilcoxon 符号秩（v0.7 新增）
    if "wilcoxon" in method_keys and real.get("ok"):
        out.append(
            "Wilcoxon 符号秩检验报告的「中位数差」与配对 T 检验的「均值差」语义不同，"
            "写作时注意区分。统计量用 W 或 Z，p 值双侧。"
        )

    # 4.13 回归分析（v1.0 新增）
    if method_keys & {"linear_regression", "logistic_regression", "regression"}:
        is_logistic = bool(method_keys & {"logistic_regression"})
        if is_logistic:
            out.append(
                "Logistic 回归要求因变量为二分类。请确认论文报告的因变量类别映射"
                "（如 1=患病/0=健康）与数据一致，且未出现完全分离（分离时 OR 会趋于无穷）。"
            )
            out.append(
                "建议报告 OR（比值比）及其 95% CI，并补充模型拟合指标"
                "（Hosmer-Lemeshow / AUC / 准确率）而非仅给 p 值。"
            )
        else:
            out.append(
                "线性回归依赖「线性、独立性、正态性、方差齐性」四项假设。"
                "建议补充残差图（Residual Plot）和 Durbin-Watson 自相关检验结果。"
            )
            out.append(
                "若自变量间存在多重共线性（VIF > 10），应剔除高相关变量或改用岭回归。"
            )
        out.append(
            "建议在论文中明确写出回归方程（含非标准化系数 B 和标准化系数 β），"
            "并报告调整 R² 而非仅报告 R²。"
        )

    # 4.14 信度分析（v1.1 新增）
    if "cronbach_alpha" in method_keys:
        out.append(
            "Cronbach's α 反映量表内部一致性，通常报告到小数点后 2 位。"
            "α ≥ 0.7 为可接受，0.8 以上为良好；但 α 过高（> 0.95）可能提示题目高度重复。"
        )
        out.append(
            "建议同时报告「删除某题后的 α」与「校正后题总相关（CITC）」，"
            "若有题目 CITC < 0.3 或删除后 α 明显提升，应说明取舍依据。"
        )
        out.append(
            "α 只证明内部一致性，不证明效度。若量表含多个维度，"
            "建议分维度报告 α，并补充结构效度（探索性 / 验证性因子分析）。"
        )
        if real.get("ok") and real.get("alpha") is not None:
            a = real["alpha"]
            if a < 0.7:
                out.append(
                    f"实测 α = {a:.3f} 低于 0.70 的常用阈值，量表信度不足，"
                    "建议增加题项、修订表述或重新检验维度结构。"
                )
            elif a > 0.95:
                out.append(
                    f"实测 α = {a:.3f} 偏高（> 0.95），可能存在题目语义重复，"
                    "建议精简题项以避免冗余。"
                )

    # 4.15 双因素方差分析（v1.1 新增）
    if "two_way_anova" in method_keys:
        out.append(
            "双因素 ANOVA 必须报告**三个效应**：因素 A 主效应、因素 B 主效应、"
            "以及 A×B 交互作用，不能只报告主效应。"
        )
        out.append(
            "建议在 ANOVA 表中给出每个效应的平方和 SS、自由度 df、均方 MS、F、p 与偏 η²，"
            "并在正文中说明交互作用的解释方式。"
        )
        if real.get("ok") and real.get("p_ab") is not None:
            if real["p_ab"] < 0.05:
                out.append(
                    f"实测交互作用显著（p = {real['p_ab']:.4f} < 0.05）："
                    "此时不应孤立解释主效应，必须做**简单效应分析**"
                    "（在 B 的各水平上分别检验 A 的效应），并绘制交互作用折线图。"
                )
            else:
                out.append(
                    f"实测交互作用不显著（p = {real['p_ab']:.4f}）："
                    "可主要解释两个因素的主效应，结论更简洁明确。"
                )
            if real.get("levene_p") is not None and real["levene_p"] < 0.05:
                out.append(
                    f"实测 Levene 方差齐性检验 p = {real['levene_p']:.4f} < 0.05，"
                    "单元格方差不齐，F 检验可能偏乐观，建议对因变量做变换或改用稳健方法。"
                )
        out.append(
            "若同一批被试在多水平 / 多时间点上重复测量，应改用重复测量 ANOVA"
            "（本工具后续版本支持），而非独立样本双因素 ANOVA。"
        )

    # 4.9 万能兜底
    if not out:
        out.append(
            "未发现明显问题。建议论文中明确报告样本量、检验方法、前提条件（正态 / 方差齐性），"
            "以及效应量与置信区间，而非只给一个 p 值。"
        )

    return out


# -----------------------------------------------------------------------------
# 5) 用户指令过滤（v0.4 新增）
# -----------------------------------------------------------------------------
# 用户的自然语言指令示例：
#   - "只看 T 检验"
#   - "只看 p<0.05 的"
#   - "只看显著的"
#   - "只看男组"
#   - "只看成绩"
#   - "只看 T 检验 且 p<0.05"
#
# 解析为三类过滤条件（method_keys / p_threshold / variable_keys），
# 然后分别对 methods / quantities / variables / comparisons 做交集过滤。

# 方法别名 → method_key
_METHOD_ALIASES = {
    "t检验": "independent_t", "t 检验": "independent_t", "t-检验": "independent_t",
    "t-test": "independent_t", "独立样本t": "independent_t", "独立样本t检验": "independent_t",
    "配对t": "paired_t", "配对样本t": "paired_t", "配对t检验": "paired_t", "配对样本t检验": "paired_t",
    "单样本t": "one_sample_t", "单样本t检验": "one_sample_t",
    "anova": "anova", "方差分析": "anova",
    "相关": "correlation", "pearson": "correlation", "相关性": "correlation", "相关分析": "correlation",
    "卡方": "chi_square", "χ²": "chi_square", "chi-square": "chi_square", "卡方检验": "chi_square",
    "mann": "mann_whitney", "mann-whitney": "mann_whitney", "mann_whitney": "mann_whitney",
    "曼惠特尼": "mann_whitney", "mw": "mann_whitney",
    "wilcoxon": "wilcoxon", "符号秩": "wilcoxon", "威尔科克森": "wilcoxon",
    "kruskal": "kruskal_wallis", "kruskal-wallis": "kruskal_wallis", "克鲁斯卡尔": "kruskal_wallis",
    # v1.0 回归
    "线性回归": "linear_regression", "多元回归": "linear_regression",
    "logistic": "logistic_regression", "逻辑回归": "logistic_regression",
    "logit": "logistic_regression", "二元逻辑": "logistic_regression",
    # v1.1 信度
    "信度": "cronbach_alpha", "cronbach": "cronbach_alpha", "α": "cronbach_alpha",
    "alpha": "cronbach_alpha", "克隆巴赫": "cronbach_alpha", "内部一致性": "cronbach_alpha",
    # v1.1 双因素
    "双因素": "two_way_anova", "two-way": "two_way_anova",
}

# 变量别名 → 关键词集合（任一命中即视为匹配）
_VAR_FILTER_ALIASES = {
    "男": ["男", "male", "men", "男生"],
    "女": ["女", "female", "women", "女生"],
    "性别": ["男", "女", "gender", "sex", "性别"],
    "gender": ["gender", "性别", "sex"],
    "成绩": ["成绩", "score", "分数", "得分", "grade", "mark"],
    "学习时长": ["学习时长", "学习时间", "study_hours", "study_time", "hours", "时长"],
    "年龄": ["年龄", "age"],
}


def _parse_directive(directive: str) -> dict[str, Any]:
    """把用户的指令文本解析成三类过滤条件。

    返回 dict 包含：
      method_keys:    set[str] | None   要保留的方法 key
      p_threshold:    (op, val) | None  (op 是 'lt' / 'gt')
      variable_keys:  set[str] | None   要保留的变量关键词集合
      raw_tokens:     list[str]         解析出来的原始 token（调试用）
      matched_summary:list[str]         对已识别过滤条件的简短描述
    """
    out: dict[str, Any] = {
        "method_keys": None, "p_threshold": None, "variable_keys": None,
        "raw_tokens": [], "matched_summary": [],
    }
    if not directive or not directive.strip():
        return out

    text = directive.strip()
    # 用空格 / 逗号 / 分号 / 且 / 和 / 与 / 或者 / 或者 拆 token
    raw_tokens = [t for t in re.split(
        r"[，,；;\s]+|且|和|与|或者|或", text) if t]
    out["raw_tokens"] = raw_tokens

    text_low = text.lower()

    # ---- 方法 ----
    method_keys: set[str] = set()
    for alias, key in _METHOD_ALIASES.items():
        if alias.lower() in text_low:
            method_keys.add(key)
    if method_keys:
        out["method_keys"] = sorted(method_keys)   # JSON-friendly
        out["matched_summary"].append(
            f"方法 ∈ {{{', '.join(sorted(method_keys))}}}")

    # ---- 显著性 ----
    # 优先匹配形如 p < 0.05 / p > 0.05 / p ≤ 0.01 等
    m = re.search(r"p\s*([<>≤≥]=?)\s*(0?\.\d+|\d+(?:\.\d+)?)", text_low)
    if m:
        op_char, num_str = m.group(1), m.group(2)
        try:
            val = float(num_str)
        except ValueError:
            val = 0.05
        op = "lt" if op_char in ("<", "≤") else "gt"
        out["p_threshold"] = (op, val)
        out["matched_summary"].append(
            f"p {'<' if op == 'lt' else '>'} {val}")
    elif "不显著" in text or "不显著的" in text:
        out["p_threshold"] = ("gt", 0.05)
        out["matched_summary"].append("p > 0.05（不显著）")
    elif "显著" in text:
        # "只看显著的" 默认为 p < 0.05
        out["p_threshold"] = ("lt", 0.05)
        out["matched_summary"].append("p < 0.05（显著）")

    # ---- 变量 ----
    var_keys: set[str] = set()
    for alias, keys in _VAR_FILTER_ALIASES.items():
        if alias.lower() in text_low:
            var_keys.update(keys)
    if var_keys:
        out["variable_keys"] = sorted(var_keys)  # JSON-friendly
        out["matched_summary"].append(
            f"变量含 {{{', '.join(sorted(var_keys))}}}")

    return out


def apply_user_directive(paper_claims: dict[str, Any], real: dict[str, Any],
                          comparisons: list[dict[str, Any]],
                          suggestions: list[str],
                          columns: list[dict[str, Any]],
                          directive: str) -> dict[str, Any]:
    """根据用户指令过滤报告的各组件（methods / quantities / variables / comparisons）。

    输入：
      paper_claims: {methods, quantities, variables}
      real:        真实数据重跑结果（用于根据真实 p 值决定 comparisons 是否清空）
      comparisons: 声称 vs 实际的对比列表
      suggestions: 改进建议（不过滤，全量保留）
      columns:     数据列概览
      directive:   用户指令字符串（自然语言）

    返回 dict：
      methods, quantities, variables, comparisons, suggestions,
      parsed, applied_directive
    """
    parsed = _parse_directive(directive)
    methods = paper_claims.get("methods", []) or []
    quantities = paper_claims.get("quantities", []) or []
    variables = paper_claims.get("variables", []) or []

    method_keys = parsed["method_keys"]
    p_th = parsed["p_threshold"]
    var_keys = parsed["variable_keys"]

    # ---- 过滤 methods ----
    if method_keys:
        methods = [m for m in methods if m.get("method_key") in method_keys]

    # ---- 过滤 quantities ----
    if p_th is not None:
        op, val = p_th

        def _p_pass(q: dict[str, Any]) -> bool:
            # p_threshold 只对 kind == "p" 的声称生效；其他类型的统计量
            # （r / t / F / χ² / d）默认通过。
            if q.get("kind") != "p":
                return True
            v = q.get("value")
            if v is None:
                return False
            op_q = q.get("op", "unknown")  # 论文声称的写法（lt / eq / unknown）
            # 用户指令阈值 X：op = lt（"只看 p<X"） / gt（"只看 p>X"）
            if op_q == "lt":
                # 论文写 "P < Xq"，声称上限是 Xq
                # 满足"用户要看 p<X" ⟺ 声称上限 ≤ 用户阈值
                # 满足"用户要看 p>X" ⟺ 声称上限 > 用户阈值
                return (v <= val) if op == "lt" else (v > val)
            # 论文写 "P = Xq"（eq）或无法判断 → 按数值精确判断
            return (v < val) if op == "lt" else (v > val)

        quantities = [q for q in quantities if _p_pass(q)]

    # ---- 过滤 variables ----
    if var_keys:
        def _v_pass(v: dict[str, Any]) -> bool:
            name = (v.get("name") or "").lower()
            return any(k.lower() in name for k in var_keys)

        variables = [v for v in variables if _v_pass(v)]

    # ---- 过滤 comparisons ----
    # 规则：如果真实数据重跑成功了，且 p_threshold 过滤了真实 p 值 → 对比表清空
    cmps = comparisons
    if p_th is not None and real.get("ok") and real.get("p") is not None:
        op, val = p_th
        real_pass = (real["p"] < val) if op == "lt" else (real["p"] > val)
        if not real_pass:
            cmps = []  # 实际不满足阈值，声称对比整段没意义

    # ---- suggestions 不过滤 ----
    return {
        "methods": methods,
        "quantities": quantities,
        "variables": variables,
        "comparisons": cmps,
        "suggestions": suggestions,
        "parsed": parsed,
        "applied_directive": directive.strip(),
    }


# -----------------------------------------------------------------------------
# 6) 主入口：生成 Markdown 报告
# -----------------------------------------------------------------------------
def build_audit_report(paper_claims: dict[str, Any], df: pd.DataFrame,
                        columns: list[dict[str, Any]],
                        directive: str = "") -> dict[str, Any]:
    """paper_claims 期望包含 keys: methods, quantities, variables, raw_text（可选）

    directive（v0.4 新增）：用户输入的过滤指令字符串，例如
      "只看 T 检验 且 p<0.05" / "只看男组" / ""（不过滤）
    传入后报告会按指令过滤；空串等价于不过滤。
    """
    methods = paper_claims.get("methods", [])
    quantities = paper_claims.get("quantities", [])
    variables = paper_claims.get("variables", [])

    # 1) 匹配变量
    matched: list[dict[str, Any]] = []
    for v in variables:
        col = _match_var_to_column(v["name"], columns)
        matched.append({"paper_var": v["name"], "data_col": col,
                        "sources": v["sources"]})
    # v0.9.2 内测反馈：匹配表只显示前 20 行——把"匹配到列"的排最前，
    # 噪音变量（after_keyword 未匹配的）自然被挤出展示区
    matched.sort(key=lambda m: (m["data_col"] is None,))

    # 2) 自动挑一对 (group, value)
    # v0.9.1 内测反馈：论文没识别到任何方法且没统计量（典型工科论文）时，
    # 不应"自作主张"跑一个 T 检验——那是报告噪音。跳过重跑，第四节给提示。
    no_stats_claimed = not methods and not quantities
    if no_stats_claimed:
        method_key = ""
        group_col = value_col = None
        real = {"ok": False, "error": "论文未声明统计方法，跳过重跑。"}
    else:
        # v1.1：方法选择不再简单取 methods[0]，而是优先挑"能真跑且统计量可对比"的方法。
        # 背景：一篇论文常同时声称多种方法（如信度 + T 检验），methods[0] 只是
        # 文本中最早出现的那句，未必是核查价值最高的。这里给出显式优先级。
        _METHOD_PRIORITY = [
            "cronbach_alpha",        # 信度 = 量表整体，独立可跑，优先
            "linear_regression",
            "logistic_regression",
            "two_way_anova",
            "independent_t",
            "anova",
            "paired_t",
            "correlation",
            "chi_square",
            "mann_whitney",
            "wilcoxon",
        ]
        claimed_keys = [m["method_key"] for m in methods]
        method_key = next(
            (k for k in _METHOD_PRIORITY if k in claimed_keys),
            claimed_keys[0] if claimed_keys else "independent_t",
        )
        group_col, value_col = _auto_pick_group_value(methods, columns, matched)
        real = _run_real_analysis(df, group_col, value_col, method_key)

    # 3) 比对每个统计量
    comparisons: list[dict[str, Any]] = []
    if real.get("ok"):
        for q in quantities:
            comparisons.append(_compare_quantity(q, real))
    else:
        comparisons = [{"status": "no_real", "reason": real.get("error")}]

    # 4) 改进建议
    suggestions = _generate_suggestions(real, methods, quantities)

    # 4.5 应用用户指令过滤（v0.4）
    applied_directive = directive.strip()
    parsed_directive: dict[str, Any] = {}
    if applied_directive:
        filtered = apply_user_directive(
            {"methods": methods, "quantities": quantities, "variables": variables},
            real, comparisons, suggestions, columns, applied_directive,
        )
        methods = filtered["methods"]
        quantities = filtered["quantities"]
        variables = filtered["variables"]
        comparisons = filtered["comparisons"]
        parsed_directive = filtered["parsed"]

    # 同步过滤 matched（变量匹配结果要跟过滤后的 variables 对齐）
    if applied_directive:
        var_names_kept = {v["name"] for v in variables}
        matched = [m for m in matched if m["paper_var"] in var_names_kept]

    # 5) 渲染 Markdown
    md_lines: list[str] = []
    md_lines.append("## 📋 论文统计方法核查报告\n")

    # 5.0 应用指令说明（如有）
    if applied_directive and parsed_directive.get("matched_summary"):
        summary = " · ".join(parsed_directive["matched_summary"])
        md_lines.append(
            f"\n> 🎯 **已应用指令**：`{applied_directive}`\n"
            f"> 过滤条件：{summary}\n"
            f"> （未匹配的条件将被忽略；建议列表保持完整。）\n"
        )
    elif applied_directive:
        md_lines.append(
            f"\n> 🎯 **已应用指令**：`{applied_directive}`"
            f"\n> ⚠️ 未从指令中识别到有效过滤条件（方法/显著性/变量）。"
            f"试试：\"只看 T 检验\" / \"只看 p<0.05\" / \"只看男组\"。\n"
        )

    # 5.1 论文声称的方法
    md_lines.append("### 一、论文中识别的统计方法\n")
    if methods:
        md_lines.append("| 序号 | 方法 | 出现位置（上下文摘要） |")
        md_lines.append("| ---: | --- | --- |")
        for i, m in enumerate(methods, 1):
            ctx = m["context"][:60].replace("|", "｜").replace("\n", " ")
            md_lines.append(f"| {i} | {m['method_label']} | …{ctx}… |")
    else:
        md_lines.append("⚠️ 未在论文中识别到明确的统计方法关键词。"
                        "请检查论文是否明确写出了方法名（如「独立样本 T 检验」「ANOVA」等）。")

    # 5.2 论文声称的统计量
    md_lines.append("\n### 二、论文中识别的统计量\n")
    if quantities:
        md_lines.append("| 类型 | 论文写法 | 上下文摘要 |")
        md_lines.append("| --- | --- | --- |")
        for q in quantities[:20]:  # 不超过 20 条
            ctx = q["context"][:50].replace("|", "｜").replace("\n", " ")
            # v0.9.4：去重后同写法合并，显示出现次数
            cnt = q.get("count", 1)
            raw = q["raw"] + (f"（×{cnt}）" if cnt > 1 else "")
            md_lines.append(f"| {q['kind']} | {raw} | …{ctx}… |")
    else:
        md_lines.append("未识别到任何具体统计量（P/t/F/χ²/r 等）。")

    # 5.3 论文变量 vs 数据列
    md_lines.append("\n### 三、论文变量 ↔ 数据列 匹配\n")
    n_hit = sum(1 for m in matched if m["data_col"])
    if n_hit:
        md_lines.append("| 论文中提到的变量 | 匹配到的数据列 | 命中方式 |")
        md_lines.append("| --- | --- | --- |")
        for m in matched[:20]:
            data_col = m["data_col"] or "❌ 未匹配"
            src = "、".join(set(m["sources"]))
            md_lines.append(f"| {m['paper_var']} | {data_col} | {src} |")
        if len(matched) > 20:
            md_lines.append(f"\n（共识别 {len(matched)} 个候选变量，仅展示匹配优先的前 20 个。）")
    elif matched:
        # v0.9.2 内测反馈：论文变量与数据列完全无交集时，20 行全 ❌ 的
        # 噪音表没有信息量——直接提示更实用
        md_lines.append(
            f"⚠️ 论文中识别到 {len(matched)} 个候选变量，但与数据列**无一匹配**。\n\n"
            "常见原因：\n"
            "- 上传的数据不是这篇论文对应的数据（比如拿了示例数据来测试）；\n"
            "- 论文变量名与数据列名差异太大（可检查列名后重新上传）；\n"
            "- 或论文本身不含统计变量（建模/案例/综述类研究）。"
        )
    else:
        md_lines.append("未在论文中识别到变量名。")

    # 5.4 用真实数据重跑 + 对比
    md_lines.append("\n### 四、用您的数据重跑一遍\n")
    if no_stats_claimed:
        md_lines.append(
            "⚠️ 论文未声明任何统计方法，无法进行「重跑对比」。\n\n"
            "可能的原因：\n"
            "- 这是一篇**非统计类论文**（如建模、仿真、控制、设计类研究），"
            "本身不涉及假设检验——这是正常的，无需核查；\n"
            "- 或论文中未明确写出方法名（如「独立样本 T 检验」「ANOVA」），"
            "可补充方法描述后重新上传。\n"
        )
    elif real.get("ok"):
        # v1.0 回归：展示方式与分组检验不同
        if method_key in ("linear_regression", "logistic_regression"):
            md_lines.append(f"采用 **{method_key}** 方法，因变量=`{value_col}`：\n")
            md_lines.append("| 指标 | 真实数据结果 |")
            md_lines.append("| --- | ---: |")
            for k, v in real.items():
                if k in ("ok", "method_key", "group_col", "value_col", "error",
                         "coefficients"):
                    continue
                if isinstance(v, float):
                    md_lines.append(f"| {k} | {v:.4f} |")
                else:
                    md_lines.append(f"| {k} | {v} |")
            # 回归系数表
            coefs = real.get("coefficients")
            if coefs:
                md_lines.append("\n**回归系数：**\n")
                md_lines.append("| 变量 | β | p |")
                md_lines.append("| --- | ---: | ---: |")
                for c in coefs:
                    md_lines.append(f"| {c.get('name', '-')} | {c.get('beta', 0):.3f} "
                                    f"| {c.get('p', 1):.4f} |")
        else:
            md_lines.append(f"采用 **{method_key}** 方法，group=`{group_col}`、value=`{value_col}`：\n")
            md_lines.append("| 指标 | 真实数据结果 |")
            md_lines.append("| --- | ---: |")
            for k, v in real.items():
                if k in ("ok", "method_key", "group_col", "value_col", "error", "group_means",
                          "group_sizes"):
                    continue
                if isinstance(v, float):
                    md_lines.append(f"| {k} | {v:.4f} |")
                else:
                    md_lines.append(f"| {k} | {v} |")
    else:
        err = real.get("error", "未知原因")
        # v0.9.2：友好文案（未实装方法）已自含完整说明，不再拼接"可能原因"后缀
        if "暂未实装自动核查" in err:
            md_lines.append(f"⚠️ {err}")
        else:
            md_lines.append(f"⚠️ 用您的数据重跑失败：{err}。"
                            f"可能原因：列不匹配、方法未实装、或前提条件不满足。")

    # 5.5 声称 vs 实际 对比
    md_lines.append("\n### 五、声称值 vs 实际值 对比\n")
    if any(c.get("status") not in ("no_real",) for c in comparisons):
        md_lines.append("| 统计量 | 论文写法 | 真实数据 | 差异 | 状态 |")
        md_lines.append("| --- | --- | --- | --- | --- |")
        for c in comparisons:
            if c.get("status") == "no_real":
                continue
            status_label = {"ok": "✅ 一致", "minor_diff": "🟡 小差异", "mismatch": "🔴 不一致",
                            "unknown": "⚪ 无法对比"}.get(c["status"], c["status"])
            md_lines.append(f"| {c.get('kind', '-')} | {c.get('paper', '-')} "
                            f"| {c.get('real', '-')} | {c.get('diff', '-')} "
                            f"| {status_label} |")
    else:
        md_lines.append("没有可对比的统计量（论文未给出具体数值，或实际跑失败）。")

    # 5.6 改进建议
    md_lines.append("\n### 六、改进建议（按优先级）\n")
    for i, s in enumerate(suggestions, 1):
        md_lines.append(f"{i}. {s}")

    # 5.7 免责声明 + 学术承诺（v0.5.1）
    md_lines.append(
        "\n---\n"
        "### ⚠️ 免责声明\n"
        "本工具仅提供统计方法核查建议，不构成学术指导或法律意见。"
        "用户需自行判断结果适用性，并对论文最终质量负责。\n"
        "\n"
        "### 📚 学术诚信承诺\n"
        "使用本工具进行论文核查时，请遵守学术规范：\n"
        "1. 仅用于自我检查和改进，不替代导师指导\n"
        "2. 确保数据真实性和方法正确性\n"
        "3. 如需引用工具结果，请注明来源\n"
        "4. 不得用于任何形式的学术不端行为\n"
        "\n"
        "本工具数据仅在内存中处理，不会保存或上传到外部服务器。"
    )

    return {
        "real": real,
        "matched_vars": matched,
        "comparisons": comparisons,
        "suggestions": suggestions,
        "markdown": "\n".join(md_lines),
        "applied_directive": applied_directive,
        "parsed_directive": parsed_directive,
    }