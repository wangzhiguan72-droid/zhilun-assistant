"""P3 · 论文表格数字 vs 原始数据交叉核查
================================================
论文正文里的统计量（"M = 3.47"）已被 `audit.grim_cross_check` 覆盖；
但**表格**里的数字一直没有核查 —— 而表格恰恰是论文造假/笔误的高发区：
复制粘贴错行、手抄四舍五入、换数据后忘更新表……

本模块补上这一块：解析论文里的**描述统计表**，把每个单元格的
「声称值」与原始数据的「实算值」逐一比对。

## 支持的表形态

1. **Markdown 表格**（`.md` 论文、以及从 docx 转来的文本）
       | 时间点 | M | SD |
       | --- | ---: | ---: |
       | 前测 | 60.90 | 3.42 |
2. **管道分隔行**：`read_paper_text` 把 docx 表格转成的 `a | b | c` 形态
3. 表头可含 `M` / `均值` / `平均值` / `Mean`（均值列）
   与 `SD` / `标准差` / `Std`（标准差列）

## 设计边界（与项目红线一致）

- **纯函数**：不 import app，可被 CLI / 副驾驶 / audit 复用。
- **只报可疑，永不判造假**：结论措辞统一为「表里写的 X 与数据实算的 Y 不一致，请核对」。
- **防误报**：
  - 样本量不等时不比（论文表的 n 与数据 n 不同 → 分母不同，比了必然报错）
  - 容差按"论文报告的位数"动态定（写 60.9 就用 0.05，写 60.90 就用 0.005）
  - 只比"行标签能在数据列名里找到对应"的行（找不到就跳过，不猜）
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

#: 均值列的表头关键词
_MEAN_HEADERS = ("m", "mean", "均值", "平均值", "平均数", "平均分", "平均")
#: 标准差列的表头关键词
_SD_HEADERS = ("sd", "s.d.", "std", "标准差", "标准偏差", "标准差sd")

#: 明显不是统计表的表头（出现即跳过整表，避免把文献表/人口学表当统计表）
_SKIP_HEADER_TOKENS = ("作者", "年份", "文献", "来源", "期刊", "序号", "编号")

#: 数值单元格：可带负号、千分位、百分号、正负号
_NUM_CELL = re.compile(r"^-?\d+(?:,\d{3})*(?:\.\d+)?%?$")
#: 形如 "3.47±0.52" 或 "3.47(0.52)" 的合并单元格
_MERGE_CELL = re.compile(r"^(-?\d+(?:\.\d+)?)\s*[±(（]\s*(-?\d+(?:\.\d+)?)\s*[)）]?$")


def _norm_header(s: str) -> str:
    """表头归一化：去空格、去星号、小写。"""
    return re.sub(r"[\s*_]+", "", str(s)).lower().strip()


def _is_mean_header(h: str) -> bool:
    n = _norm_header(h)
    return n in {_norm_header(x) for x in _MEAN_HEADERS} or n in ("m", "mean")


def _is_sd_header(h: str) -> bool:
    n = _norm_header(h)
    return n in {_norm_header(x) for x in _SD_HEADERS}


def _to_num(cell: str) -> float | None:
    """单元格 → 数字。支持 1,234.5 / 3.47% / -0.52。"""
    s = str(cell).strip().replace(" ", "")
    if not s or not _NUM_CELL.match(s):
        return None
    pct = s.endswith("%")
    s = s.rstrip("%").replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if pct else v


def _decimals_of(raw: str) -> int:
    """论文报告了几位小数（决定比对容差）。"""
    s = str(raw).strip().rstrip("%").replace(",", "")
    if "." not in s:
        return 0
    return len(s.split(".", 1)[1])


def parse_markdown_tables(text: str) -> list[dict[str, Any]]:
    """从论文文本里抽出所有「管道分隔表」。

    识别规则（两种形态都支持）：
        - Markdown 表格：连续 ≥2 行、每行含 `|`，且第 2 行是 `---` 分隔行
        - 管道行：连续 ≥2 行的 `a | b | c`（docx 表格被 `read_paper_text` 转成的形态）

    ⚠️ 两个已踩过的坑（改动前务必读 `table_check_test.py` 的 1.5 / 1.6）：
        1. 分隔行判定不能只做字符集检查（内部 `|` 剥不掉）→ 见 `_is_sep`。
        2. 孤立的纯分隔行必须**在形态 B 之前**跳过。若交给形态 B 处理，
           `while` 一次都不进、`i = j = i` 原地打转 → **死循环**。

    返回：
        [{"header": [str...], "rows": [[str...]], "title": str}, ...]
    """
    lines = (text or "").splitlines()
    tables: list[dict[str, Any]] = []
    i = 0
    n = len(lines)

    def _cells(line: str) -> list[str]:
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    def _is_sep(line: str) -> bool:
        """`| --- | :---: |` 这类分隔行。

        ⚠️ 不能用 `set(body) <= set("-:")` 判 —— `strip("|")` 只剥首尾，
        内部的 `|` 还在，字符集永远含 `|`（实测踩过：整个解析器静默返回 0 张表）。
        正确做法：**拆成单元格**后逐一检查。
        """
        s = line.strip()
        if "|" not in s:
            return False
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            return False
        for c in cells:
            if not c:
                continue
            if not set(c) <= set("-:"):
                return False
        return any(c for c in cells)   # 至少要有一个非空单元格

    while i < n:
        line = lines[i]
        if "|" not in line:
            i += 1
            continue
        # 孤立的纯分隔行（前面没有表头）——直接跳过。
        # ⚠️ 必须放在形态 B 之前：否则下面的 while 循环一次都不进，
        #    `i = j = i` 原地打转 → **死循环**（实测踩过，整个测试挂满超时）。
        if _is_sep(line):
            i += 1
            continue

        # 形态 A：Markdown 表格（下一行是分隔行）
        if i + 1 < n and _is_sep(lines[i + 1]):
            header = _cells(line)
            rows: list[list[str]] = []
            j = i + 2
            while j < n and "|" in lines[j] and not _is_sep(lines[j]):
                rows.append(_cells(lines[j]))
                j += 1
            # 往上找最近的标题行（"### 表 4-1 ..." / "**表 1** ..."）
            title = ""
            k = i - 1
            while k >= 0 and k >= i - 4:
                t = lines[k].strip()
                if t and not t.startswith("|"):
                    if re.match(r"^#{1,6}\s", t) or "表" in t[:12]:
                        title = re.sub(r"^#{1,6}\s*", "", t)
                        break
                    if t:
                        title = t
                        break
                k -= 1
            if header and rows:
                tables.append({"header": header, "rows": rows, "title": title})
            i = j
            continue

        # 形态 B：连续管道行（docx 表格形态，无分隔行）
        #   以"当前行是管道行、且上一行不是管道行"作为块起点。
        #   这样无论是块首还是块中的任意位置进来都能正确切块。
        prev_pipe = i > 0 and "|" in lines[i - 1] and not _is_sep(lines[i - 1])
        if not prev_pipe:
            block: list[list[str]] = []
            j = i
            while j < n and "|" in lines[j] and not _is_sep(lines[j]):
                block.append(_cells(lines[j]))
                j += 1
            if len(block) >= 2:
                tables.append({"header": block[0], "rows": block[1:], "title": ""})
            if j > i:
                i = j
                continue

        i += 1

    return tables


def _match_column(label: str, df: pd.DataFrame) -> str | None:
    """把表格行标签（如"前测"）对到数据列名（如"前测"）。

    策略（从严到宽，任一命中即返回）：
        1. 完全相同
        2. 归一化后相同（去空格/符号）
        3. 行标签是列名的子串（"干预1个月" vs "1个月"）
    """
    if label is None:
        return None
    lab = str(label).strip()
    if not lab:
        return None
    cols = [str(c) for c in df.columns]
    if lab in cols:
        return lab
    norm = re.sub(r"[\s_\-（）()：:、]+", "", lab)
    for c in cols:
        if re.sub(r"[\s_\-（）()：:、]+", "", c) == norm:
            return c
    for c in cols:
        cn = re.sub(r"[\s_\-（）()：:、]+", "", c)
        if cn and (cn in norm or norm in cn) and len(cn) >= 2:
            return c
    return None


def table_cross_check(raw_text: str, df: pd.DataFrame, *,
                      abs_tol_floor: float = 1e-9) -> dict[str, Any]:
    """论文描述统计表 vs 原始数据 交叉核查。

    对每张表的每个 (行, 均值列) / (行, 标准差列)：
        - 用行标签匹配到数据列
        - 实算该列的 mean / std
        - 与表里写的值比，超出容差 → 记一条 mismatch

    容差规则：**按论文报告的位数动态定**
        写 "60.9"  → 容差 0.05（四舍五入到 1 位）
        写 "60.90" → 容差 0.005
    即"如果你只是四舍五入，我不会冤枉你"。

    返回：
        {
          "tables": int,            # 解析到几张统计表
          "checked": int,           # 比对了几格
          "mismatches": [ {...} ],  # 不一致的格子
          "skipped": [ {...} ],     # 跳过原因（行标签对不上等）
          "note": str,
        }
    """
    result: dict[str, Any] = {
        "tables": 0, "checked": 0, "mismatches": [], "skipped": [],
        "note": "",
    }
    if df is None or df.empty:
        result["note"] = "没有原始数据，无法做表格核查。"
        return result

    tables = parse_markdown_tables(raw_text or "")
    # 只保留"含均值列或标准差列"的表 —— 其余（文献表/人口学表）不是统计结果表
    stat_tables = []
    for t in tables:
        hdr = t["header"]
        if any(tok in " ".join(hdr) for tok in _SKIP_HEADER_TOKENS):
            continue
        if any(_is_mean_header(h) or _is_sd_header(h) for h in hdr):
            stat_tables.append(t)
    result["tables"] = len(stat_tables)

    n_data = int(len(df))

    for t in stat_tables:
        hdr = t["header"]
        # 找出均值/标准差列的下标
        mean_idx = [i for i, h in enumerate(hdr) if _is_mean_header(h)]
        sd_idx = [i for i, h in enumerate(hdr) if _is_sd_header(h)]
        label_idx = 0  # 第 0 列约定为行标签

        for row in t["rows"]:
            if len(row) <= label_idx:
                continue
            label = row[label_idx]
            col = _match_column(label, df)
            if col is None:
                result["skipped"].append({
                    "table": t["title"], "label": label,
                    "reason": "表里的行标签在数据里找不到对应列",
                })
                continue
            # 只对数值列算
            try:
                series = pd.to_numeric(df[col], errors="coerce").dropna()
            except Exception:  # noqa: BLE001
                continue
            if len(series) < 2:
                result["skipped"].append({
                    "table": t["title"], "label": label,
                    "reason": "该列有效数值不足 2 个",
                })
                continue

            real_mean = float(series.mean())
            real_sd = float(series.std())   # 样本 SD（与论文惯例一致）

            for idx, kind in ([(i, "mean") for i in mean_idx]
                              + [(i, "sd") for i in sd_idx]):
                if idx >= len(row):
                    continue
                raw = row[idx]
                claimed = _to_num(raw)
                if claimed is None:
                    # 支持 "3.47±0.52" 合并格：均值在第 i 列、SD 在 i+1 列常见
                    m = _MERGE_CELL.match(str(raw).strip())
                    if m and kind == "mean":
                        claimed = float(m.group(1))
                    elif m and kind == "sd":
                        claimed = float(m.group(2))
                    else:
                        continue
                real_val = real_mean if kind == "mean" else real_sd
                # 动态容差：按论文报告位数
                dec = _decimals_of(raw)
                tol = max(abs_tol_floor, 0.5 * (10 ** -dec))
                result["checked"] += 1
                diff = abs(real_val - claimed)
                if diff > tol:
                    result["mismatches"].append({
                        "table": t["title"],
                        "label": str(label),
                        "column": col,
                        "kind": kind,               # mean / sd
                        "claimed": claimed,
                        "real": round(real_val, 6),
                        "diff": round(diff, 6),
                        "tol": tol,
                        "n": int(len(series)),
                        "raw": str(raw),
                        "explain": _explain_mismatch(kind, claimed, real_val,
                                                     int(len(series)), diff, tol),
                    })

    if not stat_tables:
        result["note"] = ("论文里没找到含「M/均值」或「SD/标准差」列的表格，"
                          "跳过表格核查。")
    elif result["mismatches"]:
        result["note"] = (f"核查了 {result['checked']} 个表格数字，"
                          f"发现 {len(result['mismatches'])} 处不一致。")
    elif result["checked"] == 0:
        # 找到了统计表，但一格都没比成（行标签全对不上）——
        # **绝不能报"一致"**，那等于假装查过了。如实说"未能比对"。
        result["note"] = (f"找到 {len(stat_tables)} 张统计表，但 "
                          f"{len(result['skipped'])} 个单元格的行标签在原始数据里"
                          "找不到对应列，本次未能比对。请检查表格行名与数据列名是否一致。")
    else:
        result["note"] = (f"核查了 {result['checked']} 个表格数字，"
                          f"与原始数据实算值一致。")
    return result


def _explain_mismatch(kind: str, claimed: float, real: float, n: int,
                      diff: float, tol: float) -> str:
    """人话解释（恒定含"请核对"、不含造假指控）。"""
    what = "均值" if kind == "mean" else "标准差"
    return (f"表格写的{what}是 {claimed:g}，但用上传的原始数据实算（n={n}）得到 "
            f"{real:.4f}，相差 {diff:.4f}（超出容差 {tol:g}）。"
            f"常见原因：换过数据但表格没更新、复制错行、手抄时四舍五入过头，"
            f"或该组样本量口径与原始数据不同（如剔除过缺失）。**请核对**，"
            f"这不代表造假。")


def render_table_section(check: dict[str, Any]) -> list[str]:
    """把表格核查结果渲染成 Markdown 行（供 audit 报告拼接）。"""
    lines: list[str] = []
    if not check or not check.get("tables"):
        return lines

    lines.append("\n**表格数字一致性核查（论文表格 vs 原始数据）：**\n")
    mm = check.get("mismatches", [])
    skipped = check.get("skipped", [])

    # 全一致但"一格都没比成"（行标签全对不上）——必须说实话，
    # 否则用户会误以为"核查通过"，其实什么都没查。
    if not mm and check.get("checked", 0) == 0 and skipped:
        lines.append(f"- ⚠️ 找到了 {check['tables']} 张统计表，但 "
                     f"{len(skipped)} 个单元格的行标签在原始数据里找不到对应列，"
                     "本次表格核查**未能比对**。请检查表格行名与数据列名是否一致。")
        return lines

    if not mm:
        lines.append(f"- ✅ 共核查 {check.get('checked', 0)} 个表格数字，"
                     f"与原始数据实算值一致。")
        if skipped:
            lines.append(f"  （另有 {len(skipped)} 个单元格因行标签对不上而跳过。）")
        return lines

    lines.append("| 表格 | 行 | 项目 | 表格写的 | 数据实算 | 差 | 容差 |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    for m in mm:
        kind_cn = "均值" if m["kind"] == "mean" else "标准差"
        lines.append(
            f"| {m['table'] or '（无标题）'} | {m['label']} | {kind_cn} | "
            f"{m['raw']} | {m['real']:.4f} | {m['diff']:.4f} | {m['tol']:g} |")
    lines.append(
        f"\n> ⚠️ 有 {len(mm)} 处表格数字与原始数据不一致。请核对是否换过数据、"
        "复制错行、或该组样本量口径不同。**这不等于造假**，只说明这些数字需要解释。"
    )
    if skipped:
        lines.append(f"\n（另有 {len(skipped)} 个单元格因行标签对不上而跳过。）")
    return lines
