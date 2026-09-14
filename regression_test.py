"""v1.0 · 回归分析（C 档）测试
================================
两层验证：
  1) 纯函数层：合成数据 + 已知真值（系数/方向/OR），直接调 run_* 验证
  2) HTTP 契约层：/api/analyze JSON + SSE 两条路径（服务器在线才跑，离线自动跳过）

跑法：.venv/Scripts/python.exe regression_test.py
"""
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from app import run_linear_regression, run_logistic_regression, run_wilcoxon  # noqa: F401

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
rng = np.random.default_rng(42)

# ============ 1. 线性回归：已知真值 ============
section("1. run_linear_regression · 多元 OLS 已知真值")
n = 200
x1 = rng.normal(50, 10, n)
x2 = rng.normal(30, 5, n)
y = 2.0 * x1 + 3.0 * x2 + 10.0 + rng.normal(0, 5, n)
df1 = pd.DataFrame({"y": y, "hours": x1, "input": x2})

res = run_linear_regression(df1, "y", ["hours", "input"])
s = res["summary"]
print(f"  R²={s['r2']:.4f}  F={s['f']:.1f}  p_f={s['p_f']:.2e}")
check("method 字段", res["method"] == "linear_regression")
check("R² 接近 1（信噪比高）", 0.95 < s["r2"] <= 1.0, f"r2={s['r2']}")
check("hours 系数 ≈ 2.0", abs(s["coefficients"][1]["beta"] - 2.0) < 0.1,
      f"beta={s['coefficients'][1]['beta']}")
check("input 系数 ≈ 3.0", abs(s["coefficients"][2]["beta"] - 3.0) < 0.1,
      f"beta={s['coefficients'][2]['beta']}")
check("截距 ≈ 10", abs(s["coefficients"][0]["beta"] - 10.0) < 1.0,
      f"beta0={s['coefficients'][0]['beta']}")
check("两个自变量都显著", s["sig_vars"] == ["hours", "input"], f"sig={s['sig_vars']}")
check("F 检验显著", s["p_f"] < 0.001)
check("变量回传", res["variables"] == {"y": "y", "x": ["hours", "input"]})
check("markdown 含系数表", "系数 β" in res["markdown"] and "hours" in res["markdown"])
check("markdown 含结论段", "可直接引用进论文" in res["markdown"])

# ============ 2. 线性回归：错误路径 ============
section("2. run_linear_regression · 错误路径")
try:
    run_linear_regression(df1, "y", ["y"])
    check("因变量出现在自变量里应报错", False)
except ValueError as e:
    check("因变量出现在自变量里应报错", "不能同时出现" in str(e), str(e))

df_dup = df1.copy()
df_dup["dup"] = df_dup["hours"] * 2.0
try:
    run_linear_regression(df_dup, "y", ["hours", "dup"])
    check("完全共线性应报错", False)
except ValueError as e:
    check("完全共线性应报错", "共线性" in str(e), str(e))

df_const = pd.DataFrame({"y": [5.0] * 50, "x": rng.normal(0, 1, 50)})
try:
    run_linear_regression(df_const, "y", ["x"])
    check("因变量常数应报错", False)
except ValueError as e:
    check("因变量常数应报错", "常数" in str(e), str(e))

df_cat = pd.DataFrame({"y": range(10), "g": list("abcdefghij")})
try:
    run_linear_regression(df_cat, "y", ["g"])
    check("非数值自变量应报错", False)
except ValueError as e:
    check("非数值自变量应报错", "非数值列" in str(e), str(e))

df_tiny = pd.DataFrame({"y": [1.0, 2.0], "x1": [1.0, 2.0], "x2": [3.0, 1.0]})
try:
    run_linear_regression(df_tiny, "y", ["x1", "x2"])
    check("样本量不足应报错", False)
except ValueError as e:
    check("样本量不足应报错", "样本量不足" in str(e), str(e))

# ============ 3. Logistic 回归：已知真值 ============
section("3. run_logistic_regression · IRLS 已知真值")
n2 = 300
score = rng.normal(60, 12, n2)
att = rng.normal(40, 8, n2)
logit = 0.08 * (score - 60) + 0.06 * (att - 40) - 0.5
prob = 1 / (1 + np.exp(-logit))
admit = (rng.uniform(0, 1, n2) < prob).astype(float)
df2 = pd.DataFrame({"admit": admit, "score": score, "attend": att})

res2 = run_logistic_regression(df2, "admit", ["score", "attend"])
s2 = res2["summary"]
print(f"  伪R²={s2['pseudo_r2']:.4f}  准确率={s2['accuracy']:.3f}  "
      f"OR(score)={s2['coefficients'][1]['or']:.3f}")
check("method 字段", res2["method"] == "logistic_regression")
check("score 系数为正（真值 0.08>0）", s2["coefficients"][1]["beta"] > 0,
      f"beta={s2['coefficients'][1]['beta']}")
check("attend 系数为正（真值 0.06>0）", s2["coefficients"][2]["beta"] > 0,
      f"beta={s2['coefficients'][2]['beta']}")
check("score 显著", s2["coefficients"][1]["p"] < 0.05,
      f"p={s2['coefficients'][1]['p']}")
check("OR > 1（正系数）", s2["coefficients"][1]["or"] > 1.0)
check("事件数回传", s2["n_event"] == int(admit.sum()))
check("无分离警告", s2["separation"] is False)
check("markdown 含 OR 表", "OR" in res2["markdown"] and "优势比" in res2["markdown"])
check("markdown 含映射说明", "1 = 1.0" in res2["markdown"] or "n = " in res2["markdown"])

# ============ 4. Logistic：字符串类别 + 错误路径 ============
section("4. run_logistic_regression · 类别映射与错误路径")
df3 = pd.DataFrame({
    "pass": ["是"] * 40 + ["否"] * 40,
    "score": np.r_[rng.normal(70, 0.5, 40), rng.normal(45, 0.5, 40)],
})
res3 = run_logistic_regression(df3, "pass", ["score"])
check("字符串类别自动映射（是→1）",
      "1 = 是" in res3["markdown"] and "0 = 否" in res3["markdown"],
      res3["markdown"].split("\n")[1][:80])
check("分离场景触发提示", res3["summary"]["separation"] is True,
      f"accuracy={res3['summary']['accuracy']}")

df_3cat = pd.DataFrame({"y": [1, 2, 3] * 10, "x": rng.normal(size=30)})
try:
    run_logistic_regression(df_3cat, "y", ["x"])
    check("三分类因变量应报错", False)
except ValueError as e:
    check("三分类因变量应报错", "恰好 2 类" in str(e), str(e))

try:
    run_logistic_regression(df2, "admit", ["admit"])
    check("y 在自变量里应报错", False)
except ValueError as e:
    check("y 在自变量里应报错", "不能同时出现" in str(e), str(e))

# ============ 5. 旧方法回归自检（防手滑改坏） ============
section("5. 既有方法不被破坏")
pre = rng.normal(50, 8, 60)
res5 = run_wilcoxon(pd.DataFrame({"pre": pre, "post": pre + rng.normal(2, 3, 60)}),
                    "pre", "post")
check("wilcoxon 仍正常", res5["method"] == "wilcoxon" and "p" in res5["summary"])

# ============ 6. HTTP 契约层（服务器在线才跑） ============
BASE = "http://127.0.0.1:5000"


def post_json(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"HTTP {e.code}: {body[:200]}"}


server_up = False
try:
    with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
        server_up = r.status == 200
except Exception:  # noqa: BLE001
    pass

if server_up:
    section("6. HTTP 契约 · /api/analyze JSON + SSE")

    # 造一份经济学风格 CSV 上传
    df4 = pd.DataFrame({
        "income": rng.normal(8000, 1500, 120),
        "edu_years": rng.integers(9, 19, 120).astype(float),
        "age": rng.integers(22, 55, 120).astype(float),
    })
    df4["consumption"] = 0.6 * df4["income"] + 200 * df4["edu_years"] + rng.normal(0, 500, 120)
    df4["buy"] = ((0.0004 * df4["income"] + 0.12 * df4["edu_years"] - 4.5
                   + rng.normal(0, 0.4, 120)) > 0).astype(float)
    df4.insert(0, "id", range(1, 121))
    # 临时 CSV 放在**临时目录**而不是 examples/：
    #   · 旧实现把路径硬编码成作者的绝对路径（换台机器直接 FileNotFoundError）
    #   · 而且写进 examples/ 后会留在仓库里（测试产物污染 fixtures 目录）
    csv_path = Path(tempfile.gettempdir()) / "_regression_tmp.csv"
    df4.to_csv(csv_path, index=False, encoding="utf-8-sig")

    boundary = "----multi"
    with open(csv_path, "rb") as f:
        body = f.read()
    parts = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="_regression_tmp.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode() + body + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/api/upload", data=parts,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        up = json.loads(r.read().decode())
    check("上传成功", up["ok"])

    # JSON：线性回归（x_cols 列表契约）
    r1 = post_json("/api/analyze", {
        "file_id": up["file_id"], "method": "linear_regression",
        "value_col": "consumption", "x_cols": ["income", "edu_years"],
    })
    check("HTTP 线性回归 ok", r1["ok"], r1.get("error", ""))
    r2_val = r1.get("summary", {}).get("r2") if r1.get("ok") else None
    check("HTTP 线性回归 R² 合理", r2_val is not None and r2_val > 0.7, f"r2={r2_val}")

    # JSON：x_cols 兼容逗号字符串
    r1b = post_json("/api/analyze", {
        "file_id": up["file_id"], "method": "linear_regression",
        "value_col": "consumption", "x_cols": "income,edu_years",
    })
    check("HTTP x_cols 字符串兼容", r1b["ok"], r1b.get("error", ""))

    # JSON：value_col2 兜底（单自变量，走旧契约）
    r1c = post_json("/api/analyze", {
        "file_id": up["file_id"], "method": "linear_regression",
        "value_col": "consumption", "value_col2": "income",
    })
    check("HTTP value_col2 兜底", r1c["ok"] and r1c["summary"]["k"] == 1,
          r1c.get("error", ""))

    # JSON：Logistic
    r2 = post_json("/api/analyze", {
        "file_id": up["file_id"], "method": "logistic_regression",
        "value_col": "buy", "x_cols": ["income", "edu_years"],
    })
    check("HTTP Logistic ok", r2["ok"], r2.get("error", ""))

    # SSE：线性回归走流
    sse_body = json.dumps({
        "file_id": up["file_id"], "method": "linear_regression",
        "value_col": "consumption", "x_cols": ["income"], "stream": 1,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/api/analyze", data=sse_body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    events = []
    with urllib.request.urlopen(req, timeout=30) as resp:
        for raw in resp.read().decode("utf-8").split("\n"):
            if raw.startswith("event: "):
                events.append(raw[7:].strip())
    check("SSE 含 markdown 事件", "markdown" in events, str(events))
    check("SSE 含 chart 事件", "chart" in events, str(events))
    check("SSE 含 done 事件", "done" in events, str(events))

    # 未知方法错误信息包含新方法名
    r3 = post_json("/api/analyze", {"file_id": up["file_id"], "method": "nope"})
    check("错误提示含新方法", "linear_regression" in r3["error"])

    # 清理临时文件（放在 finally 语义的位置：上面任何一步抛异常都不该留下垃圾）
    try:
        csv_path.unlink(missing_ok=True)
    except OSError:
        pass
else:
    section("6. HTTP 契约层 · 服务器未运行，跳过（启动 app.py 后可重跑）")

# ============ 汇总 ============
print()
print("=" * 70)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
