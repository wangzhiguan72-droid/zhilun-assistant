"""
论文表格取证（v2.32）
==============================
对论文 docx 表格里的**数字本身**做取证——不依赖作者是否上传原始数据。
参考"耿同学式论文打假"的朴素思路,把产品里跑在用户数据上的取证四刀
移植到论文表格上:

    1. 末位数字偏好(凑数据的人末位往往取整/聚集,如大量 x.0 / x.5)
    2. 小数尾串重复(小数点后数字大量相同,如一列全是 .33/.67)
    3. 行/列合计一致性(合计 ≠ 分项之和,硬矛盾)
    4. GRIM 表格版(n × 均值 在报告位数下不可达——"出现概率极低"的线索)

红线(与产品取证纪律一致):
    - 级别最高只到「可疑」,文案恒含"这只是线索,不代表造假";
    - 闸门保守:数字太少/位数太少一律不判,宁漏勿误;
    - 纯函数、零 LLM、不落盘。

输入:extract_paper.read_paper_tables 的输出([表 → 行 → 单元格文本])。
输出 issue 卡片与 datacheck._issue 同构(level/category/title/evidence/
explain/suggestion/rows/columns),可直接并入体检卡片流渲染。
"""
from __future__ import annotations

import re
from collections import Counter
from math import lcm  # noqa: F401  (占位:GRIM 粒度推导无需,保留可读性)

from datacheck import LEVEL_MID, LEVEL_LOW

#: 合计类单元格关键词(与 datacheck._TOTAL_KEYWORDS 对齐,表格场景补充英文)
_TOTAL_RE = re.compile(r"合计|总计|总数|总和|总额|小计|total|sum", re.IGNORECASE)
#: 样本量单元格关键词
_N_RE = re.compile(r"^(n|N|例数|人数|样本量|样本数|被试数|受试者)$|^(n|N)[=：:]")
#: 均值±标准差 / M±SD 形态
_MSD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[±±]\s*(\d+(?:\.\d+)?)")
#: 裸数字(含百分号剥离)
_NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%?")
#: 均值列头
_MEAN_HDR_RE = re.compile(r"均值|平均|平均分|M\b|mean", re.IGNORECASE)

_MIN_CELLS = 5          # 一张表至少 5 个数字才算数据表(再小无核对价值)
_MIN_DECIMAL_NUMS = 40  # 末位/尾串判定需要的"带小数的数字"总量
_MIN_TAIL_COUNT = 5     # 尾串至少出现 5 次才谈"重复"
_TAIL_SHARE = 0.30      # 且占比 ≥30%


def _issue(level: str, category: str, title: str, evidence: str,
           explain: str, suggestion: str) -> dict:
    return {"level": level, "category": category, "title": title,
            "evidence": evidence, "explain": explain, "suggestion": suggestion,
            "rows": [], "columns": []}


def _nums_in(cell: str) -> list[float]:
    return [float(m.group(1)) for m in _NUM_RE.finditer(cell or "")]


def _decimals_in(cell: str) -> list[int]:
    out = []
    for m in _NUM_RE.finditer(cell or ""):
        frac = (m.group(1).split(".") + [""])[1]
        out.append(len(frac))
    return out


def _table_numbers(tables):
    """扁平化:所有表格里的 (表号, 值, 原文本, 小数位)。"""
    flat = []
    for ti, rows in enumerate(tables, 1):
        for ri, row in enumerate(rows):
            for cell in row:
                raw = cell or ""
                for m in _NUM_RE.finditer(raw):
                    val = float(m.group(1))
                    frac = (m.group(1).split(".") + [""])[1]
                    flat.append((ti, ri, val, m.group(1), len(frac)))
    return flat


# ──────────────────────────────────────────────────────────────────────
# 检查 1 & 2:末位偏好 + 小数尾串重复(合并一次遍历)
# ──────────────────────────────────────────────────────────────────────
def check_digits(flat) -> list[dict]:
    issues = []
    dec = [(v, raw) for _t, _r, v, raw, d in flat if d > 0]
    if len(dec) < _MIN_DECIMAL_NUMS:
        return issues

    # 1) 末位数字分布(双门槛,与 datacheck 末位偏好一致:卡方 p<0.001 且 ≥25%)
    last = Counter(int(raw.replace("-", "").replace(".", "")[-1])
                   for v, raw in dec)
    n = sum(last.values())
    top_d, top_c = last.most_common(1)[0]
    share = top_c / n
    exp = n / 10.0
    chi2 = sum((c - exp) ** 2 / exp for c in
               [last.get(d, 0) for d in range(10)])
    p_lt001 = chi2 >= 29.588  # df=9, p=0.001 临界值(近似)
    if p_lt001 and share >= 0.25:
        issues.append(_issue(
            LEVEL_MID, "表格取证·末位偏好",
            f"论文表格共 {n} 个小数,末位数字「{top_d}」占 {share:.0%}",
            f"末位分布卡方 χ²={chi2:.1f}(df=9,p<0.001);最高末位 {top_d} 出现 "
            f"{top_c} 次。人工凑数/随手取整常表现为末位聚集(如大量 .0/.5)。",
            "末位偏好是统计学取证线索,不是结论——量表的求和、百分比换算"
            "都可能天然改变末位分布。",
            "对照原始数据复核这些表格;无法提供原始数据的,在论文中说明"
            "数值来源与舍入规则。这只是线索,不代表造假。"))

    # 2) 小数尾串重复(小数点后最后 2 位相同)
    tails = Counter()
    for v, raw in dec:
        frac = raw.split(".")[-1].lstrip("0") or raw.split(".")[-1]
        tail = frac[-2:] if len(frac) >= 2 else frac
        tails[tail] += 1
    top_tail, tail_c = tails.most_common(1)[0]
    if tail_c >= _MIN_TAIL_COUNT and tail_c / n >= _TAIL_SHARE:
        issues.append(_issue(
            LEVEL_MID, "表格取证·尾数重复",
            f"小数尾串「.{top_tail}」出现 {tail_c} 次,占全部小数的 {tail_c/n:.0%}",
            f"共 {n} 个带小数的数值,最高频尾串 .{top_tail} 占 {tail_c/n:.0%}"
            f"(均匀情况下两位尾串期望占比约 1%)。",
            "同一批描述统计里大量相同尾串,常见于复制粘贴同一结果或"
            "按同一模板编数。",
            "逐表核对这批 .{} 尾数对应的统计量是否各自独立计算;"
            "这只是线索,不代表造假。".format(top_tail)))
    return issues


# ──────────────────────────────────────────────────────────────────────
# 检查 3:行/列合计一致性
# ──────────────────────────────────────────────────────────────────────
def check_totals(tables) -> list[dict]:
    issues = []
    for ti, rows in enumerate(tables, 1):
        flat_cnt = sum(len(_nums_in(c)) for r in rows for c in r)
        if flat_cnt < _MIN_CELLS:
            continue
        # 行方向:某行含"合计"单元格 → 其余数字之和应等于它
        for ri, row in enumerate(rows):
            nums_per_cell = [_nums_in(c) for c in row]
            total_cells = [i for i, c in enumerate(row) if _TOTAL_RE.search(c or "")]
            if not total_cells:
                continue
            for ti_cell in total_cells:
                tv = _nums_in(row[ti_cell])
                if len(tv) != 1:
                    continue
                others = [v for i, per in enumerate(nums_per_cell)
                          if i != ti_cell for v in per]
                if len(others) < 2:
                    continue
                s = sum(others)
                if abs(s - tv[0]) > max(0.05, 5e-3 * max(abs(s), 1)):
                    issues.append(_issue(
                        LEVEL_MID, "表格取证·合计矛盾",
                        f"第 {ti} 张表「{row[ti_cell][:12]}」行:分项之和 {s:.2f} ≠ "
                        f"合计 {tv[0]:g}",
                        f"该行其余数字之和为 {s:.4f},与写出的合计 {tv[0]:g} 不符"
                        f"(差 {abs(s-tv[0]):.4f}),超过舍入可解释的范围。",
                        "合计与分项不相等是硬矛盾——要么分项抄错,要么合计"
                        "是另一次计算的结果。",
                        "回到原始数据重算该行;若分项有缺漏(如「其他」未列),"
                        "在表注中说明。"))
        # 列方向:最后一行是"合计"行 → 每列上方的和应等于它
        if len(rows) >= 3:
            last = rows[-1]
            if any(_TOTAL_RE.search(c or "") for c in last[:2]):
                ncol = max(len(r) for r in rows)
                for ci in range(1, ncol):
                    col_vals = []
                    for r in rows[:-1]:
                        if ci < len(r):
                            vs = _nums_in(r[ci])
                            if len(vs) == 1:
                                col_vals.append(vs[0])
                    cv = _nums_in(last[ci]) if ci < len(last) else []
                    if len(cv) == 1 and len(col_vals) >= 2:
                        s = sum(col_vals)
                        if abs(s - cv[0]) > max(0.05, 5e-3 * max(abs(s), 1)):
                            issues.append(_issue(
                                LEVEL_LOW, "表格取证·合计矛盾",
                                f"第 {ti} 张表第 {ci+1} 列:上方之和 {s:.2f} ≠ "
                                f"合计行 {cv[0]:g}",
                                f"该列上方 {len(col_vals)} 个数之和为 {s:.4f},"
                                f"合计行写的是 {cv[0]:g}。",
                                "列合计不符最常见的原因是漏掉一行或把百分数"
                                "与计数混加。",
                                "核对列内每一行是否都应计入合计。"))
    return issues


# ──────────────────────────────────────────────────────────────────────
# 检查 4:GRIM 表格版(n × 均值 可达性)
# ──────────────────────────────────────────────────────────────────────
def check_grim(tables) -> list[dict]:
    issues = []
    for ti, rows in enumerate(tables, 1):
        header = rows[0] if rows else []
        for ri, row in enumerate(rows[1:], start=2):
            # n 在哪:本行或表头里的"n/例数/样本量"列
            n_val = None
            for ci, cell in enumerate(row):
                hdr_is_n = (ci < len(header)
                            and bool(_N_RE.search(header[ci] or "")))
                cell_is_n = bool(_N_RE.search(cell or ""))
                if not (hdr_is_n or cell_is_n):
                    continue
                m = re.search(r"[=：:]\s*(\d+)", cell or "")
                own = _nums_in(cell)
                if m:                       # 形态一:"n=30" 自带数字
                    n_val = int(m.group(1))
                elif len(own) == 1:         # 形态二:表头 n 列的 "30"
                    n_val = int(own[0])
                elif cell_is_n and ci + 1 < len(row):  # 形态三:"n"|"30"
                    nxt = _nums_in(row[ci + 1])
                    if len(nxt) == 1:
                        n_val = int(nxt[0])
                if n_val is not None:
                    break
            if not n_val or n_val < 3 or n_val > 100000:
                continue
            # 均值:本行的 M±SD 或 均值列小数
            means = []
            for ci, cell in enumerate(row):
                msd = list(_MSD_RE.finditer(cell or ""))
                for m in msd:
                    means.append((float(m.group(1)), len(m.group(1).split(".")[-1]),
                                  m.group(0)))
                if msd:
                    # 本格是 M±SD 形态:SD 那半不是均值,绝不能进 GRIM
                    continue
                if ci > 0 and any(_MEAN_HDR_RE.search(h or "") for h in header[ci:ci+1]):
                    for v, d in zip(_nums_in(cell), _decimals_in(cell)):
                        if d > 0:
                            means.append((v, d, str(v)))
            for mean, dec, raw in means:
                if dec > 3:
                    continue
                prod = n_val * mean
                if abs(prod - round(prod)) > 1e-6:
                    issues.append(_issue(
                        LEVEL_MID, "表格取证·GRIM 可达性",
                        f"第 {ti} 张表:n={n_val} 与均值 {raw} 组合不可达",
                        f"n × 均值 = {n_val} × {mean} = {prod:.6f},不是"
                        f"{1/10**dec:g} 的整数倍——在整数计分数据下,"
                        f"n={n_val} 的样本**算不出**保留 {dec} 位小数的均值 "
                        f"{mean}。",
                        "GRIM 核查(Allular/Heathers 2017)是学术取证常用线索:"
                        "这类矛盾通常意味着均值来自另一次计算、n 写错,"
                        "或数字系拼凑。",
                        "回查该表的 n 与均值是否来自同一次统计;"
                        "这只是线索,不代表造假。"))
                    break  # 每行只报第一个
    return issues


def audit_paper_tables(tables) -> dict:
    """主入口:对论文 docx 表格做取证,返回 {ok, tables, numeric_cells, issues}。"""
    tables = tables or []
    flat = _table_numbers(tables)
    issues = []
    try:
        issues += check_digits(flat)
    except Exception:  # noqa: BLE001
        pass
    try:
        issues += check_totals(tables)
    except Exception:  # noqa: BLE001
        pass
    try:
        issues += check_grim(tables)
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "tables": len(tables),
        "numeric_cells": len(flat),
        "decimal_numbers": sum(1 for *_x, d in flat if d > 0),
        "issues": issues,
        "note": "表格取证只覆盖 docx 结构化表格;每条发现都只是线索,"
                "不代表造假。阈值刻意保守(数字少于 40 个不做末位/尾串判定)。",
    }
