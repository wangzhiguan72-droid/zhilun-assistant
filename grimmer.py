"""GRIMMER 检验 · 学术级取证（P2 补全）
================================================
`datacheck.grim_check` 只能查**均值**是否可能；本模块补上它的姊妹检验
**GRIMMER**（Granularity-Related Inconsistency of Means and Standard
Deviations with Error）——查**标准差 SD** 是否可能。

三者关系（学术圈通称 "GRIM 家族"）：

| 检验 | 输入 | 判据 |
| --- | --- | --- |
| GRIM | 均值 mean、n | n × mean 必须是粒度整数倍 |
| **GRIMMER** | 均值 mean、SD、n | 在"存在某个满足 GRIM 的整数序列"前提下，SD 必须落在一段**离散可达集合**里 |
| GRIMMER-2 | 均值 mean、SD、n、items | 同上，但每人得分是 items 项之和（粒度 items） |

## 算法来源

Nick Brown & James Heathers 的 `rsprite2` / `GRIMMER` 实现（Anaya 2016
提出，Brown & Heathers 2017 工具化）。核心结论：

> 给定 n、均值 m，**能产生该均值的最小/最大整数序列**是唯一确定的
> （"最集中"与"最分散"两种极端）。所有可行序列的 SD 都落在
> [sd_min, sd_max] 这个区间内，且由于数据是**整数**，可达的 SD 是
> **离散**的。若论文报的 SD 落在区间外、或不在离散点的容差内，
> 则该 (mean, sd, n) 组合**不可能**由真实整数数据产生。

## 设计边界

- **纯函数**：不 import app / datacheck，可被 CLI / 副驾驶 / audit 复用。
- **零依赖**：只用标准库（`math`、`fractions`），不引 numpy。
- **只报"不可能"，不判"造假"**：与项目四条红线一致。
- 复用 `datacheck.grim_check` 的口径：GRIM 不过就直接判不可能（均值都
  不可能，SD 无从谈起）。**延迟导入**避免与 datacheck 形成循环依赖。

## 已知限制（写进文档，避免被当万能工具）

GRIMMER 假设**整数原始数据**（Likert 量表、整数计数）。对于本身带小数的
数据（如反应时 0.001 秒精度），本检验**不适用**，应跳过并说明。
另外：通过 GRIMMER **不代表数据真实**，只代表"这个组合有可能"。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

#: 浮点比较容差（SD 是浮点结果，允许少量误差）
_SD_TOL = 1e-6


def _grim_ok(mean: float, n: int, items: int, decimals: int = 2) -> bool:
    """复用 datacheck.grim_check 的口径（延迟导入避免循环依赖）。

    `decimals` 是论文实际报告的位数——GRIM 判据的容差正比于它
    （`tol = 0.5 × 10⁻ᵈᵉᶜ × n`），必须一路传下去，否则 1 位小数与
    4 位小数的表会用同一个容差判，两个方向都会判错。

    兜底分支**刻意不再自己重写一遍算法**：那正是 v2.38 修掉的三处同源
    bug 的成因（另两处见 datacheck.grim_verdict / paper_table_forensics.
    check_grim）。取不到共用核心时按**弃权**处理（返回 True = 不报），
    这与三态口径一致，也符合红线 #2：宁可不报，绝不误伤。
    """
    try:
        from datacheck import GRIM_IMPOSSIBLE, grim_verdict  # noqa: PLC0415
        return grim_verdict(mean, n, items=items,
                            decimals=decimals) != GRIM_IMPOSSIBLE
    except Exception:  # noqa: BLE001 - 取不到共用核心时弃权，不自行判定
        return True


def _integer_sum(mean: float, n: int, items: int) -> int:
    """该均值对应的"总分"（整数）：n × 题均分 × items。

    口径（v2.40 修正）：论文对多题量表报的是**题均分** `M = 各题之和 ÷ items`，
    所以每人的原始总分是 `M × items`，n 人的总和即 `n × M × items`。
    v2.39 及以前写成 `n × mean ÷ items`——粒度方向正好反了（把题均分的粒度
    当成了 1/items，其实有 1/items 粒度的是每人总分），items>1 时会得到一个
    小得离谱的总分，SD 区间随之坍塌 → 漏报或误报。

    调用前应先用 `_grim_ok` 确认它是整数（或近似整数）。
    """
    prod = float(mean) * int(n) * max(1, int(items))
    return int(round(prod))


def _sd_bounds(mean: float, n: int, items: int,
               lo: int | None = None,
               hi: int | None = None) -> tuple[float, float]:
    """给定 n 与题均分，能产生的 **SD（题均分口径）** 的闭区间 [下界, 上界]。

    构造两种极端整数序列（每人**总分** S_person = mean × items 固定）：

    - **最集中**：所有值尽量贴近均值 → SD 最小
    - **最分散**：两极序列（k 个下界、(n-k) 个上界）→ SD 最大

    `lo`/`hi` 是单题取值的合法域（如 Likert 1–5）。**强烈建议传入**：
    不传时默认下界 0、上界不限，会把不现实的极端序列算进上界 → 漏报。

    ⚠️ **量纲**：内部序列是**每人总分**，所以上面算出的 SD 是总分口径的；
    而论文报的 SD 是**题均分**口径的，两者差一个 `items` 因子。返回前除以
    `items` 折算回题均分口径，否则 items>1 时上界被放大 items 倍 → 漏报。
    （items=1 时该折算为恒等，既有调用点逐位不变。）
    """
    total = _integer_sum(mean, n, items)
    if n <= 1:
        return 0.0, 0.0
    g = max(1, int(items))

    # --- 最集中：把 total 尽量均匀分成 n 份 ---
    base, rem = divmod(total, n)
    concentrated = [base + 1] * rem + [base] * (n - rem)
    sd_min = _sd(concentrated)

    # --- 最分散：两极序列 ---
    spread = _maximally_spread(total, n, lo=lo, hi=hi)
    sd_max = _sd(spread)

    return min(sd_min, sd_max) / g, max(sd_min, sd_max) / g


def _maximally_spread(total: int, n: int, lo: int | None = None,
                      hi: int | None = None) -> list[int]:
    """在总和 = total、个数 = n、每项整数且落在 [lo, hi] 内，求 SD 最大的序列。

    ## 为什么不能只试"两极序列"

    当 `hi` **不设限**时，SD 最大的序列确实只用两个取值（k 个 lo、
    n-k 个 U）。但一旦 `hi` 有限（如 Likert 1–5），最优解通常需要
    **三个取值**：a 个 lo、b 个 hi、c 个中间值 x 用来补足总和。

    反例（n=30、总和 120、域 [1,5]）：真正的最大 SD 是 1.693，
    序列为 `[1]×7 + [5]×22 + [3]×1`；若只试两极序列，会算出 0.0
    —— **漏报一整档**（把不可能的 SD 判成可能）。这是实测踩到的坑。

    ## 算法

    枚举 (a, b)：a 个 lo、b 个 hi，剩下 c = n-a-b 个取同一个中间值 x。
    由总和约束解出 x = (total - a·lo - b·hi) / c，取整且落在 [lo, hi] 时
    该切分可行，计算其 SD。O(n²) 枚举，n ≤ 数千时性能无虞。

    若 `hi is None`，退化为一维两极枚举（x 无上界，等价于两取值）。
    """
    if n <= 1:
        return [total]
    L = 0 if lo is None else int(lo)
    if hi is None:
        # --- 无上界：两极枚举（k 个 L、(n-k) 个 U）---
        best: list[int] | None = None
        best_sd = -1.0
        for k in range(0, n):
            m = n - k
            if m <= 0:
                continue
            rem = total - k * L
            if rem < 0 or rem % m:
                continue
            U = rem // m
            if U < L:
                continue
            seq = [U] * m + [L] * k
            sd = _sd(seq)
            if sd > best_sd:
                best_sd, best = sd, seq
        if best is not None:
            return best
        base, r = divmod(total, n)
        return [base + 1] * r + [base] * (n - r)

    # --- 有上界：枚举 (a, b)，c 个中间值 x 补足 ---
    H = int(hi)
    best2: list[int] | None = None
    best_sd2 = -1.0
    for a in range(0, n + 1):
        for b in range(0, n - a + 1):
            c = n - a - b
            base_sum = a * L + b * H
            if c == 0:
                if base_sum == total:
                    seq = [L] * a + [H] * b
                    sd = _sd(seq)
                    if sd > best_sd2:
                        best_sd2, best2 = sd, seq
                continue
            rem = total - base_sum
            if rem % c:
                continue
            x = rem // c
            if x < L or x > H:
                continue
            seq = [L] * a + [H] * b + [x] * c
            sd = _sd(seq)
            if sd > best_sd2:
                best_sd2, best2 = sd, seq
    if best2 is not None:
        return best2
    base, r = divmod(total, n)
    return [base + 1] * r + [base] * (n - r)


def _sd(values: list[int]) -> float:
    """总体标准差（GRIMMER 用总体口径，与论文报表口径一致）。"""
    n = len(values)
    if n <= 1:
        return 0.0
    m = sum(values) / n
    return (sum((v - m) ** 2 for v in values) / n) ** 0.5


def grimmer_check(mean: float, sd: float, n: int, *, items: int = 1,
                  lo: int | None = None, hi: int | None = None,
                  decimals: int = 2) -> dict[str, Any]:
    """GRIMMER 检验：论文报告的 (均值, SD, n) 组合是否**可能**。

    `lo` / `hi` 为单题取值的合法域（如 Likert 1–5 传 `lo=1, hi=5`）。
    **强烈建议传入**：不传时默认下界 0、上界不限，SD 上界会被高估 → 漏报。
    若论文写明了量表范围（几乎都会写），就该传。

    `decimals` 是论文**实际报告**的小数位数（默认 2）。它决定 GRIM 前置
    那一步的容差（`0.5 × 10⁻ᵈᵉᶜ × n`），必须按论文原样传。

    返回结构化结论（而非简单的 True/False），因为答辩场景需要解释：

        {
          "possible": bool,      # 该组合是否可能
          "applicable": bool,    # 本检验是否适用（样本量/输入是否合理）
          "reason": str,         # 人话结论
          "sd_min": float,       # 可达 SD 下界
          "sd_max": float,       # 可达 SD 上界
        }

    判定顺序（任一不过即"不可能"）：
        1. 输入合法性（n >= 2、sd >= 0、mean 有限）
        2. GRIM：n × mean 必须落在 items 整数倍的舍入容差内（均值本身要先可能）
        3. SD 落在 [sd_min, sd_max] 内（含容差）

    ⚠️ 文案红线：**只报"不可能 / 可疑，请核对"，永不判定"造假"。**
    """
    result: dict[str, Any] = {
        "possible": True,
        "applicable": True,
        "reason": "",
        "sd_min": 0.0,
        "sd_max": 0.0,
    }

    # --- 1. 输入合法性 ---
    try:
        mean = float(mean)
        sd = float(sd)
        n = int(n)
    except (TypeError, ValueError):
        result.update(applicable=False, reason="输入不是有效数字，无法检验。")
        return result
    if n < 2:
        result.update(applicable=False, reason="样本量不足 2，GRIMMER 不适用。")
        return result
    if sd < 0:
        result.update(applicable=False, reason="标准差为负，输入有误。")
        return result
    if items < 1:
        items = 1

    # --- 2. GRIM 前置：均值本身必须可能 ---
    if not _grim_ok(mean, n, items, decimals):
        half = 0.5 * (10.0 ** -max(0, int(decimals)))
        result.update(
            possible=False,
            reason=(f"均值 {mean} 在 n={n} 下不可能（GRIM 不通过）："
                    f"{n} × {mean} = {mean * n:.6g}，与任何合法取值的差都超过"
                    f"报告 {decimals} 位小数所允许的舍入容差 ±{half:g}"
                    f"（即该均值对应的总分区间内没有 {items} 的整数倍）。"
                    f"均值都不成立，标准差更无从谈起。"),
        )
        return result

    # --- 3. SD 区间判定 ---
    sd_min, sd_max = _sd_bounds(mean, n, items, lo=lo, hi=hi)
    result["sd_min"], result["sd_max"] = round(sd_min, 6), round(sd_max, 6)

    if sd < sd_min - _SD_TOL:
        result.update(
            possible=False,
            reason=(f"标准差 {sd} 偏小，超出 n={n}、均值 {mean} 下可达的最小值 "
                    f"{sd_min:.4f}——整数数据的离散程度不可能这么低。"),
        )
        return result
    if sd > sd_max + max(_SD_TOL, sd_max * 1e-6):
        result.update(
            possible=False,
            reason=(f"标准差 {sd} 偏大，超出 n={n}、均值 {mean} 下可达的最大值 "
                    f"{sd_max:.4f}——整数数据的离散程度不可能这么高。"),
        )
        return result

    result["reason"] = (
        f"该组合可能：标准差 {sd} 落在 n={n}、均值 {mean} 下的可达区间 "
        f"[{sd_min:.4f}, {sd_max:.4f}] 内。"
        f"（注意：这只说明'有可能'，不代表数据真实。）"
    )
    return result


def grimmer_cross_check(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量交叉核查：给一批论文声称的 (mean, sd, n)，逐条报告不可能的项。

    `pairs` 形如：
        [{"label": "实验组", "mean": 3.46, "sd": 0.52, "n": 30,
          "items": 1, "lo": 1, "hi": 5, "decimals": 2}, ...]

    `lo`/`hi` 为该量表的合法取值域（**建议提供**，否则 SD 上界偏高会漏报）。
    `decimals` 为该论文实际报告的小数位数（缺省 2）。

    返回**只含不通过项**的列表（每条附 label / reason / 可达区间），
    便于直接塞进 `audit.py` 的改进建议。
    """
    out: list[dict[str, Any]] = []
    for p in pairs or []:
        try:
            r = grimmer_check(
                p.get("mean"), p.get("sd"), p.get("n"),
                items=int(p.get("items", 1) or 1),
                lo=(int(p["lo"]) if p.get("lo") is not None else None),
                hi=(int(p["hi"]) if p.get("hi") is not None else None),
                decimals=int(p.get("decimals", 2) or 0),
            )
        except Exception:  # noqa: BLE001 - 单条失败不影响整批
            continue
        if r.get("applicable") and not r.get("possible"):
            out.append({
                "label": str(p.get("label") or "未命名"),
                "mean": p.get("mean"),
                "sd": p.get("sd"),
                "n": p.get("n"),
                "sd_min": r["sd_min"],
                "sd_max": r["sd_max"],
                "reason": r["reason"],
            })
    return out
