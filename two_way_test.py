"""v1.1 · 双因素方差分析（two_way_anova）测试
================================================
三层验证：
  1) 纯函数层：Type III 平方和正确性（用独立 OLS 设计矩阵 oracle 交叉验证）
  2) HTTP 契约层：/api/analyze JSON + SSE 两条路径
  3) 论文识别 + 论文排查（audit）端到端

跑法：.venv/Scripts/python.exe two_way_test.py
"""
import io
import os
import os
import sys
ROOT = os.path.dirname(os.path.abspath(__file__))

import numpy as np
import pandas as pd
from scipy import stats as st

from app import app, run_two_way_anova
from extract_paper import extract_methods

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
    print("=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------------
# 独立 oracle：设计矩阵 OLS + 模型比较（Type III），用于交叉验证
# ---------------------------------------------------------------------------
def oracle(df, fa, fb, ycol):
    d = df.copy()
    y = d[ycol].astype(float).values
    n = len(y)
    la = sorted(d[fa].astype(str).unique())
    lb = sorted(d[fb].astype(str).unique())
    A = d[fa].astype(str).values
    B = d[fb].astype(str).values

    def cols(inc_a, inc_b, inc_ab):
        parts = [np.ones(n)]
        if inc_a:
            for lev in la[1:]:
                parts.append((A == lev).astype(float))
        if inc_b:
            for lev in lb[1:]:
                parts.append((B == lev).astype(float))
        if inc_ab:
            for xa in la[1:]:
                for xb in lb[1:]:
                    parts.append(((A == xa) & (B == xb)).astype(float))
        return np.column_stack(parts)

    def rss(X):
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        r = y - X @ beta
        return float(r @ r)

    Xf = cols(True, True, True)
    dfres = n - Xf.shape[1]
    rss_f = rss(Xf)
    mse = rss_f / dfres

    def term_F(params):
        Xr = cols(*params)
        dfn = Xf.shape[1] - Xr.shape[1]
        F = ((rss(Xr) - rss_f) / dfn) / mse
        return F, float(st.f.sf(F, dfn, dfres))

    return term_F((False, True, True)), term_F((True, False, True)), term_F((True, True, False))


rng = np.random.default_rng(11)

# ============ 1. 纯函数：与 oracle 逐项对齐 ============
section("1. run_two_way_anova · 与独立 OLS oracle 交叉验证")
base = {"A1B1": 10, "A1B2": 12, "A1B3": 11, "A2B1": 13, "A2B2": 15, "A2B3": 14}


def build(interaction=False, unbalanced=False, seed=11):
    r = np.random.default_rng(seed)
    rows = []
    for a in ["A1", "A2"]:
        for b in ["B1", "B2", "B3"]:
            m = base[a + b]
            if interaction and a == "A2" and b == "B3":
                m += 6
            k = 4 if (unbalanced and a == "A1" and b == "B3") else 10
            for _ in range(k):
                rows.append({"fa": a, "fb": b, "y": m + r.normal(0, 1)})
    return pd.DataFrame(rows)


for label, df in [
    ("平衡·无交互", build(False)),
    ("平衡·有交互", build(True)),
    ("不平衡·有交互", build(True, unbalanced=True)),
]:
    mine = run_two_way_anova(df, "fa", "fb", "y")["summary"]
    F_a, F_b, F_ab = oracle(df, "fa", "fb", "y")
    ok = (abs(mine["f_a"] - F_a[0]) < 1e-8
          and abs(mine["f_b"] - F_b[0]) < 1e-8
          and abs(mine["f_ab"] - F_ab[0]) < 1e-8)
    check(f"{label}：F_a/F_b/F_ab 与 oracle 完全一致", ok,
          f"mine=({mine['f_a']:.5f},{mine['f_b']:.5f},{mine['f_ab']:.5f}) "
          f"oracle=({F_a[0]:.5f},{F_b[0]:.5f},{F_ab[0]:.5f})")

# 交互显著场景应能正确判定
strong = build(True)
s2 = run_two_way_anova(strong, "fa", "fb", "y")["summary"]
check("有交互时 p_ab 显著", s2["p_ab"] < 0.05, f"p_ab={s2['p_ab']:.4f}")
check("summary 含 balanced 标志", "balanced" in s2)
check("summary 含 levene_p", "levene_p" in s2)
check("method key 正确", run_two_way_anova(strong, "fa", "fb", "y")["method"] == "two_way_anova")

# 异常输入
try:
    run_two_way_anova(strong, "fa", "fa", "y")
    check("同列作两因素应报错", False, "未抛异常")
except ValueError:
    check("同列作两因素应报错", True)

try:
    run_two_way_anova(strong.head(5), "fa", "fb", "y")
    check("样本量过小应报错", False, "未抛异常")
except ValueError:
    check("样本量过小应报错", True)

try:
    run_two_way_anova(strong, "fa", "y", "y")
    check("因变量同时作因素应报错", False, "未抛异常")
except ValueError:
    check("因变量同时作因素应报错", True)

bad = strong.copy()
bad["fb"] = "same"
try:
    run_two_way_anova(bad, "fa", "fb", "y")
    check("因素仅 1 个水平应报错", False, "未抛异常")
except ValueError:
    check("因素仅 1 个水平应报错", True)

# ============ 2. 示例数据 ============
section("2. 示例数据 examples/two_way_data.csv")
tdf = pd.read_csv(os.path.join(ROOT, "examples", "two_way_data.csv"))
tres = run_two_way_anova(tdf, "gender", "teaching_method", "score")
ts = tres["summary"]
check("示例数据平衡", ts["balanced"] is True)
check("示例数据 n = 60", ts["n"] == 60)
check("示例存在显著交互", ts["p_ab"] < 0.05, f"p_ab={ts['p_ab']:.4f}")
check("示例 Levene 齐性通过", ts["levene_p"] > 0.05, f"levene_p={ts['levene_p']:.4f}")
print(f"  · 性别 F={ts['f_a']:.2f} p={ts['p_a']:.4g} | "
      f"教法 F={ts['f_b']:.2f} p={ts['p_b']:.4g} | "
      f"交互 F={ts['f_ab']:.2f} p={ts['p_ab']:.4g}")

# ============ 3. HTTP 契约 ============
section("3. HTTP 契约 · /api/analyze JSON + SSE")
client = app.test_client()

with open(os.path.join(ROOT, "examples", "two_way_data.csv"), "rb") as f:
    up = client.post("/api/upload", data={"file": (io.BytesIO(f.read()), "t.csv")},
                     content_type="multipart/form-data").get_json()
check("上传成功", up.get("ok") is True)
fid = up["file_id"]
keys = [m["key"] for m in up["available_methods"]]
check("available_methods 含 two_way_anova", "two_way_anova" in keys, f"keys={keys}")

r = client.post("/api/analyze", json={
    "file_id": fid, "method": "two_way_anova",
    "group_col": "gender", "value_col2": "teaching_method", "value_col": "score",
})
d = r.get_json()
check("JSON 版双因素 ok", r.status_code == 200 and d.get("ok") is True, str(d.get("error")))
check("返回 f_ab", isinstance(d.get("summary", {}).get("f_ab"), float))
check("markdown 含交互", "交互" in d.get("markdown", ""))

r = client.post("/api/analyze", json={
    "file_id": fid, "method": "two_way_anova", "group_col": "gender", "value_col": "score",
})
check("缺因素 B → HTTP 400", r.status_code == 400, f"status={r.status_code}")

r = client.post("/api/analyze", json={
    "file_id": fid, "method": "two_way_anova",
    "group_col": "gender", "value_col2": "teaching_method", "value_col": "score", "stream": 1,
})
body = r.get_data(as_text=True)
check("SSE 含 markdown", "event: markdown" in body)
check("SSE 含 done", "event: done" in body)

r = client.post("/api/analyze", json={"file_id": fid, "method": "nope"})
check("错误提示含 two_way_anova", "two_way_anova" in r.get_json().get("error", ""))

# ============ 4. 论文识别与排查 ============
section("4. 论文识别 + 排查（audit）")
paper = open(os.path.join(ROOT, "examples", "two_way_paper.md"), encoding="utf-8").read()
mkeys = [m["method_key"] for m in extract_methods(paper)]
check("识别出 two_way_anova", "two_way_anova" in mkeys, f"keys={mkeys}")

with open(os.path.join(ROOT, "examples", "two_way_paper.md"), "rb") as f:
    pb = f.read()
with open(os.path.join(ROOT, "examples", "two_way_data.csv"), "rb") as f:
    db = f.read()
audit = client.post("/api/check_paper", data={
    "paper": (io.BytesIO(pb), "two_way_paper.md"),
    "data": (io.BytesIO(db), "two_way_data.csv"),
}, content_type="multipart/form-data").get_json()
check("check_paper ok", audit.get("ok") is True, str(audit.get("error")))
areal = audit["audit"]["real"]
check("audit real 跑了双因素", areal.get("method_key") == "two_way_anova",
      f"got={areal.get('method_key')} err={areal.get('error')}")
check("audit 含 p_ab", isinstance(areal.get("p_ab"), float), f"got={areal.get('p_ab')}")
check("audit 论文识别到双因素", "two_way_anova" in
      [m["method_key"] for m in audit["paper_claims"]["methods"]])
sugg = " ".join(audit["audit"]["suggestions"])
check("建议含交互作用提示", "交互" in sugg)

# ============ 汇总 ============
print()
print("=" * 70)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
