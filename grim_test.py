"""v2.1 · 论文侧 GRIM 交叉核查测试（audit.grim_cross_check + extract_paper mean）
================================================================================
GRIM（Granularity-Related Inconsistency of Means）：
论文报告「均值 = 3.47，样本 30 人」，而问卷是整数计分 ——
那么 30 × 3.47 = 104.1，不可能是任何 30 个整数之和。这个均值**不可能**出现。

测什么：
  1. `datacheck.grim_check` 与 `audit.grim_cross_check` 口径一致（两边同一纯函数）
  2. 不可能的均值必须被抓出来；可能的均值**不许误报**（生命线）
  3. extract_paper 能抽出 "M = 3.47" / "均值为 3.47"，且**不把随便一个数字当均值**
  4. 闭环：build_audit_report 的报告里出现 GRIM 表格 + 进改进建议
  5. 健壮性：没写均值 / 空数据 / n<=0 → 不崩、不制造噪音
  6. 红线：文案只说「不可能出现 / 需要解释」，绝不出现「造假」

跑法：.venv/Scripts/python.exe grim_test.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

import pandas as pd  # noqa: E402

import datacheck as DC  # noqa: E402
import audit as A  # noqa: E402
from extract_paper import extract_quantities  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ===========================================================================
section("1. 纯函数基线：grim_check")
# ===========================================================================
check("3.47 × 30 = 104.1 → 不可能", DC.grim_check(3.47, 30) is False)
check("3.50 × 30 = 105   → 可能", DC.grim_check(3.5, 30) is True)
check("3.47 × 100 = 347  → 可能（换样本量就行）", DC.grim_check(3.47, 100) is True)
check("n<=0 不崩（视为通过）", DC.grim_check(3.47, 0) is True)


# ===========================================================================
section("2. grim_cross_check：该抓的抓，不该抓的不抓")
# ===========================================================================
qs_impossible = [{"kind": "mean", "value": 3.47, "raw": "M = 3.47"}]
r = A.grim_cross_check(qs_impossible, 30)
check("检出 1 条", len(r) == 1, f"got={r}")
check("3.47 / n=30 → passed=False", r and r[0]["passed"] is False, f"got={r}")
check("记录乘积 104.1", r and abs(r[0]["product"] - 104.1) < 1e-6, f"got={r and r[0]}")
check("带上原文写法（便于定位）", r and r[0]["raw"] == "M = 3.47")

qs_ok = [{"kind": "mean", "value": 3.5, "raw": "M = 3.5"}]
r2 = A.grim_cross_check(qs_ok, 30)
check("3.5 / n=30 → passed=True（不许误报）", r2 and r2[0]["passed"] is True, f"got={r2}")

# 非 mean 类型的统计量不参与 GRIM
r3 = A.grim_cross_check([{"kind": "p", "value": 0.03, "raw": "p < 0.05"}], 30)
check("p 值不参与 GRIM", r3 == [], f"got={r3}")

# 多个均值混合
r4 = A.grim_cross_check([
    {"kind": "mean", "value": 3.47, "raw": "M = 3.47"},
    {"kind": "mean", "value": 4.0, "raw": "M = 4.0"},
], 30)
check("混合：只把不可能的标 False",
      r4 and r4[0]["passed"] is False and r4[1]["passed"] is True, f"got={r4}")


# ===========================================================================
section("3. 与 datacheck 口径一致（两处复用同一纯函数）")
# ===========================================================================
same = all(
    DC.grim_check(g["mean"], g["n"]) == g["passed"]
    for g in A.grim_cross_check(
        [{"kind": "mean", "value": v, "raw": f"M = {v}"}
         for v in (3.47, 3.5, 2.33, 4.0, 5.25)], 30)
)
check("grim_cross_check 与 grim_check 结论逐条一致", same)


# ===========================================================================
section("4. extract_paper：抽得出均值，也不乱抽")
# ===========================================================================
q = extract_quantities("两组均值对比：实验组 M = 3.47，对照组均值为 2.50。")
means = [x for x in q if x["kind"] == "mean"]
check("抽出 2 条均值", len(means) == 2, f"got={[x['raw'] for x in q]}")
check("M = 3.47 → value=3.47", any(abs(x["value"] - 3.47) < 1e-9 for x in means),
      f"got={means}")
check("均值为 2.50 → value=2.5", any(abs(x["value"] - 2.5) < 1e-9 for x in means),
      f"got={means}")

q2 = extract_quantities("本研究共发放问卷 300 份，回收 285 份，有效 260 份。")
check("裸数字不会被当成均值（不许乱抽）",
      not any(x["kind"] == "mean" for x in q2), f"got={[x['raw'] for x in q2]}")

q3 = extract_quantities("回归结果显示 R² = 0.45，B = 1.23，p < 0.05。")
check("R² / B 不会被误判成均值",
      not any(x["kind"] == "mean" for x in q3), f"got={[x['raw'] for x in q3]}")


# ===========================================================================
section("5. 闭环：build_audit_report 报告里出现 GRIM")
# ===========================================================================
df = pd.DataFrame({
    "group": ["A", "B"] * 15,
    "score": list(range(30)),
})
paper_text = (
    "本研究采用独立样本 T 检验比较两组差异。\n"
    "实验组 M = 3.47，对照组 M = 4.00，t = 2.34，p = 0.023。\n"
)
claims = {
    "methods": [],
    "quantities": extract_quantities(paper_text),
    "variables": [],
    "raw_text_excerpt": paper_text,
    "raw_text_length": len(paper_text),
}
import app as _app  # noqa: E402

cols = [_app._summarize_column(df[c]) for c in df.columns]
rep = A.build_audit_report(claims, df, cols, directive="")

grim = rep.get("grim") or []
check("报告带出 grim 字段", isinstance(rep.get("grim"), list), f"keys={list(rep)[:8]}")
check("grim 检出 2 条均值", len(grim) == 2, f"got={grim}")
check("3.47 / n=30 被判不可能", any((not g["passed"]) and g["mean"] == 3.47 for g in grim),
      f"got={grim}")
check("4.00 / n=30 判为可能（不许误报）",
      any(g["passed"] and g["mean"] == 4.0 for g in grim), f"got={grim}")
check("markdown 出现 GRIM 小节", "GRIM 一致性检验" in rep["markdown"])
check("markdown 出现「不可能」结论", "🔴 不可能" in rep["markdown"])
check("进改进建议（用户最容易看到）",
      any("GRIM" in s or "数据体检" in s for s in rep["suggestions"]),
      f"suggestions={rep['suggestions'][:3]}")
# 红线复查：GRIM 只说「不可能出现 / 需要解释」，绝不给人扣「造假」的帽子。
# 注意我们自己会写「这不等于造假」这种**免责**句，所以不能简单断言不含「造假」二字，
# 要断言的是：不出现任何**肯定式**的造假指控。
check("未通过时必带「这不等于造假」免责", "这不等于造假" in rep["markdown"])
check("绝不出现肯定式造假指控",
      not any(p in rep["markdown"]
              for p in ("存在造假", "属于造假", "判定造假", "涉嫌造假", "确认造假")),
      f"md 片段={rep['markdown'][-400:]}")


# ===========================================================================
section("6. 健壮性：没写均值 / 空数据 / n<=0 都不许崩、不许制造噪音")
# ===========================================================================
check("无 mean → 空列表", A.grim_cross_check(
    [{"kind": "t", "value": 2.3, "raw": "t = 2.3"}], 30) == [])
check("None → 空列表", A.grim_cross_check(None, 30) == [])
check("n=0 → 空列表", A.grim_cross_check(qs_impossible, 0) == [])
check("n 为负 → 空列表", A.grim_cross_check(qs_impossible, -5) == [])
check("value 缺失 → 跳过不崩", A.grim_cross_check(
    [{"kind": "mean", "raw": "M = ?"}], 30) == [])

empty_claims = {"methods": [], "quantities": [], "variables": []}
rep2 = A.build_audit_report(empty_claims, pd.DataFrame({"a": [1, 2, 3]}),
                            [], directive="")
check("无声称统计量的论文：报告仍能出、无 GRIM 噪音",
      isinstance(rep2.get("markdown"), str) and "GRIM" not in rep2["markdown"])


# ===========================================================================
print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
raise SystemExit(1 if FAIL else 0)
