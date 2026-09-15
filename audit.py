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

    # v1.1 重复测量优先于配对 T：论文若声称"重复测量/被试内/多时间点"，
    # 需要 ≥3 个数值列做时间点（配对 T 只吃 2 列，不足以表达该设计）。
    # 优先用论文匹配到的数值列，其余按列顺序补齐；用 | 连接供下游拆分。
    if "repeated_measures_anova" in method_keys and len(numeric_items) >= 3:
        ordered = [n for n in matched_items if n in numeric_items]
        rest = [n for n in numeric_items if n not in ordered]
        cols = (ordered + rest)[:12]          # 上限 12 个时间点，避免过宽
        if len(cols) >= 3:
            return None, "|".join(cols)

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
        elif method_key == "repeated_measures_anova":
            # v1.1 重复测量真跑：时间点列由 _auto_pick_group_value 用 | 打包在 value_col
            from app import run_repeated_measures_anova
            time_cols = [c for c in (value_col or "").split("|") if c]
            time_cols = [c for c in time_cols if c in df.columns]
            if len(time_cols) < 3:
                raise ValueError("重复测量方差分析需要至少 3 个时间点列（数值列）。")
            res = run_repeated_measures_anova(df, time_cols)
            s = res["summary"]
            out.update({"ok": True, "n": s["n"], "k": s["k"],
                        "f": s["f"], "p": s["p"], "eta2": s["eta2"],
                        "gg_epsilon": s["gg_epsilon"],
                        "mauchly_p": s["mauchly_p"],
                        "sphericity_ok": s["sphericity_ok"]})
        else:
            # v0.9.2：识别层认得回归/双因素/Spearman 等方法，但计算层未实装
            friendly = {
                "regression": "回归分析",
                "spearman": "Spearman 等级相关",
                "one_sample_t": "单样本 T 检验",
                "non_parametric": "非参数检验",
                "kruskal_wallis": "Kruskal-Wallis 检验",
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
# ===========================================================================
# GRIM 交叉核查（v2.1 · 论文侧数据取证）
# ===========================================================================
# 一句话：论文写「均值 = 3.47，样本 30 人」，而问卷是整数计分 ——
# 那么 30 × 3.47 = 104.1，不可能是任何 30 个整数之和。GRIM 检验就查这件事。
#
# 与 `datacheck` 的分工：
#   datacheck 查「数据文件内部」的矛盾；这里查「论文声称值 × 样本量」的矛盾。
#   两边复用同一个纯函数 `datacheck.grim_check`，保证口径一致。
#
# 红线（与 datacheck 同源）：只说「该均值在给定样本量下不可能出现」，
# **绝不推论造假** —— 可能是四舍五入、加权、剔除缺失，或 n 指的是别的口径。


# ---------------------------------------------------------------------------
# v2.11 · P3 论文表格数字 vs 原始数据交叉核查
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_MSD_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*[±]\s*(\d+(?:\.\d+)?)$")
_TABLE_MAX_TABLES = 3   # 每份论文最多核对 3 张表（防大论文刷屏）
_TABLE_MAX_ROWS = 6     # 每张表最多核对 6 行


def _parse_table_row(cells: list[str]) -> dict[str, Any] | None:
    """把表格数据行解析成 {label, n, mean, sd}（解析不出返回 None）。

    认得的行形（label 之外的单元格）：
        n | M | SD   → 3 个数：整数 n + 两个小数
        n | M        → 2 个数：整数 n + 一个小数
        M | SD       → 2 个数：两个小数
        M±SD         → 1 个「M±SD」合并格
    """
    label: str | None = None
    nums_raw: list[str] = []
    for c in cells:
        t = (c or "").strip()
        if not t:
            continue
        m = _MSD_RE.match(t)
        if m:
            nums_raw.extend([m.group(1), m.group(2)])
            continue
        if _NUM_RE.match(t):
            nums_raw.append(t)
        elif label is None and len(t) <= 20 and not t.startswith(("表", "Table", "注")):
            label = t
    if label is None or len(nums_raw) < 2:
        return None
    try:
        vals = [float(x) for x in nums_raw]
    except ValueError:
        return None

    def _is_n(v: float, raw: str) -> bool:
        return "." not in raw and 1 <= v <= 1000

    rec: dict[str, Any] = {"label": label, "n": None, "mean": None, "sd": None}
    if len(vals) == 2 and not _is_n(vals[0], nums_raw[0]):
        rec["mean"], rec["sd"] = vals[0], vals[1]
    else:
        idx = 0
        if idx < len(vals) and _is_n(vals[idx], nums_raw[idx]):
            rec["n"] = int(vals[idx])
            idx += 1
        if idx < len(vals):
            rec["mean"] = vals[idx]
            idx += 1
        if idx < len(vals):
            rec["sd"] = vals[idx]
    if rec["mean"] is None and rec["n"] is None:
        return None
    return rec


def _find_group_column(labels: set[str], df: pd.DataFrame,
                       exclude: str | None = None) -> str | None:
    """找「唯一值集合与表格组标签完全一致」的列——**唯一候选才返回**。

    这是表格交叉核查的核心误报防线：两列都可能匹配时不猜（返回 None）。
    """
    if not labels:
        return None
    hits: list[str] = []
    for col in df.columns:
        if not isinstance(col, str) or col == exclude:
            continue
        s = df[col].dropna()
        if s.nunique() != len(labels) or s.nunique() > 10:
            continue
        vals = {str(v).strip() for v in s.unique()}
        if vals == labels:
            hits.append(col)
    return hits[0] if len(hits) == 1 else None


def compare_table_stats(tables: list[list[list[str]]], df: pd.DataFrame,
                        value_col: str | None = None) -> dict[str, Any]:
    """把论文 docx 里的描述统计表和原始数据逐格核对（v2.11 · P3）。

    返回：
        mismatches: 不一致条目（进 comparisons / 建议，结构与 _compare_quantity 兼容）
        notes:      过程说明（核对了几张表、哪些没核成——透明但安静）
        checked:    实际比对的格数（n / mean / sd 各算一格）
    核对口径：
        n    → 分组计数，必须相等（硬指标，零歧义）
        mean → 分组均值，|差| ≤ max(0.011, 0.5%×论文值) 视为舍入一致
        sd   → 分组标准差，同上
    **红线**：分组标签对不到唯一数据列的表只写 note，绝不硬猜硬报。
    """
    mismatches: list[dict[str, Any]] = []
    notes: list[str] = []
    checked = 0

    for ti, rows in enumerate((tables or [])[:_TABLE_MAX_TABLES], 1):
        parsed: list[dict[str, Any]] = []
        for cells in rows[1:]:  # 第一行当表头跳过
            rec = _parse_table_row(cells)
            if rec is not None:
                parsed.append(rec)
            if len(parsed) >= _TABLE_MAX_ROWS:
                break
        if len(parsed) < 2:
            continue
        labels = {r["label"] for r in parsed}
        gcol = _find_group_column(labels, df, exclude=value_col)
        if gcol is None:
            notes.append(f"第 {ti} 张表的分组（{'、'.join(sorted(labels))}）"
                         "没有唯一对应的数据列，未核对。")
            continue

        for r in parsed:
            mask = df[gcol].astype(str).str.strip() == r["label"]
            sub = df.loc[mask]
            if sub.empty:
                continue
            if r["n"] is not None:
                checked += 1
                real_n = int(len(sub))
                if real_n != r["n"]:
                    mismatches.append({
                        "status": "mismatch", "kind": "table_n",
                        "paper": f"表格「{r['label']}」行 n={r['n']}",
                        "real": f"按列「{gcol}」实算 n={real_n}",
                        "diff": str(r["n"] - real_n),
                    })
            if value_col and value_col in df.columns:
                nums = pd.to_numeric(sub[value_col], errors="coerce").dropna()
                if nums.empty:
                    continue
                if r["mean"] is not None:
                    checked += 1
                    real_m = float(nums.mean())
                    tol = max(0.011, abs(r["mean"]) * 0.005)
                    if abs(real_m - r["mean"]) > tol:
                        mismatches.append({
                            "status": "mismatch", "kind": "table_mean",
                            "paper": (f"表格「{r['label']}」行 M={r['mean']:g}"
                                      f"（列「{value_col}」）"),
                            "real": f"实算 M={real_m:.3f}",
                            "diff": f"{abs(real_m - r['mean']):.3f}",
                        })
                if r["sd"] is not None:
                    checked += 1
                    real_sd = float(nums.std())
                    tol = max(0.011, abs(r["sd"]) * 0.005)
                    if abs(real_sd - r["sd"]) > tol:
                        mismatches.append({
                            "status": "mismatch", "kind": "table_sd",
                            "paper": (f"表格「{r['label']}」行 SD={r['sd']:g}"
                                      f"（列「{value_col}」）"),
                            "real": f"实算 SD={real_sd:.3f}",
                            "diff": f"{abs(real_sd - r['sd']):.3f}",
                        })
        notes.append(f"第 {ti} 张表按列「{gcol}」分组核对完成。")

    return {"mismatches": mismatches, "notes": notes, "checked": checked}


def grim_cross_check(paper_quantities: list[dict], n: int, *,
                     items: int = 1, decimals: int = 2) -> list[dict[str, Any]]:
    """对论文里声称的每个「均值」做 GRIM 检验。

    入参：
        paper_quantities: `extract_paper.extract_quantities` 的输出
        n:                真实样本量（**由数据算出，不采信论文写的 n** ——
                           论文写的 n 本身可能就是错的，用实算值更硬）
    返回：
        [{"kind": "grim", "mean": float, "n": int, "passed": bool,
          "raw": str, "product": float, "explain": str}, ...]
    """
    from datacheck import grim_check  # 延迟导入：与 datacheck 共用同一口径

    out: list[dict[str, Any]] = []
    try:
        n_int = int(n)
    except (TypeError, ValueError):
        return out
    if n_int <= 0:
        return out

    for q in paper_quantities or []:
        if q.get("kind") != "mean":
            continue
        try:
            mean = float(q.get("value"))
        except (TypeError, ValueError):
            continue
        passed = bool(grim_check(mean, n_int, items=items, decimals=decimals))
        prod = mean * n_int
        if passed:
            explain = (f"n={n_int} 时，{mean:g} × {n_int} = {prod:g}，"
                       f"与整数计分一致 —— 该均值**可能**出现。")
        else:
            explain = (f"n={n_int} 时，{mean:g} × {n_int} = {prod:g}，不是整数 —— "
                       f"若每个得分都是整数，这个均值**不可能**由 {n_int} 个观测得到。")
        out.append({
            "kind": "grim", "mean": mean, "n": n_int, "passed": passed,
            "raw": str(q.get("raw", "")), "product": round(prod, 6),
            "explain": explain,
        })
    return out


def grimmer_cross_check_reported(paper_quantities: list[dict], n: int,
                                 *, items: int = 1,
                                 value_range: tuple[int, int] | None = None
                                 ) -> list[dict[str, Any]]:
    """对论文里声称的 (均值, 标准差, 样本量) 三元组做 GRIMMER 检验。

    与 `grim_cross_check` 的区别：GRIM 只查均值，GRIMMER 进一步查**标准差**
    —— 即使均值可能，SD 也可能落在整数数据不可达的区间里。

    做法：把同一条上下文里出现的 `mean` 与 `sd` 配对（论文通常写成
    "M = 3.47, SD = 0.52"），配合**实算样本量**送 `grimmer.grimmer_check`。

    入参：
        paper_quantities: `extract_paper.extract_quantities` 的输出
        n:                真实样本量（实算，不采信论文写的 n）
        value_range:      单题取值域 (lo, hi)，如 Likert 量表传 (1, 5)。
                          **强烈建议传** —— 不传时 SD 上界会被高估 → 漏报。
    返回：
        [{"kind": "grimmer", "mean", "sd", "n", "passed",
          "raw", "sd_min", "sd_max", "explain"}, ...]
    """
    from grimmer import grimmer_check  # 延迟导入：与 grimmer 共用同一口径

    out: list[dict[str, Any]] = []
    try:
        n_int = int(n)
    except (TypeError, ValueError):
        return out
    if n_int <= 0:
        return out

    lo, hi = (value_range if value_range else (None, None))

    # --- 配对：把上下文相邻的 mean 与 sd 组成三元组 ---
    means = [q for q in (paper_quantities or []) if q.get("kind") == "mean"]
    sds = [q for q in (paper_quantities or []) if q.get("kind") == "sd"]
    if not means or not sds:
        return out

    pairs: list[tuple[dict, dict]] = []
    for mq in means:
        mctx = str(mq.get("context", ""))
        # 优先找同一上下文里的 SD
        same_ctx = [sq for sq in sds
                    if str(sq.get("context", "")) == mctx and mctx]
        if same_ctx:
            pairs.append((mq, same_ctx[0]))
        elif len(sds) == len(means):
            # 退而求其次：一一对应（按出现顺序）
            idx = means.index(mq)
            if idx < len(sds):
                pairs.append((mq, sds[idx]))

    for mq, sq in pairs:
        try:
            mean = float(mq.get("value"))
            sd = float(sq.get("value"))
        except (TypeError, ValueError):
            continue
        res = grimmer_check(mean, sd, n_int, items=items, lo=lo, hi=hi)
        if not res.get("applicable"):
            continue
        passed = bool(res.get("possible"))
        explain = (
            f"n={n_int} 时，(均值 {mean:g}, SD {sd:g}) 落在整数数据的可达区间 "
            f"[{res['sd_min']:.4f}, {res['sd_max']:.4f}] 内 —— **可能**出现。"
            if passed else
            f"n={n_int} 时，{res['reason']}"
        )
        out.append({
            "kind": "grimmer", "mean": mean, "sd": sd, "n": n_int,
            "passed": passed, "raw": f"{mq.get('raw','')} / {sq.get('raw','')}",
            "sd_min": res["sd_min"], "sd_max": res["sd_max"],
            "explain": explain,
        })
    return out


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
# 3.5) 单条比对摘要（v1.6 · ②审计对话的数据底座）
# -----------------------------------------------------------------------------
# 目的：把「一条 comparison」压缩成一小段**自包含、可审、无原始数据**的文本，
# 作为 /api/audit_chat 唯一的上下文来源。
# 铁律②：这里产出的文本不会包含任何 df 单元格 / 原始观测，只有统计量本身。
_STATUS_CN = {
    "ok": "一致",
    "minor_diff": "略有出入",
    "mismatch": "不一致",
    "unknown": "无法比对",
    "no_real": "未能复算",
}

# 统计量 → 中文名（供解释时引用）
_KIND_CN = {
    "p": "p 值", "t": "t 值", "F": "F 值", "r": "相关系数 r",
    "chi2": "卡方值 χ²", "df": "自由度", "r2": "R²",
    "pseudo_r2": "伪 R²", "beta": "回归系数 β", "or": "优势比 OR",
    "alpha": "信度系数 α", "u": "U 统计量", "w": "W 统计量",
    # v2.16：表格核查的三种统计量。缺了它们，审计对话的条目标签会显示成
    # 原始 key（"table_mean"）而不是中文，前端 chip 里出现一坨英文很出戏。
    "table_n": "表格样本量 n", "table_mean": "表格均值",
    "table_sd": "表格标准差",
}


def _attach_comparison_summaries(comparisons: list[dict], real: dict,
                                 methods: list[dict]) -> list[dict]:
    """就地给每条 comparison 挂 summary 子字典；返回同一列表。

    summary 字段（前端 / 对话端点共用）：
        method_key : 论文声称的方法 key（可能为空）
        method_cn  : 方法中文名（可能为空）
        kind_cn    : 统计量中文名
        status     : 原样状态
        status_cn  : 中文状态
        paper      : 论文写的值（字符串）
        real       : 实算的值（字符串）
        diff       : 差值（字符串或 None）
        verdict    : 一句话人话结论（模板，零 LLM）
        hint       : 可选的方向性提示（为什么会不一致）
    """
    method_key = real.get("method") or ""
    # 论文声称的方法：取第一条（通常也是唯一一条）用于措辞
    paper_key = ""
    if methods:
        first = methods[0]
        paper_key = first.get("key") or first.get("method") or ""
    method_cn = _xai_method_label(method_key or paper_key) if (
        method_key or paper_key) else ""

    out: list[dict] = []
    for c in comparisons or []:
        st = c.get("status", "unknown")
        kind = c.get("kind") or ""
        kind_cn = _KIND_CN.get(kind, kind or "统计量")
        paper_s = c.get("paper")
        real_s = c.get("real")
        diff_s = c.get("diff")

        if st == "ok":
            verdict = f"论文写的 {kind_cn} 与实算结果一致，这项没问题。"
            hint = ""
        elif st == "minor_diff":
            verdict = (f"{kind_cn} 与实算有出入（论文 {paper_s} / 实算 {real_s}），"
                       f"差 {diff_s}——可能是四舍五入，建议核对原始输出。")
            hint = "差值处于四舍五入范围，通常不算错误，但最好与原软件输出核对一遍。"
        elif st == "mismatch":
            verdict = (f"{kind_cn} 对不上：论文写 {paper_s}，用你上传的数据实算是 "
                       f"{real_s}（差 {diff_s}）。")
            hint = ("常见原因：①论文用的样本/分组与本次上传的不完全一致；"
                    "②缺失值处理方式不同（删行 vs 填补）；"
                    "③论文上报的是别的统计量或做了手动换算。")
        elif st == "no_real":
            verdict = "这次没能复算成功，建议先确认数据列选对了。"
            hint = c.get("reason") or ""
        else:  # unknown
            verdict = f"这条（{kind_cn}）无法比对——论文值或实算值缺一个。"
            hint = ""

        c["summary"] = {
            "method_key": method_key or paper_key,
            "method_cn": method_cn,
            "kind_cn": kind_cn,
            "status": st,
            "status_cn": _STATUS_CN.get(st, st),
            "paper": paper_s,
            "real": real_s,
            "diff": diff_s,
            "verdict": verdict,
            "hint": hint,
        }
        out.append(c)
    return out


def _table_text_comparisons(tct: dict | None) -> list[dict]:
    """把 v2.12 文本形态表格（Markdown 表 / 管道表）的不一致转成 comparison 条目。

    **为什么要有这个函数**：v2.12 原本只把不一致写进 `suggestions`，
    于是它们既不在 `comparisons` 里（**没有 summary → 审计对话点不到**），
    也不在协作审阅的逐条比对表里。而 v2.11 的 docx 表格是两者都进的 ——
    同一类问题两种待遇，用户会以为"文本表那条不能追问是故意的"。

    统一成与 docx 表格相同的 `table_mean` / `table_sd` kind，
    这样「报告里看到的条目」＝「能追问的条目」＝「分享出去的条目」。

    **只比不判**：措辞沿用 table_check 的口径，绝不暗示造假。
    """
    out: list[dict] = []
    for m in (tct or {}).get("mismatches") or []:
        if not isinstance(m, dict):
            continue  # 脏数据：不是字典就跳过，别把整份报告搞崩
        try:
            real_v = float(m.get("real"))
            diff_v = float(m.get("diff"))
            real_s = f"{real_v:.4f}"
            diff_s = f"{diff_v:.4f}"
        except (TypeError, ValueError):
            continue
        kind = "table_mean" if m.get("kind") == "mean" else "table_sd"
        cn = "均值" if kind == "table_mean" else "标准差"
        label = (m.get("label") or "?")[:40]
        # 表名只在"看着真像表题"时才带。table_check 取的是表格**前一行**，
        # 那行经常是一整句正文（"本研究对被试进行了测量，描述统计如下。"），
        # 照抄进来会把结论句撑成读不通的长句。
        # 判定：形如「表1 …」/「表一 …」/「Table 1 …」，或很短（≤12 字）的一行；
        # 且以句号结尾的一律不当表题（那是正文句子）。
        cap = (m.get("table") or "").strip().strip("：: ")
        cap_s = ""
        if cap and not cap.endswith(("。", ".", "；", ";")):
            cap_like = bool(re.match(r"^(表\s*[\d一二三四五六七八九十]+"
                                     r"|Table\s*\d+)", cap, re.I))
            if cap_like or len(cap) <= 12:
                cap_s = f"「{cap}」"  # 不额外套"表"字，避免出现 表「表1 描述统计」
        out.append({
            "status": "mismatch",
            "kind": kind,
            "paper": f"{cap_s}第「{label}」行 {cn} {m.get('raw')}",
            "real": f"{real_s}（列「{m.get('column') or '?'}」，n={m.get('n')}）",
            "diff": diff_s,
            "source": "table_text",
        })
    return out


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

    if "repeated_measures_anova" in method_keys:
        out.append(
            "重复测量 ANOVA 必须报告**球形度检验（Mauchly's W）**结果；"
            "若球形度假定不成立，应报告 Greenhouse-Geisser 校正后的自由度与 p 值，"
            "而不是未校正的 F 检验结果。"
        )
        out.append(
            "被试内设计**不能**用普通单因素/双因素 ANOVA 代替——后者会把"
            "「被试」的个体差异误并入误差项，导致 F 值被人为放大、p 值偏小。"
        )
        if real.get("ok"):
            if real.get("sphericity_ok") is False:
                eps = real.get("gg_epsilon")
                eps_txt = f"（ε = {eps:.3f}）" if eps is not None else ""
                out.append(
                    f"实测球形度假定**不成立**{eps_txt}：论文报告 p 值时"
                    "必须使用 Greenhouse-Geisser 校正，否则会高估显著性。"
                )
            elif real.get("sphericity_ok") is True:
                out.append(
                    "实测球形度假定成立，可直接报告未校正的 F 检验结果，"
                    "但建议在方法部分说明已做 Mauchly 检验。"
                )
            if real.get("f") is not None and real.get("p") is not None:
                out.append(
                    f"实测时间/条件主效应：F = {real['f']:.3f}，p = {real['p']:.4f}"
                    + (f"，偏 η² = {real['eta2']:.3f}" if real.get("eta2") is not None else "")
                    + "。请核对该值与论文所报是否一致。"
                )
            out.append(
                "建议绘制各时间点的均值折线图（带标准误误差条），"
                "并报告事后两两比较的 Bonferroni 校正 p 值与效应量（Cohen's d_z）。"
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
    # v1.1 重复测量
    "重复测量": "repeated_measures_anova", "被试内": "repeated_measures_anova",
    "组内设计": "repeated_measures_anova", "重复测量方差分析": "repeated_measures_anova",
    "repeated measures": "repeated_measures_anova", "rm-anova": "repeated_measures_anova",
    "rm anova": "repeated_measures_anova",
}

# 论文声称多方法时的"真跑"优先级（列表顺序 = 优先级，靠前者先跑）。
# 取值理由：
#   - cronbach_alpha 优先：信度是"量表整体"分析，独立可跑，与其它方法不冲突
#   - repeated_measures_anova 次之：被试内设计需 ≥3 列，优先于只会吃 2 列的配对 T
#   - 回归类先于方差类：回归能给出更多可对比统计量（R²/F/t）
#   - 非参数垫底：参数方法能满足时核查价值更高
# v1.3：从 _run_real_analysis 的局部变量提升为模块级常量，供 registry_test 断言覆盖度。
_METHOD_PRIORITY = [
    "cronbach_alpha",           # 信度 = 量表整体，独立可跑，优先
    "repeated_measures_anova",  # 被试内设计，需 ≥3 列；优先于配对 T
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
                        directive: str = "",
                        paper_tables: list[list[list[str]]] | None = None) -> dict[str, Any]:
    """paper_claims 期望包含 keys: methods, quantities, variables, raw_text（可选）

    directive（v0.4 新增）：用户输入的过滤指令字符串，例如
      "只看 T 检验 且 p<0.05" / "只看男组" / ""（不过滤）
    传入后报告会按指令过滤；空串等价于不过滤。

    paper_tables（v2.11 · P3 新增）：docx 结构化表格
      （extract_paper.read_paper_tables 的输出）。非空时对描述统计表
      逐格核对（组标签匹配到唯一数据列才核对，n/mean/sd）。
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
        # v1.3：提升为模块级常量 `_METHOD_PRIORITY`（定义见文件上方），
        # 便于 registry_test.py 做"注册表 ↔ 优先级表"覆盖度断言。
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

    # 3.5) v2.1 GRIM 交叉核查：论文声称的均值 × 真实样本量 → 这个均值可能出现吗？
    #      样本量取**实算值**（不采信论文写的 n）：论文里的 n 本身可能就是错的。
    grim = grim_cross_check(quantities, int(len(df)))
    # 3.6) v2.10 GRIMMER 交叉核查：进一步查 (均值, 标准差, n) 三元组是否可能。
    #      只在论文同时写了 mean 与 sd 时才产出（没写就不制造噪音）。
    grimmer = grimmer_cross_check_reported(quantities, int(len(df)))
    # 3.7) v2.11 P3 表格交叉核查：docx 描述统计表 vs 按同样分组实算。
    #      标签匹配不到唯一数据列的表只记 note，绝不硬猜硬报。
    table_check = compare_table_stats(paper_tables, df, value_col=value_col)
    # 3.8) v2.12 P3 补充：**文本形态**的表格核对（Markdown 表 / docx 转文本的管道行）。
    #      compare_table_stats 吃的是 docx 结构化表格；.md 论文、以及从文本流
    #      进来的管道表它看不到。table_check 按"行标签 → 数据列名"直接配对，
    #      容差按论文报告位数动态定（写 60.90 用 0.005，写 60.9 用 0.05）。
    #      两者互补、互不覆盖：各自只在解析到表时才产出内容。
    from table_check import (  # 延迟导入：与 table_check 共用同一口径
        render_table_section, table_cross_check,
    )
    table_check_text = table_cross_check(paper_claims.get("raw_text", "") or "", df)

    # 4) 改进建议
    suggestions = _generate_suggestions(real, methods, quantities)
    # 4.05) GRIM 未通过 → 直接进建议列表（用户最容易看到的地方）
    for g in grim:
        if not g["passed"]:
            suggestions.append(
                f"[数据体检] 论文写的均值 {g['mean']:g}（原文「{g['raw']}」）在 n={g['n']} 下"
                f"不可能由整数计分得到（{g['mean']:g} × {g['n']} = {g['product']:g}），"
                f"请核对样本量口径（是否剔除缺失 / 分组报告）或计分方式。"
            )
    # 4.06) GRIMMER 未通过 → 同样直接进建议
    for g in grimmer:
        if not g["passed"]:
            suggestions.append(
                f"[数据体检] 论文写的「{g['raw']}」在 n={g['n']} 下不可能："
                f"{g['explain']} 请核对该组的标准差与样本量口径。"
            )
    # 4.07) v2.11 P3 表格核对不一致 → 直接进建议（用户最容易看到）
    for tm in table_check["mismatches"]:
        suggestions.append(
            f"[表格核对] {tm['paper']}，但{tm['real']}。"
            "论文表格数字与你的数据对不上，请核对该行口径"
            "（样本范围 / 剔除缺失 / 分组定义）。"
        )
    # 4.08) v2.12 P3 文本表核对不一致 → 同样进建议
    for tm in table_check_text.get("mismatches", []):
        kind_cn = "均值" if tm["kind"] == "mean" else "标准差"
        suggestions.append(
            f"[表格核对] 表格「{tm['table'] or tm['label']}」行的{kind_cn}"
            f"写的是 {tm['raw']}，用原始数据实算（列「{tm['column']}」，n={tm['n']}）"
            f"得到 {tm['real']:.4f}，相差 {tm['diff']:.4f}（超出容差 {tm['tol']:g}）。"
            "请核对是否换过数据、复制错行，或该行样本量口径不同。"
        )
    # 4.1) v1.5 ③人话解释卡片：结构化 explanations 与 suggestions 并行输出
    #      （suggestions 保持 list[str] 原样，旧前端 / 报告正文不受影响）
    explanations = _explain_suggestions(real, methods, quantities, columns)

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

    # 4.9) v1.6 ②审计对话：给每条 comparison 补一个 summary 子字典，
    #      供 /api/audit_chat 单条问答直接取用（**只喂这一小段给 LLM，
    #      绝不传 df / 原始数据**，铁律②）。
    #      放在指令过滤之后，保证用户看到的条目与可追问的条目一致。
    #      v2.11：表格核对的不一致条目也在这里合并（不受指令过滤影响——
    #      它们是另一类核查），同样能被追问。
    comparisons = comparisons + table_check["mismatches"]
    # v2.16：文本形态表格（Markdown / 管道表）的不一致同样进 comparisons，
    # 让它们可被追问、也能出现在协作审阅的逐条比对里（见 _table_text_comparisons）。
    comparisons = comparisons + _table_text_comparisons(table_check_text)
    comparisons = _attach_comparison_summaries(comparisons, real, methods)

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
            src = "、".join(sorted(set(m["sources"])))  # v2.24: sorted——set 迭代序随 hash 随机，Windows CI 上造成「等长不等值」
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
    # v2.16：表格核查条目（table_n / table_mean / table_sd）在 5.5d / 5.5e
    # 有专门的、信息更全的表（含表名、行标签、容差），这里不再重复一行，
    # 否则同一处不一致在报告里出现两次，反而让人以为查出了两倍的问题。
    _cmp_rows = [c for c in comparisons
                 if c.get("status") != "no_real"
                 and not str(c.get("kind") or "").startswith("table_")]
    if _cmp_rows:
        md_lines.append("| 统计量 | 论文写法 | 真实数据 | 差异 | 状态 |")
        md_lines.append("| --- | --- | --- | --- | --- |")
        for c in _cmp_rows:
            status_label = {"ok": "✅ 一致", "minor_diff": "🟡 小差异", "mismatch": "🔴 不一致",
                            "unknown": "⚪ 无法对比"}.get(c["status"], c["status"])
            md_lines.append(f"| {c.get('kind', '-')} | {c.get('paper', '-')} "
                            f"| {c.get('real', '-')} | {c.get('diff', '-')} "
                            f"| {status_label} |")
    elif any(str(c.get("kind") or "").startswith("table_") for c in comparisons):
        # 只有表格类比对时，别误报"没有可对比的统计量"——明明比过
        md_lines.append("统计量级别的比对见本节上方的「表格数字一致性核查」。")
    else:
        md_lines.append("没有可对比的统计量（论文未给出具体数值，或实际跑失败）。")

    # 5.5b GRIM 一致性检验（v2.1 · 论文侧数据取证）
    #      只在论文确实写了"均值"时才出现 —— 没写就不制造噪音。
    if grim:
        md_lines.append("\n**GRIM 一致性检验（均值 × 样本量是否可能）：**\n")
        md_lines.append("| 论文写法 | 均值 | 实算样本量 n | 均值 × n | 结论 |")
        md_lines.append("| --- | ---: | ---: | ---: | --- |")
        for g in grim:
            flag = "✅ 可能" if g["passed"] else "🔴 不可能"
            md_lines.append(f"| {g['raw']} | {g['mean']:g} | {g['n']} | {g['product']:g} | {flag} |")
        failed = [g for g in grim if not g["passed"]]
        if failed:
            md_lines.append(
                f"\n> ⚠️ 有 {len(failed)} 个均值在实算样本量下**不可能**出现。请核对："
                "样本量口径（是否剔除缺失 / 分组报告）、计分粒度、或是否存在四舍五入。"
                "**这不等于造假**，只说明这个数字需要解释。"
            )

    # 5.5c GRIMMER 一致性检验（v2.10 · 查标准差是否可能）
    #       只在论文同时写了均值与标准差时才出现。
    if grimmer:
        md_lines.append("\n**GRIMMER 一致性检验（均值 + 标准差 × 样本量是否可能）：**\n")
        md_lines.append("| 论文写法 | 均值 | SD | n | 可达 SD 区间 | 结论 |")
        md_lines.append("| --- | ---: | ---: | ---: | --- | --- |")
        for g in grimmer:
            flag = "✅ 可能" if g["passed"] else "🔴 不可能"
            md_lines.append(
                f"| {g['raw']} | {g['mean']:g} | {g['sd']:g} | {g['n']} | "
                f"[{g['sd_min']:.4f}, {g['sd_max']:.4f}] | {flag} |")
        failed_g = [g for g in grimmer if not g["passed"]]
        if failed_g:
            md_lines.append(
                f"\n> ⚠️ 有 {len(failed_g)} 组「均值 + 标准差」在实算样本量下**不可能**。"
                "请核对：该组的样本量口径、计分粒度、取值域（如 Likert 1–5）、"
                "或是否存在四舍五入 / 手算错误。"
                "**这不等于造假**，只说明这组数字需要解释。"
            )

    # 5.5d 论文表格交叉核对（v2.11 · P3：docx 描述统计表 vs 原始数据）
    #      有 docx 表格才出现本节；没核成的表也如实说明（透明但安静）。
    if paper_tables:
        md_lines.append("\n**表格交叉核对（论文 docx 表格 vs 你的数据）：**\n")
        if table_check["checked"] == 0:
            md_lines.append(
                "未找到可核对的表格行（需要形如「组别 | n | M | SD」的描述统计表，"
                "且分组标签与数据列取值能对上）。")
        else:
            tc_note = (f"共核对 **{table_check['checked']}** 格"
                       f"（跨 {len(table_check['notes'])} 张表）。")
            if table_check["mismatches"]:
                md_lines.append(tc_note + " 发现以下不一致：\n")
                md_lines.append("| 论文表格写的 | 用你的数据实算 | 差异 |")
                md_lines.append("| --- | --- | --- |")
                for tm in table_check["mismatches"]:
                    md_lines.append(
                        f"| 🔴 {tm['paper']} | {tm['real']} | {tm.get('diff', '-')} |")
                md_lines.append(
                    "\n> 表格数字与数据对不上时，优先核对：该行的样本范围"
                    "（是否剔除缺失）、分组定义（如「男」是否含其他编码）。")
            else:
                md_lines.append(tc_note + " ✅ 全部一致——表格数字与你的数据吻合。")
        for note in table_check["notes"]:
            if "没有唯一对应" in note:
                md_lines.append(f"- ⚪ {note}")

    # 5.5e 论文表格交叉核对 · 文本形态（v2.12 · P3 补充：Markdown 表 / 管道表）
    #      与 5.5d 互补：docx 走上面，.md / 文本流走这里。解析不到表则整节不出现。
    _tct = render_table_section(table_check_text) if table_check_text else []
    md_lines.extend(_tct)

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
        # v2.1 GRIM 交叉核查（论文声称均值 × 实算样本量）
        "grim": grim,
        "grimmer": grimmer,
        "table_check": table_check,
        # v2.12 P3 补充：文本形态表格核对（Markdown 表 / docx 转文本的管道行）
        "table_check_text": table_check_text,
        # v2.11 P3 表格交叉核对（docx 描述统计表 vs 分组实算）
        "table_check": table_check,
        "suggestions": suggestions,
        "explanations": explanations,
        "markdown": "\n".join(md_lines),
        "applied_directive": applied_directive,
        "parsed_directive": parsed_directive,
    }


# ===========================================================================
# 学术红线自检（v1.5 · ⑩红线引擎）
# ===========================================================================
# 把「合规红线」从营销文档变成代码：解析用户指令 / 生成报告前先扫一遍，
# 命中明确的学术不端意图就拦截，并给出正确用法。
#
# 设计原则（护栏，改动前务必读）：
#   1. 拦的是**学术不端**，不是"写作"本身。本工具的论文副驾驶本来就基于真实
#      数据生成初稿，那是合规的产品功能，绝不能误伤。
#   2. 因此分两类：
#        core    代写 / 买卖 / 规避查重 / 规避 AI 检测 / 伪造数据 → 无条件拦截
#        writing 整段整篇代写 → 拦截，但遇到明确的润色 / 改语病意图时豁免
#   3. 正则宁可保守：拿不准的不拦。误伤比漏拦伤害更大（漏拦可以后续补规则，
#      误伤会直接赶走正常用户）。
#   4. 纯正则、零 LLM、无副作用 —— 任何入口都能安全调用。

# 每条：(正则, 命中说明, 类别)
RED_LINE_PATTERNS: list[tuple[str, str, str]] = [
    # ---- core：学术不端，无条件拦截 ----
    (r"(代写|代笔|枪手|替写|找人写|代人写|请写手|代做|包写)",
     "代写 / 枪手：本工具不参与任何形式的代写服务", "core"),

    (r"(买|卖|购买|出售|收购|定制|求购).{0,6}(论文|毕设|毕业论文|学位论文|作业|文稿)"
     r"|(论文|毕设|毕业论文).{0,4}(交易|买卖|代做|包过)",
     "论文买卖：属于学术不端，且可能涉及违法违规", "core"),

    # 只拦"规避"动词（绕过/躲过/降重…），不拦"通过查重"这类陈述结果的合规说法
    (r"(绕过|躲过|规避|逃避|骗过|逃过|避开).{0,8}(查重|重复率|知网|维普|万方|turnitin|相似度)"
     r"|降重|(重复率|查重率|相似度).{0,4}(降|改|压|做)",
     "规避查重：本工具不提供任何规避查重的服务", "core"),

    (r"(去除|去掉|消除|洗掉|洗去|降低|伪装|隐藏).{0,8}(aigc|ai|人工智能).{0,6}(痕迹|味|率|检测|特征|标记)"
     r"|(绕过|躲过|骗过|规避|逃避).{0,8}(aigc|ai).{0,6}(检测|审查|识别)"
     r"|(aigc|ai).{0,4}(降重|去痕|洗稿)",
     "规避 AI 检测：本工具不帮助伪装 AI 生成内容", "core"),

    (r"(伪造|编造|造假|捏造|杜撰|篡改|瞎编|乱编|凑).{0,8}"
     r"(数据|结果|样本|问卷|实验结果|统计量|统计结果|p\s*值|显著性)"
     r"|(数据|结果|样本).{0,4}(造假|掺水|动手脚)"
     r"|(把|将).{0,6}(数据|结果|p\s*值).{0,6}(改|调).{0,4}(大|小|高|低|到|成)",
     "伪造 / 篡改数据：属于学术造假，绝对不可触碰", "core"),

    # ---- writing：整段 / 整篇代写（白名单可豁免）----
    (r"(帮我|替我|给我|请|帮).{0,6}(写|生成|产出一?篇?|来一?篇?).{0,6}"
     r"(整段|整篇|全文|完整|一篇).{0,8}(讨论|discussion|结论|摘要|正文|章节|论文|文章|报告)?",
     "整段 / 整篇代写：请基于你自己的真实数据与分析结果来写", "writing"),

    (r"(帮我|替我|给我|请).{0,6}(写|生成).{0,4}(一篇|一整篇|完整的).{0,4}(论文|文章|毕设|毕业论文|报告)",
     "整篇代写：本工具只能辅助你改进自己写的内容", "writing"),
]

# 明确的合规意图：出现这些词时豁免 writing 类红线（**绝不豁免 core**）
_RED_LINE_SAFE = (
    r"(润色|改.{0,3}语病|改.{0,3}语法|修改.{0,3}语病|调整.{0,3}结构|优化.{0,3}表达|"
    r"翻译|校对|检查.{0,3}格式|缩写|精简|帮我看看|帮我检查|这样写.{0,3}(对吗|对不对)|"
    r"是否合理|有没有问题)"
)

_RED_LINE_CORRECT_USAGE = (
    "本工具可以帮你做的：用你自己的真实数据跑统计；核查论文里的统计量是否对得上；"
    "指出方法误用与报告缺项；润色你已经写好的文字（表达、结构、语病）。\n"
    "本工具不会做的：代写论文、买卖论文、规避查重或 AI 检测、伪造篡改数据。\n"
    "正确用法：先在「数据分析」上传你的数据跑出结果，再用「论文副驾驶」基于这些"
    "真实结果生成初稿，最后由你自己改写、补充并署名。"
)


def _red_line_scan(text: str) -> dict[str, Any]:
    """学术安全自检。入参为**用户指令 / 备注**等自由文本。

    出参：
      blocked        bool        是否命中红线（命中即应拒绝执行）
      hits           list[str]   命中的红线说明
      correct_usage  str         被拦截时给出的正确用法（未拦截为空串）
      categories     list[str]   命中类别："core"（学术不端）/ "writing"（整段代写）

    纯函数、零 LLM、无副作用，可在任何入口安全调用。
    """
    out: dict[str, Any] = {
        "blocked": False, "hits": [], "correct_usage": "", "categories": [],
    }
    if not text or not text.strip():
        return out

    tl = text.strip().lower()
    core_hits: list[str] = []
    writing_hits: list[str] = []

    for pat, msg, cat in RED_LINE_PATTERNS:
        try:
            hit = re.search(pat, tl, re.IGNORECASE)
        except re.error:
            # 正则写错不应拖垮主流程，跳过该条
            continue
        if hit:
            (core_hits if cat == "core" else writing_hits).append(msg)

    safe_intent = bool(re.search(_RED_LINE_SAFE, tl, re.IGNORECASE))

    if core_hits:
        out["blocked"] = True
        out["hits"] = core_hits
        out["categories"] = ["core"]
    elif writing_hits and not safe_intent:
        out["blocked"] = True
        out["hits"] = writing_hits
        out["categories"] = ["writing"]

    if out["blocked"]:
        out["correct_usage"] = _RED_LINE_CORRECT_USAGE
    return out


# ===========================================================================
# ③ 人话解释卡片（v1.5 · 学术诚信 XAI）
# ===========================================================================
# `_generate_suggestions` 给的是一句话建议（"建议改 ANOVA"），但用户真正想知道的
# 是"**我哪里用错了**"。这里把方法误用讲成人话：为什么错、硬用会怎样、该改什么。
#
# 与 _generate_suggestions 的分工（**保持向后兼容**）：
#   suggestions   list[str]  一句话建议 —— 报告正文 / 旧前端，保持原样不动
#   explanations  list[dict] 结构化解释卡片 —— 前端展开用（本函数产出）
#
# 护栏：
#   - fix 必须是**注册表里真实存在**的方法（从 methods_registry 取中文名，绝不编造）
#   - why_wrong 只引用**已经算出来的**统计量（n_groups / mean / sd / p …）
#   - 拿不准的不出卡片 —— 宁缺毋滥：误报比漏报更伤信任

def _xai_method_label(key: str) -> str:
    """方法中文名 —— 从注册表取，保证与下拉框 / 报告 / 图谱的措辞一致。"""
    try:
        from methods_registry import method_labels
        return method_labels().get(key, key)
    except Exception:  # noqa: BLE001
        return key


def _xai_paired_hint(columns: list[dict] | None) -> tuple[str, str] | None:
    """列里是否有「前测 / 后测」这样的配对线索。返回 (前测列名, 后测列名)。

    锚定列名结尾，避免把 predict 这类词误判成 pre。
    """
    if not columns:
        return None
    names = [c.get("name", "") for c in columns]
    pre = [n for n in names if re.search(r"(前测|pre|before|基线|t1)$", n, re.I)]
    post = [n for n in names if re.search(r"(后测|post|after|t2)$", n, re.I)]
    if pre and post:
        return pre[0], post[0]
    return None


def _explain_suggestions(real: dict, paper_methods: list[dict],
                         paper_quantities: list[dict],
                         columns: list[dict] | None = None) -> list[dict]:
    """把「你哪里用错了方法」讲成人话。

    入参同 `_generate_suggestions`，额外接收 columns（列类型信息，用于判断
    变量是不是分类 / 有没有前后测线索）。

    出参：list[dict]，每项
      text            一句话结论
      why_wrong       为什么这样用是错的
      counter_example 硬用会有什么后果（具体到一类错误 / 检验效能）
      fix             建议改成的方法 key（注册表里存在；空串 = 不是改方法，是补报告）
      fix_label       建议方法的中文名（fix 为空时为空串）
      severity        high / mid / low
      from_method     原方法 key（可空）

    纯模板、零 LLM。出参按 severity 排序（high 在前）。
    """
    items: list[dict] = []
    method_keys = {m.get("method_key") for m in (paper_methods or [])}
    col_type = {c.get("name"): c.get("type") for c in (columns or [])}

    def add(text: str, why: str, counter: str, fix: str,
            severity: str, from_method: str | None):
        items.append({
            "text": text,
            "why_wrong": why,
            "counter_example": counter,
            "fix": fix,
            "fix_label": _xai_method_label(fix) if fix else "",
            "severity": severity,
            "from_method": from_method,
        })

    # 参数检验方法的稳定取一个做 from_method（set 无序，不能直接取）
    from_param = None
    for k in ("independent_t", "paired_t", "anova"):
        if k in method_keys:
            from_param = k
            break

    n_groups = real.get("n_groups") or 0
    gcol = real.get("group_col")

    # --- R1 分组数 > 2 却用独立样本 T 检验 ---
    if "independent_t" in method_keys and n_groups > 2:
        pairs = n_groups * (n_groups - 1) // 2
        inflated = 1 - 0.95 ** pairs
        add(
            f"【{gcol}】有 {n_groups} 个分组，不该用独立样本 T 检验",
            f"独立样本 T 检验只用于比较 2 个独立组的均值，"
            f"而分组变量【{gcol}】实际有 {n_groups} 个水平。",
            f"若拆成两两比较，{n_groups} 个组要比较 {pairs} 次；每次 α=0.05，"
            f"整体至少犯一次一类错误的概率会膨胀到约 {inflated:.0%}，远超 0.05。",
            "anova", "high", "independent_t",
        )

    # --- R2 有前后测线索却用独立样本 T 检验 ---
    hint = _xai_paired_hint(columns)
    if "independent_t" in method_keys and hint and n_groups <= 2:
        pre_col, post_col = hint
        add(
            f"数据里有【{pre_col}】【{post_col}】这样的前后测列，建议用配对检验",
            "前后测是**同一批对象**被测量两次（重复测量设计），"
            "独立样本 T 检验却假定两组互不相关，白扔掉了配对带来的信息。",
            "配对设计下用独立 T，标准误会被高估、检验效能下降——"
            "本来显著的差异可能变得不显著（假阴性）。",
            "paired_t", "high", "independent_t",
        )

    # --- R3 只有 2 组却用单因素 ANOVA ---
    if "anova" in method_keys and n_groups == 2:
        add(
            f"【{gcol}】只有 2 个分组，用 T 检验更贴合报告惯例",
            "单因素 ANOVA 用于 3 组及以上；2 组时它与 T 检验在数学上完全等价（F = t²）。",
            "不算错，但审稿人通常期待 2 组比较报告 t 值与 Cohen's d，而不是 F 与 η²。",
            "independent_t", "low", "anova",
        )

    # --- R4 卡方用在连续变量上 ---
    if "chi_square" in method_keys:
        vcol = real.get("value_col")
        if vcol and col_type.get(vcol) == "continuous":
            add(
                f"【{vcol}】是连续变量，不适合做卡方检验",
                "卡方检验要求两个变量都是**分类变量**（如性别 × 是否及格），"
                "考察的是各类别频数之间是否独立。",
                "把连续变量拿去算卡方，等于先丢掉了数值大小信息，"
                "结论既不稳定也难以解释。",
                "correlation", "high", "chi_square",
            )

    # --- R5 相关分析却选了分类列 ---
    if "correlation" in method_keys:
        vcol = real.get("value_col")
        if vcol and col_type.get(vcol) == "categorical":
            add(
                f"【{vcol}】是分类变量，Pearson 相关不适用",
                "Pearson 相关要求两个变量都是**连续变量**且关系近似线性。",
                "对分类变量算相关系数，得到的数值没有实际意义——"
                "类别之间的「大小关系」本身是人为指定的。",
                "chi_square", "high", "correlation",
            )

    # --- R6 小样本 + 参数检验，且未提非参数 ---
    n_total = (real.get("n1", 0) + real.get("n2", 0)) or real.get("n", 0)
    has_nonparam = bool({"mann_whitney", "wilcoxon"} & method_keys)
    if from_param and not has_nonparam and 0 < n_total < 30:
        add(
            f"样本量偏小（N={n_total}），参数检验的正态性假定难以保证",
            f"T 检验 / ANOVA 依赖「各组近似正态」的假定；N={n_total} 时"
            f"这个假定既难检验也难成立。",
            "假定不满足时 p 值不可靠，可能把噪声当成效应（假阳性）。",
            "mann_whitney", "mid", from_param,
        )

    # --- R7 / R8：不是"改方法"而是"补报告"的两种常见缺项（fix 留空） ---
    if from_param and real.get("ok"):
        has_effect = any(q.get("kind") in ("d", "eta", "eta_sq", "r")
                         for q in (paper_quantities or []))
        if not has_effect:
            add(
                "结论只报了 p 值，建议补充效应量",
                "p 值只回答「差异是否存在」，不回答「差异有多大」。",
                "缺少效应量时，读者无法判断差异在实际意义上是否重要——"
                "这也是审稿人最常见的退改意见之一。",
                "", "mid", None,
            )
        add(
            "未看到正态性 / 方差齐性检验的报告",
            "T 检验与 ANOVA 的结论依赖前提假定，报告前提检验结果是规范做法。",
            "若方差不齐却用了合并方差的 T 检验，p 值会有偏；"
            "正确做法是改用 Welch 校正并在文中说明。",
            "", "mid", None,
        )

    # 按严重度排序：high → mid → low
    order = {"high": 0, "mid": 1, "low": 2}
    items.sort(key=lambda it: order.get(it.get("severity"), 9))
    return items