"""学术级取证测试（P2 · Benford + 末位偏好）· 纯本地，零 API
================================================================
这两个检测器的**生命线是误报率** —— 把一份完全正常的数据报成"可疑"，
工具的可信度就一次性归零。所以本套件的组织方式是**双边验证**：

  1. **该报的必须报**（真阳性）：编造数据 / 取整偏好必须触发
  2. **不该报的绝不报**（真阴性）：正常数据必须沉默
  3. **真阴性必须是"判出来的"，不是"被闸门跳过的"**（关键）
     —— 每个真阴性都要额外断言它确实通过了所有前置闸门，
        否则门一开就误报，测试却还是绿的。这是本套件最重要的一条。
  4. **闸门本身**：列名/样本量/量程/唯一值数 各有其防误报的理由，逐个测
  5. **文案红线**：只说"线索/请核对"，永不断言造假；级别恒为 MID
  6. **永不崩**：脏输入不抛异常
  7. **单一真源**：GRIMMER 只在 `grimmer.py`，`datacheck` 不得再有一份

跑法：.venv/Scripts/python.exe forensics_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import datacheck as dc  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _frame(**cols) -> pd.DataFrame:
    return pd.DataFrame(cols)


# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------
def _benford_perfect(n_total: int = 1000) -> list[float]:
    """**完美服从**本福特定律的数据（真阴性原料）。

    首位数字的比例严格等于 log10(1+1/d)。
    构造：尾数 mant ∈ [d, d+0.097)，再乘 10^k —— 首位恒为 d，
    且不同 (d, k) 的取值区间互不重叠，保证唯一值足够多。
    """
    vals: list[float] = []
    cnt = 0
    for d in range(1, 10):
        k = int(round(dc._BENFORD_EXPECTED[d - 1] * n_total))
        for j in range(k):
            mant = d + (cnt % 97) / 1000.0
            vals.append(mant * (10 ** (j % 4)))
            cnt += 1
    return vals


def _benford_fabricated() -> list[float]:
    """**编造痕迹明显**的数据（真阳性原料）。

    首位数字**均匀分布**（1-9 各占约 11%），而本福特期望首位 1 占 30.1%。
    这正是"随手编数字"的典型指纹 —— 人编数时首位是均匀想的。
    """
    vals: list[float] = []
    for k in range(4):
        for d in range(1, 10):
            for i in range(10):
                vals.append((d + i / 100.0) * (10 ** k))
    return vals


def _bp_heaping() -> list[float]:
    """人工读血压的**取整偏好**（真阳性原料）：大量落在整十与整五。"""
    seq: list[float] = []
    for i in range(60):
        seq.append(float(100 + (i % 5) * 10))      # 100/110/120/130/140
    for i in range(20):
        seq.append(float(105 + (i % 5) * 10))      # 105/115/.../145
    for i in range(20):
        seq.append(float(101 + i))                 # 一点真实散布 101-120
    return seq


def _uniform_terminals(n: int = 200) -> list[float]:
    """末位**均匀**的连续量（真阴性原料）：末位 0-9 各占 10%。"""
    return [100.0 + i for i in range(n)]


# ===========================================================================
section("1. 契约：两个检测器已注册且会被跑")
# ===========================================================================
_labels = [lab for lab, _ in dc._CHECKS]
check("「本福特分布」已注册", "本福特分布" in _labels, str(_labels))
check("「末位偏好」已注册", "末位偏好" in _labels, str(_labels))

_rep = dc.run_datacheck(_frame(x=[1.0, 2.0, 3.0]))
_ran = (_rep.get("summary") or {}).get("checks_run") or []
check("run_datacheck 实跑「本福特分布」", "本福特分布" in _ran, str(_ran))
check("run_datacheck 实跑「末位偏好」", "末位偏好" in _ran, str(_ran))
check("报告结构含 summary/issues",
      "summary" in _rep and "issues" in _rep, str(list(_rep)))


# ===========================================================================
section("2. 本福特期望分布与 MAD 刻度")
# ===========================================================================
_exp = dc._BENFORD_EXPECTED
check("期望分布共 9 项", len(_exp) == 9, str(len(_exp)))
check("首位 1 的期望 ≈ 30.1%", abs(_exp[0] - 0.30103) < 1e-4, f"{_exp[0]:.5f}")
check("概率和 ≈ 1", abs(sum(_exp) - 1.0) < 1e-9, f"{sum(_exp):.9f}")
check("单调递减（首位越大越罕见）",
      all(_exp[i] > _exp[i + 1] for i in range(8)), str(_exp))

check("MAD 0.005 → 高度符合", dc._mad_conformity(0.005) == "高度符合")
check("MAD 0.006 → 可接受（边界取严格小于）", dc._mad_conformity(0.006) == "可接受")
check("MAD 0.012 → 勉强符合", dc._mad_conformity(0.012) == "勉强符合")
check("MAD 0.015 → 不符合（门槛：超过才报）", dc._mad_conformity(0.015) == "不符合")
check("MAD 0.059 → 不符合", dc._mad_conformity(0.059) == "不符合")


# ===========================================================================
section("3. Benford · 真阳性：编造数据必须报")
# ===========================================================================
_fab = _benford_fabricated()
_df_fab = _frame(交易额=_fab)
check("编造数据样本量 ≥ 100", len(_fab) >= 100, str(len(_fab)))
check("编造数据**通过了所有前置闸门**（不是被跳过）",
      dc._benford_eligible(_df_fab["交易额"], "交易额") is True)

_iss_fab = dc.check_benford(_df_fab)
check("编造数据触发本福特告警", len(_iss_fab) == 1, f"issues={len(_iss_fab)}")
if _iss_fab:
    _it = _iss_fab[0]
    check("类别为「本福特」", _it.get("category") == "本福特", str(_it.get("category")))
    check("级别为 MID", _it.get("level") == dc.LEVEL_MID, str(_it.get("level")))
    check("标题含 MAD 数值", "MAD=" in (_it.get("title") or ""), _it.get("title"))
    check("标题含符合度结论", "不符合" in (_it.get("title") or ""), _it.get("title"))
    check("证据含卡方（仅供参考）", "卡方=" in (_it.get("evidence") or ""))
    check("相关列指向该列", _it.get("columns") == ["交易额"], str(_it.get("columns")))


# ===========================================================================
section("4. Benford · 真阴性：正常数据必须沉默（且是判出来的）")
# ===========================================================================
_perf = _benford_perfect()
_df_perf = _frame(交易额=_perf)
check("完美本福特数据样本量 ≥ 100", len(_perf) >= 100, str(len(_perf)))
check("完美本福特数据**通过了所有前置闸门**",
      dc._benford_eligible(_df_perf["交易额"], "交易额") is True)
check("完美本福特数据唯一值 ≥ 50",
      _df_perf["交易额"].nunique() >= 50, str(_df_perf["交易额"].nunique()))
check("完美本福特数据跨 ≥ 2 个数量级",
      float(_df_perf["交易额"].max()) / float(_df_perf["交易额"].min()) >= 100,
      f"ratio={float(_df_perf['交易额'].max()) / float(_df_perf['交易额'].min()):.1f}")
check("完美本福特数据不告警（真阴性）",
      dc.check_benford(_df_perf) == [], str(dc.check_benford(_df_perf)))

# —— 闸门逐个验证：每一道门都有它防误报的理由 ——
check("闸门①：列名含「金额」→ 跳过",
      dc._benford_eligible(pd.Series(_fab), "订单金额") is False)
check("闸门①：列名含「score」→ 跳过",
      dc._benford_eligible(pd.Series(_fab), "total_score") is False)
check("闸门①：列名含「百分比」→ 跳过",
      dc._benford_eligible(pd.Series(_fab), "完成百分比") is False)
check("闸门①：列名含「年份」→ 跳过",
      dc._benford_eligible(pd.Series(_fab), "年份") is False)
check("闸门②：含负数或 0 → 跳过",
      dc._benford_eligible(pd.Series(list(_fab[:-1]) + [-1.0]), "交易额") is False)
check("闸门③：样本量 < 100 → 跳过",
      dc._benford_eligible(pd.Series(_fab[:80]), "交易额") is False)
check("闸门④：跨不到 2 个数量级 → 跳过",
      dc._benford_eligible(pd.Series([10.0 + i * 0.01 for i in range(300)]), "读数") is False)
check("闸门⑤：唯一值 < 50 → 跳过",
      dc._benford_eligible(pd.Series([1.0, 500.0] * 150), "读数") is False)
check("闸门（回归）：线性均匀编造数据**不被**量程门误杀",
      dc._benford_eligible(pd.Series(_fab), "交易额") is True,
      "若此处为 False，说明有人又加回了会让它永不触发的分位比闸门")


# ===========================================================================
section("5. 末位数字取值（历史 bug 护栏）")
# ===========================================================================
# 曾因直接对 repr(float) 取末位字符，把 120.0 的小数点后那个 0 当成末位，
# 导致任何整数列全部读成 0 —— 既假阳性、又让检测器形同虚设。
check("120.0 → 0（不是小数点后的 0 被误读成别的值）", dc._terminal_digit(120.0) == 0,
      str(dc._terminal_digit(120.0)))
check("125.0 → 5", dc._terminal_digit(125.0) == 5, str(dc._terminal_digit(125.0)))
check("123 → 3", dc._terminal_digit(123) == 3, str(dc._terminal_digit(123)))
check("1000 → 0", dc._terminal_digit(1000) == 0, str(dc._terminal_digit(1000)))
check("12.34 → 4", dc._terminal_digit(12.34) == 4, str(dc._terminal_digit(12.34)))
check("0.0420 → 2（末尾补位零不算）", dc._terminal_digit(0.0420) == 2,
      str(dc._terminal_digit(0.0420)))
check("12.50 → 5", dc._terminal_digit(12.50) == 5, str(dc._terminal_digit(12.50)))
check("负数 -123 → 3（符号不干扰）", dc._terminal_digit(-123) == 3,
      str(dc._terminal_digit(-123)))
check("0 → 0", dc._terminal_digit(0) == 0, str(dc._terminal_digit(0)))
check("科学计数法 → None", dc._terminal_digit(1.2e-12) is None)
check("NaN → None", dc._terminal_digit(float("nan")) is None)
check("inf → None", dc._terminal_digit(float("inf")) is None)
check("非数字 → None", dc._terminal_digit("abc") is None)
check("None → None", dc._terminal_digit(None) is None)


# ===========================================================================
section("6. 末位偏好 · 真阳性：人工取整必须报")
# ===========================================================================
_bp = _bp_heaping()
_df_bp = _frame(收缩压=_bp)
check("血压数据样本量 ≥ 50", len(_bp) >= 50, str(len(_bp)))
check("血压数据量程 ≥ 10", max(_bp) - min(_bp) >= 10,
      f"range={max(_bp) - min(_bp)}")
check("血压数据唯一值 ≥ 5", _df_bp["收缩压"].nunique() >= 5,
      str(_df_bp["收缩压"].nunique()))

_iss_bp = dc.check_terminal_digits(_df_bp)
check("取整偏好触发末位告警", len(_iss_bp) == 1, f"issues={len(_iss_bp)}")
if _iss_bp:
    _it = _iss_bp[0]
    check("类别为「末位偏好」", _it.get("category") == "末位偏好", str(_it.get("category")))
    check("级别为 MID", _it.get("level") == dc.LEVEL_MID, str(_it.get("level")))
    check("标题点名偏好的数字（0）", "偏好 0" in (_it.get("title") or ""), _it.get("title"))
    check("证据含卡方与 p", "卡方=" in (_it.get("evidence") or "")
          and "p=" in (_it.get("evidence") or ""))
    check("相关列指向该列", _it.get("columns") == ["收缩压"], str(_it.get("columns")))

# 末位**只剩** 0 和 5 的极端取整也必须报（不能有"缺数字就跳过"的误杀）。
# 注意：取值个数下限是 5 —— 低于 5 个取值基本等同分类变量，末位没有信息量，
# 那是**有意**放行，不是误杀。所以这里用 6 个取值来构造真阳性。
_df_extreme = _frame(舒张压=[80.0, 85.0, 90.0, 95.0, 100.0, 105.0] * 40)
check("末位只剩 0/5 的极端取整也要报（不得因缺数字而误杀）",
      len(dc.check_terminal_digits(_df_extreme)) == 1,
      f"issues={len(dc.check_terminal_digits(_df_extreme))}")
check("取值个数下限为 5：< 5 时按设计放行（分类变量不查末位）",
      dc.check_terminal_digits(_frame(舒张压=[80.0, 85.0, 90.0, 95.0] * 40)) == [])


# ===========================================================================
section("7. 末位偏好 · 真阴性：正常数据必须沉默（且是判出来的）")
# ===========================================================================
_uni = _uniform_terminals(200)
_df_uni = _frame(体重=_uni)
check("均匀分布数据样本量 ≥ 50", len(_uni) >= 50, str(len(_uni)))
check("均匀分布数据量程 ≥ 10", max(_uni) - min(_uni) >= 10)
check("均匀分布数据唯一值 ≥ 5", _df_uni["体重"].nunique() >= 5)
check("均匀分布数据末位确实均匀（0-9 各 10%）",
      all(sum(1 for v in _uni if dc._terminal_digit(v) == d) == 20 for d in range(10)))
check("末位均匀 → 不告警（真阴性）",
      dc.check_terminal_digits(_df_uni) == [], str(dc.check_terminal_digits(_df_uni)))

# 真随机的正态连续量也不该报
_rng = np.random.default_rng(20260914)
_df_norm = _frame(反应时=[float(round(x, 2)) for x in _rng.normal(420, 60, 300)])
check("正态连续量 → 不告警（真阴性）",
      dc.check_terminal_digits(_df_norm) == [], str(dc.check_terminal_digits(_df_norm)))

# —— 闸门 ——
check("闸门：列名含「编号」→ 跳过",
      dc.check_terminal_digits(_frame(问卷编号=_bp)) == [])
check("闸门：列名含「id」→ 跳过",
      dc.check_terminal_digits(_frame(record_id=_bp)) == [])
check("闸门：样本量 < 50 → 跳过",
      dc.check_terminal_digits(_frame(收缩压=_bp[:30])) == [])
check("闸门：量程 < 10（量表 1-5）→ 跳过",
      dc.check_terminal_digits(_frame(题项1=[float(1 + i % 5) for i in range(200)])) == [])
check("闸门：唯一值 < 5 → 跳过",
      dc.check_terminal_digits(_frame(等级=[1.0, 2.0, 3.0] * 100)) == [])


# ===========================================================================
section("8. 文案红线：只报线索，永不断言造假")
# ===========================================================================
_ACCUSE = ("造假属实", "确属造假", "可以认定造假", "已确认造假", "存在造假行为")
_all_forensic = dc.check_benford(_df_fab) + dc.check_terminal_digits(_df_bp)
check("取证类告警共 2 条", len(_all_forensic) == 2, str(len(_all_forensic)))
for _it in _all_forensic:
    _blob = " ".join(str(_it.get(k) or "") for k in
                     ("title", "evidence", "explain", "suggestion"))
    _cat = _it.get("category")
    check(f"「{_cat}」级别不是 HIGH（取证只是线索）",
          _it.get("level") != dc.LEVEL_HIGH, str(_it.get("level")))
    check(f"「{_cat}」建议含免责「不代表造假」", "不代表造假" in _blob)
    check(f"「{_cat}」无肯定式造假指控",
          not any(w in _blob for w in _ACCUSE), _blob[:80])


# ===========================================================================
section("9. 永不崩：脏输入不抛异常")
# ===========================================================================
_dirty = [
    ("空表", _frame()),
    ("全 NaN 列", _frame(x=[float("nan")] * 120)),
    ("字符串列", _frame(x=[f"s{i}" for i in range(120)])),
    ("混合类型", _frame(x=[1.0, "a", None, 3.5] * 40)),
    ("全零", _frame(x=[0.0] * 200)),
    ("单值重复", _frame(x=[7.0] * 200)),
    ("含 inf", _frame(x=[float("inf"), 1.0, 2.0] * 60)),
]
for _name, _d in _dirty:
    try:
        _a = dc.check_benford(_d)
        _b = dc.check_terminal_digits(_d)
        _ok = isinstance(_a, list) and isinstance(_b, list)
    except Exception as _e:  # noqa: BLE001
        _ok = False
        print(f"    ! {_name} 抛异常：{_e}")
    check(f"脏输入不崩：{_name}", _ok)


# ===========================================================================
section("10. 示例数据零误报（生命线）")
# ===========================================================================
_here = os.path.dirname(os.path.abspath(__file__))
_csv_dir = os.path.join(_here, "examples")
_csvs = sorted(f for f in os.listdir(_csv_dir) if f.endswith(".csv")) \
    if os.path.isdir(_csv_dir) else []
check("找到示例数据", len(_csvs) > 0, str(_csvs))
_total = 0
for _f in _csvs:
    try:
        _d = pd.read_csv(os.path.join(_csv_dir, _f))
        _n = len(dc.run_datacheck(_d).get("issues") or [])
    except Exception as _e:  # noqa: BLE001
        _n = -1
        print(f"    ! {_f} 读取/体检异常：{_e}")
    _total += max(0, _n)
    check(f"示例「{_f}」零误报（issues={_n}）", _n == 0)
check("全部示例合计 0 条误报", _total == 0, str(_total))


# ===========================================================================
section("11. 单一真源：GRIMMER 只在 grimmer.py")
# ===========================================================================
check("datacheck 仍提供 GRIM 纯函数（查均值）", hasattr(dc, "grim_check"))
check("datacheck **不再**自带 grimmer_check（避免口径分裂）",
      not hasattr(dc, "grimmer_check"))
try:
    import grimmer  # noqa: E402

    check("grimmer 模块可用", hasattr(grimmer, "grimmer_check"))
except Exception as _e:  # noqa: BLE001
    check("grimmer 模块可用", False, str(_e))

check("GRIM：30 × 3.47 = 104.1 → 不可能", dc.grim_check(3.47, 30) is False)
check("GRIM：30 × 3.5 = 105 → 可能", dc.grim_check(3.5, 30) is True)
check("GRIM：100 × 3.47 = 347 → 可能", dc.grim_check(3.47, 100) is True)
check("GRIM：n 非法 → 放行（不冤枉人）", dc.grim_check(3.47, 0) is True)


# ===========================================================================
print()
print("=" * 72)
print(f"取证测试：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
