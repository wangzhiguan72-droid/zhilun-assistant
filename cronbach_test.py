"""v1.1 · 信度分析（Cronbach's α）测试
========================================
两层验证：
  1) 纯函数层：run_cronbach_alpha 合成数据 + 已知真值（手算 α 校验）
  2) HTTP 契约层：/api/analyze JSON + SSE 两条路径
  3) 论文识别：extract_paper 识别 Cronbach / α 统计量
  4) 论文排查：audit 重跑信度并对比 α

跑法：.venv/Scripts/python.exe cronbach_test.py
"""
import io
import json
import os
import os
import sys
ROOT = os.path.dirname(os.path.abspath(__file__))

import numpy as np
import pandas as pd

from app import app, run_cronbach_alpha
from extract_paper import extract_methods, extract_quantities

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
rng = np.random.default_rng(7)

# ============ 1. 纯函数：已知真值（手算校验） ============
section("1. run_cronbach_alpha · 手算真值校验")
base = rng.normal(0, 1, 60)
demo = pd.DataFrame({f"q{i}": base * 0.9 + rng.normal(0, 0.4, 60) + i * 0.05
                     for i in range(1, 5)})
res = run_cronbach_alpha(demo, ["q1", "q2", "q3", "q4"])
X = demo[["q1", "q2", "q3", "q4"]].values
k = 4
manual = (k / (k - 1)) * (1 - X.var(axis=0, ddof=1).sum() / X.sum(axis=1).var(ddof=1))
check("α 与手算一致", abs(res["summary"]["alpha"] - manual) < 1e-9,
      f"got={res['summary']['alpha']} manual={manual}")
check("k = 4", res["summary"]["k"] == 4)
check("n = 60", res["summary"]["n"] == 60)
check("method key 正确", res["method"] == "cronbach_alpha")
check("markdown 非空", len(res["markdown"]) > 200)

# 高相关题组 → α 高；独立题组 → α 低
hi = pd.DataFrame({c: base + rng.normal(0, 0.15, 60) for c in "abc"})
lo = pd.DataFrame({c: rng.normal(0, 1, 60) for c in "abc"})
a_hi = run_cronbach_alpha(hi, ["a", "b", "c"])["summary"]["alpha"]
a_lo = run_cronbach_alpha(lo, ["a", "b", "c"])["summary"]["alpha"]
check("高相关题组 α > 0.9", a_hi > 0.9, f"got={a_hi:.4f}")
check("独立题组 α 接近 0", abs(a_lo) < 0.3, f"got={a_lo:.4f}")

# 删除该项后的 α / CITC 结构完整
s = run_cronbach_alpha(demo, ["q1", "q2", "q3", "q4"])["summary"]
check("drop_items 长度 = k", len(s["drop_items"]) == 4)
check("item_total_r 长度 = k", len(s["item_total_r"]) == 4)
check("drop_items 字段完整",
      all("name" in d and "alpha_if_deleted" in d for d in s["drop_items"]))

# 异常输入
for bad_args, msg in [
    ((["q1"],), "少于 2 题应报错"),
    ((["q1", "nope"],), "不存在的列应报错"),
]:
    try:
        run_cronbach_alpha(demo, *bad_args)
        check(msg, False, "未抛异常")
    except ValueError:
        check(msg, True)
try:
    tiny = demo.head(2)
    run_cronbach_alpha(tiny, ["q1", "q2", "q3", "q4"])
    check("样本量 < 3 应报错", False, "未抛异常")
except ValueError:
    check("样本量 < 3 应报错", True)

# 非数值列报错
mixed = demo.copy()
mixed["txt"] = "x"
try:
    run_cronbach_alpha(mixed, ["q1", "txt"])
    check("非数值列应报错", False, "未抛异常")
except ValueError:
    check("非数值列应报错", True)

# ============ 2. 示例数据 ============
section("2. 示例数据 examples/questionnaire_data.csv")
qdf = pd.read_csv(os.path.join(ROOT, "examples", "questionnaire_data.csv"))
qres = run_cronbach_alpha(qdf, ["q1", "q2", "q3", "q4", "q5", "q6"])
qa = qres["summary"]["alpha"]
check("示例数据 α 在 0.7~0.95 合理区间", 0.7 <= qa <= 0.95, f"got={qa:.4f}")
check("示例数据 n = 30", qres["summary"]["n"] == 30)
print(f"  · 示例 α = {qa:.4f}（{qres['summary']['grade']}）")

# ============ 3. HTTP 契约（test_client） ============
section("3. HTTP 契约 · /api/analyze JSON + SSE")
client = app.test_client()

with open(os.path.join(ROOT, "examples", "questionnaire_data.csv"), "rb") as f:
    r = client.post("/api/upload", data={"file": (io.BytesIO(f.read()), "q.csv")},
                    content_type="multipart/form-data")
up = r.get_json()
check("上传成功", up.get("ok") is True)
fid = up["file_id"]

keys = [m["key"] for m in up["available_methods"]]
check("available_methods 含 cronbach_alpha", "cronbach_alpha" in keys,
      f"keys={keys}")

r = client.post("/api/analyze", json={
    "file_id": fid, "method": "cronbach_alpha",
    "item_cols": ["q1", "q2", "q3", "q4", "q5", "q6"],
})
d = r.get_json()
check("JSON 版信度分析 ok", r.status_code == 200 and d.get("ok") is True,
      str(d.get("error")))
check("返回 alpha", isinstance(d.get("summary", {}).get("alpha"), float))
check("返回 markdown", "Cronbach" in d.get("markdown", ""))

# x_cols 字符串兼容
r = client.post("/api/analyze", json={
    "file_id": fid, "method": "cronbach_alpha",
    "x_cols": "q1,q2,q3,q4,q5,q6",
})
check("x_cols 字符串兼容", r.get_json().get("ok") is True)

# 题项不足 → 400
r = client.post("/api/analyze", json={
    "file_id": fid, "method": "cronbach_alpha", "item_cols": ["q1"],
})
check("题项 < 2 → HTTP 400", r.status_code == 400, f"status={r.status_code}")

# SSE 路径
r = client.post("/api/analyze", json={
    "file_id": fid, "method": "cronbach_alpha",
    "item_cols": ["q1", "q2", "q3", "q4", "q5", "q6"], "stream": 1,
})
body = r.get_data(as_text=True)
check("SSE 含 markdown 事件", "event: markdown" in body)
check("SSE 含 done 事件", "event: done" in body)
check("SSE 含 alpha", "cronbach_alpha" in body)

# 未知方法提示含新方法
r = client.post("/api/analyze", json={"file_id": fid, "method": "nope"})
check("错误提示含 cronbach_alpha",
      "cronbach_alpha" in r.get_json().get("error", ""))

# ============ 4. 论文识别 ============
section("4. 论文识别 · extract_paper")
paper = (
    "本研究采用 Cronbach's α 系数检验量表内部一致性信度，"
    "结果显示 Cronbach's α = 0.85，表明量表信度良好。"
    "此外采用独立样本 T 检验比较性别差异。"
)
methods = extract_methods(paper)
mkeys = [m["method_key"] for m in methods]
check("识别出 cronbach_alpha", "cronbach_alpha" in mkeys, f"keys={mkeys}")

quants = extract_quantities(paper)
alpha_qs = [q for q in quants if q["kind"] == "alpha"]
check("识别出 alpha 统计量", len(alpha_qs) >= 1, f"got={len(alpha_qs)}")
if alpha_qs:
    check("alpha 数值 = 0.85", abs(alpha_qs[0]["value"] - 0.85) < 1e-6,
          f"got={alpha_qs[0]['value']}")

# ============ 5. 论文排查（audit）端到端 ============
section("5. 论文排查 · audit 重跑信度（in-process test_client）")
with open(os.path.join(ROOT, "examples", "questionnaire_paper.md"), "rb") as f:
    paper_body = f.read()
with open(os.path.join(ROOT, "examples", "questionnaire_data.csv"), "rb") as f:
    data_body = f.read()

audit = client.post("/api/check_paper", data={
    "paper": (io.BytesIO(paper_body), "questionnaire_paper.md"),
    "data": (io.BytesIO(data_body), "questionnaire_data.csv"),
}, content_type="multipart/form-data").get_json()

check("check_paper ok", audit.get("ok") is True, str(audit.get("error")))
real = audit["audit"]["real"]
check("real 真跑了信度", real.get("method_key") == "cronbach_alpha",
      f"got={real.get('method_key')} err={real.get('error')}")
check("real 含 alpha", isinstance(real.get("alpha"), float),
      f"got={real.get('alpha')}")
check("real α 与直接调用一致", abs((real.get("alpha") or 0) - qa) < 1e-6)
mkeys_paper = [m["method_key"] for m in audit["paper_claims"]["methods"]]
check("论文识别到 cronbach_alpha", "cronbach_alpha" in mkeys_paper,
      f"keys={mkeys_paper}")
# 声称 α=0.72 vs 实际 0.884 → 应判定 mismatch
alpha_cmp = [c for c in audit["audit"]["comparisons"] if c.get("kind") == "alpha"]
check("α 被纳入对比", len(alpha_cmp) >= 1, f"got={len(alpha_cmp)}")
if alpha_cmp:
    check("虚构 α=0.72 被判 mismatch", alpha_cmp[0].get("status") in ("mismatch", "minor_diff"),
          f"status={alpha_cmp[0].get('status')}")
# 建议里应出现信度相关条目
sugg = " ".join(audit["audit"]["suggestions"])
check("建议含 Cronbach/信度", "Cronbach" in sugg or "信度" in sugg)

# ============ 汇总 ============
print()
print("=" * 70)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
