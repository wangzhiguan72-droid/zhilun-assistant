"""⑥ 模拟数据生成器 —— 备课 / 答辩预演零成本
================================================
纯 numpy 生成可复现（seed）的演示数据，覆盖全部 12 个注册方法。
每个 `generate_*` 返回 `(df, truth)`：
  - df    : pd.DataFrame，可直接喂给 run_* 函数
  - truth : dict，真实效应 / 应得的统计量（用于验证工具算得对不对）

设计原则（护栏）：
  1. 纯 numpy + scipy.stats，零 LLM、零外部依赖
  2. 模拟数据明确标注"模拟"，不与真实数据混淆
  3. 同 seed 结果一致（可复现）
  4. 每个方法都有 truth 反算，方便验证

用法：
    from simulate import generate

    df, truth = generate("independent_t", effect_size=0.5, n_per_group=30, seed=42)
    # df → 喂给 run_independent_t(df, "group", "value")
    # truth → 对比结果是否一致
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats
from typing import Any


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _make_rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _add_noise(values: np.ndarray, noise: float, rng: np.random.Generator) -> np.ndarray:
    """给数组加高斯噪声（noise = 标准差占均值的比例，0 = 无噪声）。

    ⚠️ 缩放基准是 `values.mean()`：数据带大基数（如 50 分的量表）时，
    noise=0.1 会加上 sd≈5 的噪声，足以把效应完全淹没。
    有基数的生成器应把噪声加在**离差**上再抬回基数（见 `_gen_two_way_anova`），
    否则既淹效应，又会因各单元格均值不同而引入异方差。
    """
    if noise <= 0:
        return values
    scale = max(abs(values.mean()) * noise, 0.1)
    return values + rng.normal(0, scale, size=values.shape)


# ---------------------------------------------------------------------------
# 独立 oracle：不复用 app.py 的 ANOVA 实现，另走一条代码路径算 F / p
# ---------------------------------------------------------------------------
def _ols_ss(y: np.ndarray, X: np.ndarray) -> float:
    """最小二乘残差平方和（用 lstsq，与 app 的 Type III SS 是两套代码）。"""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    return float(resid @ resid)


def _two_way_terms(df: pd.DataFrame, a_col: str, b_col: str, v_col: str) -> dict:
    """2×2 双因素 ANOVA 三项效应的 F / p（额外平方和法，平衡设计下 = Type III）。

    返回 {"a": (F, p), "b": (F, p), "ab": (F, p)}。
    """
    y = df[v_col].to_numpy(dtype=float)
    n = len(y)
    a = pd.get_dummies(df[a_col], drop_first=True).to_numpy(dtype=float)
    b = pd.get_dummies(df[b_col], drop_first=True).to_numpy(dtype=float)
    ab = a * b
    ones = np.ones((n, 1))

    def sse(cols: list[np.ndarray]) -> float:
        X = np.hstack([ones] + cols) if cols else ones
        return _ols_ss(y, X)

    full = [a, b, ab]
    sse_full = sse(full)
    df_full = n - (1 + a.shape[1] + b.shape[1] + ab.shape[1])
    out: dict[str, tuple[float, float]] = {}
    for name, term in (("a", a), ("b", b), ("ab", ab)):
        reduced = [c for c in full if c is not term]
        ss_term = sse(reduced) - sse_full
        df_term = term.shape[1]
        f = (ss_term / df_term) / (sse_full / df_full) if sse_full > 0 else float("nan")
        out[name] = (float(f), float(stats.f.sf(f, df_term, df_full)))
    return out


def _rm_anova_f(data: np.ndarray) -> tuple[float, float]:
    """被试内单因素重复测量 ANOVA 的 F / p（定义式，独立于 app 实现）。

    data: (n_subjects, k_timepoints)
    """
    n, k = data.shape
    grand = float(data.mean())
    ss_total = float(((data - grand) ** 2).sum())
    ss_time = float(n * ((data.mean(axis=0) - grand) ** 2).sum())
    ss_subj = float(k * ((data.mean(axis=1) - grand) ** 2).sum())
    ss_err = ss_total - ss_time - ss_subj
    df_time, df_err = k - 1, (n - 1) * (k - 1)
    f = (ss_time / df_time) / (ss_err / df_err) if ss_err > 0 else float("nan")
    return float(f), float(stats.f.sf(f, df_time, df_err))


# ---------------------------------------------------------------------------
# 各方法的生成器
# ---------------------------------------------------------------------------
def _gen_independent_t(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    d = effect_size  # Cohen's d
    g1 = rng.normal(0, 1, n_per_group)
    g2 = rng.normal(d, 1, n_per_group)
    g1 = _add_noise(g1, noise, rng)
    g2 = _add_noise(g2, noise, rng)
    df = pd.DataFrame({
        "group": ["A"] * n_per_group + ["B"] * n_per_group,
        "value": np.concatenate([g1, g2]),
    })
    # truth: 独立 T 检验应得的结果
    # v2.17：d 的符号必须与 `run_independent_t` 同一约定 —— 那边是
    # `d = (mean1 - mean2) / pooled_sd`，与 t 同号。旧写法用 (g2-g1) 会得到
    # 与 expected_t 反号的 d（实测 +0.75 vs -0.75），拿它当标准答案去比对
    # 会让人以为工具算错了符号。真值必须与被测对象同口径。
    t_stat, p_val = stats.ttest_ind(g1, g2)
    pooled_sd = np.sqrt(((n_per_group - 1) * g1.var(ddof=1) + (n_per_group - 1) * g2.var(ddof=1)) / (2 * n_per_group - 2))
    cohens_d = (g1.mean() - g2.mean()) / pooled_sd if pooled_sd > 0 else float("nan")
    return df, {
        "method": "independent_t",
        "expected_t": float(t_stat),
        "expected_p": float(p_val),
        "expected_d": float(cohens_d),
        "n1": n_per_group, "n2": n_per_group,
    }


def _gen_paired_t(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    n = n_per_group
    pre = rng.normal(50, 10, n)
    post = pre + effect_size * 10 + rng.normal(0, 5, n)
    pre = _add_noise(pre, noise, rng)
    post = _add_noise(post, noise, rng)
    df = pd.DataFrame({"pre_test": pre, "post_test": post})
    t_stat, p_val = stats.ttest_rel(post, pre)
    return df, {
        "method": "paired_t",
        "expected_t": float(t_stat),
        "expected_p": float(p_val),
        "n": n,
    }


def _gen_anova(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    k = 3  # 3 组
    groups = []
    data = []
    for i in range(k):
        g = rng.normal(i * effect_size, 1, n_per_group)
        g = _add_noise(g, noise, rng)
        groups.extend([f"G{i+1}"] * n_per_group)
        data.extend(g)
    df = pd.DataFrame({"group": groups, "value": data})
    groups_list = [data[i*n_per_group:(i+1)*n_per_group] for i in range(k)]
    f_stat, p_val = stats.f_oneway(*groups_list)
    return df, {
        "method": "anova",
        "expected_F": float(f_stat),
        "expected_p": float(p_val),
        "k": k, "n_per_group": n_per_group,
    }


def _gen_two_way_anova(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    """2×2 双因素，交互效应**真的可检出**。

    v2.17 修的真缺陷：旧写法把噪声加在 base=50 的量上（`_add_noise` 按均值
    缩放 → noise=0.1 就加 sd≈5.5），2.5 的交互当场被淹没 —— 实测 20 个 seed
    只有 5 个 p_ab<0.05，中位 p=0.17，而 `note` 却写着"有交互效应"。
    这也是《进一步完善计划》⑥ 验收标准（"重跑得到显著交互 p"）不达标的原因。
    现在改成：噪声加在**离差**上再抬回基数 —— 各单元格共用同一个噪声尺度，
    既保住同方差（不破坏 ANOVA 前提），也不再淹掉效应。
    """
    rng = _make_rng(seed)
    n = max(n_per_group, 5)
    a_levels = ["A1", "A2"]
    b_levels = ["B1", "B2"]
    rows: list[dict] = []
    mu: list[float] = []
    for a in a_levels:
        for b in b_levels:
            a_eff = effect_size * 10 if a == "A2" else 0
            b_eff = effect_size * 10 if b == "B2" else 0
            interaction = effect_size * 14 if (a == "A2" and b == "B2") else 0
            mu.extend([a_eff + b_eff + interaction] * n)
            rows.extend([{"factor_a": a, "factor_b": b} for _ in range(n)])
    dev = np.array(mu, dtype=float) + rng.normal(0, 4, len(mu))
    dev = _add_noise(dev, noise, rng)
    df = pd.DataFrame(rows)
    df["value"] = 50 + dev
    terms = _two_way_terms(df, "factor_a", "factor_b", "value")
    return df, {
        "method": "two_way_anova",
        "n_cells": 4, "n_per_cell": n,
        "expected_f_a": terms["a"][0], "expected_p_a": terms["a"][1],
        "expected_f_b": terms["b"][0], "expected_p_b": terms["b"][1],
        "expected_f_ab": terms["ab"][0], "expected_p_ab": terms["ab"][1],
        "note": "双因素 ANOVA（交互 = effect_size×14；effect_size 越大越易检出）",
    }


def _gen_repeated_measures_anova(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    n = max(n_per_group, 10)
    k = 3  # 3 个时间点
    cols = [f"T{i+1}" for i in range(k)]
    base = rng.normal(50, 10, n)
    # 与 two_way 同理：噪声加在**离差**上（基数 50 会让 noise=0.1 变成 sd=5，
    # 足以把 3 个时间点之间的差异淹掉），加完再抬回基数。
    dev = np.column_stack([
        i * effect_size * 5 + rng.normal(0, 3, n) for i in range(k)
    ])
    dev = _add_noise(dev, noise, rng)
    mat = base[:, None] + dev
    df = pd.DataFrame(mat, columns=cols)
    f_val, p_val = _rm_anova_f(mat)
    return df, {
        "method": "repeated_measures_anova",
        "k_timepoints": k, "n_subjects": n,
        "expected_F": f_val, "expected_p": p_val,
    }


def _gen_correlation(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    n = max(n_per_group * 2, 30)
    r_target = np.clip(effect_size, -0.95, 0.95)
    # 用 Cholesky 生成相关二元正态
    cov = [[1, r_target], [r_target, 1]]
    xy = rng.multivariate_normal([0, 0], cov, n)
    x = _add_noise(xy[:, 0], noise, rng)
    y = _add_noise(xy[:, 1], noise, rng)
    df = pd.DataFrame({"x_var": x, "y_var": y})
    r_actual, p_val = stats.pearsonr(x, y)
    return df, {
        "method": "correlation",
        "expected_r": float(r_actual),
        "expected_p": float(p_val),
        "n": n,
    }


def _gen_linear_regression(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    n = max(n_per_group * 2, 30)
    x = rng.normal(0, 1, n)
    y = effect_size * x + rng.normal(0, 1, n)
    x = _add_noise(x, noise, rng)
    y = _add_noise(y, noise, rng)
    df = pd.DataFrame({"y_var": y, "x_var": x})
    slope, intercept, r_val, p_val, se = stats.linregress(x, y)
    return df, {
        "method": "linear_regression",
        "expected_slope": float(slope),
        "expected_intercept": float(intercept),
        "expected_r": float(r_val),
        "expected_p": float(p_val),
        "n": n,
    }


def _gen_logistic_regression(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    n = max(n_per_group * 2, 40)
    x = rng.normal(0, 1, n)
    logit = effect_size * x
    prob = 1 / (1 + np.exp(-logit))
    y = (rng.uniform(0, 1, n) < prob).astype(int)
    x = _add_noise(x, noise, rng)
    df = pd.DataFrame({"y_binary": y, "x_var": x})
    return df, {
        "method": "logistic_regression",
        "expected_n": int(n), "expected_n_events": int(y.sum()),
        "true_beta": float(effect_size),
        "note": ("二分类因变量 + 1 个连续自变量。"
                 "`expected_n*` 可与实算精确比对；`true_beta` 是**生成参数**——"
                 "估计值只会落在它附近，不会相等（抽样变异），别拿它当精确 oracle。"),
    }


def _gen_chi_square(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    n = max(n_per_group * 2, 40)
    # 构造关联的两个分类变量
    strength = np.clip(effect_size, 0.1, 0.9)
    x = rng.choice(["X1", "X2"], n)
    y = []
    for xi in x:
        if xi == "X1":
            y.append(rng.choice(["Y1", "Y2"], p=[0.5 + strength/2, 0.5 - strength/2]))
        else:
            y.append(rng.choice(["Y1", "Y2"], p=[0.5 - strength/2, 0.5 + strength/2]))
    df = pd.DataFrame({"row_var": x, "col_var": y})
    ct = pd.crosstab(df["row_var"], df["col_var"])
    chi2, p_val, dof, expected = stats.chi2_contingency(ct)
    return df, {
        "method": "chi_square",
        "expected_chi2": float(chi2),
        "expected_p": float(p_val),
        "dof": int(dof),
        "n": n,
    }


def _gen_mann_whitney(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    g1 = rng.exponential(1, n_per_group)
    g2 = rng.exponential(1 + effect_size, n_per_group)
    g1 = _add_noise(g1, noise, rng)
    g2 = _add_noise(g2, noise, rng)
    df = pd.DataFrame({
        "group": ["A"] * n_per_group + ["B"] * n_per_group,
        "value": np.concatenate([g1, g2]),
    })
    u_stat, p_val = stats.mannwhitneyu(g1, g2, alternative="two-sided")
    return df, {
        "method": "mann_whitney",
        "expected_U": float(u_stat),
        "expected_p": float(p_val),
        "n1": n_per_group, "n2": n_per_group,
    }


def _gen_wilcoxon(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    n = n_per_group
    pre = rng.normal(50, 10, n)
    post = pre + effect_size * 8 + rng.exponential(3, n)
    pre = _add_noise(pre, noise, rng)
    post = _add_noise(post, noise, rng)
    df = pd.DataFrame({"pre_test": pre, "post_test": post})
    stat, p_val = stats.wilcoxon(post, pre)
    return df, {
        "method": "wilcoxon",
        "expected_stat": float(stat),
        "expected_p": float(p_val),
        "n": n,
    }


def _gen_cronbach_alpha(effect_size: float, n_per_group: int, seed: int, noise: float) -> tuple[pd.DataFrame, dict]:
    rng = _make_rng(seed)
    n = max(n_per_group * 2, 50)
    k_items = 5
    # 生成有潜在因子的量表数据
    latent = rng.normal(0, 1, n)
    items = {}
    for i in range(k_items):
        loading = 0.5 + effect_size * 0.3
        item = loading * latent + rng.normal(0, 1, n)
        item = _add_noise(item, noise, rng)
        items[f"item_{i+1}"] = item
    df = pd.DataFrame(items)
    # Cronbach's alpha 真值
    # v2.17：去掉 `np.clip(alpha, 0, 1)` —— α 可以是负数（题目间负相关），
    # 这是合法且有诊断意义的取值：实测一份反向题混编的量表 α = -4.86，
    # 而 clip 后的"真值"会说 0，拿它当标准答案等于**美化真值**。
    item_vars = df.var(ddof=1).values
    total_var = df.sum(axis=1).var(ddof=1)
    alpha = (k_items / (k_items - 1)) * (1 - item_vars.sum() / total_var) if total_var > 0 else float("nan")
    return df, {
        "method": "cronbach_alpha",
        "expected_alpha": float(alpha),
        "n_items": k_items, "n": n,
    }


# ---------------------------------------------------------------------------
# 生成器注册表
# ---------------------------------------------------------------------------
_GENERATORS = {
    "independent_t": _gen_independent_t,
    "paired_t": _gen_paired_t,
    "anova": _gen_anova,
    "two_way_anova": _gen_two_way_anova,
    "repeated_measures_anova": _gen_repeated_measures_anova,
    "correlation": _gen_correlation,
    "linear_regression": _gen_linear_regression,
    "logistic_regression": _gen_logistic_regression,
    "chi_square": _gen_chi_square,
    "mann_whitney": _gen_mann_whitney,
    "wilcoxon": _gen_wilcoxon,
    "cronbach_alpha": _gen_cronbach_alpha,
}


def generate(method: str, effect_size: float = 0.5, n_per_group: int = 30,
             seed: int = 42, noise: float = 0.1) -> tuple[pd.DataFrame, dict]:
    """生成模拟数据 + 真值。

    参数：
      method       : 注册表方法 key
      effect_size  : 效应量（Cohen's d / r / 关联强度，0.2 小 / 0.5 中 / 0.8 大）
      n_per_group  : 每组样本量（或总样本量的基数）
      seed         : 随机种子（同 seed 结果一致）
      noise        : 噪声比例（0 = 无噪声，0.1 = 轻微，0.5 = 较大）

    返回：(df, truth)
      df    : pd.DataFrame，可直接喂给 run_*
      truth : dict，含 expected_* 字段（应得的统计量）

    异常：ValueError（未知方法）
    """
    gen = _GENERATORS.get(method)
    if gen is None:
        supported = " / ".join(sorted(_GENERATORS.keys()))
        raise ValueError(f"不支持的方法 {method}。当前支持：{supported}")

    # v2.17：入参守门。旧版把 n=1 / 负数直接喂给 numpy，结果是
    #   n=1  → 统计量算不出来，truth 里全是 NaN；
    #   n<0  → `ValueError: negative dimensions are not allowed`（numpy 原生异常）。
    # 前者尤其危险：**NaN 不在 JSON 标准里**，`jsonify` 会把 truth 序列化成
    # 非法 JSON（NaN 裸奔），前端 `JSON.parse` 直接炸。宁可明确报错，
    # 也绝不产出 NaN 真值。（`/api/simulate` 另有 `_clamp_num` 兜底，
    # 但 CLI 与直接调用也会走这里，守门必须放在纯函数这一层。）
    try:
        n_per_group = int(n_per_group)
    except (TypeError, ValueError):
        raise ValueError("n_per_group 必须是整数。") from None
    if n_per_group < 3:
        raise ValueError("n_per_group 至少为 3（每组少于 3 个观测，统计量无意义）。")
    for name, v in (("effect_size", effect_size), ("noise", noise), ("seed", seed)):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须是数字。") from None
        if not np.isfinite(fv):
            raise ValueError(f"{name} 必须是有限数字（不能是 NaN / Inf）。")
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        raise ValueError("seed 必须是整数。") from None

    return gen(effect_size, n_per_group, seed, noise)


def available_methods() -> list[str]:
    """返回支持的模拟方法列表。"""
    return sorted(_GENERATORS.keys())


# ---------------------------------------------------------------------------
# CLI 快速测试
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    method = sys.argv[1] if len(sys.argv) > 1 else "independent_t"
    df, truth = generate(method)
    print(f"=== 模拟数据：{method} ===")
    print(f"样本量：{len(df)}")
    print(f"真值：{truth}")
    print(f"\n前 5 行：")
    print(df.head())
    print(f"\n列类型：{dict(df.dtypes)}")
