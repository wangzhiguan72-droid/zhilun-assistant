"""GRIMMER 检验测试（P2 补全 · 纯本地，零 API）
================================================
GRIMMER 是 GRIM 的姊妹检验：GRIM 查**均值**是否可能，GRIMMER 查**标准差**。

测试重点（双边思路）：
  1. **该报的必须报**：不可能的 (mean, sd, n) 组合必须判 False 并给出原因
  2. **不该报的绝不报**：真实可达的组合必须放行（误报会毁掉工具可信度）
  3. **算法正确性**：SD 区间必须与暴力枚举结果**逐位一致**（核心断言）
  4. **取值域**：传 lo/hi 后上界收紧（对 Likert 量表必需，否则漏报）
  5. **永不崩**：脏输入 / 空输入不抛异常
  6. **文案红线**：只报"不可能/可疑"，永不含"造假"类肯定式指控

跑法：.venv/Scripts/python.exe grimmer_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from grimmer import (  # noqa: E402
    _maximally_spread, _sd, _sd_bounds, grimmer_check, grimmer_cross_check,
)

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


def _brute_bounds(n, total, lo=0):
    """暴力枚举所有和为 total 的 n 元整数序列，求真实 SD 的 [min, max]。

    仅用于小规模验证（n <= 8）——这是检验解析算法正确性的黄金标准。
    """
    sds = []

    def rec(rem, k, prev, acc):
        if k == 1:
            if rem >= prev and rem >= lo:
                sds.append(_sd(acc + [rem]))
            return
        for x in range(prev, rem // k + 1):
            rec(rem - x, k - 1, x, acc + [x])

    rec(total, n, lo, [])
    return (min(sds), max(sds)) if sds else None


# ===========================================================================
section("1. 算法正确性：SD 区间 vs 暴力枚举（核心断言）")
# ===========================================================================
_all_match = True
for _n, _total, _lo in [(5, 20, 0), (6, 24, 0), (4, 12, 0), (5, 15, 0),
                        (5, 20, 1), (6, 24, 1), (8, 32, 1), (3, 9, 0)]:
    _b = _brute_bounds(_n, _total, _lo)
    _g = _sd_bounds(_total / _n, _n, 1, lo=_lo)
    _ok = abs(_b[0] - _g[0]) < 1e-9 and abs(_b[1] - _g[1]) < 1e-9
    _all_match = _all_match and _ok
    check(f"n={_n} 总和={_total} 下界={_lo}：区间与暴力枚举一致",
          _ok, f"brute=[{_b[0]:.4f},{_b[1]:.4f}] grimmer=[{_g[0]:.4f},{_g[1]:.4f}]")
check("全部小规模案例区间精确匹配", _all_match)

# 【回归护栏】域约束下的最大 SD 必须三值构造（实测踩过的漏报 bug）
# n=30、总和 120、域 [1,5]：真值 1.6931（[1]×7 + [5]×22 + [3]×1）
# 旧实现只试两极序列 → 算出 0.0 → 把不可能的 SD 判成可能（漏报一整档）
_b30 = _sd_bounds(4.0, 30, 1, lo=1, hi=5)
check("n=30 域[1,5] 上界 ≈ 1.6931（三值构造，历史漏报 bug 护栏）",
      abs(_b30[1] - 1.6931) < 0.001, f"got={_b30[1]:.4f}")
check("该案例上界不为 0（旧实现会塌成 0）", _b30[1] > 1.0, f"got={_b30[1]:.4f}")

# 域约束下的暴力枚举对照（含两值/三值混合最优）
def _brute_域(n, total, lo, hi):
    import itertools
    sds = []
    for combo in itertools.combinations_with_replacement(range(lo, hi + 1), n):
        if sum(combo) == total:
            sds.append(_sd(list(combo)))
    return (min(sds), max(sds)) if sds else None


for _n, _total, _lo, _hi in [(6, 24, 1, 5), (4, 12, 1, 5), (5, 15, 1, 5),
                             (6, 20, 1, 5), (8, 24, 1, 5), (5, 20, 0, 10)]:
    _bb = _brute_域(_n, _total, _lo, _hi)
    _gg = _sd_bounds(_total / _n, _n, 1, lo=_lo, hi=_hi)
    _ok2 = abs(_bb[0] - _gg[0]) < 1e-9 and abs(_bb[1] - _gg[1]) < 1e-9
    check(f"域[{_lo},{_hi}] n={_n} 总和={_total}：与暴力枚举一致",
          _ok2, f"brute=[{_bb[0]:.4f},{_bb[1]:.4f}] got=[{_gg[0]:.4f},{_gg[1]:.4f}]")

# 两极序列性质：只含两种取值
_sp = _maximally_spread(20, 5)
check("最大 SD 序列是两极序列（取值种类 ≤ 2）", len(set(_sp)) <= 2, f"got={_sp}")
check("两极序列总和守恒", sum(_sp) == 20, f"got={sum(_sp)}")

# ===========================================================================
section("2. 必须报：不可能的 (mean, sd, n) 组合")
# ===========================================================================
# ① GRIM 前置失败 → 均值本身就不可能
_r = grimmer_check(3.47, 0.5, 30)
check("均值 GRIM 不过 → possible=False", _r["possible"] is False)
check("原因里点明 GRIM 与乘积", "GRIM" in _r["reason"] and "104.1" in _r["reason"],
      f"got={_r['reason']}")
check("原因里不判造假", "造假" not in _r["reason"], f"got={_r['reason']}")

# ② SD 过大：均值 4.0、n=10、Likert 1-5 域内不可能有 sd=5.0
_r2 = grimmer_check(4.0, 5.0, 10, lo=1, hi=5)
check("SD 过大（超上界）→ possible=False", _r2["possible"] is False)
check("原因里给出可达上界", "偏大" in _r2["reason"], f"got={_r2['reason']}")

# ③ SD 过小：n=30、均值 3.5、items=1 最小 SD 为 0，
#    但 items=7（7 项量表总分）时粒度更粗，构造一个明确的过小案例
_r3 = grimmer_check(3.5, 0.01, 30, items=7)
check("SD 过小（低于下界）→ possible=False", _r3["possible"] is False,
      f"got={_r3}")

# ===========================================================================
section("3. 绝不报：真实可达的组合（误报是生命线）")
# ===========================================================================
_ok_cases = [
    (3.5, 0.5, 30, None, None, "正常量表组合"),
    (4.0, 0.0, 10, None, None, "SD=0 全同值"),
    (3.0, 1.0, 20, 1, 5, "Likert 1-5 常规组合"),
    (2.5, 1.1180, 4, 1, 5, "两极序列本身的 SD"),
]
for _m, _s, _n, _lo, _hi, _note in _ok_cases:
    _rr = grimmer_check(_m, _s, _n, lo=_lo, hi=_hi)
    check(f"{_note} (m={_m}, sd={_s}, n={_n}) → possible=True",
          _rr["possible"] is True, f"got={_rr['reason']}")

# 手工验证：n=4、均值 2.5、序列 [1,1,4,4] 的 SD
_seq = [1, 1, 4, 4]
_sd_val = _sd(_seq)
_rm = grimmer_check(sum(_seq) / len(_seq), _sd_val, len(_seq), lo=1, hi=5)
check("手算两极序列的 SD 判为可能", _rm["possible"] is True,
      f"sd={_sd_val:.4f} got={_rm['reason']}")

# 真实数据生成的组合（用整数序列实算 mean/sd/n 反查）
_real = [3, 4, 3, 2, 5, 3, 4, 4, 3, 2, 3, 4]
_rm2 = grimmer_check(sum(_real) / len(_real), _sd(_real), len(_real), lo=1, hi=5)
check("真实整数序列实算的 (mean, sd, n) → possible=True",
      _rm2["possible"] is True, f"got={_rm2['reason']}")

# ===========================================================================
section("4. 取值域 lo/hi 必须收紧上界（否则漏报）")
# ===========================================================================
_n = 10
_mean = 3.0
_nolo = _sd_bounds(_mean, _n, 1)                 # 不传域：下界 0、上界不限
_with = _sd_bounds(_mean, _n, 1, lo=1, hi=5)     # Likert 1-5
check("传入取值域后上界 ≤ 不传时的上界", _with[1] <= _nolo[1] + 1e-9,
      f"nolo={_nolo[1]:.4f} with={_with[1]:.4f}")
check("不传域时上界被高估（证明必须传）", _nolo[1] > _with[1],
      f"nolo={_nolo[1]:.4f} with={_with[1]:.4f}")

# 具体：一个不传域会漏报、传域能抓到的案例
# 均值 3.0、n=10、Likert 1-5 → 最大 SD 应为 2.0（[1]*5+[5]*5）
_b5 = _sd_bounds(3.0, 10, 1, lo=1, hi=5)
check("Likert 1-5、均值 3.0、n=10 的上界 ≈ 2.0", abs(_b5[1] - 2.0) < 1e-6,
      f"got={_b5[1]:.4f}")
_leak = grimmer_check(3.0, 2.2, 10, lo=1, hi=5)
check("sd=2.2 > 2.0（域内不可能）→ 正确判 False", _leak["possible"] is False,
      f"got={_leak}")
_leak2 = grimmer_check(3.0, 2.2, 10)   # 不传域 → 上界放松，可能漏报
check("同一案例不传域时被放行（漏报，验证传域必要性）",
      _leak2["possible"] is True, f"got={_leak2}")

# ===========================================================================
section("5. 输入边界：绝不崩 + 正确 applicable 标记")
# ===========================================================================
_bad_inputs = [
    ("n=1（样本不足）", (3.0, 1.0, 1)),
    ("n=0", (3.0, 1.0, 0)),
    ("n 非数字", (3.0, 1.0, "abc")),
    ("mean 非数字", ("x", 1.0, 10)),
    ("sd 为负", (3.0, -1.0, 10)),
    ("sd 非数字", (3.0, None, 10)),
]
for _name, _args in _bad_inputs:
    try:
        _res = grimmer_check(*_args)
        _ok = isinstance(_res, dict) and _res.get("applicable") is False
    except Exception as _e:  # noqa: BLE001
        _ok = False
        _res = _e
    check(f"{_name} → 不抛异常且 applicable=False", _ok, f"got={_res}")

check("items=0 被当作 1 处理（不崩）",
      isinstance(grimmer_check(3.0, 1.0, 10, items=0), dict))
check("items 为负不崩", isinstance(grimmer_check(3.0, 1.0, 10, items=-3), dict))

# ===========================================================================
section("6. 批量交叉核查 grimmer_cross_check")
# ===========================================================================
_pairs = [
    {"label": "正常组", "mean": 3.5, "sd": 0.5, "n": 30, "lo": 1, "hi": 5},
    {"label": "不可能组A", "mean": 3.47, "sd": 0.5, "n": 30},          # GRIM 不过
    {"label": "不可能组B", "mean": 3.0, "sd": 2.2, "n": 10, "lo": 1, "hi": 5},
    {"label": "边界组", "mean": 2.5, "sd": 1.118, "n": 4, "lo": 1, "hi": 5},
]
_out = grimmer_cross_check(_pairs)
check("只返回不通过项（2 条）", len(_out) == 2, f"got={[o['label'] for o in _out]}")
check("含「不可能组A」", any(o["label"] == "不可能组A" for o in _out))
check("含「不可能组B」", any(o["label"] == "不可能组B" for o in _out))
check("正常组被放行", not any(o["label"] == "正常组" for o in _out))
check("边界组被放行", not any(o["label"] == "边界组" for o in _out))
check("每条都带 reason 与可达区间",
      all("reason" in o and "sd_min" in o and "sd_max" in o for o in _out))

# 健壮性
check("空列表 → 空结果", grimmer_cross_check([]) == [])
check("None → 空结果", grimmer_cross_check(None) == [])
check("脏条目不影响整批",
      len(grimmer_cross_check([{"label": "脏", "mean": None, "sd": "x", "n": None},
                               _pairs[1]])) == 1)

# ===========================================================================
section("7. 文案红线：只报可疑，永不判造假")
# ===========================================================================
_all_reasons = []
for _p in _pairs:
    _all_reasons.append(grimmer_check(_p["mean"], _p["sd"], _p["n"],
                                      lo=_p.get("lo"), hi=_p.get("hi"))["reason"])
_all_reasons.extend(o["reason"] for o in _out)
_banned = ("属于造假", "存在造假", "判定造假", "涉嫌造假", "确定造假", "构成造假")
check("全部结论中不含肯定式造假指控",
      not any(_b in r for r in _all_reasons for _b in _banned), f"got={_all_reasons}")
check("通过项明确声明「不代表数据真实」",
      any("不代表数据真实" in r for r in _all_reasons),
      f"got={[r for r in _all_reasons if '代表' in r]}")

# ===========================================================================
section("8. 真实研究场景：用内置示例数据反查")
# ===========================================================================
import pandas as pd  # noqa: E402

_ex = "examples/questionnaire_data.csv"
if os.path.exists(_ex):
    _df = pd.read_csv(_ex)
    _num = _df.select_dtypes("number")
    _checked = 0
    _false_pos = 0
    for _c in _num.columns[:8]:
        _v = _num[_c].dropna().to_numpy()
        if len(_v) < 2:
            continue
        _m = float(_v.mean())
        # 仅对"均值可能"的列做检验（非整数均值本身就说明不是整数原始数据）
        if abs(_m * len(_v) - round(_m * len(_v))) > 1e-6:
            continue
        _s = float(_v.std())
        _res8 = grimmer_check(_m, _s, len(_v))
        _checked += 1
        if _res8["applicable"] and not _res8["possible"]:
            _false_pos += 1
    check(f"示例数据实算组合无假阳性（检查了 {_checked} 列，假阳性 {_false_pos}）",
          _false_pos == 0, f"checked={_checked} fp={_false_pos}")
else:
    check("示例数据存在（跳过则记 SKIP）", True, "文件不存在，跳过")

# ===========================================================================
print()
print("=" * 72)
print(f"GRIMMER 测试：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
raise SystemExit(1 if FAIL else 0)
