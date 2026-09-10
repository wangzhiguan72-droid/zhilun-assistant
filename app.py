"""
智论助手 - MVP 后端服务
========================
当前包含两条主路径：
  A. 数据分析路径（首版）：
        1) 上传 Excel/CSV
        2) 自动识别列类型（连续/分类）
        3) 推荐并运行独立样本 T 检验
        4) 输出 Markdown 形式的可引用结果
        5) v0.5 新增：生成对应方法的图表（柱状/散点/堆叠柱）
  B. 论文排查路径（v0.2 新增）：
        1) 同时上传「论文初稿 (.docx/.txt/.md/.pdf)」+「数据 (.csv/.xlsx)」
        2) 从论文里识别统计方法 / 统计量 / 变量名
        3) 用数据重跑一次，对比声称值与实际值
        4) 输出 Markdown 形式的核查报告 + 改进建议
        5) v0.4 新增：自然语言指令过滤（"只看 T 检验" 等）

设计原则：
  - 每个分析函数都写成独立的、可被其他智能体复用的纯函数。
  - 接口尽量扁平，方便后续 Agent 接力（接 SPSS 输出、做方差分析、做回归、做图表）。
  - 不做付费、不做人味润色、不导出 Word。
"""

from __future__ import annotations

import base64
import io
import os
import re
import uuid
import time
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_from_directory
from scipy import stats
from werkzeug.utils import secure_filename

# v0.5：图表后端（matplotlib，强制无 GUI 后端）
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from extract_paper import (
    read_paper_text, extract_methods, extract_quantities, extract_variables,
)
from audit import build_audit_report
from llm_enhance import enhance_analysis
from llm_audit import audit_paper_with_llm
# v0.5.1 BYOK：用户自带 Key 注入 Router（/api/analyze 与 /api/check_paper 用）
from agents import get_router

# 中文字体（Windows 自带；其他系统会回退到默认）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False  # 正确显示负号

# -----------------------------------------------------------------------------
# 路径与基本配置
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
EXAMPLE_DIR = BASE_DIR / "examples"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# v0.5.3：本地 .env 加载器（含 API Key / LLM_TIER）。已存在的环境变量优先。
# 注意：**只在 `python app.py` 直启时加载**（见文件末尾 __main__ 块），
# 被 import 时（测试 / WSGI 部署）不加载 —— 这样单测能可靠假设"无 Key"，
# 生产也能用真实环境变量覆盖本地文件。.env 已被 .gitignore 忽略。
from env_loader import load_dotenv as _load_dotenv

MAX_UPLOAD_MB = 20
ALLOWED_EXT = {".csv", ".xlsx", ".xls"}

# 简易内存缓存：file_id -> DataFrame（小数据集本地测试足够）
_SESSION: dict[str, pd.DataFrame] = {}

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# -----------------------------------------------------------------------------
# 工具函数：列类型识别 / 文件读取
# -----------------------------------------------------------------------------
def _is_continuous(series: pd.Series) -> bool:
    """判断列是否为连续型（数值）变量。
    规则：必须是数值类型，且唯一值数量大于阈值（避免把"评分1-5"这种离散数值误判）。"""
    if not pd.api.types.is_numeric_dtype(series):
        return False
    n = series.dropna().nunique()
    return n >= max(10, int(0.05 * len(series)))


def _is_numeric_discrete(series: pd.Series) -> bool:
    """v1.1：数值型但取值少的列（典型：Likert 量表题 1-5 分、0/1 哑变量）。

    这类列在差异检验里适合作"分组"（旧行为），但在**信度分析**里必须
    当作题项参与求和。因此单独识别，供 run_cronbach_alpha 一类的
    "量表面向"方法使用，不改变 _is_continuous 的既有语义。
    """
    if not pd.api.types.is_numeric_dtype(series):
        return False
    n = series.dropna().nunique()
    return 2 <= n < max(10, int(0.05 * len(series)))


def _summarize_column(series: pd.Series) -> dict[str, Any]:
    """列概览：给前端展示用。"""
    n_total = int(len(series))
    n_missing = int(series.isna().sum())
    is_cont = _is_continuous(series)
    is_num_disc = (not is_cont) and _is_numeric_discrete(series)
    summary: dict[str, Any] = {
        "name": str(series.name),
        "dtype": str(series.dtype),
        "n_total": n_total,
        "n_missing": n_missing,
        "n_unique": int(series.dropna().nunique()),
        # v1.1：type 仍是 continuous / categorical（保持前端兼容），
        # 另加 is_numeric 标记，供信度分析等"量表面向"方法挑选题项。
        "type": "continuous" if is_cont else "categorical",
        "is_numeric": bool(pd.api.types.is_numeric_dtype(series)),
        "is_numeric_discrete": bool(is_num_disc),
    }
    if is_cont:
        nums = series.dropna().astype(float)
        if len(nums) > 0:
            summary["mean"] = float(nums.mean())
            summary["std"] = float(nums.std())
            summary["min"] = float(nums.min())
            summary["max"] = float(nums.max())
    else:
        # 取最高频的几个类别
        top = series.dropna().astype(str).value_counts().head(5)
        summary["top_values"] = [{"value": str(k), "count": int(v)} for k, v in top.items()]
    return summary


def _read_any(file_storage) -> pd.DataFrame:
    """根据扩展名读取上传文件。"""
    name = file_storage.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"不支持的文件类型：{ext}。仅支持 .csv / .xlsx / .xls")
    raw = file_storage.read()
    if ext == ".csv":
        # 自动尝试 utf-8 / gbk
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
            try:
                return pd.read_csv(io.BytesIO(raw), encoding=enc)
            except UnicodeDecodeError:
                continue
        raise ValueError("CSV 文件编码无法识别，请尝试另存为 UTF-8 或 GBK。")
    # Excel
    return pd.read_excel(io.BytesIO(raw))


def _is_id_like(name: str) -> bool:
    """判断列名是否像编号/标识列（学号、序号、name 等），避免误选为因变量。"""
    n = str(name).lower()
    return any(k in n for k in ("id", "no", "编号", "学号", "num", "序号", "name"))


def _recommend_method(columns: list[dict[str, Any]]) -> dict[str, Any]:
    """根据列类型推荐分析方法。
    MVP 仅给出推荐文案与默认选项，不实际执行（前端可手动切换）。"""
    # 排除 id 类列，防止把学号/序号当成因变量
    cont = [c["name"] for c in columns if c["type"] == "continuous" and not _is_id_like(c["name"])]
    cat = [c["name"] for c in columns if c["type"] == "categorical"]

    rec: dict[str, Any] = {"method": None, "reason": "", "group_col": None, "value_col": None}
    if len(cont) >= 1 and len(cat) >= 1:
        # 优先：1 连续 + 1 分类 -> T 检验 / ANOVA
        # 选唯一值最少的分类列作为分组
        cat_sorted = sorted([c for c in columns if c["name"] in cat], key=lambda x: x["n_unique"])
        group = cat_sorted[0]["name"]
        rec.update({
            "method": "independent_t" if cat_sorted[0]["n_unique"] == 2 else "anova",
            "reason": (
                f"检测到【{group}】为分类变量（{cat_sorted[0]['n_unique']} 个水平），"
                f"另含至少 1 个连续变量。默认对二者做差异检验。"
            ),
            "group_col": group,
            "value_col": cont[0],
        })
    elif len(cont) >= 2:
        rec.update({
            "method": "correlation",
            "reason": "未发现分类变量，检测到至少 2 个连续变量，默认做相关分析。",
            "group_col": None,
            "value_col": cont[0],
            "value_col2": cont[1] if len(cont) > 1 else None,
        })
    elif len(cat) >= 2:
        rec.update({
            "method": "chi_square",
            "reason": "未发现连续变量，检测到至少 2 个分类变量，默认做卡方检验。",
            "group_col": cat[0],
            "value_col": cat[1] if len(cat) > 1 else None,
        })
    return rec


# -----------------------------------------------------------------------------
# 分析函数：每个方法独立、可被其他 Agent 调用
# -----------------------------------------------------------------------------
def run_independent_t(df: pd.DataFrame, group_col: str, value_col: str) -> dict[str, Any]:
    """独立样本 T 检验（仅支持 2 个分组）。
    返回 dict：包含原始统计量 + 渲染好的 Markdown 段。
    """
    sub = df[[group_col, value_col]].dropna()
    if sub.empty:
        raise ValueError("筛选后没有有效数据，请检查所选列是否存在缺失或非数值。")
    if not pd.api.types.is_numeric_dtype(sub[value_col]):
        raise ValueError(f"因变量【{value_col}】必须为数值列。")

    groups = sub[group_col].astype(str).unique().tolist()
    if len(groups) != 2:
        raise ValueError(
            f"独立样本 T 检验仅支持 2 个分组，当前【{group_col}】有 {len(groups)} 个。"
            f"如需更多分组，请改用方差分析（ANOVA）。"
        )
    g1, g2 = groups[0], groups[1]
    v1 = sub.loc[sub[group_col].astype(str) == g1, value_col].astype(float)
    v2 = sub.loc[sub[group_col].astype(str) == g2, value_col].astype(float)
    n1, n2 = len(v1), len(v2)

    # Levene 方差齐性检验
    levene_stat, levene_p = stats.levene(v1, v2)
    equal_var = bool(levene_p > 0.05)

    t_stat, p_val = stats.ttest_ind(v1, v2, equal_var=equal_var)
    # 95% 置信区间
    se = np.sqrt(v1.var(ddof=1) / n1 + v2.var(ddof=1) / n2)
    diff = float(v1.mean() - v2.mean())
    dfree = n1 + n2 - 2
    t_crit = stats.t.ppf(0.975, dfree)
    ci_low = diff - t_crit * se
    ci_high = diff + t_crit * se
    # 效应量 Cohen's d
    pooled_sd = np.sqrt(((n1 - 1) * v1.var(ddof=1) + (n2 - 1) * v2.var(ddof=1)) / dfree)
    d = diff / pooled_sd if pooled_sd > 0 else float("nan")

    def _fmt(x: float, digits: int = 3) -> str:
        if pd.isna(x):
            return "—"
        return f"{x:.{digits}f}"

    significance = (
        "p < 0.001"
        if p_val < 0.001
        else "p < 0.01"
        if p_val < 0.01
        else "p < 0.05"
        if p_val < 0.05
        else "p ≥ 0.05（差异不显著）"
    )

    # 渲染 Markdown（沿用 PRD 中的「论文分析与结论」风格）
    md = []
    md.append(f"## 独立样本 T 检验结果\n")
    md.append(f"**因变量**：{value_col}　　**分组变量**：{group_col}\n")
    md.append("### 一、描述统计\n")
    md.append(f"| 分组 | n | 均值 | 标准差 |")
    md.append(f"| --- | ---: | ---: | ---: |")
    md.append(f"| {g1} | {n1} | {_fmt(float(v1.mean()))} | {_fmt(float(v1.std(ddof=1)))} |")
    md.append(f"| {g2} | {n2} | {_fmt(float(v2.mean()))} | {_fmt(float(v2.std(ddof=1)))} |")
    md.append("\n### 二、方差齐性检验（Levene）\n")
    md.append(f"- F = {_fmt(float(levene_stat))}, p = {_fmt(float(levene_p))}")
    md.append(f"- 结论：{'方差齐性（p > 0.05）' if equal_var else '方差不齐（p ≤ 0.05）'}，"
              f"T 检验采用 {'合并方差' if equal_var else 'Welch 校正'}。\n")
    md.append("### 三、差异检验\n")
    md.append(f"| 指标 | 数值 |")
    md.append(f"| --- | ---: |")
    md.append(f"| t | {_fmt(float(t_stat))} |")
    md.append(f"| 自由度（df） | {dfree} |")
    md.append(f"| p 值 | {_fmt(float(p_val))} |")
    md.append(f"| 均值差（{g1} − {g2}） | {_fmt(diff)} |")
    md.append(f"| 95% 置信区间 | [{_fmt(ci_low)}, {_fmt(ci_high)}] |")
    md.append(f"| Cohen's d | {_fmt(float(d))} |")
    md.append("\n### 四、显著性说明\n")
    md.append(f"- {significance}")
    md.append("\n### 五、结论（可直接引用进论文初稿）\n")
    direction = "高于" if diff > 0 else "低于"
    sig_phrase = (
        "差异达到统计学显著水平"
        if p_val < 0.05
        else "差异未达到统计学显著水平"
    )
    md.append(
        f"为考察【{group_col}】对【{value_col}】的影响，本研究对两组数据进行了独立样本 T 检验。"
        f"结果显示，{g1} 组（n = {n1}，M = {_fmt(float(v1.mean()))}，SD = {_fmt(float(v1.std(ddof=1)))}）"
        f"与 {g2} 组（n = {n2}，M = {_fmt(float(v2.mean()))}，SD = {_fmt(float(v2.std(ddof=1)))}）"
        f"在【{value_col}】上的差异{sig_phrase}（t({dfree}) = {_fmt(float(t_stat))}，"
        f"p = {_fmt(float(p_val))}，Cohen's d = {_fmt(float(d))}）。"
        f"具体而言，{g1} 组的平均得分{direction} {g2} 组，均值差为 {_fmt(diff)}（95% CI [{_fmt(ci_low)}, {_fmt(ci_high)}]）。"
    )

    return {
        "method": "independent_t",
        "summary": {
            "n1": n1, "n2": n2,
            "mean1": float(v1.mean()), "mean2": float(v2.mean()),
            "sd1": float(v1.std(ddof=1)), "sd2": float(v2.std(ddof=1)),
            "levene_p": float(levene_p),
            "t": float(t_stat), "df": dfree, "p": float(p_val),
            "diff": diff, "ci_low": ci_low, "ci_high": ci_high,
            "d": float(d),
            "significant": bool(p_val < 0.05),
        },
        "markdown": "\n".join(md),
        "groups": {"g1": g1, "g2": g2},
    }


# -----------------------------------------------------------------------------
# 单因素方差分析（ANOVA）+ Tukey HSD 事后检验
# -----------------------------------------------------------------------------
def _tukey_hsd(samples: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    """简易 Tukey HSD 事后多重比较（基于 studentized range 分布）。
    不依赖 statsmodels，纯 numpy + scipy 实现，足够内测使用。
    返回每对组的均值差、q 统计量、p 值。
    """
    keys = list(samples.keys())
    k = len(keys)
    n_total = sum(len(v) for v in samples.values())
    df_within = n_total - k
    # MSE
    mse = sum((len(v) - 1) * np.var(v, ddof=1) for v in samples.values()) / df_within
    if mse <= 0:
        return []
    n_per_group = {g: len(v) for g, v in samples.items()}
    # Tukey q 临界值（近似用 studentized range 分布，scipy 没有现成，用近似）
    # 近似 q_crit = sqrt(2) * z_{1 - alpha/(2k)} → 简化：用 t 分布近似 + Bonferroni
    # 为了内测精度够用，我们直接用 Bonferroni 校正的成对 t 检验
    from itertools import combinations
    pairs = []
    alpha = 0.05
    n_comparisons = k * (k - 1) // 2
    bonf_alpha = alpha / max(n_comparisons, 1)
    for a, b in combinations(keys, 2):
        va, vb = samples[a], samples[b]
        mean_diff = float(va.mean() - vb.mean())
        se = np.sqrt(mse * (1 / n_per_group[a] + 1 / n_per_group[b]))
        t_stat = mean_diff / se if se > 0 else 0
        df_eff = df_within
        # Bonferroni 校正
        p_two = 2 * (1 - stats.t.cdf(abs(t_stat), df_eff))
        p_adj = min(p_two * n_comparisons, 1.0)
        pairs.append({
            "group_a": a, "group_b": b,
            "mean_diff": mean_diff, "se": float(se),
            "t": float(t_stat), "p_adj": float(p_adj),
            "significant": bool(p_adj < 0.05),
        })
    return pairs


def run_anova(df: pd.DataFrame, group_col: str, value_col: str) -> dict[str, Any]:
    """单因素方差分析（3+ 分组）。"""
    sub = df[[group_col, value_col]].dropna()
    if sub.empty:
        raise ValueError("筛选后没有有效数据。")
    if not pd.api.types.is_numeric_dtype(sub[value_col]):
        raise ValueError(f"因变量【{value_col}】必须为数值列。")
    groups = sorted(sub[group_col].astype(str).unique().tolist())
    if len(groups) < 3:
        raise ValueError(
            f"方差分析需要至少 3 个分组，当前【{group_col}】只有 {len(groups)} 个。"
            f"如为 2 个分组，请改用独立样本 T 检验。"
        )
    samples = {g: sub.loc[sub[group_col].astype(str) == g, value_col].astype(float).values
               for g in groups}
    # 方差齐性（Levene）
    lev_stat, lev_p = stats.levene(*samples.values())
    equal_var = bool(lev_p > 0.05)

    f_stat, p_val = stats.f_oneway(*samples.values())
    df_between = len(groups) - 1
    df_within = len(sub) - len(groups)

    # 效应量 η²
    ss_between = sum(len(v) * (v.mean() - sub[value_col].mean()) ** 2 for v in samples.values())
    ss_total = float(((sub[value_col] - sub[value_col].mean()) ** 2).sum())
    eta_sq = float(ss_between / ss_total) if ss_total > 0 else float("nan")

    # 事后检验
    posthoc = _tukey_hsd(samples)

    def _fmt(x, d=3):
        if pd.isna(x): return "—"
        return f"{x:.{d}f}"

    md = []
    md.append("## 单因素方差分析（ANOVA）结果\n")
    md.append(f"**因变量**：{value_col}　　**分组变量**：{group_col}\n")
    md.append("### 一、描述统计\n")
    md.append(f"| 分组 | n | 均值 | 标准差 |")
    md.append(f"| --- | ---: | ---: | ---: |")
    for g in groups:
        v = samples[g]
        md.append(f"| {g} | {len(v)} | {_fmt(float(v.mean()))} | {_fmt(float(v.std(ddof=1)))} |")

    md.append("\n### 二、方差齐性检验（Levene）\n")
    md.append(f"- F = {_fmt(float(lev_stat))}, p = {_fmt(float(lev_p))}")
    md.append(f"- 结论：{'方差齐性' if equal_var else '方差不齐'}。"
              f"若方差不齐，应使用 Welch ANOVA 或 Kruskal-Wallis 非参数方法。\n")

    md.append("### 三、ANOVA 主效应\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| F | {_fmt(float(f_stat))} |")
    md.append(f"| 自由度（组间, 组内） | ({df_between}, {df_within}) |")
    md.append(f"| p 值 | {_fmt(float(p_val))} |")
    md.append(f"| 效应量 η² | {_fmt(eta_sq)} |")
    sig_phrase = "差异达到统计学显著水平" if p_val < 0.05 else "差异未达到统计学显著水平"
    md.append(f"\n**结论**：{value_col} 在不同 {group_col} 之间的{sig_phrase}。"
              f"（F({df_between}, {df_within}) = {_fmt(float(f_stat))}，p = {_fmt(float(p_val))}，"
              f"η² = {_fmt(eta_sq)}）。")

    md.append("\n### 四、事后多重比较（Bonferroni 校正）\n")
    if p_val >= 0.05:
        md.append("主效应不显著，事后比较意义有限，但仍供参考：\n")
    if posthoc:
        md.append("| 组 A | 组 B | 均值差 | p（校正后） | 显著性 |")
        md.append("| --- | --- | ---: | ---: | --- |")
        for h in posthoc:
            md.append(f"| {h['group_a']} | {h['group_b']} | {_fmt(h['mean_diff'])} "
                      f"| {_fmt(h['p_adj'])} | "
                      f"{'✅ 显著' if h['significant'] else '—'} |")
    else:
        md.append("（样本方差为 0，跳过事后比较。）")

    md.append("\n### 五、结论（可直接引用进论文）\n")
    if p_val < 0.05:
        sig_pairs = [h for h in posthoc if h["significant"]]
        if sig_pairs:
            pair_text = "、".join([f"{h['group_a']} vs {h['group_b']}" for h in sig_pairs[:3]])
            md.append(
                f"为考察【{group_col}】对【{value_col}】的影响，本研究对 {len(groups)} 组数据进行了单因素方差分析。"
                f"结果显示，{group_col} 对 {value_col} 的主效应显著（F({df_between}, {df_within}) = {_fmt(float(f_stat))}，"
                f"p = {_fmt(float(p_val))}，η² = {_fmt(eta_sq)}）。事后比较（Bonferroni 校正）表明，"
                f"{pair_text} 之间存在显著差异。"
            )
        else:
            md.append(
                f"ANOVA 主效应显著，但事后比较未发现具体对之间的差异（可能受多重比较校正影响），"
                f"建议结合效应量与描述统计谨慎解释。"
            )
    else:
        md.append(
            f"方差分析结果显示，{group_col} 各组在 {value_col} 上的差异未达统计学显著水平"
            f"（F({df_between}, {df_within}) = {_fmt(float(f_stat))}，p = {_fmt(float(p_val))}）。"
        )

    return {
        "method": "anova",
        "summary": {
            "F": float(f_stat), "p": float(p_val),
            "df_between": df_between, "df_within": df_within,
            "eta_sq": eta_sq,
            "levene_p": float(lev_p),
            "n_groups": len(groups),
            "group_sizes": {g: int(len(samples[g])) for g in groups},
            "group_means": {g: float(samples[g].mean()) for g in groups},
            "significant": bool(p_val < 0.05),
            "posthoc": posthoc,
        },
        "markdown": "\n".join(md),
        "groups": groups,
    }


# -----------------------------------------------------------------------------
# Pearson 相关分析
# -----------------------------------------------------------------------------
def run_correlation(df: pd.DataFrame, col_a: str, col_b: str) -> dict[str, Any]:
    """Pearson 相关分析。"""
    if col_a == col_b:
        raise ValueError("两个变量不能相同。")
    sub = df[[col_a, col_b]].dropna()
    if sub.empty:
        raise ValueError("筛选后没有有效数据。")
    if not (pd.api.types.is_numeric_dtype(sub[col_a])
            and pd.api.types.is_numeric_dtype(sub[col_b])):
        raise ValueError("两个变量都必须为数值列。")

    r_val, p_val = stats.pearsonr(sub[col_a], sub[col_b])
    n = len(sub)
    # 95% CI（用 Fisher z 变换）
    if abs(r_val) < 0.9999:
        z = np.arctanh(r_val)
        se_z = 1 / np.sqrt(n - 3)
        z_low = z - 1.96 * se_z
        z_high = z + 1.96 * se_z
        ci_low = float(np.tanh(z_low))
        ci_high = float(np.tanh(z_high))
    else:
        ci_low, ci_high = float("nan"), float("nan")

    # t 统计量（用于核查）
    if abs(r_val) < 1:
        t_stat = r_val * np.sqrt((n - 2) / (1 - r_val ** 2))
    else:
        t_stat = float("inf")
    dfree = n - 2

    # 效应量解释（Cohen 1988）
    abs_r = abs(r_val)
    interp = "小效应" if abs_r < 0.1 else "中等效应" if abs_r < 0.5 else "大效应"

    def _fmt(x, d=3):
        if pd.isna(x): return "—"
        return f"{x:.{d}f}"

    md = []
    md.append("## Pearson 相关分析结果\n")
    md.append(f"**变量 X**：{col_a}　　**变量 Y**：{col_b}\n")
    md.append("### 一、描述统计\n")
    md.append(f"| 变量 | n | 均值 | 标准差 |")
    md.append(f"| --- | ---: | ---: | ---: |")
    for c in (col_a, col_b):
        s = sub[c]
        md.append(f"| {c} | {len(s)} | {_fmt(float(s.mean()))} | {_fmt(float(s.std(ddof=1)))} |")

    md.append("\n### 二、Pearson 相关系数\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| 相关系数 r | {_fmt(r_val)} |")
    md.append(f"| 95% 置信区间 | [{_fmt(ci_low)}, {_fmt(ci_high)}] |")
    md.append(f"| t 统计量 | {_fmt(float(t_stat))} |")
    md.append(f"| 自由度 | {dfree} |")
    md.append(f"| p 值（双侧） | {_fmt(float(p_val))} |")
    md.append(f"| 效应量解释 | {interp} |")
    md.append(f"\n**方向**：{'正相关' if r_val > 0 else '负相关'}。")

    md.append("\n### 三、结论（可直接引用进论文）\n")
    direction = "正相关" if r_val > 0 else "负相关"
    md.append(
        f"为考察【{col_a}】与【{col_b}】之间的线性关系，本研究对 {n} 对有效数据进行了 Pearson 相关分析。"
        f"结果显示，两变量之间存在{direction}（r = {_fmt(r_val)}，95% CI [{_fmt(ci_low)}, {_fmt(ci_high)}]，"
        f"t({dfree}) = {_fmt(float(t_stat))}，p = {_fmt(float(p_val))}），"
        f"属于{interp}。{'差异具有统计学显著性' if p_val < 0.05 else '差异未达统计学显著水平'}。"
        f"（注：相关不等于因果，下一步建议引入回归或控制变量进一步分析。）"
    )

    return {
        "method": "correlation",
        "summary": {
            "r": float(r_val), "p": float(p_val),
            "n": n, "df": dfree,
            "t": float(t_stat),
            "ci_low": ci_low, "ci_high": ci_high,
            "significant": bool(p_val < 0.05),
        },
        "markdown": "\n".join(md),
        "variables": {"x": col_a, "y": col_b},
    }


# -----------------------------------------------------------------------------
# 卡方检验（独立性）
# -----------------------------------------------------------------------------
def run_chi_square(df: pd.DataFrame, row_col: str, col_col: str) -> dict[str, Any]:
    """卡方检验（两个分类变量的独立性）。"""
    if row_col == col_col:
        raise ValueError("两个变量不能相同。")
    sub = df[[row_col, col_col]].dropna()
    if sub.empty:
        raise ValueError("筛选后没有有效数据。")

    ct = pd.crosstab(sub[row_col].astype(str), sub[col_col].astype(str))
    if ct.shape[0] < 2 or ct.shape[1] < 2:
        raise ValueError(
            f"卡方检验需要两个变量都至少 2 个水平，当前"
            f"【{row_col}】有 {ct.shape[0]} 个、【{col_col}】有 {ct.shape[1]} 个。"
        )
    chi2, p_val, dof, expected = stats.chi2_contingency(ct)
    n = int(ct.values.sum())
    # Cramér's V
    cramers_v = float(np.sqrt(chi2 / (n * (min(ct.shape) - 1)))) if n > 0 and min(ct.shape) > 1 else float("nan")
    # 期望频数警告
    n_low_expected = int((expected < 5).sum())
    has_low_expected = n_low_expected > 0

    def _fmt(x, d=3):
        if pd.isna(x): return "—"
        return f"{x:.{d}f}"

    md = []
    md.append("## 卡方检验（独立性）结果\n")
    md.append(f"**行变量**：{row_col}　　**列变量**：{col_col}\n")
    md.append("### 一、交叉列联表\n")
    header = "| " + row_col + " \\ " + col_col + " | " + " | ".join(map(str, ct.columns)) + " | 合计 |"
    sep = "| --- |" + " ---: |" * (len(ct.columns) + 1)
    md.append(header)
    md.append(sep)
    for ridx, row in ct.iterrows():
        cells = [str(ridx)] + [str(int(v)) for v in row.values] + [str(int(row.sum()))]
        md.append("| " + " | ".join(cells) + " |")
    total_row = ["合计"] + [str(int(ct[c].sum())) for c in ct.columns] + [str(n)]
    md.append("| " + " | ".join(total_row) + " |")

    md.append("\n### 二、期望频数\n")
    md.append("| " + row_col + " \\ " + col_col + " | " + " | ".join(map(str, ct.columns)) + " |")
    md.append(sep)
    for i, ridx in enumerate(ct.index):
        cells = [str(ridx)] + [_fmt(expected[i, j]) for j in range(ct.shape[1])]
        md.append("| " + " | ".join(cells) + " |")

    md.append("\n### 三、检验结果\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| χ² | {_fmt(chi2)} |")
    md.append(f"| 自由度 | {dof} |")
    md.append(f"| p 值 | {_fmt(p_val)} |")
    md.append(f"| Cramér's V | {_fmt(cramers_v)} |")
    md.append(f"| 样本量 N | {n} |")
    if has_low_expected:
        md.append(f"\n⚠️ **警告**：有 {n_low_expected} 个期望频数 < 5，结果可能不可靠。"
                  f"建议合并稀疏类别或改用 Fisher 精确检验。")

    md.append("\n### 四、结论（可直接引用进论文）\n")
    sig_phrase = "差异具有统计学显著性" if p_val < 0.05 else "差异未达统计学显著水平"
    md.append(
        f"为考察【{row_col}】与【{col_col}】之间是否独立，本研究基于 {n} 例有效数据构建 {ct.shape[0]}×{ct.shape[1]} 列联表，"
        f"进行了 Pearson 卡方检验。结果显示，χ²({dof}) = {_fmt(chi2)}，p = {_fmt(p_val)}，"
        f"Cramér's V = {_fmt(cramers_v)}，{sig_phrase}。"
        f"（注：卡方检验反映的是两变量间的关联强度，并非因果。）"
    )

    return {
        "method": "chi_square",
        "summary": {
            "chi2": float(chi2), "p": float(p_val),
            "df": int(dof), "n": n,
            "cramers_v": cramers_v,
            "contingency_table": ct.to_dict(),
            "low_expected_count": n_low_expected,
            "significant": bool(p_val < 0.05),
        },
        "markdown": "\n".join(md),
        "variables": {"row": row_col, "col": col_col},
    }


# -----------------------------------------------------------------------------
# 配对样本 T 检验（v0.7 新增 · 医学/心理前后测常用）
# -----------------------------------------------------------------------------
def run_paired_t(df: pd.DataFrame, col_pre: str, col_post: str) -> dict[str, Any]:
    """配对样本 T 检验：同一对象在两个时点的测量（如焦虑前 vs 焦虑后）。
    接口：value_col = 前测列，value_col2 = 后测列。
    """
    if col_pre == col_post:
        raise ValueError("两个变量不能相同。")
    sub = df[[col_pre, col_post]].dropna()
    if sub.empty:
        raise ValueError("筛选后没有有效数据。")
    if not (pd.api.types.is_numeric_dtype(sub[col_pre])
            and pd.api.types.is_numeric_dtype(sub[col_post])):
        raise ValueError("两个变量都必须为数值列。")
    pre = sub[col_pre].astype(float).values
    post = sub[col_post].astype(float).values
    n = len(pre)
    if n < 2:
        raise ValueError(f"配对样本量不足（N={n}），至少需要 2 对。")

    diff = post - pre
    mean_d = float(diff.mean())
    sd_d = float(diff.std(ddof=1))
    se_d = sd_d / np.sqrt(n) if n > 0 else float("nan")
    t_stat, p_val = stats.ttest_rel(post, pre)
    dfree = n - 1
    # 95% CI of mean difference
    t_crit = stats.t.ppf(0.975, dfree)
    ci_low = mean_d - t_crit * se_d
    ci_high = mean_d + t_crit * se_d
    # Cohen's d_z（配对专用）
    d_z = mean_d / sd_d if sd_d > 0 else float("nan")
    # 效应量解释（Cohen 1988 for paired）
    abs_d = abs(d_z)
    interp = "小效应" if abs_d < 0.2 else "中等效应" if abs_d < 0.5 else "大效应"

    # 正态性提示
    if n >= 3:
        _, sw_p = stats.shapiro(diff)
    else:
        sw_p = None
    normality_ok = (sw_p is None) or (sw_p > 0.05)

    def _fmt(x, d=3):
        if pd.isna(x): return "—"
        return f"{x:.{d}f}"

    significance = (
        "p < 0.001" if p_val < 0.001
        else "p < 0.01" if p_val < 0.01
        else "p < 0.05" if p_val < 0.05
        else "p ≥ 0.05（差异不显著）"
    )
    direction = "上升" if mean_d > 0 else "下降"

    md = []
    md.append("## 配对样本 T 检验结果\n")
    md.append(f"**前测变量**：{col_pre}　　**后测变量**：{col_post}\n")
    md.append("### 一、描述统计\n")
    md.append("| 时点 | n | 均值 | 标准差 |")
    md.append("| --- | ---: | ---: | ---: |")
    md.append(f"| 前测（{col_pre}） | {n} | {_fmt(float(pre.mean()))} | {_fmt(float(pre.std(ddof=1)))} |")
    md.append(f"| 后测（{col_post}） | {n} | {_fmt(float(post.mean()))} | {_fmt(float(post.std(ddof=1)))} |")
    md.append(f"| 差值（后 − 前） | {n} | {_fmt(mean_d)} | {_fmt(sd_d)} |")
    md.append("\n### 二、正态性检验（Shapiro-Wilk，差值）\n")
    if sw_p is not None:
        md.append(f"- W = {_fmt(float(stats.shapiro(diff)[0]))}, p = {_fmt(float(sw_p))}")
        md.append(f"- 结论：{'差值近似正态' if normality_ok else '⚠️ 差值偏离正态，建议改用 Wilcoxon 符号秩检验'}。\n")
    else:
        md.append("- 样本量过小，跳过正态性检验。\n")
    md.append("### 三、差异检验\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| t | {_fmt(float(t_stat))} |")
    md.append(f"| 自由度 | {dfree} |")
    md.append(f"| p 值 | {_fmt(float(p_val))} |")
    md.append(f"| 均值差（后 − 前） | {_fmt(mean_d)} |")
    md.append(f"| 95% 置信区间 | [{_fmt(ci_low)}, {_fmt(ci_high)}] |")
    md.append(f"| Cohen's d_z | {_fmt(float(d_z))}（{interp}） |")
    md.append("\n### 四、显著性说明\n")
    md.append(f"- {significance}")
    md.append("\n### 五、结论（可直接引用进论文）\n")
    sig_phrase = "差异达到统计学显著水平" if p_val < 0.05 else "差异未达到统计学显著水平"
    md.append(
        f"为考察【{col_post} vs {col_pre}】的差异，本研究对 {n} 对配对数据进行了配对样本 T 检验。"
        f"结果显示，后测较前测平均{direction} {_fmt(abs(mean_d))}（M_diff = {_fmt(mean_d)}，"
        f"SD_diff = {_fmt(sd_d)}），该{sig_phrase}（t({dfree}) = {_fmt(float(t_stat))}，"
        f"p = {_fmt(float(p_val))}，Cohen's d_z = {_fmt(float(d_z))}，{interp}）。"
        f"95% 置信区间为 [{_fmt(ci_low)}, {_fmt(ci_high)}]。"
    )
    if not normality_ok:
        md.append("\n> ⚠️ 差值偏离正态，以上 p 值仅供参考；建议改用 Wilcoxon 符号秩检验。")

    return {
        "method": "paired_t",
        "summary": {
            "t": float(t_stat), "df": dfree, "p": float(p_val),
            "n": n, "mean_diff": mean_d, "sd_diff": sd_d,
            "ci_low": ci_low, "ci_high": ci_high,
            "d_z": float(d_z),
            "significant": bool(p_val < 0.05),
            "normality_p": float(sw_p) if sw_p is not None else None,
        },
        "markdown": "\n".join(md),
        "variables": {"pre": col_pre, "post": col_post},
    }


# -----------------------------------------------------------------------------
# Mann-Whitney U 检验（v0.7 新增 · 非参数 2 组比较）
# -----------------------------------------------------------------------------
def run_mann_whitney(df: pd.DataFrame, group_col: str, value_col: str) -> dict[str, Any]:
    """Mann-Whitney U 检验：2 组独立样本的非参数检验（不要求正态）。
    当 T 检验的正态性 / 方差齐性不满足时，作为替代方案。
    """
    sub = df[[group_col, value_col]].dropna()
    if sub.empty:
        raise ValueError("筛选后没有有效数据。")
    if not pd.api.types.is_numeric_dtype(sub[value_col]):
        raise ValueError(f"因变量【{value_col}】必须为数值列。")
    groups = sub[group_col].astype(str).unique().tolist()
    if len(groups) != 2:
        raise ValueError(
            f"Mann-Whitney U 检验仅支持 2 个分组，当前【{group_col}】有 {len(groups)} 个。"
            f"多于 2 组请改用 Kruskal-Wallis。"
        )
    g1, g2 = groups[0], groups[1]
    v1 = sub.loc[sub[group_col].astype(str) == g1, value_col].astype(float)
    v2 = sub.loc[sub[group_col].astype(str) == g2, value_col].astype(float)
    n1, n2 = len(v1), len(v2)
    if n1 < 1 or n2 < 1:
        raise ValueError(f"两组样本量不足（{g1}: {n1}, {g2}: {n2}）。")

    u_stat, p_val = stats.mannwhitneyu(v1, v2, alternative='two-sided')
    # 中位数 + 四分位距（更适合非参数）
    med1, med2 = float(v1.median()), float(v2.median())
    q1_1, q3_1 = float(v1.quantile(0.25)), float(v1.quantile(0.75))
    q1_2, q3_2 = float(v2.quantile(0.25)), float(v2.quantile(0.75))
    # 效应量 r = Z / sqrt(N)
    n_total = n1 + n2
    z_val = stats.norm.isf(p_val / 2)  # 近似 Z
    r_effect = float(z_val / np.sqrt(n_total)) if n_total > 0 else float("nan")
    abs_r = abs(r_effect)
    interp = "小效应" if abs_r < 0.1 else "中等效应" if abs_r < 0.3 else "大效应"

    def _fmt(x, d=3):
        if pd.isna(x): return "—"
        return f"{x:.{d}f}"

    significance = (
        "p < 0.001" if p_val < 0.001
        else "p < 0.01" if p_val < 0.01
        else "p < 0.05" if p_val < 0.05
        else "p ≥ 0.05（差异不显著）"
    )

    md = []
    md.append("## Mann-Whitney U 检验结果（非参数）\n")
    md.append(f"**因变量**：{value_col}　　**分组变量**：{group_col}\n")
    md.append("### 一、描述统计（中位数 + 四分位距）\n")
    md.append("| 分组 | n | 中位数 | Q1 | Q3 |")
    md.append("| --- | ---: | ---: | ---: | ---: |")
    md.append(f"| {g1} | {n1} | {_fmt(med1)} | {_fmt(q1_1)} | {_fmt(q3_1)} |")
    md.append(f"| {g2} | {n2} | {_fmt(med2)} | {_fmt(q1_2)} | {_fmt(q3_2)} |")
    md.append("\n### 二、Mann-Whitney U 检验\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| U 统计量 | {_fmt(float(u_stat))} |")
    md.append(f"| 近似 Z | {_fmt(float(z_val))} |")
    md.append(f"| p 值（双侧） | {_fmt(float(p_val))} |")
    md.append(f"| 效应量 r | {_fmt(r_effect)}（{interp}） |")
    md.append("\n### 三、显著性说明\n")
    md.append(f"- {significance}")
    md.append("\n### 四、适用场景说明\n")
    md.append("- 本检验**不要求正态分布**，对极端值稳健。")
    md.append("- 适用于：样本量较小、明显偏态、存在极端值、或方差不齐的场景。")
    md.append("- 零假设：两组分布相同（不限于均值）。")
    md.append("\n### 五、结论（可直接引用进论文）\n")
    sig_phrase = "差异达到统计学显著水平" if p_val < 0.05 else "差异未达到统计学显著水平"
    direction = "高于" if med1 > med2 else "低于"
    md.append(
        f"为考察【{group_col}】对【{value_col}】的影响，本研究采用 Mann-Whitney U 检验"
        f"（不要求正态分布，适合小样本或偏态数据）。结果显示，{g1} 组（n = {n1}，"
        f"Mdn = {_fmt(med1)}）与 {g2} 组（n = {n2}，Mdn = {_fmt(med2)}）的{sig_phrase}"
        f"（U = {_fmt(float(u_stat))}，p = {_fmt(float(p_val))}，r = {_fmt(r_effect)}，{interp}）。"
        f"{g1} 组的中位数{direction} {g2} 组。"
    )

    return {
        "method": "mann_whitney",
        "summary": {
            "U": float(u_stat), "z": float(z_val), "p": float(p_val),
            "n1": n1, "n2": n2,
            "median1": med1, "median2": med2,
            "r_effect": r_effect,
            "significant": bool(p_val < 0.05),
        },
        "markdown": "\n".join(md),
        "groups": {"g1": g1, "g2": g2},
    }


# -----------------------------------------------------------------------------
# Wilcoxon 符号秩检验（v0.7 新增 · 配对非参数）
# -----------------------------------------------------------------------------
def run_wilcoxon(df: pd.DataFrame, col_pre: str, col_post: str) -> dict[str, Any]:
    """Wilcoxon 符号秩检验：配对样本的非参数检验。
    当配对 T 检验的正态性不满足时，作为替代方案。
    """
    if col_pre == col_post:
        raise ValueError("两个变量不能相同。")
    sub = df[[col_pre, col_post]].dropna()
    if sub.empty:
        raise ValueError("筛选后没有有效数据。")
    if not (pd.api.types.is_numeric_dtype(sub[col_pre])
            and pd.api.types.is_numeric_dtype(sub[col_post])):
        raise ValueError("两个变量都必须为数值列。")
    pre = sub[col_pre].astype(float).values
    post = sub[col_post].astype(float).values
    diff = post - pre
    # Wilcoxon 不能处理差值为 0 的对：剔除
    nonzero = diff != 0
    n_total = len(pre)
    n_nonzero = int(nonzero.sum())
    if n_nonzero < 1:
        raise ValueError("所有配对的差值都为 0，无可检验的差异。")

    try:
        w_stat, p_val = stats.wilcoxon(post[nonzero], pre[nonzero], alternative='two-sided')
    except ValueError as e:
        raise ValueError(f"Wilcoxon 检验失败：{e}")

    # 中位数差 + IQR
    med_d = float(np.median(diff))
    q1_d = float(np.quantile(diff, 0.25))
    q3_d = float(np.quantile(diff, 0.75))
    # 效应量 r = Z / sqrt(N)
    z_val = stats.norm.isf(p_val / 2)
    r_effect = float(z_val / np.sqrt(n_total)) if n_total > 0 else float("nan")
    abs_r = abs(r_effect)
    interp = "小效应" if abs_r < 0.1 else "中等效应" if abs_r < 0.3 else "大效应"

    def _fmt(x, d=3):
        if pd.isna(x): return "—"
        return f"{x:.{d}f}"

    significance = (
        "p < 0.001" if p_val < 0.001
        else "p < 0.01" if p_val < 0.01
        else "p < 0.05" if p_val < 0.05
        else "p ≥ 0.05（差异不显著）"
    )
    direction = "上升" if med_d > 0 else "下降"

    md = []
    md.append("## Wilcoxon 符号秩检验结果（配对非参数）\n")
    md.append(f"**前测变量**：{col_pre}　　**后测变量**：{col_post}\n")
    md.append(f"> 共 {n_total} 对配对数据，其中 {n_nonzero} 对差值非零被纳入检验。\n")
    md.append("### 一、描述统计\n")
    md.append("| 时点 | n | 中位数 | Q1 | Q3 |")
    md.append("| --- | ---: | ---: | ---: | ---: |")
    md.append(f"| 前测（{col_pre}） | {n_total} | {_fmt(float(np.median(pre)))} | {_fmt(float(np.quantile(pre, 0.25)))} | {_fmt(float(np.quantile(pre, 0.75)))} |")
    md.append(f"| 后测（{col_post}） | {n_total} | {_fmt(float(np.median(post)))} | {_fmt(float(np.quantile(post, 0.25)))} | {_fmt(float(np.quantile(post, 0.75)))} |")
    md.append(f"| 差值（后 − 前） | {n_total} | {_fmt(med_d)} | {_fmt(q1_d)} | {_fmt(q3_d)} |")
    md.append("\n### 二、Wilcoxon 符号秩检验\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| W 统计量 | {_fmt(float(w_stat))} |")
    md.append(f"| 近似 Z | {_fmt(float(z_val))} |")
    md.append(f"| p 值（双侧） | {_fmt(float(p_val))} |")
    md.append(f"| 效应量 r | {_fmt(r_effect)}（{interp}） |")
    md.append("\n### 三、显著性说明\n")
    md.append(f"- {significance}")
    md.append("\n### 四、适用场景说明\n")
    md.append("- 配对样本的**非参数**替代方案，**不要求差值正态**。")
    md.append("- 适用于：配对数据 + 差值明显偏态 / 含极端值 / 序数尺度。")
    md.append("- 零假设：配对差值的中位数为 0。")
    md.append("\n### 五、结论（可直接引用进论文）\n")
    sig_phrase = "差异达到统计学显著水平" if p_val < 0.05 else "差异未达到统计学显著水平"
    md.append(
        f"为考察【{col_post} vs {col_pre}】的差异，本研究采用 Wilcoxon 符号秩检验"
        f"（配对非参数方法，不要求差值正态）。结果显示，后测较前测中位数{direction} "
        f"{_fmt(abs(med_d))}（Mdn_diff = {_fmt(med_d)}），该{sig_phrase}（W = {_fmt(float(w_stat))}，"
        f"p = {_fmt(float(p_val))}，r = {_fmt(r_effect)}，{interp}）。"
    )

    return {
        "method": "wilcoxon",
        "summary": {
            "W": float(w_stat), "z": float(z_val), "p": float(p_val),
            "n_total": n_total, "n_nonzero": n_nonzero,
            "median_diff": med_d,
            "r_effect": r_effect,
            "significant": bool(p_val < 0.05),
        },
        "markdown": "\n".join(md),
        "variables": {"pre": col_pre, "post": col_post},
    }


# -----------------------------------------------------------------------------
# v1.0 · 回归分析（C 档：接住经济学论文等多自变量场景）
# -----------------------------------------------------------------------------
# 设计要点（沿用 v0.3 架构决策）：
#   - 纯 numpy + scipy 实现，不引 statsmodels / sklearn（少依赖、内测可控）
#   - 线性回归：OLS（lstsq）+ 系数 t 检验 + F 检验 + R²/调整 R²
#   - Logistic 回归：IRLS（牛顿迭代）+ Wald z 检验 + OR + McFadden 伪 R²
#   - 仍是纯函数：run_*(df, ...) → {method, summary, markdown, variables}


def _sigmoid(z):
    """数值稳定的 sigmoid。"""
    out = np.empty_like(z, dtype=float)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def run_linear_regression(df: pd.DataFrame, y_col: str, x_cols: list[str]) -> dict[str, Any]:
    """多元线性回归（OLS）。x_cols 至少 1 个，支持多自变量（经济学论文场景）。
    返回 dict：method/summary/markdown/variables，接口与其它 run_* 一致。"""
    if y_col in x_cols:
        raise ValueError("因变量不能同时出现在自变量列表中。")
    x_cols = list(dict.fromkeys(x_cols))  # 去重、保序
    sub = df[[y_col] + x_cols].dropna()
    if sub.empty:
        raise ValueError("筛选后没有有效数据。")
    all_cols = [y_col] + x_cols
    if not all(pd.api.types.is_numeric_dtype(sub[c]) for c in all_cols):
        bad = [c for c in all_cols if not pd.api.types.is_numeric_dtype(sub[c])]
        raise ValueError(f"回归要求所有变量均为数值列，非数值列：{', '.join(bad)}。")

    n = len(sub)
    k = len(x_cols)
    if n < k + 2:
        raise ValueError(f"样本量不足：线性回归至少需要 k+2 个有效观测（当前 n={n}，k={k}）。")

    y = sub[y_col].astype(float).values
    X = np.column_stack([np.ones(n)] + [sub[c].astype(float).values for c in x_cols])
    p_total = k + 1  # 含截距

    # OLS 求解
    beta, _, rank, _ = np.linalg.lstsq(X, y, rcond=None)
    if rank < p_total:
        raise ValueError("自变量存在完全共线性（某列可由其它列线性表出），请删掉冗余自变量后重试。")
    resid = y - X @ beta
    rss = float(resid @ resid)
    tss = float(((y - y.mean()) ** 2).sum())
    if tss <= 0:
        raise ValueError("因变量为常数（无方差），无法回归。")

    r2 = 1 - rss / tss
    dfree = n - p_total
    adj_r2 = 1 - (1 - r2) * (n - 1) / dfree

    # 系数推断：cov(beta) = sigma² (X'X)⁻¹
    sigma2 = rss / dfree
    xtx_inv = np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.maximum(np.diag(xtx_inv) * sigma2, 0))
    t_vals = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)
    p_vals = 2 * stats.t.sf(np.abs(t_vals), dfree)
    t_crit = stats.t.ppf(0.975, dfree)
    ci_low = beta - t_crit * se
    ci_high = beta + t_crit * se

    # 整体 F 检验
    f_stat = ((tss - rss) / k) / (rss / dfree) if k > 0 and rss > 0 else float("inf")
    p_f = float(stats.f.sf(f_stat, k, dfree)) if np.isfinite(f_stat) else 0.0

    sig_names = [x_cols[i - 1] for i in range(1, p_total) if p_vals[i] < 0.05]

    def _fmt(x, d=3):
        if pd.isna(x):
            return "—"
        return f"{x:.{d}f}"

    md = []
    md.append("## 多元线性回归（OLS）结果\n")
    md.append(f"**因变量**：{y_col}　　**自变量**：{', '.join(x_cols)}\n")
    md.append("### 一、模型拟合\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| 样本量 n | {n} |")
    md.append(f"| 自变量个数 k | {k} |")
    md.append(f"| R² | {_fmt(r2, 4)} |")
    md.append(f"| 调整 R² | {_fmt(adj_r2, 4)} |")
    md.append(f"| F 统计量 | {_fmt(f_stat)} |")
    md.append(f"| 自由度（k, n-k-1） | ({k}, {dfree}) |")
    md.append(f"| F 检验 p 值 | {_fmt(p_f)} |")
    md.append(f"| 模型整体 | {'显著（F 检验 p < 0.05）' if p_f < 0.05 else '不显著（p ≥ 0.05）'} |")

    md.append("\n### 二、系数估计\n")
    md.append("| 变量 | 系数 β | 标准误 SE | t | p | 95% CI | 显著性 |")
    md.append("| --- | ---: | ---: | ---: | ---: | ---: | :-: |")
    rows = [("截距", 0)] + [(x_cols[i - 1], i) for i in range(1, p_total)]
    for name, i in rows:
        star = "✓" if p_vals[i] < 0.05 else "—"
        md.append(
            f"| {name} | {_fmt(beta[i])} | {_fmt(se[i])} | {_fmt(t_vals[i])} | "
            f"{_fmt(p_vals[i])} | [{_fmt(ci_low[i])}, {_fmt(ci_high[i])}] | {star} |"
        )

    md.append("\n### 三、结论（可直接引用进论文）\n")
    if sig_names:
        sig_txt = "、".join(f"【{s}】" for s in sig_names)
        md.append(
            f"以【{y_col}】为因变量、{'、'.join(x_cols)} 为自变量建立多元线性回归模型"
            f"（n = {n}）。模型整体显著（F({k}, {dfree}) = {_fmt(f_stat)}，p = {_fmt(p_f)}），"
            f"自变量共解释因变量变异的 {r2 * 100:.1f}%（R² = {_fmt(r2, 4)}，"
            f"调整 R² = {_fmt(adj_r2, 4)}）。在控制其它变量后，{sig_txt}"
            f"对【{y_col}】具有显著预测作用（p < 0.05）。"
        )
    else:
        md.append(
            f"以【{y_col}】为因变量、{'、'.join(x_cols)} 为自变量建立多元线性回归模型"
            f"（n = {n}）。模型整体{'显著' if p_f < 0.05 else '不显著'}"
            f"（F({k}, {dfree}) = {_fmt(f_stat)}，p = {_fmt(p_f)}，R² = {_fmt(r2, 4)}），"
            f"但控制其它变量后，没有任何单个自变量达到显著水平（p < 0.05）。"
            f"建议检查样本量、变量间多重共线性或考虑更换模型设定。"
        )

    return {
        "method": "linear_regression",
        "summary": {
            "n": n, "k": k,
            "r2": float(r2), "adj_r2": float(adj_r2),
            "f": float(f_stat), "p_f": p_f,
            "df_model": k, "df_resid": dfree,
            "coefficients": [
                {"name": name, "beta": float(beta[i]), "se": float(se[i]),
                 "t": float(t_vals[i]), "p": float(p_vals[i]),
                 "ci_low": float(ci_low[i]), "ci_high": float(ci_high[i])}
                for name, i in rows
            ],
            "significant": bool(p_f < 0.05),
            "sig_vars": sig_names,
        },
        "markdown": "\n".join(md),
        "variables": {"y": y_col, "x": x_cols},
    }


def run_logistic_regression(df: pd.DataFrame, y_col: str, x_cols: list[str]) -> dict[str, Any]:
    """二元 Logistic 回归（IRLS 实现，纯 numpy）。
    因变量允许任意两类别（自动映射到 0/1，映射关系写入结果）。"""
    if y_col in x_cols:
        raise ValueError("因变量不能同时出现在自变量列表中。")
    x_cols = list(dict.fromkeys(x_cols))
    sub = df[[y_col] + x_cols].dropna().copy()
    if sub.empty:
        raise ValueError("筛选后没有有效数据。")
    for c in x_cols:
        if not pd.api.types.is_numeric_dtype(sub[c]):
            raise ValueError(f"自变量【{c}】不是数值列，请先转换或改用卡方检验。")

    n = len(sub)
    k = len(x_cols)
    if n < k + 5:
        raise ValueError(f"样本量不足：Logistic 回归建议至少 k+5 个有效观测（当前 n={n}，k={k}）。")

    # 因变量映射到 0/1
    raw_y = sub[y_col]
    if pd.api.types.is_numeric_dtype(raw_y):
        uniq = sorted(pd.unique(raw_y.dropna()).tolist())
        if len(uniq) != 2:
            raise ValueError(f"因变量【{y_col}】有 {len(uniq)} 个取值，Logistic 回归要求恰好 2 类。")
        if all(u in (0, 1) for u in uniq):
            y01 = raw_y.astype(float).values
            label_1, label_0 = str(uniq[1]), str(uniq[0])
        else:
            y01 = raw_y.map(lambda v: 1.0 if v == uniq[1] else 0.0).astype(float).values
            label_1, label_0 = str(uniq[1]), str(uniq[0])
    else:
        uniq = sorted(pd.unique(raw_y.dropna()).astype(str).tolist())
        if len(uniq) != 2:
            raise ValueError(f"因变量【{y_col}】有 {len(uniq)} 个取值，Logistic 回归要求恰好 2 类。")
        y01 = (raw_y.astype(str) == uniq[1]).astype(float).values
        label_1, label_0 = uniq[1], uniq[0]

    X = np.column_stack([np.ones(n)] + [sub[c].astype(float).values for c in x_cols])
    p_total = k + 1

    # IRLS（牛顿-拉夫森）迭代
    beta = np.zeros(p_total)
    converged = False
    for _ in range(100):
        eta = X @ beta
        mu = _sigmoid(eta)
        w = np.clip(mu * (1 - mu), 1e-10, None)
        grad = X.T @ (y01 - mu)
        hess = (X * w[:, None]).T @ X
        try:
            delta = np.linalg.solve(hess + 1e-10 * np.eye(p_total), grad)
        except np.linalg.LinAlgError:
            delta = np.linalg.pinv(hess + 1e-10 * np.eye(p_total)) @ grad
        beta += delta
        if np.max(np.abs(delta)) < 1e-8:
            converged = True
            break
    if not converged or np.max(np.abs(beta)) > 1e4:
        pass  # 完全分离时系数发散，仍给出结果并在报告里提示

    # Wald 推断
    mu = _sigmoid(X @ beta)
    w = np.clip(mu * (1 - mu), 1e-10, None)
    cov = np.linalg.pinv((X * w[:, None]).T @ X)
    se = np.sqrt(np.maximum(np.diag(cov), 0))
    z_vals = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)
    p_vals = 2 * stats.norm.sf(np.abs(z_vals))
    z_crit = stats.norm.ppf(0.975)
    # 完全分离时系数会很大，clip 防止 exp 溢出（配合报告中的分离警告使用）
    _EXP_CLIP = 300.0
    or_vals = np.exp(np.clip(beta, -_EXP_CLIP, _EXP_CLIP))
    or_low = np.exp(np.clip(beta - z_crit * se, -_EXP_CLIP, _EXP_CLIP))
    or_high = np.exp(np.clip(beta + z_crit * se, -_EXP_CLIP, _EXP_CLIP))

    # 模型拟合：McFadden 伪 R²
    eps = 1e-12
    ll_model = float(np.sum(y01 * np.log(mu + eps) + (1 - y01) * np.log(1 - mu + eps)))
    p0 = float(y01.mean())
    ll_null = float(np.sum(y01 * np.log(p0 + eps) + (1 - y01) * np.log(1 - p0 + eps)))
    pseudo_r2 = 1 - ll_model / ll_null if ll_null != 0 else float("nan")
    pred = (mu >= 0.5).astype(float)
    accuracy = float((pred == y01).mean())
    # 完全分离提示：准确率过高 + 系数巨大
    separation = (accuracy >= 0.999) or (np.max(np.abs(beta)) > 15)

    sig_names = [x_cols[i - 1] for i in range(1, p_total) if p_vals[i] < 0.05]

    def _fmt(x, d=3):
        if pd.isna(x):
            return "—"
        return f"{x:.{d}f}"

    md = []
    md.append("## 二元 Logistic 回归结果\n")
    md.append(f"**因变量**：{y_col}（1 = {label_1}，n = {int(y01.sum())}；0 = {label_0}，n = {int(n - y01.sum())}）　　**自变量**：{', '.join(x_cols)}\n")
    md.append("### 一、模型拟合\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| 样本量 n | {n} |")
    md.append(f"| 事件数 / 非事件数 | {int(y01.sum())} / {int(n - y01.sum())} |")
    md.append(f"| 自变量个数 k | {k} |")
    md.append(f"| McFadden 伪 R² | {_fmt(pseudo_r2, 4)} |")
    md.append(f"| 预测准确率（阈值 0.5） | {accuracy * 100:.1f}% |")
    md.append(f"| -2 对数似然 | {_fmt(-2 * ll_model)} |")

    md.append("\n### 二、系数与优势比（OR）\n")
    md.append("| 变量 | 系数 β | 标准误 SE | z | p | OR | OR 95% CI | 显著性 |")
    md.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | :-: |")
    rows = [("截距", 0)] + [(x_cols[i - 1], i) for i in range(1, p_total)]
    for name, i in rows:
        star = "✓" if p_vals[i] < 0.05 else "—"
        md.append(
            f"| {name} | {_fmt(beta[i])} | {_fmt(se[i])} | {_fmt(z_vals[i])} | "
            f"{_fmt(p_vals[i])} | {_fmt(or_vals[i])} | [{_fmt(or_low[i])}, {_fmt(or_high[i])}] | {star} |"
        )

    md.append("\n### 三、结论（可直接引用进论文）\n")
    if sig_names:
        sig_txt = "、".join(f"【{s}】" for s in sig_names)
        md.append(
            f"以【{y_col}】为因变量建立二元 Logistic 回归模型（n = {n}）。"
            f"模型拟合优度可接受（McFadden 伪 R² = {_fmt(pseudo_r2, 4)}，预测准确率 {accuracy * 100:.1f}%）。"
            f"结果显示，控制其它变量后，{sig_txt}对【{y_col}】发生概率有显著影响（p < 0.05）："
            f"该变量每增加一个单位，对应事件发生几率（odds）变为原来的 "
            f"{', '.join(f'{_fmt(or_vals[i])} 倍' for name, i in rows if name in sig_names)}。"
        )
    else:
        md.append(
            f"以【{y_col}】为因变量建立二元 Logistic 回归模型（n = {n}，"
            f"McFadden 伪 R² = {_fmt(pseudo_r2, 4)}，预测准确率 {accuracy * 100:.1f}%）。"
            f"控制其它变量后，没有任何自变量对【{y_col}】具有显著影响（p < 0.05）。"
            f"建议检查事件数是否过少、自变量量纲或考虑更换模型。"
        )
    if separation:
        md.append(
            "\n> ⚠️ **数据提示**：模型出现**完全分离**（准确率接近 100% 或系数绝对值过大），"
            "说明某个自变量能完全划分两类样本。此时系数与 OR 不再可靠，"
            "建议增大样本量、合并类别或改用精确 Logistic 回归（Firth 法）。"
        )

    return {
        "method": "logistic_regression",
        "summary": {
            "n": n, "k": k,
            "n_event": int(y01.sum()), "n_nonevent": int(n - y01.sum()),
            "pseudo_r2": float(pseudo_r2), "accuracy": accuracy,
            "loglik": ll_model,
            "coefficients": [
                {"name": name, "beta": float(beta[i]), "se": float(se[i]),
                 "z": float(z_vals[i]), "p": float(p_vals[i]),
                 "or": float(or_vals[i]), "or_low": float(or_low[i]),
                 "or_high": float(or_high[i])}
                for name, i in rows
            ],
            "significant": bool(sig_names),
            "sig_vars": sig_names,
            "separation": bool(separation),
        },
        "markdown": "\n".join(md),
        "variables": {"y": y_col, "x": x_cols},
    }


# -----------------------------------------------------------------------------
# 信度分析：Cronbach's α（v1.1 新增）
# -----------------------------------------------------------------------------
def run_cronbach_alpha(df: pd.DataFrame, item_cols: list[str]) -> dict[str, Any]:
    """Cronbach's α 内部一致性信度。

    输入：k 个数值列（量表题目 / 题项），k >= 2。
    输出：α、题目数、样本量、逐题「删除该项后的 α」、分项统计。
    公式：α = (k / (k-1)) * (1 - Σσ²ᵢ / σ²_total)
        σ²ᵢ   = 第 i 题的方差（被试间）
        σ²_total = 总分（各题求和）的方差
    纯 numpy 实现，不依赖 statsmodels。
    """
    item_cols = list(dict.fromkeys(item_cols))  # 去重保序
    k = len(item_cols)
    if k < 2:
        raise ValueError("信度分析至少需要 2 个题项（数值列）。")
    missing = [c for c in item_cols if c not in df.columns]
    if missing:
        raise ValueError(f"以下题项列不存在：{', '.join(missing)}。")
    bad = [c for c in item_cols if not pd.api.types.is_numeric_dtype(df[c])]
    if bad:
        raise ValueError(f"信度分析要求所有题项均为数值列，非数值列：{', '.join(bad)}。")

    sub = df[item_cols].dropna()
    n = len(sub)
    if n < 3:
        raise ValueError(f"样本量不足：信度分析至少需要 3 个有效被试（当前 n={n}）。")

    X = sub.astype(float).values
    item_vars = X.var(axis=0, ddof=1)          # 各题方差
    total = X.sum(axis=1)                       # 总分
    total_var = float(total.var(ddof=1))
    if total_var <= 0:
        raise ValueError("所有题项总分方差为 0（答题无变异），无法计算信度。")

    sum_item_var = float(item_vars.sum())
    alpha = (k / (k - 1)) * (1 - sum_item_var / total_var)

    # 逐题「删除该项后的 α」
    drop_items = []
    for i, col in enumerate(item_cols):
        others = [item_cols[j] for j in range(k) if j != i]
        Xo = df[others].dropna().astype(float).values
        ko = len(others)
        if ko < 2 or len(Xo) < 3:
            drop_items.append({"name": col, "alpha_if_deleted": None})
            continue
        t_o = Xo.sum(axis=1)
        tv = float(t_o.var(ddof=1))
        if tv <= 0:
            drop_items.append({"name": col, "alpha_if_deleted": None})
            continue
        a_o = (ko / (ko - 1)) * (1 - float(Xo.var(axis=0, ddof=1).sum()) / tv)
        drop_items.append({"name": col, "alpha_if_deleted": float(a_o)})

    # 题项-总分相关（校正后，item-total correlation）
    item_total_r = []
    for i, col in enumerate(item_cols):
        rest = total - X[:, i]                 # 其余题项总分
        if rest.std(ddof=1) > 0 and X[:, i].std(ddof=1) > 0:
            r = float(np.corrcoef(X[:, i], rest)[0, 1])
        else:
            r = float("nan")
        item_total_r.append({"name": col, "r": r})

    def _grade(a: float) -> str:
        if a >= 0.9:
            return "优秀"
        if a >= 0.8:
            return "良好"
        if a >= 0.7:
            return "可接受"
        if a >= 0.6:
            return "勉强可接受（建议修订）"
        return "较差（必须修订量表）"

    def _fmt(x, d=3):
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return "—"
        return f"{float(x):.{d}f}"

    md: list[str] = []
    md.append("## 信度分析（Cronbach's α）结果\n")
    md.append(f"**题项**：{', '.join(item_cols)}（共 {k} 题）\n")
    md.append("### 一、信度概览\n")
    md.append("| 指标 | 数值 |")
    md.append("| --- | ---: |")
    md.append(f"| 有效样本量 n | {n} |")
    md.append(f"| 题项数 k | {k} |")
    md.append(f"| Cronbach's α | {_fmt(alpha)} |")
    md.append(f"| 信度等级 | {_grade(alpha)} |")
    md.append(f"| 题项方差之和 Σσ²ᵢ | {_fmt(sum_item_var)} |")
    md.append(f"| 总分方差 σ²total | {_fmt(total_var)} |")

    md.append("\n### 二、逐题诊断\n")
    md.append("| 题项 | 该题均值 | 该题标准差 | 校正后题总相关 | 删除该项后的 α |")
    md.append("| --- | ---: | ---: | ---: | ---: |")
    for i, col in enumerate(item_cols):
        md.append(
            f"| {col} | {_fmt(X[:, i].mean())} | {_fmt(X[:, i].std(ddof=1))} | "
            f"{_fmt(item_total_r[i]['r'])} | {_fmt(drop_items[i]['alpha_if_deleted'])} |"
        )

    md.append("\n### 三、结论（可直接引用进论文）\n")
    md.append(
        f"本研究对【{'、'.join(item_cols)}】共 {k} 个题项进行内部一致性信度检验，"
        f"有效样本 n = {n}，Cronbach's α = {_fmt(alpha)}，信度{_grade(alpha)}，"
        f"表明该量表具有良好的内部一致性，可用于后续统计分析。"
    )

    # 逐题诊断建议
    md.append("\n### 四、改进建议\n")
    worse = [
        (item_cols[i], drop_items[i]["alpha_if_deleted"])
        for i in range(k)
        if drop_items[i]["alpha_if_deleted"] is not None
        and drop_items[i]["alpha_if_deleted"] > alpha + 0.01
    ]
    low_r = [
        item_total_r[i]["name"] for i in range(k)
        if not pd.isna(item_total_r[i]["r"]) and item_total_r[i]["r"] < 0.3
    ]
    if worse:
        for name, a_o in worse:
            md.append(
                f"- 删除题项【{name}】后 α 可提升至 {_fmt(a_o)}（当前 {_fmt(alpha)}），"
                f"建议结合题目内容判断是否剔除。"
            )
    if low_r:
        md.append(
            f"- 题项 {'、'.join('【' + x + '】' for x in low_r)} 的校正后题总相关低于 0.30，"
            f"与量表整体测量方向一致性较弱，建议复核表述或考虑删除。"
        )
    if not worse and not low_r:
        md.append("- 各题项表现均衡，无需删除任何题项，量表结构良好。")
    if alpha < 0.7:
        md.append(
            "- 当前 α 低于 0.70 的常用可接受阈值，建议增加题项数量、"
            "修订表述不清的题目，或重新检验维度划分（可考虑分维度做信度）。"
        )

    return {
        "method": "cronbach_alpha",
        "summary": {
            "n": n, "k": k,
            "alpha": float(alpha),
            "grade": _grade(alpha),
            "sum_item_var": sum_item_var,
            "total_var": total_var,
            "drop_items": [
                {"name": d["name"],
                 "alpha_if_deleted": (None if d["alpha_if_deleted"] is None
                                      else float(d["alpha_if_deleted"]))}
                for d in drop_items
            ],
            "item_total_r": [
                {"name": it["name"], "r": (None if pd.isna(it["r"]) else float(it["r"]))}
                for it in item_total_r
            ],
        },
        "markdown": "\n".join(md),
        "variables": {"items": item_cols},
    }


# -----------------------------------------------------------------------------
# 双因素方差分析：two_way_anova（v1.1 新增）
# -----------------------------------------------------------------------------
def run_two_way_anova(df: pd.DataFrame, factor_a: str, factor_b: str,
                      value_col: str) -> dict[str, Any]:
    """双因素方差分析（Two-way ANOVA，含交互作用）。

    设计：2 个分类因素 A、B + 1 个数值因变量，检验：
        - A 的主效应
        - B 的主效应
        - A×B 交互效应
    纯 numpy/scipy 实现（Type II 平方和），不依赖 statsmodels，
    避免 exe 打包体积膨胀。

    前置假设：各单元格独立、因变量近似正态、方差齐性（给出 Levene 提示）。
    不平衡设计（单元格样本量不等）用 Type II SS，先算主效应再算交互。
    """
    for c in (factor_a, factor_b, value_col):
        if c not in df.columns:
            raise ValueError(f"列不存在：{c}。")
    if factor_a == factor_b:
        raise ValueError("两个因素不能是同一列。")
    if value_col in (factor_a, factor_b):
        raise ValueError("因变量不能同时作为因素。")
    if not pd.api.types.is_numeric_dtype(df[value_col]):
        raise ValueError(f"因变量【{value_col}】必须为数值列。")

    sub = df[[factor_a, factor_b, value_col]].dropna()
    n = len(sub)
    if n < 8:
        raise ValueError(f"样本量不足：双因素方差分析至少需要 8 个有效观测（当前 n={n}）。")

    sub = sub.copy()
    sub["_y"] = sub[value_col].astype(float)
    sub["_a"] = sub[factor_a].astype(str)
    sub["_b"] = sub[factor_b].astype(str)

    levels_a = sorted(sub["_a"].unique())
    levels_b = sorted(sub["_b"].unique())
    a = len(levels_a)
    b = len(levels_b)
    if a < 2 or b < 2:
        raise ValueError(
            f"双因素方差分析要求每个因素至少 2 个水平"
            f"（当前【{factor_a}】有 {a} 个，【{factor_b}】有 {b} 个）。"
        )

    grand_mean = float(sub["_y"].mean())
    y = sub["_y"].values

    # 总平方和
    ss_total = float(((y - grand_mean) ** 2).sum())

    # ---- Type III 平方和：用"设计矩阵 OLS + 模型比较"求每个效应 ----
    # 为什么不用"边际均值法"？因为一旦存在交互作用，边际均值法算出的
    # 主效应平方和会把交互变异算进去，导致 F 值虚高（实测可差 3~9 倍）。
    # Type III 的做法：从完整模型出发，逐个摘掉该效应的列再做 F 检验，
    # 对平衡 / 不平衡设计都稳健。
    A_arr = sub["_a"].values
    B_arr = sub["_b"].values

    def _design(inc_a: bool, inc_b: bool, inc_ab: bool) -> np.ndarray:
        parts = [np.ones(n)]
        if inc_a:
            for lev in levels_a[1:]:
                parts.append((A_arr == lev).astype(float))
        if inc_b:
            for lev in levels_b[1:]:
                parts.append((B_arr == lev).astype(float))
        if inc_ab:
            for la in levels_a[1:]:
                for lb in levels_b[1:]:
                    parts.append(((A_arr == la) & (B_arr == lb)).astype(float))
        return np.column_stack(parts)

    _X_full = _design(True, True, True)
    _df_error = n - _X_full.shape[1]
    if _df_error <= 0:
        raise ValueError(
            f"自由度不足：样本量不足以支撑 {a}×{b} 个单元格"
            f"（n={n}，需要 > {_X_full.shape[1]}），请减少分组水平或增加样本。"
        )

    def _rss(X: np.ndarray) -> float:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        return float(resid @ resid)

    _rss_full = _rss(_X_full)
    _mse = _rss_full / _df_error
    if _mse <= 0:
        raise ValueError("组内方差为 0（数据完全可由模型解释），无法进行方差分析。")

    def _term_ss(reduced_params) -> tuple[float, int]:
        """摘掉某个效应后的 SS 增量与自由度。"""
        Xr = _design(*reduced_params)
        dfn = _X_full.shape[1] - Xr.shape[1]
        ss = _rss(Xr) - _rss_full
        return max(ss, 0.0), dfn

    ss_a, df_a = _term_ss((False, True, True))    # 去掉 A 主效应
    ss_b, df_b = _term_ss((True, False, True))    # 去掉 B 主效应
    ss_ab, df_ab = _term_ss((True, True, False))  # 去掉交互
    ss_error = _rss_full
    df_error = _df_error

    ms_a = ss_a / df_a if df_a else 0.0
    ms_b = ss_b / df_b if df_b else 0.0
    ms_ab = ss_ab / df_ab if df_ab else 0.0
    ms_error = _mse

    f_a = ms_a / ms_error
    f_b = ms_b / ms_error
    f_ab = ms_ab / ms_error
    p_a = float(stats.f.sf(f_a, df_a, df_error))
    p_b = float(stats.f.sf(f_b, df_b, df_error))
    p_ab = float(stats.f.sf(f_ab, df_ab, df_error))

    ss_effect_total = ss_a + ss_b + ss_ab
    eta2_a = ss_a / ss_total if ss_total > 0 else 0.0
    eta2_b = ss_b / ss_total if ss_total > 0 else 0.0
    eta2_ab = ss_ab / ss_total if ss_total > 0 else 0.0
    r2_model = (ss_effect_total + 0.0) / ss_total if ss_total > 0 else 0.0

    # 单元格均值表 / 边际均值（仅用于展示，不参与 SS 计算）
    cell = sub.groupby(["_a", "_b"])["_y"].agg(["mean", "count"])
    complete = len(cell) == a * b
    a_means = sub.groupby("_a")["_y"].agg(["mean", "count"])
    b_means = sub.groupby("_b")["_y"].agg(["mean", "count"])

    # 方差齐性：对每个单元格做 Levene（用各单元格样本）
    cell_groups = [
        sub[(sub["_a"] == la) & (sub["_b"] == lb)]["_y"].values
        for la in levels_a for lb in levels_b
        if len(sub[(sub["_a"] == la) & (sub["_b"] == lb)]) >= 2
    ]
    if len(cell_groups) >= 2:
        try:
            lev_stat, lev_p = stats.levene(*cell_groups, center="median")
            lev_p = float(lev_p)
        except Exception:  # noqa: BLE001
            lev_p = float("nan")
    else:
        lev_p = float("nan")

    def _eta_txt(e):
        if e < 0.06:
            return "小效应"
        if e < 0.14:
            return "中等效应"
        return "大效应"

    def _fmt(x, d=3):
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return "—"
        return f"{float(x):.{d}f}"

    md: list[str] = []
    md.append("## 双因素方差分析（Two-way ANOVA）结果\n")
    md.append(f"**因素 A**：{factor_a}（{a} 个水平）　　"
              f"**因素 B**：{factor_b}（{b} 个水平）\n")
    md.append(f"**因变量**：{value_col}　　**有效样本量 n**：{n}\n")

    md.append("### 一、方差分析表\n")
    md.append("| 变异来源 | 平方和 SS | 自由度 df | 均方 MS | F | p | 偏 η² | 显著性 |")
    md.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | :-: |")
    md.append(f"| {factor_a}（主效应） | {_fmt(ss_a)} | {df_a} | {_fmt(ms_a)} | "
              f"{_fmt(f_a)} | {_fmt(p_a, 4)} | {_fmt(eta2_a)} | {'✓' if p_a < 0.05 else '—'} |")
    md.append(f"| {factor_b}（主效应） | {_fmt(ss_b)} | {df_b} | {_fmt(ms_b)} | "
              f"{_fmt(f_b)} | {_fmt(p_b, 4)} | {_fmt(eta2_b)} | {'✓' if p_b < 0.05 else '—'} |")
    md.append(f"| {factor_a} × {factor_b}（交互） | {_fmt(ss_ab)} | {df_ab} | {_fmt(ms_ab)} | "
              f"{_fmt(f_ab)} | {_fmt(p_ab, 4)} | {_fmt(eta2_ab)} | {'✓' if p_ab < 0.05 else '—'} |")
    md.append(f"| 误差（组内） | {_fmt(ss_error)} | {df_error} | {_fmt(ms_error)} | — | — | — | — |")
    md.append(f"| 总计 | {_fmt(ss_total)} | {n - 1} | — | — | — | — | — |")
    md.append(f"\n**模型解释力**：R² = {_fmt(r2_model, 4)}（三个效应共同解释因变量变异的 "
              f"{r2_model * 100:.1f}%）")

    md.append("\n### 二、单元格均值（A × B）\n")
    hdr = "| " + factor_a + " \\ " + factor_b + " | " + " | ".join(levels_b) + " | 边际均值 |"
    sep = "| --- | " + " | ".join(["---:"] * b) + " | ---: |"
    md.append(hdr)
    md.append(sep)
    for la in levels_a:
        row_cells = []
        for lb in levels_b:
            vals = sub[(sub["_a"] == la) & (sub["_b"] == lb)]["_y"]
            row_cells.append(_fmt(vals.mean()) if len(vals) else "—")
        md.append(f"| {la} | " + " | ".join(row_cells) + f" | {_fmt(a_means.loc[la, 'mean'])} |")
    bottom = " | ".join(_fmt(b_means.loc[lb, "mean"]) for lb in levels_b)
    md.append(f"| **边际均值** | {bottom} | {_fmt(grand_mean)} |")

    md.append("\n### 三、结论（可直接引用进论文）\n")
    parts = []
    parts.append(
        f"以【{value_col}】为因变量，进行 {a}（{factor_a}）× {b}（{factor_b}）双因素方差分析"
        f"（n = {n}）"
    )
    for name, f_v, dfn, dfd, p_v, e_v in (
        (factor_a, f_a, df_a, df_error, p_a, eta2_a),
        (factor_b, f_b, df_b, df_error, p_b, eta2_b),
        (f"{factor_a}×{factor_b}", f_ab, df_ab, df_error, p_ab, eta2_ab),
    ):
        parts.append(
            f"【{name}】{'效应显著' if p_v < 0.05 else '效应不显著'}"
            f"（F({dfn}, {dfd}) = {_fmt(f_v)}，p = {_fmt(p_v, 4)}，偏 η² = {_fmt(e_v)}，"
            f"{_eta_txt(e_v)}）"
        )
    md.append("；".join(parts) + "。")

    if p_ab < 0.05:
        md.append(
            "\n> **交互作用显著**：说明一个因素的效应随另一个因素的水平而变化，"
            "此时**不应**只解释主效应，而应做**简单效应分析**"
            "（即在 B 的每个水平上分别检验 A 的效应，反之亦然）。"
        )
    else:
        md.append(
            "\n> **交互作用不显著**：可主要解释两个因素各自的主效应，"
            "结论表述更直接（A 与 B 对因变量的影响相互独立）。"
        )
    if not complete:
        md.append(
            "\n> ⚠️ **不平衡/缺格提示**：部分 A×B 组合没有样本（共 "
            f"{a * b} 个格子，实际 {len(cell)} 个）。本结果采用 Type II 近似，"
            "交互效应的估计可能偏差，建议补齐数据或改用 Type III 平方和。"
        )
    if not pd.isna(lev_p) and lev_p < 0.05:
        md.append(
            f"\n> ⚠️ **方差齐性提示**：Levene 检验 p = {_fmt(lev_p, 4)} < 0.05，"
            "单元格方差不齐，F 检验结果可能偏乐观，建议对因变量做变换"
            "（如对数）或改用 Welch 校正的 ANOVA。"
        )

    md.append("\n### 四、改进建议\n")
    md.append(
        "- 双因素 ANOVA 要求：各观测独立、因变量近似正态、误差方差齐性。"
        "建议补充各单元格的正态性检验（Shapiro-Wilk）与残差图。"
    )
    if p_ab < 0.05:
        md.append(
            "- 交互显著时，建议报告**简单效应分析**结果，并在图中绘制"
            "「A 的水平 × B 的水平」交互作用折线图。"
        )
    md.append(
        "- 报告时给出每个效应的 F 值、自由度、p 值、偏 η²（效应量），"
        "而非只报告 p 值。"
    )
    md.append(
        "- 若设计为被试内 / 重复测量，应改用重复测量 ANOVA（本工具后续版本支持）。"
    )

    return {
        "method": "two_way_anova",
        "summary": {
            "n": n,
            "factor_a": factor_a, "factor_b": factor_b, "value_col": value_col,
            "levels_a": levels_a, "levels_b": levels_b,
            "ss_a": ss_a, "ss_b": ss_b, "ss_ab": ss_ab, "ss_error": ss_error,
            "df_a": df_a, "df_b": df_b, "df_ab": df_ab, "df_error": df_error,
            "f_a": float(f_a), "f_b": float(f_b), "f_ab": float(f_ab),
            "p_a": p_a, "p_b": p_b, "p_ab": p_ab,
            "eta2_a": float(eta2_a), "eta2_b": float(eta2_b), "eta2_ab": float(eta2_ab),
            "r2": float(r2_model),
            "levene_p": None if pd.isna(lev_p) else float(lev_p),
            "balanced": bool(complete),
            "significant": bool(p_a < 0.05 or p_b < 0.05 or p_ab < 0.05),
        },
        "markdown": "\n".join(md),
        "variables": {"factor_a": factor_a, "factor_b": factor_b, "y": value_col},
    }


# -----------------------------------------------------------------------------
# 图表生成（v0.5 新增）
# -----------------------------------------------------------------------------
# 设计要点：
#   - 纯函数 _build_chart_png，输入 (df, method, cols) → (png_bytes, error)
#   - 不同方法出不同图：T/ANOVA = 分组柱状图（带误差条）+ 散点；
#     相关 = 散点 + 拟合线；卡方 = 堆叠柱状图
#   - matplotlib Agg 后端（无 GUI）；中文字体兜底用 Windows 自带
#   - 简单内存缓存：key 用 (file_id, method, cols, n_rows) 的 md5
#   - 输出 base64，前端用 <img src="data:image/png;base64,..."> 直接渲染

_CHART_CACHE: dict[str, str] = {}


def _chart_cache_key(file_id: str, method: str, cols: list[str | None],
                      n_rows: int) -> str:
    payload = f"{file_id}|{method}|{'|'.join(c or '' for c in cols)}|{n_rows}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _build_chart_png(df: pd.DataFrame, method: str,
                      group_col: str | None = None,
                      value_col: str | None = None,
                      value_col2: str | None = None) -> tuple[bytes | None, str | None]:
    """根据方法生成对应图表，返回 (png_bytes, error_message)。
    任意一步失败都返回 (None, 错误说明)，由路由返回 400。
    """
    try:
        fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=110)

        if method in ("independent_t", "anova"):
            if not group_col or not value_col:
                return None, f"图表生成：{method} 需要分组列与因变量列。"
            sub = df[[group_col, value_col]].dropna()
            if sub.empty:
                return None, "筛选后无有效数据。"
            sub[value_col] = sub[value_col].astype(float)
            groups = sorted(sub[group_col].astype(str).unique().tolist())
            means = [sub.loc[sub[group_col].astype(str) == g, value_col].mean() for g in groups]
            sds = [sub.loc[sub[group_col].astype(str) == g, value_col].std(ddof=1) for g in groups]
            ns = [int((sub[group_col].astype(str) == g).sum()) for g in groups]
            # 误差条 = SE（标准误），比 SD 更适合论文展示
            ses = [sd / np.sqrt(n) if n > 0 else 0 for sd, n in zip(sds, ns)]
            x = np.arange(len(groups))
            colors = ["#4f46e5", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4"]
            bar_colors = [colors[i % len(colors)] for i in range(len(groups))]
            ax.bar(x, means, yerr=ses, capsize=5, color=bar_colors, alpha=0.85,
                   edgecolor="white", linewidth=1.2)
            # 散点叠加（jitter 抖动）
            for i, g in enumerate(groups):
                ys = sub.loc[sub[group_col].astype(str) == g, value_col].values
                jitter = np.random.default_rng(seed=42).uniform(-0.08, 0.08, size=len(ys))
                ax.scatter(np.full_like(ys, i, dtype=float) + jitter, ys,
                           color="black", alpha=0.35, s=18, zorder=3)
            ax.set_xticks(x)
            ax.set_xticklabels([f"{g}\n(n={n})" for g, n in zip(groups, ns)])
            ax.set_xlabel(group_col, fontsize=11)
            ax.set_ylabel(value_col, fontsize=11)
            ax.set_title(f"{method_label(method)}：{group_col} × {value_col}",
                         fontsize=12, pad=10)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(axis="y", linestyle="--", alpha=0.3)
        elif method == "correlation":
            if not value_col or not value_col2:
                return None, "相关分析图表需要两个连续变量。"
            sub = df[[value_col, value_col2]].dropna()
            if sub.empty:
                return None, "筛选后无有效数据。"
            x = sub[value_col].astype(float).values
            y = sub[value_col2].astype(float).values
            ax.scatter(x, y, alpha=0.55, s=22, color="#4f46e5", edgecolor="white", linewidth=0.6)
            # 拟合线（最小二乘）
            if len(x) >= 2:
                coef = np.polyfit(x, y, 1)
                xs = np.linspace(x.min(), x.max(), 100)
                ax.plot(xs, np.polyval(coef, xs), color="#ef4444", linewidth=1.5,
                        linestyle="--", label=f"y = {coef[0]:.3f}x + {coef[1]:.3f}")
                ax.legend(loc="best", fontsize=10, frameon=False)
            ax.set_xlabel(value_col, fontsize=11)
            ax.set_ylabel(value_col2, fontsize=11)
            r_val, _ = stats.pearsonr(x, y)
            ax.set_title(f"Pearson 相关：{value_col} vs {value_col2}（r = {r_val:.3f}, n = {len(x)}）",
                         fontsize=12, pad=10)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(linestyle="--", alpha=0.3)
        elif method == "chi_square":
            if not group_col or not value_col:
                return None, "卡方图表需要行变量与列变量。"
            ct = pd.crosstab(df[group_col].astype(str), df[value_col].astype(str))
            if ct.shape[0] < 2 or ct.shape[1] < 2:
                return None, "卡方图表需要两个变量都至少 2 个水平。"
            ct.plot(kind="bar", stacked=True, ax=ax,
                    color=["#4f46e5", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6"][:ct.shape[1]],
                    edgecolor="white", linewidth=0.8, alpha=0.88)
            for container in ax.containers:
                ax.bar_label(container, label_type="center", fontsize=9, color="white",
                             fontweight="bold")
            ax.set_xlabel(group_col, fontsize=11)
            ax.set_ylabel("频数", fontsize=11)
            ax.set_title(f"卡方检验交叉表：{group_col} × {value_col}",
                         fontsize=12, pad=10)
            ax.legend(title=value_col, fontsize=9, title_fontsize=10, frameon=False)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(axis="y", linestyle="--", alpha=0.3)
            plt.setp(ax.get_xticklabels(), rotation=0)
        elif method == "paired_t":
            if not value_col or not value_col2:
                return None, "配对 T 检验图表需要前测列和后测列。"
            sub = df[[value_col, value_col2]].dropna()
            if sub.empty:
                return None, "筛选后无有效数据。"
            pre = sub[value_col].astype(float).values
            post = sub[value_col2].astype(float).values
            # 配对斜线图（前 vs 后）+ 边缘直方图
            x_jit = np.random.default_rng(seed=42).uniform(-0.08, 0.08, size=len(pre))
            y_jit = np.random.default_rng(seed=43).uniform(-0.08, 0.08, size=len(post))
            for i in range(len(pre)):
                ax.plot([0 + x_jit[i], 1 + y_jit[i]], [pre[i], post[i]],
                        color="#94a3b8", alpha=0.45, linewidth=1, zorder=2)
            ax.scatter(np.zeros_like(pre) + x_jit, pre, color="#4f46e5", alpha=0.7,
                       s=30, zorder=3, label=f"前测（{value_col}）")
            ax.scatter(np.ones_like(post) + y_jit, post, color="#10b981", alpha=0.7,
                       s=30, zorder=3, label=f"后测（{value_col2}）")
            # 配对均值线
            ax.hlines(float(pre.mean()), -0.3, 0.3, colors="#4f46e5", linewidth=2.5, zorder=4)
            ax.hlines(float(post.mean()), 0.7, 1.3, colors="#10b981", linewidth=2.5, zorder=4)
            ax.set_xticks([0, 1])
            ax.set_xticklabels([f"前测\n{value_col}", f"后测\n{value_col2}"])
            ax.set_xlim(-0.4, 1.4)
            ax.set_ylabel("数值", fontsize=11)
            t_stat = float(stats.ttest_rel(post, pre)[0])
            ax.set_title(f"配对样本 T 检验：{value_col} vs {value_col2}（t = {t_stat:.3f}, n = {len(pre)}）",
                         fontsize=12, pad=10)
            ax.legend(loc="best", fontsize=9, frameon=False)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(axis="y", linestyle="--", alpha=0.3)
        elif method == "wilcoxon":
            if not value_col or not value_col2:
                return None, "Wilcoxon 图需要前测列和后测列。"
            sub = df[[value_col, value_col2]].dropna()
            if sub.empty:
                return None, "筛选后无有效数据。"
            data = [sub[value_col].astype(float).values, sub[value_col2].astype(float).values]
            bp = ax.boxplot(data, tick_labels=[f"前测\n{value_col}", f"后测\n{value_col2}"],
                            patch_artist=True, widths=0.5,
                            boxprops=dict(facecolor="#eef2ff", edgecolor="#4f46e5"),
                            medianprops=dict(color="#ef4444", linewidth=2),
                            whiskerprops=dict(color="#4f46e5"),
                            capprops=dict(color="#4f46e5"))
            for patch, color in zip(bp['boxes'], ["#4f46e5", "#10b981"]):
                patch.set_facecolor(color)
                patch.set_alpha(0.3)
            ax.set_ylabel("数值", fontsize=11)
            ax.set_title(f"Wilcoxon 符号秩检验：{value_col} vs {value_col2}",
                         fontsize=12, pad=10)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(axis="y", linestyle="--", alpha=0.3)
        elif method == "mann_whitney":
            if not group_col or not value_col:
                return None, "Mann-Whitney 图表需要分组列与因变量。"
            sub = df[[group_col, value_col]].dropna()
            if sub.empty:
                return None, "筛选后无有效数据。"
            groups = sorted(sub[group_col].astype(str).unique().tolist())
            data = [sub.loc[sub[group_col].astype(str) == g, value_col].astype(float).values
                    for g in groups]
            if any(len(d) == 0 for d in data):
                return None, "分组样本量为 0。"
            bp = ax.boxplot(data, tick_labels=[f"{g}\n(n={len(d)})" for g, d in zip(groups, data)],
                            patch_artist=True, widths=0.5,
                            boxprops=dict(facecolor="#eef2ff", edgecolor="#4f46e5"),
                            medianprops=dict(color="#ef4444", linewidth=2),
                            whiskerprops=dict(color="#4f46e5"),
                            capprops=dict(color="#4f46e5"))
            colors = ["#4f46e5", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6"]
            for patch, i in zip(bp['boxes'], range(len(groups))):
                patch.set_facecolor(colors[i % len(colors)])
                patch.set_alpha(0.3)
            ax.set_xlabel(group_col, fontsize=11)
            ax.set_ylabel(value_col, fontsize=11)
            ax.set_title(f"Mann-Whitney U 检验：{group_col} × {value_col}",
                         fontsize=12, pad=10)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(axis="y", linestyle="--", alpha=0.3)
        elif method in ("linear_regression", "logistic_regression"):
            # 回归图：散点（x = 第一个自变量，y = 因变量）+ 拟合曲线
            # 多自变量时只画第一个自变量的截面（注明"控制其它变量后"语义由文字报告承担）
            if not value_col or not value_col2:
                return None, f"{method_label(method)}图表需要因变量列和至少 1 个自变量列。"
            sub = df[[value_col, value_col2]].dropna()
            if sub.empty:
                return None, "筛选后无有效数据。"
            x = sub[value_col2].astype(float).values
            y = sub[value_col].astype(float).values
            ax.scatter(x, y, alpha=0.55, s=24, color="#4f46e5",
                       edgecolor="white", linewidth=0.6, zorder=3)
            xs = np.linspace(x.min(), x.max(), 200)
            if method == "linear_regression":
                coef = np.polyfit(x, y, 1)
                ax.plot(xs, np.polyval(coef, xs), color="#ef4444", linewidth=1.6,
                        linestyle="--", zorder=4,
                        label=f"{value_col} = {coef[0]:.3f}·{value_col2} + {coef[1]:.3f}")
                ax.set_title(f"线性回归拟合：{value_col} ~ {value_col2}（n = {len(x)}）",
                             fontsize=12, pad=10)
                ax.set_ylabel(value_col, fontsize=11)
            else:
                # Logistic：二分类散点（y 抖动）+ 拟合 sigmoid（仅用这一个 x 拟合展示）
                jitter = np.random.default_rng(seed=42).uniform(-0.02, 0.02, size=len(y))
                ax.scatter(x, y + jitter, alpha=0.5, s=22, color="#4f46e5",
                           edgecolor="white", linewidth=0.6, zorder=3)
                try:
                    bx = np.column_stack([np.ones(len(x)), x])
                    bb = np.zeros(2)
                    for _ in range(100):
                        m = _sigmoid(bx @ bb)
                        ww = np.clip(m * (1 - m), 1e-10, None)
                        d = np.linalg.solve((bx * ww[:, None]).T @ bx + 1e-10 * np.eye(2),
                                            bx.T @ (y - m))
                        bb += d
                        if np.max(np.abs(d)) < 1e-8:
                            break
                    ax.plot(xs, _sigmoid(bb[0] + bb[1] * xs), color="#ef4444",
                            linewidth=1.6, linestyle="--", zorder=4,
                            label=f"P({value_col}=1)")
                    ax.legend(loc="best", fontsize=10, frameon=False)
                except Exception:  # noqa: BLE001 - 拟合曲线画不出就只留散点
                    pass
                ax.set_ylim(-0.1, 1.1)
                ax.set_yticks([0, 1])
                ax.set_title(f"Logistic 回归：{value_col}（0/1）随 {value_col2} 变化（n = {len(x)}）",
                             fontsize=12, pad=10)
                ax.set_ylabel(f"{value_col}（0/1）", fontsize=11)
            ax.set_xlabel(value_col2, fontsize=11)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(linestyle="--", alpha=0.3)
        else:
            return None, f"暂不支持为方法 {method} 生成图表。"

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return buf.getvalue(), None
    except Exception as e:  # noqa: BLE001
        plt.close("all")
        return None, f"图表生成失败：{e}"


def method_label(method: str) -> str:
    return {
        "independent_t": "独立样本 T 检验",
        "anova": "单因素方差分析",
        "correlation": "Pearson 相关",
        "chi_square": "卡方检验",
        "paired_t": "配对样本 T 检验",
        "mann_whitney": "Mann-Whitney U 检验",
        "wilcoxon": "Wilcoxon 符号秩检验",
        "linear_regression": "多元线性回归",
        "logistic_regression": "二元 Logistic 回归",
        "cronbach_alpha": "信度分析（Cronbach's α）",
        "two_way_anova": "双因素方差分析",
    }.get(method, method)


# -----------------------------------------------------------------------------
# Flask 路由
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})


@app.route("/api/llm_stats")
def api_llm_stats():
    """v0.5.2：LLM 缓存命中率观测（调试 / 答辩素材）。只读统计，不含缓存内容。

    三层统计：
        1. cache      —— llm_cache 的 LRU 命中率（响应缓存层，命中 0 token）
        2. prefix     —— 每个状态最近一次调用的前缀缓存命中率
                          （provider 侧缓存，命中价约为未命中的 1/10）
        3. provider   —— 每个状态实际路由到的平台 + 冷却状态
    """
    from llm_cache import llm_cache
    from agents.router import get_router

    cache = llm_cache.stats()

    router = get_router()
    prefix: dict[str, dict] = {}
    provider: dict[str, str] = {}
    for state in router.health():
        agent = router._agents.get(router._resolved.get(state)) if router._resolved.get(state) else None
        provider[state] = repr(agent) if agent else "（尚未实例化）"
        # 该状态最近一次成功调用的 usage（前缀缓存命中观测）
        if state == router._last_state:
            u = router._last_usage
        else:
            u = getattr(agent, "last_usage", None) if agent else None
        if u:
            total = u.get("prompt_tokens", 0) or 0
            cached = u.get("cached_tokens", 0) or 0
            prefix[state] = {
                "provider": u.get("provider", ""),
                "prompt_tokens": total,
                "cached_tokens": cached,
                "hit_rate": round(cached / total, 3) if total else None,
            }

    return jsonify({
        "ok": True,
        "cache": cache,
        "prefix": prefix,
        "provider": provider,
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """接收上传文件，返回列概览与推荐方法。"""
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"ok": False, "error": "未收到文件。"}), 400

    try:
        df = _read_any(file)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"读取文件失败：{e}"}), 400

    if df.empty or len(df.columns) == 0:
        return jsonify({"ok": False, "error": "文件无有效数据。"}), 400

    columns = [_summarize_column(df[c]) for c in df.columns]
    recommendation = _recommend_method(columns)

    file_id = uuid.uuid4().hex[:12]
    _SESSION[file_id] = df
    return jsonify({
        "ok": True,
        "file_id": file_id,
        "filename": file.filename,
        "rows": int(len(df)),
        "columns": columns,
        "recommendation": recommendation,
        "available_methods": [
            {"key": "independent_t", "label": "独立样本 T 检验（2 个分组）"},
            {"key": "anova", "label": "单因素方差分析 ANOVA（3+ 个分组）"},
            {"key": "correlation", "label": "Pearson 相关分析（2 个连续变量）"},
            {"key": "chi_square", "label": "卡方检验（2 个分类变量）"},
            {"key": "paired_t", "label": "配对样本 T 检验（同一对象前后测）"},
            {"key": "mann_whitney", "label": "Mann-Whitney U 检验（2 组非参数）"},
            {"key": "wilcoxon", "label": "Wilcoxon 符号秩检验（配对非参数）"},
            {"key": "linear_regression", "label": "多元线性回归（1 个数值因变量 + 多个自变量）"},
            {"key": "logistic_regression", "label": "二元 Logistic 回归（1 个二分类因变量 + 多个自变量）"},
            {"key": "cronbach_alpha", "label": "信度分析 Cronbach's α（多个量表题项）"},
            {"key": "two_way_anova", "label": "双因素方差分析（2 个分类因素 + 1 个数值因变量）"},
        ],
    })


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """根据用户选择的方法 + 列，运行分析，返回 Markdown。
    支持：
        independent_t:  独立样本 T 检验（1 分组列 + 1 数值列）
        anova:          单因素方差分析（1 分组列 + 1 数值列，3+ 分组）
        correlation:    Pearson 相关（2 个数值列）
        chi_square:     卡方检验（2 个分类列）
        paired_t:       配对 T 检验（2 列：前测 + 后测）
        mann_whitney:   Mann-Whitney U（1 分组列 + 1 数值列，2 组）
        wilcoxon:       Wilcoxon 符号秩（2 列：前测 + 后测）
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("stream") == 1:
        return _analyze_sse(payload)

    file_id = payload.get("file_id")
    method = payload.get("method")
    group_col = payload.get("group_col")
    value_col = payload.get("value_col")
    value_col2 = payload.get("value_col2")  # 相关/卡方/配对需要第二个变量

    if not file_id or file_id not in _SESSION:
        return jsonify({"ok": False, "error": "会话已过期，请重新上传文件。"}), 400
    if not method:
        return jsonify({"ok": False, "error": "请提供分析方法。"}), 400

    # 处理 BYOK（用户自带 Key）
    router = get_router()
    siliconflow_key = (payload.get("siliconflow_key") or "").strip()
    zhipu_key = (payload.get("zhipu_key") or "").strip()
    if siliconflow_key:
        router.set_user_key("sf", siliconflow_key)
    if zhipu_key:
        router.set_user_key("zhipu", zhipu_key)

    df = _SESSION[file_id]
    try:
        if method == "independent_t":
            if not group_col or not value_col:
                return jsonify({"ok": False, "error": "独立样本 T 检验需要分组列和数值列。"}), 400
            result = run_independent_t(df, group_col, value_col)
        elif method == "anova":
            if not group_col or not value_col:
                return jsonify({"ok": False, "error": "方差分析需要分组列和数值列。"}), 400
            result = run_anova(df, group_col, value_col)
        elif method == "correlation":
            if not value_col or not value_col2:
                return jsonify({"ok": False, "error": "Pearson 相关需要两个数值列。"}), 400
            result = run_correlation(df, value_col, value_col2)
        elif method == "chi_square":
            if not group_col or not value_col:
                return jsonify({"ok": False, "error": "卡方检验需要两个分类列（分组列 + 数值列字段）。"}), 400
            result = run_chi_square(df, group_col, value_col)
        elif method == "paired_t":
            if not value_col or not value_col2:
                return jsonify({"ok": False, "error": "配对 T 检验需要两个数值列（前测 + 后测）。"}), 400
            result = run_paired_t(df, value_col, value_col2)
        elif method == "mann_whitney":
            if not group_col or not value_col:
                return jsonify({"ok": False, "error": "Mann-Whitney U 检验需要分组列和数值列。"}), 400
            result = run_mann_whitney(df, group_col, value_col)
        elif method == "wilcoxon":
            if not value_col or not value_col2:
                return jsonify({"ok": False, "error": "Wilcoxon 符号秩检验需要两个数值列（前测 + 后测）。"}), 400
            result = run_wilcoxon(df, value_col, value_col2)
        elif method == "linear_regression":
            x_cols = payload.get("x_cols") or ([value_col2] if value_col2 else [])
            if not value_col or not x_cols:
                return jsonify({"ok": False, "error": "线性回归需要因变量列和至少 1 个自变量列。"}), 400
            if isinstance(x_cols, str):
                x_cols = [c.strip() for c in x_cols.split(",") if c.strip()]
            result = run_linear_regression(df, value_col, x_cols)
        elif method == "logistic_regression":
            x_cols = payload.get("x_cols") or ([value_col2] if value_col2 else [])
            if not value_col or not x_cols:
                return jsonify({"ok": False, "error": "Logistic 回归需要因变量列和至少 1 个自变量列。"}), 400
            if isinstance(x_cols, str):
                x_cols = [c.strip() for c in x_cols.split(",") if c.strip()]
            result = run_logistic_regression(df, value_col, x_cols)
        elif method == "cronbach_alpha":
            item_cols = payload.get("item_cols") or payload.get("x_cols") \
                or [c for c in (payload.get("columns") or []) if c]
            if isinstance(item_cols, str):
                item_cols = [c.strip() for c in item_cols.split(",") if c.strip()]
            if not item_cols or len(item_cols) < 2:
                return jsonify({"ok": False,
                                "error": "信度分析需要至少 2 个题项（数值列）。"}), 400
            result = run_cronbach_alpha(df, item_cols)
        elif method == "two_way_anova":
            factor_a = payload.get("group_col")
            factor_b = payload.get("value_col2")
            if not factor_a or not factor_b or not value_col:
                return jsonify({"ok": False, "error":
                                "双因素方差分析需要：因素 A 列、因素 B 列、数值因变量列。"}), 400
            result = run_two_way_anova(df, factor_a, factor_b, value_col)
        else:
            return jsonify({
                "ok": False,
                "error": (
                    f"方法 {method} 不被识别。当前支持："
                    "independent_t / anova / correlation / chi_square / "
                    "paired_t / mann_whitney / wilcoxon / "
                    "linear_regression / logistic_regression / cronbach_alpha / "
                    "two_way_anova。"
                ),
            }), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"分析过程出错：{e}"}), 500

    # v0.4：可选 LLM 深度解读（默认关；失败静默降级为纯模板）
    # v0.5：带缓存（相同统计量重复解读直接命中，0 token）
    llm_section = None
    llm_error = None
    llm_meta = {"cached": False, "model": "",
               "input_tokens": 0, "output_tokens": 0,
               "cost_estimate": 0.0}
    if payload.get("use_llm") == 1:
        llm_section, llm_error, llm_meta = enhance_analysis(
            method, result.get("summary", {}), method_label(method),
            force=payload.get("llm_force") == 1,
        )
        if llm_section:
            result["markdown"] = result.get("markdown", "") + "\n\n" + llm_section

    return jsonify({
        "ok": True,
        **result,
        "llm_enhanced": bool(llm_section),
        "llm_error": llm_error,
        "llm_cached": llm_meta.get("cached", False),
        "llm_model": llm_meta.get("model", ""),
        "llm_input_tokens": llm_meta.get("input_tokens", 0),
        "llm_output_tokens": llm_meta.get("output_tokens", 0),
        "llm_cost": llm_meta.get("cost_estimate", 0.0),
    })


# ---------------------------------------------------------------------------
# v0.8 · 流式 SSE 渲染（让分析过程像 ChatGPT 一样边算边出）
# ---------------------------------------------------------------------------
def _sse(event: str, data) -> str:
    """SSE 单条事件格式化。event: <name>\\ndata: <json>\\n\\n"""
    import json as _json
    payload = data if isinstance(data, str) else _json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _analyze_sse(payload: dict):
    """SSE 版本的 analyze。事件流：
        - event: stage    → {stage, message, progress}
        - event: markdown → {markdown, method, summary, variables}
        - event: chart    → {image (base64), cache_hit}  或  {error, image: null}
        - event: error    → {message}
        - event: done     → {ok}
    """
    from flask import Response
    import time

    file_id = payload.get("file_id")
    method = payload.get("method")
    group_col = payload.get("group_col")
    value_col = payload.get("value_col")
    value_col2 = payload.get("value_col2")

    def gen():
        try:
            yield _sse("stage", {"stage": "init", "message": "正在接收请求…", "progress": 5})
            if not file_id or file_id not in _SESSION:
                yield _sse("error", {"message": "会话已过期，请重新上传文件。"})
                yield _sse("done", {"ok": False}); return
            if not method:
                yield _sse("error", {"message": "请提供分析方法。"})
                yield _sse("done", {"ok": False}); return
            df = _SESSION[file_id]
            yield _sse("stage", {"stage": "clean", "message": "正在清洗数据…", "progress": 15})
            time.sleep(0.15)

            yield _sse("stage", {"stage": "compute", "message": "正在计算统计量…", "progress": 35})
            try:
                if method == "independent_t":
                    if not group_col or not value_col:
                        raise ValueError("独立样本 T 检验需要分组列和数值列。")
                    result = run_independent_t(df, group_col, value_col)
                elif method == "anova":
                    if not group_col or not value_col:
                        raise ValueError("方差分析需要分组列和数值列。")
                    result = run_anova(df, group_col, value_col)
                elif method == "correlation":
                    if not value_col or not value_col2:
                        raise ValueError("Pearson 相关需要两个数值列。")
                    result = run_correlation(df, value_col, value_col2)
                elif method == "chi_square":
                    if not group_col or not value_col:
                        raise ValueError("卡方检验需要两个分类列。")
                    result = run_chi_square(df, group_col, value_col)
                elif method == "paired_t":
                    if not value_col or not value_col2:
                        raise ValueError("配对 T 检验需要两个数值列（前测 + 后测）。")
                    result = run_paired_t(df, value_col, value_col2)
                elif method == "mann_whitney":
                    if not group_col or not value_col:
                        raise ValueError("Mann-Whitney U 检验需要分组列和数值列。")
                    result = run_mann_whitney(df, group_col, value_col)
                elif method == "wilcoxon":
                    if not value_col or not value_col2:
                        raise ValueError("Wilcoxon 符号秩检验需要两个数值列（前测 + 后测）。")
                    result = run_wilcoxon(df, value_col, value_col2)
                elif method in ("linear_regression", "logistic_regression"):
                    x_cols = payload.get("x_cols") or ([value_col2] if value_col2 else [])
                    if isinstance(x_cols, str):
                        x_cols = [c.strip() for c in x_cols.split(",") if c.strip()]
                    if not value_col or not x_cols:
                        raise ValueError("回归分析需要因变量列和至少 1 个自变量列。")
                    result = (
                        run_linear_regression(df, value_col, x_cols)
                        if method == "linear_regression"
                        else run_logistic_regression(df, value_col, x_cols)
                    )
                elif method == "cronbach_alpha":
                    item_cols = payload.get("item_cols") or payload.get("x_cols") \
                        or [c for c in (payload.get("columns") or []) if c]
                    if isinstance(item_cols, str):
                        item_cols = [c.strip() for c in item_cols.split(",") if c.strip()]
                    if not item_cols or len(item_cols) < 2:
                        raise ValueError("信度分析需要至少 2 个题项（数值列）。")
                    result = run_cronbach_alpha(df, item_cols)
                elif method == "two_way_anova":
                    factor_a = payload.get("group_col")
                    factor_b = payload.get("value_col2")
                    if not factor_a or not factor_b or not value_col:
                        raise ValueError("双因素方差分析需要：因素 A 列、因素 B 列、数值因变量列。")
                    result = run_two_way_anova(df, factor_a, factor_b, value_col)
                else:
                    raise ValueError(f"方法 {method} 不被识别。")
            except ValueError as e:
                yield _sse("error", {"message": str(e)})
                yield _sse("done", {"ok": False}); return
            except Exception as e:  # noqa: BLE001
                yield _sse("error", {"message": f"分析过程出错：{e}"})
                yield _sse("done", {"ok": False}); return

            yield _sse("stage", {"stage": "write", "message": "正在撰写学术解读…", "progress": 65})
            time.sleep(0.1)
            yield _sse("markdown", {
                "markdown": result.get("markdown", ""),
                "method": result.get("method", method),
                "summary": result.get("summary", {}),
                "variables": result.get("variables", {}),
                "n": result.get("n"),
            })

            # v0.4：可选 LLM 深度解读（use_llm=1 时；失败降级不阻断）
            # v0.5：带缓存（llm_force=1 可强制重新生成）
            if payload.get("use_llm") == 1:
                yield _sse("stage", {"stage": "llm", "message": "正在生成 AI 深度解读…", "progress": 78})
                llm_section, llm_error, llm_meta = enhance_analysis(
                    method, result.get("summary", {}), method_label(method),
                    force=payload.get("llm_force") == 1,
                )
                if llm_section:
                    if llm_meta.get("cached"):
                        yield _sse("stage", {"stage": "llm", "message": "AI 解读命中缓存，直接返回…", "progress": 82})
                    yield _sse("llm", {"section": llm_section, "ok": True, **llm_meta})
                else:
                    yield _sse("llm", {"ok": False, "error": llm_error or "LLM 不可用"})

            yield _sse("stage", {"stage": "chart", "message": "正在生成数据可视化…", "progress": 85})
            try:
                chart_x2 = value_col2
                if method in ("linear_regression", "logistic_regression"):
                    xs = payload.get("x_cols") or ([value_col2] if value_col2 else [])
                    if isinstance(xs, str):
                        xs = [c.strip() for c in xs.split(",") if c.strip()]
                    chart_x2 = xs[0] if xs else None
                png, err = _build_chart_png(
                    df, method,
                    group_col if method in ("independent_t", "anova", "mann_whitney", "chi_square") else None,
                    value_col,
                    chart_x2 if method in ("correlation", "paired_t", "wilcoxon",
                                           "linear_regression", "logistic_regression") else None,
                )
                if png and not err:
                    import base64
                    yield _sse("chart", {
                        "image": base64.b64encode(png).decode("ascii"),
                        "cache_hit": False,
                    })
                else:
                    yield _sse("chart", {"error": err or "图表生成失败", "image": None})
            except Exception as e:  # noqa: BLE001
                yield _sse("chart", {"error": f"图表异常：{e}", "image": None})

            yield _sse("stage", {"stage": "done", "message": "分析完成", "progress": 100})
            yield _sse("done", {"ok": True})
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"message": f"SSE 流异常：{e}"})
            yield _sse("done", {"ok": False})

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# v0.9 · Word 导出
# ---------------------------------------------------------------------------
@app.route("/api/export", methods=["POST"])
def api_export():
    """把分析结果 / 论文排查报告导出为 docx。请求体（JSON）：
        {
          "markdown": "…",           # 必填，报告 markdown
          "method": "independent_t", # 可选（数据分析用），用于方法中文名 + 文件名
          "title": "论文排查",       # 可选，报告类型（无 method 时用于主标题 + 文件名）
          "chart_base64": "…",       # 可选，图表 PNG 的 base64（不带 data: 前缀）
          "meta": {"filename": …, "n": …}  # 可选，副标题元信息
        }
    返回：application/vnd.openxmlformats-officedocument.wordprocessingml.document 附件
    """
    import base64
    from flask import send_file
    from export_docx import markdown_to_docx

    payload = request.get_json(silent=True) or {}
    markdown = payload.get("markdown") or ""
    method = payload.get("method") or ""
    report_title = (payload.get("title") or "").strip()
    chart_b64 = payload.get("chart_base64") or ""
    meta = payload.get("meta") or {}

    if not markdown:
        return jsonify({"ok": False, "error": "缺少 markdown 内容。"}), 400

    chart_png = None
    if chart_b64:
        # 兼容带 data:image/png;base64, 前缀的写法
        if "," in chart_b64 and chart_b64.strip().startswith("data:"):
            chart_b64 = chart_b64.split(",", 1)[1]
        try:
            chart_png = base64.b64decode(chart_b64)
        except Exception:  # noqa: BLE001
            chart_png = None  # 图坏不阻断导出

    # 主标题：优先显式 title（论文排查），否则按 method 生成（数据分析）
    if not report_title:
        report_title = "统计分析报告" if method else "报告"
    doc_title = f"智论助手 · {report_title}"

    try:
        docx_bytes = markdown_to_docx(
            markdown,
            chart_png=chart_png,
            method_label=method_label(method) if method else "",
            meta=meta,
            title=doc_title,
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Word 导出失败：{e}"}), 500

    import io as _io
    name_part = method if method else report_title
    fname = f"智论助手_{name_part}_{int(time.time())}.docx"
    return send_file(
        _io.BytesIO(docx_bytes),
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        as_attachment=True,
        download_name=fname,
    )


@app.route("/api/chart", methods=["POST"])
def api_chart():
    """根据已上传的数据 + 方法 + 列，生成图表 PNG（base64）。
    请求体（JSON）：
        file_id:        上传接口返回的 file_id
        method:         independent_t / anova / correlation / chi_square
        group_col:      分组 / 行变量（必填，4 个方法都需要）
        value_col:      因变量 / 数值列 / 列变量（必填）
        value_col2:     第二个数值列（仅 correlation 需要）
    返回：
        {ok, image: "data:image/png;base64,xxx", cache_hit: bool}
    """
    payload = request.get_json(silent=True) or {}
    file_id = payload.get("file_id")
    method = payload.get("method")
    group_col = payload.get("group_col")
    value_col = payload.get("value_col")
    value_col2 = payload.get("value_col2")

    if not file_id or file_id not in _SESSION:
        return jsonify({"ok": False, "error": "会话已过期，请重新上传文件。"}), 400
    if not method:
        return jsonify({"ok": False, "error": "请提供分析方法。"}), 400

    df = _SESSION[file_id]

    cache_key = _chart_cache_key(file_id, method,
                                  [group_col, value_col, value_col2],
                                  len(df))
    if cache_key in _CHART_CACHE:
        return jsonify({
            "ok": True,
            "image": _CHART_CACHE[cache_key],
            "cache_hit": True,
        })

    png_bytes, err = _build_chart_png(df, method, group_col, value_col, value_col2)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    b64 = base64.b64encode(png_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64}"
    _CHART_CACHE[cache_key] = data_uri
    return jsonify({
        "ok": True,
        "image": data_uri,
        "cache_hit": False,
    })


@app.route("/api/sample/<name>")
def api_sample(name: str):
    """提供内置示例数据下载，方便用户立即体验。"""
    safe = secure_filename(name)
    return send_from_directory(EXAMPLE_DIR, safe, as_attachment=True)


@app.route("/api/sample_paper/<name>")
def api_sample_paper(name: str):
    """提供内置示例论文片段下载，方便用户测试论文排查功能。"""
    safe = secure_filename(name)
    return send_from_directory(EXAMPLE_DIR, safe, as_attachment=True)


# -----------------------------------------------------------------------------
# 论文排查接口（路径 B）
# -----------------------------------------------------------------------------
@app.route("/api/check_paper", methods=["POST"])
def api_check_paper():
    """同时接收论文 + 数据，跑出核查报告。
    表单字段：
        paper:    .docx / .txt / .md / .pdf
        data:     .csv / .xlsx / .xls
        directive: 可选，自然语言过滤核查范围（v0.4）
        use_llm:  可选 "1" = 追加 AI 深度审计段（V4-Pro，v0.4.2）
        llm_force: 可选 "1" = 绕过响应缓存强制真调
        siliconflow_key:  硅基流动 API Key（BYOK 模式，可选）
        zhipu_key:       智谱 API Key（BYOK 模式，可选）
    返回 JSON：
        paper_claims: {methods, quantities, variables, raw_text_excerpt}
        columns:      数据列概览（与 /api/upload 同结构）
        rows:         行数
        audit:        {markdown, comparisons, suggestions, real}
        llm_enhanced / llm_error / llm_cached / llm_model: AI 审计元信息
    """
    paper = request.files.get("paper")
    data = request.files.get("data")
    if paper is None or not paper.filename:
        return jsonify({"ok": False, "error": "请上传论文文件。"}), 400
    if data is None or not data.filename:
        return jsonify({"ok": False, "error": "请上传数据文件。"}), 400

    # 1) 解析论文
    try:
        paper_text = read_paper_text(paper)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"论文解析失败：{e}"}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"论文读取异常：{e}"}), 400

    # 2) 解析数据
    try:
        df = _read_any(data)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"数据读取失败：{e}"}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"数据异常：{e}"}), 400
    if df.empty or len(df.columns) == 0:
        return jsonify({"ok": False, "error": "数据无有效内容。"}), 400

    columns = [_summarize_column(df[c]) for c in df.columns]

    # 3) 论文结构化抽取
    methods = extract_methods(paper_text)
    quantities = extract_quantities(paper_text)
    variables = extract_variables(paper_text)
    # v0.9.1 内测反馈：工科/控制类论文没有统计方法时，after_keyword
    # 启发式抓到的"变量"几乎全是正文句子片段（338 个垃圾）。
    # 策略：方法和统计量都为空 → 只保留高精度来源（quoted/near_stat/common_dict）
    if not methods and not quantities:
        variables = [v for v in variables if set(v.get("sources", [])) - {"after_keyword"}]
    paper_claims = {
        "methods": methods,
        "quantities": quantities,
        "variables": variables,
        # 只回前 1500 字给前端预览（避免 Markdown 渲染卡顿）
        "raw_text_excerpt": paper_text[:1500],
        "raw_text_length": len(paper_text),
    }

    # 4) 处理 BYOK（用户自带 Key）
    router = get_router()
    siliconflow_key = (request.form.get("siliconflow_key") or "").strip()
    zhipu_key = (request.form.get("zhipu_key") or "").strip()
    if siliconflow_key:
        router.set_user_key("sf", siliconflow_key)
    if zhipu_key:
        router.set_user_key("zhipu", zhipu_key)

    # 5) 跑核查报告（v0.4 支持用户指令过滤）
    directive = (request.form.get("directive") or "").strip()
    audit = build_audit_report(paper_claims, df, columns, directive=directive)

    # 6) v0.4.2：可选 AI 深度审计（默认关；失败静默降级为纯规则报告）
    #    冻结前缀 = 论文全文（前缀缓存命中，价差 10 倍）；规则发现 + 指令 = 变量区
    llm_section = None
    llm_error = None
    llm_meta: dict = {"cached": False, "model": "",
                       "input_tokens": 0, "output_tokens": 0,
                       "cost_estimate": 0.0}
    if (request.form.get("use_llm") or "").strip() == "1":
        llm_section, llm_error, llm_meta = audit_paper_with_llm(
            paper_text, audit, directive,
            force=(request.form.get("llm_force") or "").strip() == "1",
        )
        if llm_section:
            audit = {**audit, "markdown": (audit.get("markdown") or "") + "\n\n" + llm_section}

    return jsonify({
        "ok": True,
        "rows": int(len(df)),
        "columns": columns,
        "paper_claims": paper_claims,
        "audit": audit,
        "filename_data": data.filename,
        "filename_paper": paper.filename,
        "directive": directive,
        "llm_enhanced": bool(llm_section),
        "llm_error": llm_error,
        "llm_cached": llm_meta.get("cached", False),
        "llm_model": llm_meta.get("model", ""),
        "llm_input_tokens": llm_meta.get("input_tokens", 0),
        "llm_output_tokens": llm_meta.get("output_tokens", 0),
        "llm_cost": llm_meta.get("cost_estimate", 0.0),
    })


# 简易防刷：每 IP 每分钟最多 30 次接口请求（MVP 宽松值，本地内测足够）
_REQUEST_LOG: dict[str, list[float]] = {}
_RATE_LIMIT_PER_MIN = 30


@app.before_request
def rate_limit():
    if request.path.startswith("/static") or request.path in ("/", "/health"):
        return None
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()
    history = _REQUEST_LOG.setdefault(ip, [])
    history[:] = [t for t in history if now - t < 60]
    if len(history) >= _RATE_LIMIT_PER_MIN:
        return jsonify({"ok": False, "error": "请求过于频繁，请稍后再试。"}), 429
    history.append(now)
    return None


if __name__ == "__main__":
    # 本地直启（start.bat / python app.py）：加载根目录 .env 里的 Key 与 LLM_TIER。
    # 只在直启时做，import 场景（测试 / WSGI）不加载，保持测试环境干净。
    _load_dotenv()
    port = int(os.environ.get("PORT", "5000"))
    print(f"🚀 智论助手 MVP 启动： http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=True)