"""数据体检（Data Check）—— 产品入口的第一站
====================================================================
对应用户思路：**"最开始出发点是辅助大学生查找数据是否有错误"**。
本模块把"数据里有没有硬伤 / 有没有可疑规律"做成一套**纯本地规则引擎**，
输出一份人话「体检报告」，不调用任何大模型、不判定造假、不改动原数据。

## 设计原则（与 ROADMAP / 专属桌面端开发总纲 一致）

1. **纯函数**：每个检测器 `check_xxx(df) -> list[dict]`，输入 DataFrame，返回问题列表。
   可被 Web 接口、CLI、副驾驶流水线复用（与 `methods_registry.py` 同一约定）。
2. **零依赖外部服务**：只用 pandas / numpy。断网可用、结果确定、零延迟。
3. **宁可漏报，不可误报**：所有"规律类"检测都用**多数一致才报告少数异常**的策略，
   绝不对正常数据乱扣帽子（这是本项目反复踩过的坑）。
4. **只报可疑**：文案统一"请核对"，永不说"造假"。

## 检测清单（第一批 9 项）

| 类别 | 检测器 | 说明 |
| --- | --- | --- |
| 一致性 | `check_column_sum` | 合计 ≠ 分项之和（多数行对得上 → 少数对不上的就是错） |
| 一致性 | `check_value_range` | 取值越界（成绩>100 / 年龄<0 / Likert 超 1-7） |
| 一致性 | `check_integer_decimals` | 计数列（人数/次数）里混进了小数 |
| 重复 | `check_duplicate_rows` | 完全重复行 / 去掉 id 列后重复 |
| 规律 | `check_diff_regularity` | 前后测差值过于规律（全是同一个数） |
| 规律 | `check_low_variance` | 全列常数（没有信息量） |
| 量表 | `check_reverse_scoring` | 反向题疑似漏反向计分（题项与总分负相关） |
| 量表 | `check_straightlining` | 直线作答（整行题项答案完全相同） |
| 缺失 | `check_missing_pattern` | 高缺失列 / 大面积缺失行 |

## 输出契约

`run_datacheck(df)` 返回：

    {
      "issues":  [ {level, category, title, evidence, explain, suggestion,
                    rows: [excel 行号...], columns: [列名...]} ],
      "summary": {total, high, mid, low, rows, cols, verdict, checks_run}
    }

- `level`：`high`（硬矛盾/不可能取值）· `mid`（可疑规律）· `low`（提示）
- `rows`：**Excel 行号**（假设表头占第 1 行，故第 i 条数据 = Excel 第 i+2 行）

## 学术级取证的分工（别重复实现）

| 检验 | 谁负责 | 入口 |
| --- | --- | --- |
| GRIM（均值） | 本模块 | `datacheck.grim_check()` |
| GRIMMER（均值 + 标准差） | **`grimmer.py`**（唯一真源） | `grimmer.grimmer_check()` |
| 本福特分布 | 本模块（列级） | `datacheck.check_benford()` |
| 末位偏好 | 本模块（列级） | `datacheck.check_terminal_digits()` |

GRIMMER **不要**在本模块再写一份 —— `grimmer.py` 用的是 rsprite2 口径
（`Fraction` 精确算术 + 可达 SD 区间），两处维护会口径分裂。
论文侧的三元组核查走 `audit.grimmer_cross_check_reported()`。

## 尚未纳入本批（后续阶段）

- 论文声称值 vs 数据实算（已有 `audit.py` 承担）
- 论文表格数字 vs 原始数据（计划中）

## 用法

    from datacheck import run_datacheck, propose_fix
    report = run_datacheck(df)
    print(report["summary"]["verdict"])

    fix = propose_fix(df, report)   # 生成「清洗后副本」（绝不动原 df）
    print(fix["stats"], len(fix["actions"]))

"""
from __future__ import annotations

import re
import math
from typing import Any

import numpy as np
import pandas as pd
# 仅用于末位偏好的卡方 p 值（别名导入，避免与任何局部变量撞名）
from scipy import stats as _scipy_stats

# ---------------------------------------------------------------------------
# 问题卡片构造 + 常量
# ---------------------------------------------------------------------------
LEVEL_HIGH = "high"
LEVEL_MID = "mid"
LEVEL_LOW = "low"
_LEVEL_ORDER = {LEVEL_HIGH: 0, LEVEL_MID: 1, LEVEL_LOW: 2}

#: 合计类列名关键词
_TOTAL_KEYWORDS = ("总", "合计", "总计", "总额", "总分", "总和", "综合", "total", "sum", "overall")

#: 计数类列名关键词（出现小数即异常）
_COUNT_KEYWORDS = ("人数", "个数", "次数", "数量", "频数", "数目", "人数", "样本", "count", "num", "size")

#: 缺省给出证据时，最多列几个行号
_MAX_EVIDENCE_ROWS = 5


def _issue(level: str, category: str, title: str, evidence: str, explain: str,
           suggestion: str, *, rows: list[int] | None = None,
           columns: list[str] | None = None) -> dict[str, Any]:
    """构造一张体检问题卡片。"""
    return {
        "level": level,
        "category": category,
        "title": title,
        "evidence": evidence,
        "explain": explain,
        "suggestion": suggestion,
        "rows": list(rows or []),
        "columns": list(columns or []),
    }


def _excel_row(idx: Any) -> int:
    """pandas 行索引 → Excel 行号（表头占第 1 行）。"""
    try:
        return int(idx) + 2
    except (TypeError, ValueError):
        return -1


def _fmt_rows(idx_list: list[Any]) -> tuple[list[int], str]:
    """把行索引列表格式化：返回 (Excel 行号列表, 人话片段)。"""
    rows = [_excel_row(i) for i in idx_list]
    shown = rows[:_MAX_EVIDENCE_ROWS]
    frag = "、".join(f"第 {r} 行" for r in shown)
    if len(rows) > len(shown):
        frag += f" 等（共 {len(rows)} 处）"
    return rows, frag


def _numeric_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def _as_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return df[cols].apply(pd.to_numeric, errors="coerce")


def _prefix_of(name: Any) -> str:
    """去掉结尾的编号（分项1 → 分项；q3 → q），用于识别"同一组"的列。"""
    s = str(name).strip()
    m = re.match(r"^(.*?)[_\-\s]*\d+$", s)
    return (m.group(1).strip("_- ") if m else s) or s


# ---------------------------------------------------------------------------
# 量表题项识别（反向题 / 直线作答 / Likert 越界 共用）
# ---------------------------------------------------------------------------
#: 列名含这些词 → 不当作量表题项（年级/组别/编号等天然是 1..N 的小整数）
_NOT_SCALE_KEYWORDS = ("年级", "班级", "组别", "分组", "编号", "序号", "学号", "被试",
                       "id", "no", "num", "class", "group", "level", "性别", "sex", "gender",
                       "第几", "次序", "排序")


def _scale_groups(df: pd.DataFrame) -> dict[str, list[str]]:
    """找疑似量表题项组。

    入选条件：数值列、**至少 80% 的取值落在 1..7**（容忍个别越界值，好让越界检测抓到它）、
    列名不含「年级/组别/编号」这类非量表词。

    分组：列名共享前缀且带编号（q1..q5 / 题1..题5 → 前缀 q / 题），组内至少 3 列；
    若无任何带编号的列，但存在 >=4 个疑似量表列，则整体视作一组（兼容「焦虑1/焦虑2/…」）。
    """
    groups: dict[str, list[str]] = {}
    loose: list[str] = []
    for c in _numeric_cols(df):
        if any(k in str(c).lower() for k in _NOT_SCALE_KEYWORDS):
            continue
        s = pd.to_numeric(df[c], errors="coerce").dropna()
        if len(s) < 5:
            continue
        if ((s >= 1) & (s <= 7)).mean() < 0.8:
            continue
        loose.append(c)
        if re.search(r"\d", str(c)):
            groups.setdefault(_prefix_of(c), []).append(c)

    out = {k: v for k, v in groups.items() if len(v) >= 3}
    if not out and len(loose) >= 4:
        out = {"（未命名量表）": loose}
    return out


def _item_total_corr(sub: pd.DataFrame) -> dict[str, float]:
    """校正后题项-总分相关（CITC）。返回 {列名: r}。"""
    if sub.shape[1] < 3 or len(sub) < 8:
        return {}
    total = sub.sum(axis=1)
    out: dict[str, float] = {}
    for c in sub.columns:
        x = pd.to_numeric(sub[c], errors="coerce")
        rest = total - x
        if x.std(ddof=1) == 0 or rest.std(ddof=1) == 0:
            out[str(c)] = 0.0
            continue
        out[str(c)] = float(np.corrcoef(x, rest)[0, 1])
    return out


# ---------------------------------------------------------------------------
# ① 一致性：合计 ≠ 分项之和
# ---------------------------------------------------------------------------
def _sum_match(df: pd.DataFrame, total_col: str, comp_cols: list[str]) -> tuple[float, list[Any]]:
    """返回 (对得上的行占比, 对不上的行索引)。只在 total 与全部分项都非空的行比较。"""
    sub = _as_numeric(df, [total_col] + comp_cols)
    sub = sub.dropna(how="any")
    if len(sub) < 5:
        return 0.0, []
    s = sub[comp_cols].sum(axis=1)
    tol = 0.015 + 1e-6 * sub[total_col].abs()
    ok = (s - sub[total_col]).abs() <= tol
    return float(ok.mean()), sub.index[~ok].tolist()


def check_column_sum(df: pd.DataFrame) -> list[dict[str, Any]]:
    """合计列 ≠ 分项之和。多数行对得上时，少数对不上即为可疑。"""
    issues: list[dict[str, Any]] = []
    num_cols = _numeric_cols(df)
    for t in [c for c in num_cols if any(k in str(c).lower() for k in _TOTAL_KEYWORDS)]:
        others = [c for c in num_cols if c != t]
        if not (2 <= len(others) <= 15):
            continue
        # 候选分项集合：全部其它数值列 + 按前缀分组的子集
        cands: list[list[str]] = [others]
        grp: dict[str, list[str]] = {}
        for c in others:
            grp.setdefault(_prefix_of(c), []).append(c)
        cands += [v for v in grp.values() if len(v) >= 2]
        # 去重
        seen: set[tuple[str, ...]] = set()
        best: tuple[float, list[str], list[Any]] | None = None
        for cs in cands:
            key = tuple(sorted(cs))
            if key in seen:
                continue
            seen.add(key)
            ratio, bad = _sum_match(df, t, cs)
            if best is None or ratio > best[0]:
                best = (ratio, cs, bad)
        if best and best[0] >= 0.6 and best[2]:
            ratio, cs, bad = best
            rows, frag = _fmt_rows(bad)
            issues.append(_issue(
                LEVEL_HIGH, "一致性",
                f"「{t}」与分项之和对不上",
                f"{frag}：这些行的「{t}」≠ " + " + ".join(f"「{c}」" for c in cs) + "。",
                f"共有 {len(bad)} 行对不上（其余 {ratio:.0%} 的行是对得上的，"
                f"说明这几列本应构成合计关系）。这类前后矛盾是答辩抽查的高危险区。",
                f"逐行核对：确认是原始录入错误，还是「{t}」漏算了某个分项。",
                rows=rows, columns=[t] + cs,
            ))
    return issues


# ---------------------------------------------------------------------------
# ② 一致性：取值越界
# ---------------------------------------------------------------------------
_RANGE_RULES: list[tuple[tuple[str, ...], float, float, str]] = [
    (("成绩", "分数", "得分", "考分", "分值", "score", "grade", "mark"), 0, 100, "成绩"),
    (("年龄", "age"), 0, 120, "年龄"),
    (("百分比", "占比", "比例", "percent", "pct"), 0, 100, "百分比"),
    (("人数", "个数", "次数", "数量", "频数", "样本", "count"), 0, math.inf, "计数"),
]


def _range_outliers(s: pd.Series, lo: float, hi: float) -> tuple[float, list[Any]]:
    """返回 (落在区间内的占比, 越界的行索引)。"""
    if s.empty:
        return 1.0, []
    inside = ((s >= lo) & (s <= hi))
    return float(inside.mean()), s.index[~inside].tolist()


def check_value_range(df: pd.DataFrame) -> list[dict[str, Any]]:
    """取值越界：按列名推断合理区间；多数值在区间内 → 少数越界即为可疑。"""
    issues: list[dict[str, Any]] = []

    # 2.1 命名规则
    for c in _numeric_cols(df):
        low = str(c).lower()
        rule = next((r for r in _RANGE_RULES if any(k in low for k in r[0])), None)
        if rule is None:
            continue
        _, lo, hi, label = rule
        s = pd.to_numeric(df[c], errors="coerce").dropna()
        if len(s) < 5 or s.nunique() < 2:
            continue
        ratio, bad = _range_outliers(s, lo, hi)
        if not bad:
            continue
        # 判定：中位数在合理区间内 → 说明该列确实是我们按列名猜的那个量，
        # 此时任何越界值都可疑（对小样本友好，不依赖"多数一致"比例）。
        med = float(s.median())
        hi_txt = "∞" if hi == math.inf else f"{hi:g}"
        if lo <= med <= hi:
            rows, frag = _fmt_rows(bad)
            issues.append(_issue(
                LEVEL_HIGH, "一致性",
                f"「{c}」出现越界值（{label}应在 {lo:g}–{hi_txt}）",
                f"{frag} 的取值超出了正常范围（该列中位数为 {med:g}，说明整体是「{label}」量纲）。",
                "越界值会直接拉偏均值、方差与显著性判断，是典型的数据录入或单位错误。",
                "核对这些行的原始来源；若是单位问题（如「元」写成「万元」）请统一。",
                rows=rows, columns=[c],
            ))
        elif ratio <= 0.1:
            issues.append(_issue(
                LEVEL_MID, "一致性",
                f"「{c}」整列疑似不符合取值范围",
                f"该列几乎全部取值都在 {lo:g}–{hi:g} 之外（中位数 {med:g}），可能不是我们按列名猜的那个含义。",
                "系统是按列名（如「成绩」「年龄」）推断合理区间的，整列越界通常意味着命名用途不同。",
                "确认该列的真实含义；若含义不同可忽略本条。",
                columns=[c],
            ))

    # 2.2 Likert 量表（1..7）
    for gname, cols in _scale_groups(df).items():
        for c in cols:
            s = pd.to_numeric(df[c], errors="coerce").dropna()
            if len(s) < 5:
                continue
            bad = s.index[(s < 1) | (s > 7)].tolist()
            if not bad:
                continue
            ratio = 1 - len(bad) / len(s)
            if ratio < 0.8:
                continue
            rows, frag = _fmt_rows(bad)
            issues.append(_issue(
                LEVEL_HIGH, "一致性",
                f"量表题「{c}」出现 1–7 之外的值",
                f"{frag} 的取值不在 1–7 之间，而同组其它题项都在 1–7。",
                "Likert 量表越界值常见于把「缺失」编成了 0 或 99，会污染信度与总分计算。",
                "确认这些值是否代表缺失；如是，请改为空值或统一缺失编码。",
                rows=rows, columns=[c],
            ))
    return issues


# ---------------------------------------------------------------------------
# ③ 一致性：计数列混入小数
# ---------------------------------------------------------------------------
def check_integer_decimals(df: pd.DataFrame) -> list[dict[str, Any]]:
    """计数类列（人数/次数/数量）里出现小数。"""
    issues: list[dict[str, Any]] = []
    for c in _numeric_cols(df):
        if not any(k in str(c).lower() for k in _COUNT_KEYWORDS):
            continue
        s = pd.to_numeric(df[c], errors="coerce").dropna()
        if len(s) < 5:
            continue
        frac_mask = (s % 1 != 0)
        n_frac = int(frac_mask.sum())
        if n_frac == 0 or n_frac > len(s) * 0.5:
            continue
        if (s % 1 == 0).mean() < 0.8:
            continue
        bad = s.index[frac_mask].tolist()
        rows, frag = _fmt_rows(bad)
        issues.append(_issue(
            LEVEL_HIGH, "一致性",
            f"计数列「{c}」出现了小数",
            f"{frag} 的取值不是整数（如 {', '.join(f'{v:g}' for v in s[frac_mask].head(3))}）。",
            "人数、次数、个数这类计数天然是整数；出现小数通常意味着该列混入了别的量（如均值、比例）。",
            "确认这些行是否填错了列，或该列是否本就不是计数。",
            rows=rows, columns=[c],
        ))
    return issues


# ---------------------------------------------------------------------------
# ④ 重复行
# ---------------------------------------------------------------------------
def _expected_collisions(sub: pd.DataFrame) -> float:
    """估算"纯属巧合的重复行对"数量（用于判断"全列重复"是否真可疑）。

    做法：把每列编码成整数，各列取值数的乘积当作可能的组合数 C；
    随机情况下期望撞车对数 ≈ n(n-1)/2 / C。

    注意：这个模型**不适用于**"重复测量"那类结构化数据（各列并不独立），
    所以它只服务"全列重复"这一条 —— 那里含编号列，证据本就强，
    且窄粗表（如 性别×方法×成绩）能被它稳稳挡住。
    """
    n = len(sub)
    if n < 2 or sub.shape[1] == 0:
        return 0.0
    combos = 1.0
    for c in sub.columns:
        combos *= max(2, int(sub[c].astype(str).nunique()))
        if combos > 1e18:
            break
    return (n * (n - 1) / 2) / max(1.0, combos)


def _has_near_unique(sub: pd.DataFrame) -> bool:
    """是否存在"近似唯一"的列（取值数 >= 95% 行数）。

    为什么用这个判据：如果 body 里有一列几乎每行都不同，那么两行在整组字段上
    完全相同就**几乎不可能是巧合**，只能是真的重复录入。反过来，像重复测量数据
    （4 个时间点都是 55–85 的整数）或 性别×方法×成绩 这类粗粒度数据，
    没有这样的列，撞车就是常态 —— 一律不报。
    """
    n = len(sub)
    if n < 5:
        return False
    threshold = max(2, int(np.ceil(0.95 * n)))
    return any(sub[c].astype(str).nunique() >= threshold for c in sub.columns)


def check_duplicate_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """完全重复行；以及去掉 id 类列后重复的行。

    **防误报护栏**（本项目真实踩过的坑，见 datacheck_test 第 4 节）：
      - 全列重复：含编号列（多半唯一），出现重复即强证据；仅当期望撞车 < 1 次才报。
      - 去编号重复：证据较弱，**必须** body 里存在"近似唯一"的列才报，
        否则粗粒度窄表会撞出一堆假重复。
    """
    issues: list[dict[str, Any]] = []
    if len(df) < 2:
        return issues

    dup_all = df.index[df.duplicated(keep=False)].tolist()
    if dup_all and _expected_collisions(df) < 1.0:
        rows, frag = _fmt_rows(sorted(set(dup_all)))
        issues.append(_issue(
            LEVEL_MID, "重复",
            f"存在完全重复的行（共 {len(set(dup_all))} 行）",
            f"{frag}：这些行在所有列上取值完全相同。",
            "完全重复常见于复制粘贴或导出重复；会让样本量虚高、显著性被夸大。",
            "确认是否为重复录入，如是请保留一条。",
            rows=rows, columns=list(df.columns),
        ))

    id_like = [c for c in df.columns
               if any(k in str(c).lower() for k in ("id", "编号", "学号", "序号", "no", "name"))]
    body = [c for c in df.columns if c not in id_like]
    if id_like and len(body) >= 3:
        sub = df[body].dropna()
        dup_body = sub.index[sub.duplicated(keep=False)].tolist()
        if dup_body and _has_near_unique(sub):
            rows, frag = _fmt_rows(sorted(set(dup_body)))
            issues.append(_issue(
                LEVEL_MID, "重复",
                "去掉编号列后出现重复（疑似重复录入）",
                f"{frag}：忽略「{'、'.join(id_like)}」后，这些行的数据完全相同。",
                "编号不同但内容完全相同的多条记录，通常是同一份被试被录入了两次"
                "（该数据中存在近似唯一的列，因此这种相同几乎不可能是巧合）。",
                "确认是否为同一对象的重复记录。",
                rows=rows, columns=body,
            ))
    return issues


# ---------------------------------------------------------------------------
# ⑤ 前后测差值过于规律
# ---------------------------------------------------------------------------
_PRE_TOKENS = ("前测", "前", "pre", "pretest", "t1", "_1")
_POST_TOKENS = ("后测", "后", "post", "posttest", "t2", "_2")


def _strip_tokens(name: Any, tokens: tuple[str, ...]) -> str:
    s = str(name)
    for t in tokens:
        s = re.sub(re.escape(t), "", s, flags=re.IGNORECASE)
    return re.sub(r"[_\-\s]+", "", s).lower()


def _find_pre_post_pairs(cols: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    posts: dict[str, str] = {}
    for c in cols:
        base = _strip_tokens(c, _POST_TOKENS)
        if base and base != str(c).lower():
            posts.setdefault(base, c)
    for c in cols:
        base = _strip_tokens(c, _PRE_TOKENS)
        if not base or base == str(c).lower():
            continue
        if base in posts and posts[base] != c:
            pairs.append((c, posts[base]))
    return pairs


def check_diff_regularity(df: pd.DataFrame) -> list[dict[str, Any]]:
    """前后测差值过于规律（全是同一个数）。仅对"较精细"的连续变量生效，避免 Likert 误报。"""
    issues: list[dict[str, Any]] = []
    num_cols = _numeric_cols(df)
    for pre, post in _find_pre_post_pairs([str(c) for c in num_cols]):
        sub = _as_numeric(df, [pre, post]).dropna()
        if len(sub) < 8:
            continue
        # 只在取值较精细（>5 档）时判规律，Likert 1-5 天然差值是 0/±1，会误报
        if sub[pre].nunique() <= 5 or sub[post].nunique() <= 5:
            continue
        d = (sub[post] - sub[pre]).round(6)
        uniq = d.unique()
        if len(uniq) != 1:
            continue
        bad = sub.index.tolist()
        rows, frag = _fmt_rows(bad)
        val = float(uniq[0])
        issues.append(_issue(
            LEVEL_MID, "规律",
            f"「{pre}」到「{post}」的差值完全一致（恒为 {val:+g}）",
            f"{frag}：每一行的后测都恰好比前测 {val:+g}，没有任何个体差异。",
            "真实被试的前后变化几乎不可能完全一致；这种「整齐」往往意味着数据是凑出来的。",
            "回看原始记录：确认是否为批量填充或公式拖动导致。",
            rows=rows, columns=[pre, post],
        ))
    return issues


# ---------------------------------------------------------------------------
# ⑥ 全列常数
# ---------------------------------------------------------------------------
def check_low_variance(df: pd.DataFrame) -> list[dict[str, Any]]:
    """数值列所有取值完全相同 —— 没有信息量。"""
    issues: list[dict[str, Any]] = []
    for c in _numeric_cols(df):
        s = pd.to_numeric(df[c], errors="coerce").dropna()
        if len(s) < 5 or s.nunique() != 1:
            continue
        v = s.iloc[0]
        issues.append(_issue(
            LEVEL_MID, "规律",
            f"「{c}」整列取值完全相同（恒为 {v:g}）",
            f"该列 {len(s)} 个有效值全部是 {v:g}。",
            "常数列无法参与相关、回归与差异检验，且常见于「填错列」或「整列格式错误」。",
            "确认该列是否本应有变化；如无意义可忽略。",
            columns=[c],
        ))
    return issues


# ---------------------------------------------------------------------------
# ⑦ 反向题疑似漏反向计分
# ---------------------------------------------------------------------------
def check_reverse_scoring(df: pd.DataFrame) -> list[dict[str, Any]]:
    """题项与总分负相关 → 极大概率是反向题忘了反向计分。"""
    issues: list[dict[str, Any]] = []
    for gname, cols in _scale_groups(df).items():
        sub = _as_numeric(df, cols).dropna(how="any")
        corr = _item_total_corr(sub)
        bad = {k: v for k, v in corr.items() if v <= -0.2}
        if not bad:
            continue
        detail = "、".join(f"「{k}」(r={v:.2f})" for k, v in sorted(bad.items(), key=lambda x: x[1]))
        issues.append(_issue(
            LEVEL_MID, "量表",
            f"量表组「{gname}」中 {len(bad)} 个题项与总分呈负相关",
            f"{detail} 与其它题项之和负相关，方向明显相反。",
            "反向题（如「我从不焦虑」）必须先反向计分再加总；漏反向会同时压低信度 α 并扭曲总分。",
            "检查这些题项是否为反向题；如是请在分析前做反向计分。",
            columns=list(bad.keys()),
        ))
    return issues


# ---------------------------------------------------------------------------
# ⑧ 直线作答
# ---------------------------------------------------------------------------
def check_straightlining(df: pd.DataFrame) -> list[dict[str, Any]]:
    """同一行所有题项答案完全相同（敷衍作答特征）。"""
    issues: list[dict[str, Any]] = []
    for gname, cols in _scale_groups(df).items():
        sub = _as_numeric(df, cols).dropna(how="any")
        if len(sub) < 10 or sub.shape[1] < 3:
            continue
        nunique = sub.nunique(axis=1)
        flat = sub.index[nunique == 1].tolist()
        if not flat:
            continue
        ratio = len(flat) / len(sub)
        if ratio < 0.2:
            continue
        rows, frag = _fmt_rows(flat)
        issues.append(_issue(
            LEVEL_MID if ratio < 0.5 else LEVEL_HIGH, "量表",
            f"存在直线作答（{len(flat)} 人所有题项答案相同，占 {ratio:.0%}）",
            f"{frag}：这些行在「{gname}」组的 {sub.shape[1]} 个题项上选了完全一样的答案。",
            "整行同分是典型的敷衍作答；会同时压缩方差、虚高或虚低信度 α。",
            "考虑剔除这些被试，或在论文中说明无效问卷的筛除标准。",
            rows=rows, columns=cols,
        ))
    return issues


# ---------------------------------------------------------------------------
# ⑨b 作答时长（问卷场景：总时长列过快的行 = 疑似没认真答）
# ---------------------------------------------------------------------------
def check_response_duration(df: pd.DataFrame) -> list[dict[str, Any]]:
    """总作答时长列里「过快」的行（问卷敷衍作答的另一典型特征）。

    只认列名里带「时长 / 用时 / 耗时 / duration / time」的列
    （排除「反应时」这类单题指标列）。判定口径：时长 < 中位数 × 30%
    ——过快作答研究（Huang et al., 2012）的常用近似是「显著低于样本
    正常节奏」，取中位数锚点比拍固定阈值更能自适应长短问卷。
    样本 <10 人或中位数 ≤5 秒（多半不是总时长）时不启用，防误报。
    """
    issues: list[dict[str, Any]] = []
    for col in df.columns:
        name = str(col).lower()
        if not any(k in name for k in ("时长", "用时", "耗时", "duration", "time")):
            continue
        if "反应" in name or "reaction" in name:
            continue  # 单题反应时不是总作答时长
        sub = _as_numeric(df, [col]).dropna()
        if len(sub) < 10:
            continue
        med = float(sub[col].median())
        if med <= 5:
            continue  # 单位可疑（毫秒/题均秒），不冒进
        threshold = med * 0.30
        fast = sub.index[sub[col] < threshold].tolist()
        if not fast or len(fast) >= len(sub) * 0.5:
            # 超一半都"过快"说明锚点本身错了（如单位混录），不报
            continue
        rows, frag = _fmt_rows(fast)
        ratio = len(fast) / len(sub)
        issues.append(_issue(
            LEVEL_MID, "量表",
            f"「{col}」列有 {len(fast)} 人作答时长过快（占 {ratio:.0%}）",
            f"{frag}：这些行的作答时长低于全样本中位数（{med:.0f} 秒）的 30%"
            f"（即不足 {threshold:.0f} 秒）。",
            "正常读题 + 作答每题至少要几秒；总时长明显过短的问卷，"
            "答案可信度存疑，答辩抽查时容易被追问。",
            "核对这几份问卷是否有效；论文中说明无效问卷的剔除标准"
            "（如「作答时长低于中位数 30% 的予以剔除」）。",
            rows=rows, columns=[col],
        ))
    return issues


# ---------------------------------------------------------------------------
# ⑩ 缺失模式
# ---------------------------------------------------------------------------
def check_missing_pattern(df: pd.DataFrame) -> list[dict[str, Any]]:
    """高缺失列 + 大面积缺失行。"""
    issues: list[dict[str, Any]] = []
    n = len(df)
    if n < 5:
        return issues

    empty_cols = [c for c in df.columns if df[c].isna().all()]
    if empty_cols:
        issues.append(_issue(
            LEVEL_HIGH, "缺失",
            f"{len(empty_cols)} 列完全没有数据",
            "、" .join(f"「{c}」" for c in empty_cols[:6]) + "整列为空。",
            "整列为空通常是数据导出时选错了工作表，或该列本不该出现在这份数据里。",
            "确认导出范围是否正确。",
            columns=empty_cols,
        ))

    high_miss: list[tuple[str, float]] = []
    for c in df.columns:
        rate = float(df[c].isna().mean())
        if 0.3 <= rate < 1.0:
            high_miss.append((str(c), rate))
    if high_miss:
        high_miss.sort(key=lambda x: -x[1])
        detail = "、".join(f"「{c}」{r:.0%}" for c, r in high_miss[:6])
        issues.append(_issue(
            LEVEL_MID, "缺失",
            f"{len(high_miss)} 列缺失率超过 30%",
            detail + "。",
            "高缺失列会让有效样本骤减；若是问卷题，还可能意味着题目表述有问题（很多人不答）。",
            "确认是问卷设计问题还是数据导出问题；必要时在论文中报告缺失处理方式。",
            columns=[c for c, _ in high_miss],
        ))

    if df.shape[1] >= 4:
        row_miss = df.isna().mean(axis=1)
        bad = df.index[row_miss >= 0.5].tolist()
        if bad:
            rows, frag = _fmt_rows(bad)
            issues.append(_issue(
                LEVEL_MID, "缺失",
                f"{len(bad)} 行有一半以上的字段为空",
                f"{frag}：这些行超过 50% 的列没有值。",
                "大面积缺失行几乎无法参与分析，保留它们会静默缩小有效样本。",
                "确认这些行是否为无效记录，考虑剔除。",
                rows=rows,
            ))
    return issues


# ---------------------------------------------------------------------------
# ⑩a 本福特定律（Benford's Law）· 首位数字分布
# ---------------------------------------------------------------------------
#: Benford 期望分布：P(d) = log10(1 + 1/d)
_BENFORD_EXPECTED: list[float] = [math.log10(1.0 + 1.0 / d) for d in range(1, 10)]

#: Nigrini 的 MAD 符合度门槛（审计实务通用刻度）
_MAD_CLOSE = 0.006
_MAD_ACCEPTABLE = 0.012
_MAD_MARGINAL = 0.015

#: 明显不适用于 Benford 的列（出现即整列跳过）
_BENFORD_SKIP_TOKENS = (
    # 编号 / 标识：人工分配的数字，首位分布由编码规则决定
    "id", "编号", "学号", "工号", "编码", "序号", "流水", "电话", "手机", "邮编",
    "身份证", "卡号",
    # 时间：年份/月份有天然上下界
    "年", "月", "日", "year", "month", "day", "date", "日期", "学期", "届",
    # 量表 / 评分 / 等级：取值区间窄（1-5、1-100），根本跨不了数量级
    "likert", "量表", "评分", "得分", "score", "等级", "评级", "rank", "排名",
    "名次", "满意度",
    # 比例 / 百分比：被 0-1 或 0-100 人为截断
    "百分比", "percent", "pct", "比例", "rate", "率",
    # 价格 / 金额：定价策略决定首位（9.9 定价法）
    "价格", "金额", "price", "cost", "fee", "费用",
)


def _benford_eligible(s: pd.Series, name: str) -> bool:
    """这一列**能不能**用 Benford 判（防误报的第一道闸门）。

    Benford 只在"自然生成、跨多个数量级、无人工上下界"的数字上成立
    （人口、交易额、物理量、财务流水）。不满足前提却硬套，
    会把大量完全正常的数据报成"可疑"——**误报比漏报更伤信任**。

    六道关：
        1. 列名不含编号/时间/量表/比例/价格类关键词
        2. 全部为正（Benford 定义在正数上）
        3. 样本量 ≥ 100（小样本下比例噪声远大于信号）
        4. 跨 ≥ 2 个数量级（max/min ≥ 100）—— 最关键的前提
        5. 取值足够分散（nunique ≥ 50，排除"只有几种取值"的列）
        6. 没有明显的人为截断（剔除上下各 1% 后仍跨数量级）
    """
    low = str(name).lower()
    if any(tok in low for tok in _BENFORD_SKIP_TOKENS):
        return False

    vals = pd.to_numeric(s, errors="coerce").dropna()
    vals = vals[np.isfinite(vals.to_numpy(dtype="float64", na_value=np.nan))]
    if len(vals) < 100:
        return False
    if (vals <= 0).any():
        return False
    if vals.nunique() < 50:
        return False

    vmax = float(vals.max())
    vmin = float(vals.min())
    if vmin <= 0:
        return False
    if vmax / vmin < 100:
        return False

    # ⚠️ 这里曾经还有一道"截尾后仍须跨数量级"的分位比闸门，已移除：
    # 线性均匀分布（最典型的编造数据）q90/q10 只有约 8，会被它误杀，
    # 导致检测器**永不触发**。而它唯一想防的"人为量程封顶"场景，
    # 靠 MAD>0.015 的严门槛 + "这只是线索"的措辞已经兜住了。
    # 教训：宁可留一点点误报率，也不要写一个从不报警的检测器。
    return True


def _mad_conformity(mad: float) -> str:
    """按 Nigrini 刻度给出符合度结论（中文）。"""
    if mad < _MAD_CLOSE:
        return "高度符合"
    if mad < _MAD_ACCEPTABLE:
        return "可接受"
    if mad < _MAD_MARGINAL:
        return "勉强符合"
    return "不符合"


def check_benford(df: pd.DataFrame) -> list[dict[str, Any]]:
    """首位数字分布是否符合本福特定律（学术级取证）。

    判据用 **MAD（平均绝对偏差）** 而不是卡方：卡方在 n 很大时几乎必然显著
    ——样本一多，任何微小偏离都会被判"不合格"，那是**误报机器**。
    MAD 是审计实务（Nigrini）的通用刻度，与样本量无关。

    只在 MAD > 0.015（明确不符合）时才报，级别 MID ——
    Benford 不符只是"值得核对的线索"，绝不是造假的证据。
    """
    issues: list[dict[str, Any]] = []
    for col in _numeric_cols(df):
        try:
            if not _benford_eligible(df[col], col):
                continue
            vals = pd.to_numeric(df[col], errors="coerce").dropna()
            vals = vals[vals > 0]
            if len(vals) < 100:
                continue
            first = vals.astype(str).str.lstrip("0.").str[0]
            first = pd.to_numeric(first, errors="coerce").dropna().astype(int)
            first = first[(first >= 1) & (first <= 9)]
            n = int(len(first))
            if n < 100:
                continue

            counts = np.array([int((first == d).sum()) for d in range(1, 10)],
                              dtype="float64")
            observed = counts / n
            expected = np.array(_BENFORD_EXPECTED, dtype="float64")
            mad = float(np.mean(np.abs(observed - expected)))
            if mad <= _MAD_MARGINAL:
                continue

            # 附带卡方（仅供参考，不作判定——大样本下它几乎必然显著）
            exp_cnt = expected * n
            chi2 = float(np.sum((counts - exp_cnt) ** 2 / exp_cnt))
            worst = int(np.argmax(np.abs(observed - expected))) + 1
            dev = float(np.max(np.abs(observed - expected)))

            issues.append(_issue(
                LEVEL_MID, "本福特",
                f"「{col}」首位数字分布不符合本福特定律（MAD={mad:.4f}，{_mad_conformity(mad)}）",
                f"共 {n} 个数值；偏离最大的是首位 {worst}"
                f"（实际 {observed[worst - 1]:.1%}，本福特期望 {expected[worst - 1]:.1%}，"
                f"差 {dev:.1%}）；卡方={chi2:.1f}。",
                "自然产生的数字（人口、交易额、物理量）首位分布应遵循本福特定律；"
                "明显偏离通常意味着数据被人为编造、拼接或截断。",
                "请核对原始记录来源；注意本福特定律只是线索，"
                "不符合也可能是该数据天然不适用（如人工定价、量程封顶），不代表造假。",
                columns=[str(col)],
            ))
        except Exception:  # noqa: BLE001 - 单列出错不拖垮整份报告
            continue
    return issues


# ---------------------------------------------------------------------------
# ⑩b 末位数字偏好（terminal digit preference / heaping）
# ---------------------------------------------------------------------------
#: 末位分析的排除词（编号类列的末位无统计意义）
_TERMINAL_SKIP_TOKENS = (
    "id", "编号", "学号", "工号", "编码", "序号", "流水", "电话", "手机", "邮编",
    "身份证", "卡号", "年份", "year", "年",
)


def _terminal_digit(v: Any) -> int | None:
    """取一个数的**读数末位**数字。

    规则（关键）：先按"报告精度"规范化，再取最后一位有效数字。
        - 整数/整十整百：120 → 0，125 → 5（读数到个位）
        - 带小数：12.34 → 4
        - 小数末尾的补位零不算：120.0 → 0（规范成 120），
          0.0420 → 2（规范成 0.042）

    ⚠️ 历史 bug：直接对 `repr(float)` 取末位字符会把 `120.0` 的
    小数点后那个 `0` 当成末位 → 任何整数列都会全部读成 0，
    既产生假阳性、又让检测器形同虚设。必须先剥掉".0"这层表示噪声。

    返回 None = 取不到（非数字 / 无穷 / 科学计数法）。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None

    s = repr(f)
    if "e" in s.lower() or "E" in s:
        return None  # 科学计数法：末位不是"读数末位"，跳过
    if "-" in s:
        s = s.replace("-", "")
    # 剥掉小数部分末尾的 0（120.0 → 120.；12.50 → 12.5）
    if "." in s:
        # 120.0 → "120." → "120"；12.50 → "12.5"；0.0420 → "0.042"
        s = s.rstrip("0")
        if s.endswith("."):
            s = s[:-1]
    if not s:
        return None
    for ch in reversed(s):
        if ch.isdigit():
            return int(ch)
    return None


def check_terminal_digits(df: pd.DataFrame) -> list[dict[str, Any]]:
    """末位数字是否偏好某些值（人工读数"取整偏好"的典型痕迹）。

    人工记录连续量时（血压、体重、身高、反应时）常不自觉地向 0 / 5 靠拢：
    血压记成 120/80 而不是 123/78。这样末位 0、5 会显著偏多。

    **双重门槛**（缺一不可，防误报）：
        1. 统计显著：卡方检验 p < 0.001（10 个数字应均匀）
        2. 效应量够大：某个末位数字占比 ≥ 25%（均匀应为 10%）

    只满足显著但偏离很小（如 12% vs 10%）的不报 —— 大样本下那太容易触发。
    """
    issues: list[dict[str, Any]] = []
    for col in _numeric_cols(df):
        try:
            low = str(col).lower()
            if any(tok in low for tok in _TERMINAL_SKIP_TOKENS):
                continue

            vals = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(vals) < 50:
                continue
            # ⚠️ 不能用 nunique 做门槛：**取整本身就会压低唯一值数量**，
            # 用"唯一值少"来排除会正好把要检测的 heaping 现象过滤掉。
            # 改用「量程」区分：量表/哑变量（1-5 分）量程窄，连续测量量程宽。
            if vals.nunique() < 5:
                continue
            try:
                if float(vals.max()) - float(vals.min()) < 10:
                    continue  # 量程太窄（量表/等级），末位分布没有信息量
            except (TypeError, ValueError):
                continue
            if vals.nunique() == len(vals) and vals.dtype.kind in "iu":
                continue  # 整数且全不重复 → 多半是编号类，不是测量值

            digits = [d for d in (_terminal_digit(v) for v in vals) if d is not None]
            n = len(digits)
            if n < 50:
                continue

            counts = np.array([digits.count(d) for d in range(10)], dtype="float64")
            # 注意：不要因为"很多末位数字没出现过"就跳过——那正是 heaping 的
            # 典型特征（血压全记成 120/130/140，末位就只剩 0 和 5）。
            # 判定交给下面的双门槛（显著性 + 效应量），别在门口就误杀。
            expected = n / 10.0
            chi2 = float(np.sum((counts - expected) ** 2 / expected))
            # 自由度 9，用 scipy 得 p 值
            p = float(_scipy_stats.chi2.sf(chi2, 9))

            props = counts / n
            worst = int(np.argmax(props))
            worst_prop = float(props[worst])
            if p >= 0.001 or worst_prop < 0.25:
                continue

            top = sorted(((float(p_), int(d_)) for d_, p_ in enumerate(props)),
                         reverse=True)[:3]
            detail = "、".join(f"末位 {d} 占 {pp:.0%}" for pp, d in top)
            issues.append(_issue(
                LEVEL_MID, "末位偏好",
                f"「{col}」末位数字明显偏好 {worst}（占 {worst_prop:.0%}，均匀应为 10%）",
                f"{n} 个数值中，{detail}；卡方={chi2:.1f}，p={p:.2g}。",
                "人工读取或录入连续量时，常不自觉地把数值记成以 0 或 5 结尾"
                "（如血压记 120 而非 123），这叫末位偏好（heaping）。"
                "它会人为压缩方差，进而影响均值与显著性。",
                "请核对原始记录是仪器自动采集还是人工录入；"
                "若是人工录入，考虑在论文中说明读数精度。这同样只是线索，不代表造假。",
                columns=[str(col)],
            ))
        except Exception:  # noqa: BLE001 - 单列出错不拖垮整份报告
            continue
    return issues


# ---------------------------------------------------------------------------
# 纯函数工具：GRIM（供论文交叉核查路径调用）
# --------------------------------------------------------------------------------------
def grim_check(mean: float, n: int, *, items: int = 1, decimals: int = 2) -> bool:
    """GRIM 检验：论文报告的均值在给定样本量下是否"可能"。

    原理：若每人得分是 `items` 项的整数之和（粒度 items），则
    `n × mean` 必须是 `items` 的整数倍；否则该均值**不可能**由这份 n 产生。

    返回 True = 可能（通过），False = 不可能（该均值有鬼）。

    用法：
        grim_check(3.47, 30)   # 30 × 3.47 = 104.1，不是整数 → False
        grim_check(3.5, 30)    # 30 × 3.5  = 105   → True
        grim_check(3.47, 100)  # 347 → True（换样本量后就可能了）
    """
    if not n or n <= 0:
        return True
    g = max(1, int(items))
    try:
        prod = float(mean) * int(n)
    except (TypeError, ValueError):
        return True
    nearest = round(prod / g) * g
    tol = max(1e-6, 1e-9 * abs(prod))
    return abs(prod - nearest) <= tol


# 注意：GRIMMER（查标准差）不在本模块 —— 唯一真源是 `grimmer.py`
# （rsprite2 口径、Fraction 精确算术）。本模块只保留 GRIM（查均值）的纯函数，
# 避免在两处维护同一套 k 空间推导，造成口径分裂。


# ---------------------------------------------------------------------------
# 聚合入口
# ---------------------------------------------------------------------------
#: 体检跑哪些检测器（顺序即报告顺序）
_CHECKS: list[tuple[str, Any]] = [
    ("合计一致性", check_column_sum),
    ("取值越界", check_value_range),
    ("计数列小数", check_integer_decimals),
    ("重复行", check_duplicate_rows),
    ("前后测差值规律", check_diff_regularity),
    ("常数列", check_low_variance),
    ("反向题计分", check_reverse_scoring),
    ("直线作答", check_straightlining),
    ("作答时长", check_response_duration),
    ("缺失模式", check_missing_pattern),
    # v2.10 学术级取证（纯 numpy，零 LLM）
    ("本福特分布", check_benford),
    ("末位偏好", check_terminal_digits),
]


def run_datacheck(df: pd.DataFrame) -> dict[str, Any]:
    """跑全部检测器，返回结构化体检报告。

    每个检测器独立 try/except —— 单个检测器出错绝不影响整份报告（永不白屏）。
    """
    issues: list[dict[str, Any]] = []
    ran: list[str] = []
    for label, fn in _CHECKS:
        try:
            found = fn(df) or []
            issues.extend(found)
            ran.append(label)
        except Exception:  # noqa: BLE001 - 单个检测器失败不拖垮整体
            continue

    issues.sort(key=lambda it: (_LEVEL_ORDER.get(it.get("level"), 9), it.get("category", "")))

    high = sum(1 for i in issues if i["level"] == LEVEL_HIGH)
    mid = sum(1 for i in issues if i["level"] == LEVEL_MID)
    low = sum(1 for i in issues if i["level"] == LEVEL_LOW)

    if high:
        verdict = (f"发现 {high} 处高优先级问题（硬矛盾 / 不可能取值），"
                   f"建议先核对修正，再做统计分析与论文撰写。")
    elif mid:
        verdict = f"未发现硬伤，但有 {mid} 处可疑规律，建议抽查确认。"
    else:
        verdict = "未发现明显数据问题，可以进入统计分析。"

    return {
        "issues": issues,
        "summary": {
            "total": len(issues),
            "high": high,
            "mid": mid,
            "low": low,
            "rows": int(len(df)),
            "cols": int(df.shape[1]),
            "verdict": verdict,
            "checks_run": ran,
            "disclaimer": "本报告只提示「可疑点」，不代表数据造假；请以原始记录为准逐条核对。",
        },
    }


# ---------------------------------------------------------------------------
# 报告渲染：把体检结果写成「人话 Markdown」
# ---------------------------------------------------------------------------
#: 各严重度的图标与中文名
_MD_ICON = {"high": "🔴", "mid": "🟡", "low": "🔵"}
_MD_LABEL = {"high": "高", "mid": "中", "low": "提示"}


def render_markdown(report: dict[str, Any], *, filename: str = "") -> str:
    """把 `run_datacheck` 的结果渲染成人话 Markdown 报告。

    放在本模块（而不是 `cli.py`）是为了让 **CLI / 流水线门控 / 导出** 共用同一份文案，
    避免出现「三处渲染三种说法」。
    """
    s = report.get("summary", {}) or {}
    issues = list(report.get("issues") or [])

    groups: dict[str, list[dict[str, Any]]] = {}
    for it in issues:
        groups.setdefault(str(it.get("level", LEVEL_LOW)), []).append(it)

    lines = ["# 数据体检报告", ""]
    if filename:
        lines += [f"- **文件**：{filename}", ""]
    lines += [
        f"- **规模**：{s.get('rows', 0)} 行 × {s.get('cols', 0)} 列",
        f"- **问题数**：{len(issues)}"
        f"（高 {s.get('high', 0)} / 中 {s.get('mid', 0)} / 提示 {s.get('low', 0)}）",
        f"- **结论**：{s.get('verdict', '')}",
        "",
    ]
    if not issues:
        lines += ["逐项检查未发现问题。", ""]

    for level in (LEVEL_HIGH, LEVEL_MID, LEVEL_LOW):
        bucket = groups.get(level) or []
        if not bucket:
            continue
        lines += [f"## {_MD_ICON[level]} {_MD_LABEL[level]}（{len(bucket)}）", ""]
        for it in bucket:
            lines.append(f"### {it.get('title', '')}")
            if it.get("evidence"):
                lines.append(f"- **证据**：{it['evidence']}")
            if it.get("explain"):
                lines.append(f"- **意味着什么**：{it['explain']}")
            if it.get("suggestion"):
                lines.append(f"- **建议**：{it['suggestion']}")
            cols = it.get("columns") or []
            if cols:
                lines.append(f"- **相关列**：{'、'.join(str(c) for c in cols)}")
            lines.append("")

    lines += ["---", "", f"> {s.get('disclaimer', '')}"]
    ran = s.get("checks_run") or []
    if ran:
        lines.append(f"> 已跑检测：{' / '.join(str(x) for x in ran)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 清洗副本：把「体检报告」变成「可执行的清洗动作 + 清洗后副本」
# ---------------------------------------------------------------------------
#: 反向计分锚点（Likert 1..7 → new = 8 − x）
_REVERSE_LO, _REVERSE_HI = 1.0, 7.0

#: changes 逐格明细上限（防止大表把响应撑爆）
_MAX_CHANGES = 300


def _jsonable(v: Any) -> Any:
    """把 numpy 标量 / NaN 转成 JSON 友好的 Python 原生类型。"""
    if v is None:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        return None if math.isnan(v) else v
    if isinstance(v, (str, int, bool)):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v)


def _reverse_scored(series: pd.Series) -> tuple[pd.Series, list[Any]]:
    """对 Likert 题项做反向计分：new = (lo+hi) − x。

    只翻转落在 1..7 的值；越界值 / 空值原样保留（交给越界检测去报）。
    返回 (新列, 发生变化的行索引)。
    """
    s = pd.to_numeric(series, errors="coerce")
    mask = s.between(_REVERSE_LO, _REVERSE_HI).fillna(False).astype(bool)
    rev = (_REVERSE_LO + _REVERSE_HI) - s
    out = series.where(~mask, rev)
    if pd.api.types.is_integer_dtype(series):
        out = out.round().astype("Int64")
    return out, list(series.index[mask])


def _act(kind: str, auto: bool, level: str, title: str, detail: str,
         columns: list[str], rows: list[int], n: int) -> dict[str, Any]:
    return {
        "kind": kind, "auto": bool(auto), "level": level, "title": title,
        "detail": detail, "columns": [str(c) for c in (columns or [])],
        "rows": list(rows or []), "n": int(n),
    }


def _to_csv(df: pd.DataFrame) -> str:
    """导出 CSV 文本（带 BOM，Excel 打开中文不乱码）。"""
    return "\ufeff" + df.to_csv(index=False)


def propose_fix(df: pd.DataFrame, report: dict[str, Any] | None = None) -> dict[str, Any]:
    """根据体检报告生成「清洗动作清单」并产出**清洗后副本**。

    **绝不改动入参 df**（全程 copy），清洗结果只作为「副本」返回给用户下载。

    分级处理 —— 只做**语义无歧义**的自动修复：

    | 问题 | 处理 | 自动？ |
    | --- | --- | --- |
    | 完全重复行 / 去编号重复 | 保留首条，删除其余 | ✅ 自动 |
    | 反向题与总分负相关 | 按 (1+7) − x 反向计分 | ✅ 自动 |
    | 合计 ≠ 分项之和 | 只给建议，**不改**（无法判断谁错） | ⛔ 人工 |
    | 越界 / 小数 / 常数列 / 差值规律 / 直线作答 / 缺失 | 只给建议 | ⛔ 人工 |

    返回：
        {
          "actions": [ {kind, auto, level, title, detail, columns, rows, n} ],
          "changes": [ {row, column, before, after} ],   # 逐格改动（最多 300 条）
          "changes_truncated": bool,
          "stats": {cells_changed, rows_removed, auto_actions, manual_actions},
          "rows", "cols", "clean_csv"
        }

    注意：`actions[].rows` 是**原始数据**的 Excel 行号；若清洗过程删了行，
    这些行号与清洗后副本的行号不再一一对应（文档已注明，供用户回溯原始记录）。
    """
    if report is None:
        report = run_datacheck(df)

    clean = df.copy()
    issues = list(report.get("issues") or [])
    actions: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    # 去重延后统一处理：先删行会打乱"原始 Excel 行号"，故留到最后
    dedup_jobs: list[tuple[str, list[str], list[int], str]] = []

    for it in issues:
        try:
            cat = str(it.get("category", ""))
            title = str(it.get("title", ""))
            cols = [str(c) for c in (it.get("columns") or [])]
            rows = list(it.get("rows") or [])
            lv = str(it.get("level", LEVEL_LOW))

            # ① 重复行 → 排队，最后统一去重
            if cat == "重复" and "完全重复" in title:
                dedup_jobs.append(("dedup_exact", [str(c) for c in clean.columns], rows,
                                   f"删除 {len(rows)} 行完全重复记录"))
                continue
            if cat == "重复" and "去掉编号" in title:
                dedup_jobs.append(("dedup_body", cols, rows, "删除去掉编号列后重复的记录"))
                continue

            # ② 反向题漏反向计分 → 立即反向计分（此时尚未删行，索引 = 原始索引）
            if cat == "量表" and "负相关" in title and cols:
                touched = 0
                for col in cols:
                    if col not in clean.columns:
                        continue
                    new, changed_idx = _reverse_scored(clean[col])
                    for idx in changed_idx:
                        changes.append({
                            "row": _excel_row(idx), "column": col,
                            "before": _jsonable(clean.at[idx, col]),
                            "after": _jsonable(new.at[idx]),
                        })
                    clean[col] = new
                    touched += len(changed_idx)
                if touched:
                    actions.append(_act(
                        "reverse_score", True, lv,
                        f"对 {len(cols)} 个反向题做了反向计分",
                        f"按「(1+7) − 原值」折算，使题项方向与总分一致（共改 {touched} 个单元格）。",
                        cols, rows, touched))
                continue

            # ③ 合计 ≠ 分项和 → 只建议，不自动改（无法判断是合计错还是分项错）
            if cat == "一致性" and "对不上" in title and len(cols) >= 2:
                actions.append(_act(
                    "sum_overwrite", False, lv,
                    f"建议核对「{cols[0]}」与分项之和",
                    f"共 {len(rows)} 行对不上。**未自动修改**：无法判断是「{cols[0]}」错、"
                    f"还是某个分项错 —— 请核对原始记录后再决定用谁覆盖谁。",
                    cols, rows, len(rows)))
                continue

            # ④ 其余 → 人工确认
            actions.append(_act(
                "manual", False, lv, title,
                str(it.get("suggestion") or "请人工核对。"), cols, rows, len(rows)))
        except Exception:  # noqa: BLE001 - 单条问题处理失败不拖垮整体
            continue

    # 统一应用去重（保留首条；行号明细已在上面按原始索引记好）
    removed = 0
    for kind, cols, rows, title in dedup_jobs:
        try:
            before = len(clean)
            if kind == "dedup_exact":
                clean = clean.drop_duplicates(keep="first")
                detail = f"在全部列上取值完全相同的记录只保留第一条（共删 {before - len(clean)} 行）。"
            else:
                sub = [c for c in cols if c in clean.columns]
                if not sub:
                    continue
                keep_body = clean[sub].notna().all(axis=1)
                drop = clean.duplicated(subset=sub, keep="first") & keep_body
                clean = clean[~drop]
                detail = f"忽略编号列后内容相同的记录只保留第一条（共删 {before - len(clean)} 行）。"
            clean = clean.reset_index(drop=True)
            n = before - len(clean)
            if n > 0:
                removed += n
                actions.append(_act(kind, True, LEVEL_MID, title, detail, cols, rows, n))
        except Exception:  # noqa: BLE001
            continue

    # 排序：自动修复在前，其余按严重度
    actions.sort(key=lambda a: (0 if a["auto"] else 1, _LEVEL_ORDER.get(a["level"], 9)))

    return {
        "actions": actions,
        "changes": changes[:_MAX_CHANGES],
        "changes_truncated": len(changes) > _MAX_CHANGES,
        "stats": {
            "cells_changed": len(changes),
            "rows_removed": removed,
            "auto_actions": sum(1 for a in actions if a["auto"]),
            "manual_actions": sum(1 for a in actions if not a["auto"]),
        },
        "rows": int(len(clean)),
        "cols": int(clean.shape[1]),
        "clean_csv": _to_csv(clean),
    }


if __name__ == "__main__":  # 手工自测：python datacheck.py
    demo = pd.DataFrame({
        "学号": [1, 2, 3, 4, 5, 6],
        "分项1": [10, 20, 30, 40, 50, 60],
        "分项2": [10, 20, 30, 40, 50, 60],
        "总分": [20, 40, 60, 80, 100, 125],
        "年龄": [19, 20, 21, 22, 23, 24],
    })
    rep = run_datacheck(demo)
    print(rep["summary"]["verdict"])
    for it in rep["issues"]:
        print(f"[{it['level']}] {it['title']} :: {it['evidence']}")
