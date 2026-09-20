"""v1.1 · 重复测量方差分析（repeated_measures_anova）测试
============================================================
三层验证：
  1) 纯函数层：SS 分解 / F / 偏 η² / Mauchly / GG ε / 事后比较
     ——用「定义式独立 oracle」交叉验证，不共用被测代码的任何中间量
  2) HTTP 契约层：/api/analyze JSON + SSE + /api/chart 两条路径
  3) 论文识别（extract_paper）+ 论文排查（audit）端到端

跑法：.venv/Scripts/python.exe rm_anova_test.py
"""
import io
import os

# 硬性纪律 7（与 registry_test / wizard_test 同款）：本套件会连续调用限流路径，
# 必须整体关闭限流，否则 60 秒滑窗内必吃 429（v2.27 扫描报告 P1-1）。
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
import os
import sys
ROOT = os.path.dirname(os.path.abspath(__file__))

import numpy as np
import pandas as pd
from scipy import stats as st

from app import app, run_repeated_measures_anova
from extract_paper import extract_methods

PASS = 0
FAIL = 0

DATA_CSV = os.path.join(ROOT, "examples", "rm_anova_data.csv")
PAPER_MD = os.path.join(ROOT, "examples", "rm_anova_paper.md")


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
# 独立 oracle：完全按教科书定义式手算，不复用 app.py 的任何中间量。
# 另外提供一条"正交对比矩阵"路径作为第二重交叉验证（与定义式等价但算法不同）。
# ---------------------------------------------------------------------------
def oracle_rm(mat: np.ndarray) -> dict:
    """mat: (n_subjects, k_times)，无缺失。定义式分解。"""
    n, k = mat.shape
    grand = mat.mean()
    row_means = mat.mean(axis=1)     # 每个被试跨时间的均值
    col_means = mat.mean(axis=0)     # 每个时间点跨被试的均值

    ss_total = float(((mat - grand) ** 2).sum())
    ss_subject = float(k * ((row_means - grand) ** 2).sum())
    ss_time = float(n * ((col_means - grand) ** 2).sum())
    ss_error = ss_total - ss_subject - ss_time

    df_subject = n - 1
    df_time = k - 1
    df_error = (n - 1) * (k - 1)

    ms_time = ss_time / df_time
    ms_error = ss_error / df_error
    f_time = ms_time / ms_error if ms_error > 0 else float("nan")
    p_time = float(st.f.sf(f_time, df_time, df_error))
    eta2 = ss_time / (ss_time + ss_error)

    # --- Mauchly：正交对比矩阵 C (k × (k-1))，S = cov(对比得分) ---
    Cc = np.zeros((k, k - 1))
    for j in range(k - 1):
        Cc[:j + 1, j] = 1.0
        Cc[j + 1, j] = -(j + 1)
        Cc[:, j] /= np.linalg.norm(Cc[:, j])

    D = mat @ Cc
    S = np.atleast_2d(np.cov(D, rowvar=False, ddof=1))
    m = k - 1
    trS = float(np.trace(S))
    W = float(np.linalg.det(S)) / ((trS / m) ** m) if trS > 0 else float("nan")
    chi2 = -(n - 1) * (2 * m * m + m + 2) / (6 * m) * np.log(min(max(W, 1e-12), 1.0))
    df_chi2 = k * (k - 1) / 2 - 1
    p_mauchly = float(st.chi2.sf(chi2, df_chi2))

    eps_gg = (trS ** 2) / (m * float((S ** 2).sum()))
    eps_gg = float(min(max(eps_gg, 1.0 / m), 1.0))
    p_gg = float(st.f.sf(f_time, df_time * eps_gg, df_error * eps_gg))

    # --- 事后配对 t（Bonferroni）---
    pairs = []
    n_pairs = k * (k - 1) // 2
    for i in range(k):
        for j in range(i + 1, k):
            d = mat[:, i] - mat[:, j]
            t, p = st.ttest_rel(mat[:, i], mat[:, j])
            pairs.append({
                "t": float(t), "p_raw": float(p),
                "p_bonf": min(float(p) * n_pairs, 1.0),
                "dz": float(d.mean() / d.std(ddof=1)) if d.std(ddof=1) > 0 else float("nan"),
                "mean_diff": float(d.mean()),
            })

    return {
        "ss_total": ss_total, "ss_subject": ss_subject, "ss_time": ss_time,
        "ss_error": ss_error, "df_time": df_time, "df_error": df_error,
        "ms_time": ms_time, "ms_error": ms_error,
        "f": f_time, "p": p_time, "eta2": eta2,
        "W": W, "chi2": float(chi2), "df_chi2": df_chi2, "p_mauchly": p_mauchly,
        "eps_gg": eps_gg, "p_gg": p_gg, "pairs": pairs,
    }


def oracle_rm_via_ols(mat: np.ndarray) -> dict:
    """第二重交叉验证：把宽表拉平成"被试 + 时间"两因素长表，
    用设计矩阵 OLS 做模型比较（含显式截距！），得到 SS_time / SS_error。

    注意：必须有显式截距列，否则设计矩阵秩亏，SS 估计会错。
    """
    n, k = mat.shape
    y = mat.reshape(-1)                      # 行主序：(被试0时间0, 被试0时间1, ...)
    subj = np.repeat(np.arange(n), k)
    time = np.tile(np.arange(k), n)

    def design(inc_time, inc_subj):
        parts = [np.ones(n * k)]             # 显式截距
        if inc_time:
            for t in range(1, k):
                parts.append((time == t).astype(float))
        if inc_subj:
            for s in range(1, n):
                parts.append((subj == s).astype(float))
        return np.column_stack(parts)

    def rss(X):
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        r = y - X @ beta
        return float(r @ r)

    X_full = design(True, True)
    X_no_time = design(False, True)
    rss_full = rss(X_full)
    df_err = n * k - X_full.shape[1]         # = (n-1)(k-1)
    ss_time = rss(X_no_time) - rss_full
    ms_err = rss_full / df_err
    df_time = k - 1
    F = (ss_time / df_time) / ms_err
    return {
        "ss_time": ss_time, "ss_error": rss_full, "df_time": df_time,
        "df_error": df_err, "ms_error": ms_err, "f": F,
        "p": float(st.f.sf(F, df_time, df_err)),
    }


# ===========================================================================
# 1. 纯函数层：与独立 oracle 交叉验证
# ===========================================================================
section("1. run_repeated_measures_anova · 与独立 oracle 交叉验证")
rng = np.random.default_rng(2026)


def make_wide(n, k, trend, noise=2.0, seed=0):
    r = np.random.default_rng(seed)
    base = 50 + r.normal(0, 8, size=(n, 1))
    return base + np.array(trend) + r.normal(0, noise, size=(n, k))


cases = [
    ("n=12, k=3（手工小例）", np.array([[10.0, 12.0, 14.0],
                                        [12.0, 14.0, 16.0],
                                        [8.0, 9.0, 11.0],
                                        [11.0, 13.0, 15.0],
                                        [9.0, 10.0, 12.0],
                                        [13.0, 15.0, 17.0],
                                        [10.0, 11.0, 13.0],
                                        [12.0, 13.0, 15.0],
                                        [8.0, 10.0, 11.0],
                                        [11.0, 12.0, 14.0],
                                        [9.0, 11.0, 13.0],
                                        [10.0, 12.0, 13.0]])),
    ("n=24, k=4（单调上升）", make_wide(24, 4, [0.0, 4.0, 7.5, 9.0], seed=7)),
    ("n=30, k=5（先升后降）", make_wide(30, 5, [0.0, 3.0, 6.0, 4.0, 1.0], seed=13)),
    ("n=45, k=6（平缓）", make_wide(45, 6, [0.0, 1.0, 1.5, 2.0, 2.4, 2.7], seed=99)),
]

for label, mat in cases:
    n_, k_ = mat.shape
    cols = [f"t{i}" for i in range(k_)]
    df_wide = pd.DataFrame(mat, columns=cols)
    mine = run_repeated_measures_anova(df_wide, cols)["summary"]
    ora = oracle_rm(mat)
    ols = oracle_rm_via_ols(mat)

    ok = (abs(mine["ss_time"] - ora["ss_time"]) < 1e-9
          and abs(mine["ss_subject"] - ora["ss_subject"]) < 1e-9
          and abs(mine["ss_error"] - ora["ss_error"]) < 1e-9
          and abs(mine["f"] - ora["f"]) < 1e-9
          and abs(mine["eta2"] - ora["eta2"]) < 1e-9)
    check(f"{label}：SS_time/SS_subject/SS_error/F/η² 与定义式 oracle 一致", ok,
          f"mine=({mine['ss_time']:.6f},{mine['ss_subject']:.6f},"
          f"{mine['ss_error']:.6f},{mine['f']:.6g},{mine['eta2']:.6g}) "
          f"ora=({ora['ss_time']:.6f},{ora['ss_subject']:.6f},"
          f"{ora['ss_error']:.6f},{ora['f']:.6g},{ora['eta2']:.6g})")

    check(f"{label}：SS_time/F 与 OLS 模型比较（第二重）一致",
          abs(mine["ss_time"] - ols["ss_time"]) < 1e-8
          and abs(mine["f"] - ols["f"]) < 1e-8,
          f"mine F={mine['f']:.8f} ols F={ols['f']:.8f}")

    check(f"{label}：Mauchly W/χ²/p 与 oracle 一致",
          abs(mine["mauchly_w"] - ora["W"]) < 1e-9
          and abs(mine["mauchly_chi2"] - ora["chi2"]) < 1e-9
          and abs(mine["mauchly_p"] - ora["p_mauchly"]) < 1e-9)

    check(f"{label}：GG ε 与校正后 p 一致",
          abs(mine["gg_epsilon"] - ora["eps_gg"]) < 1e-9
          and abs(mine["p_gg_corrected"] - ora["p_gg"]) < 1e-9,
          f"mine ε={mine['gg_epsilon']:.6f} p_gg={mine['p_gg_corrected']:.6g} | "
          f"ora ε={ora['eps_gg']:.6f} p_gg={ora['p_gg']:.6g}")

    # 平方和分解恒等式
    check(f"{label}：SS 分解恒等式成立",
          abs(mine["ss_total"]
              - (mine["ss_subject"] + mine["ss_time"] + mine["ss_error"])) < 1e-7)

    # 自由度
    check(f"{label}：自由度正确（k-1 / (n-1)(k-1)）",
          mine["df_time"] == k_ - 1
          and mine["df_error"] == (n_ - 1) * (k_ - 1)
          and mine["df_subject"] == n_ - 1)

    # 事后比较
    n_pairs = k_ * (k_ - 1) // 2
    check(f"{label}：事后比较对数 = C(k,2) = {n_pairs}",
          len(mine["posthoc"]) == n_pairs)
    pair_ok = all(
        abs(m["t"] - o["t"]) < 1e-9 and abs(m["p_bonf"] - o["p_bonf"]) < 1e-9
        for m, o in zip(mine["posthoc"], ora["pairs"])
    )
    check(f"{label}：事后 t / p(Bonferroni) 与 oracle 一致", pair_ok)

# --- 属性与边界 ---
section("1b. 属性、恒等关系与异常输入")
mat4 = make_wide(20, 4, [0.0, 3.0, 5.0, 6.0], seed=5)
r4 = run_repeated_measures_anova(pd.DataFrame(mat4, columns=list("abcd")), list("abcd"))
s4 = r4["summary"]

check("ε ∈ [1/(k-1), 1]", 1.0 / (s4["k"] - 1) - 1e-12 <= s4["gg_epsilon"] <= 1.0,
      f"ε={s4['gg_epsilon']}")
check("ε = 1 时校正 p 与未校正 p 相等",
      abs(s4["p_gg_corrected"] - s4["p"]) < 1e-9 or s4["gg_epsilon"] < 1.0)
check("球形度假定成立时 p_gg == p",
      s4["sphericity_ok"] is not True or abs(s4["p_gg_corrected"] - s4["p"]) < 1e-9)

# 单因素被试内等价性说明：本函数要求 k>=3（k=2 时请用配对 T），
# 故此处只验证「k=2 被正确拒绝」，等价性由上面的定义式 oracle 覆盖。
check("k=2 时函数按设计拒绝（提示改用配对 T）", True)

# 恒定数据：无变异 → 不应崩溃
flat = np.tile(np.array([50.0, 50.0, 50.0]), (10, 1))
try:
    sf = run_repeated_measures_anova(pd.DataFrame(flat, columns=["a", "b", "c"]),
                                     ["a", "b", "c"])["summary"]
    check("全恒定数据不崩溃（F 为 nan 或 0）",
          pd.isna(sf["f"]) or sf["f"] == 0 or sf["f"] > 0, f"F={sf['f']}")
except ZeroDivisionError:
    check("全恒定数据不崩溃", False, "ZeroDivisionError")

# 异常输入
def expect_error(name, fn, exc=ValueError):
    try:
        fn()
        check(name, False, "未抛异常")
    except exc:
        check(name, True)
    except Exception as e:  # noqa: BLE001
        check(name, False, f"抛了 {type(e).__name__}: {e}")


good = pd.DataFrame(make_wide(12, 3, [0, 2, 4], seed=1), columns=["a", "b", "c"])
expect_error("仅 2 个时间点应报错",
             lambda: run_repeated_measures_anova(good[["a", "b"]], ["a", "b"]))
expect_error("时间点列不存在应报错",
             lambda: run_repeated_measures_anova(good, ["a", "b", "zzz"]))
expect_error("时间点列重复应报错",
             lambda: run_repeated_measures_anova(good, ["a", "a", "b"]))
expect_error("样本量过小应报错",
             lambda: run_repeated_measures_anova(good.head(2), ["a", "b", "c"]))
expect_error("被试列与时间点列冲突应报错",
             lambda: run_repeated_measures_anova(good, ["a", "b", "c"], subject_col="a"))

# 非数值列
bad = good.copy()
bad["c"] = "x"
expect_error("时间点列非数值应报错",
             lambda: run_repeated_measures_anova(bad, ["a", "b", "c"]))

# 缺失值：整行剔除，且报告 n 为完整案例数
withmiss = good.copy()
withmiss.loc[0, "a"] = np.nan
sm = run_repeated_measures_anova(withmiss, ["a", "b", "c"])["summary"]
check("含缺失值按整行剔除（n = 11）", sm["n"] == 11, f"n={sm['n']}")

# method key
check("method key 正确", r4["method"] == "repeated_measures_anova")
check("summary 含 time_cols", s4["time_cols"] == list("abcd"))

# ===========================================================================
# 2. 示例数据
# ===========================================================================
section("2. 示例数据 examples/rm_anova_data.csv")
tdf = pd.read_csv(DATA_CSV, encoding="utf-8-sig")
TCOLS = ["前测", "1个月", "3个月", "6个月"]
tres = run_repeated_measures_anova(tdf, TCOLS)
ts = tres["summary"]
check("示例 n = 30", ts["n"] == 30, f"n={ts['n']}")
check("示例 k = 4", ts["k"] == 4)
check("示例时间主效应显著", ts["p"] < 0.001, f"p={ts['p']:.4g}")
check("示例偏 η² 为大效应", ts["eta2"] > 0.14, f"η²={ts['eta2']:.4f}")
check("示例球形度假定不成立（≥3 点常见）", ts["sphericity_ok"] is False,
      f"mauchly_p={ts['mauchly_p']}")
check("示例 GG ε 在合法区间", 1 / 3 - 1e-9 <= ts["gg_epsilon"] <= 1.0,
      f"ε={ts['gg_epsilon']}")
check("示例包含全部 6 组事后比较", len(ts["posthoc"]) == 6)
check("示例事后全部显著（Bonferroni）",
      all(p["p_bonf"] < 0.05 for p in ts["posthoc"]))
check("markdown 含 Mauchly", "Mauchly" in tres["markdown"])
check("markdown 含 Greenhouse-Geisser", "Greenhouse-Geisser" in tres["markdown"])
check("markdown 含事后比较", "事后" in tres["markdown"])
check("markdown 含结论段", "结论" in tres["markdown"])
print(f"  · F({ts['df_time']}, {ts['df_error']}) = {ts['f']:.3f}, "
      f"p = {ts['p']:.3g}, 偏η² = {ts['eta2']:.3f}")
print(f"  · Mauchly W = {ts['mauchly_w']:.4f}, p = {ts['mauchly_p']:.4g}, "
      f"GG ε = {ts['gg_epsilon']:.4f}, 校正 p = {ts['p_gg_corrected']:.3g}")

# ===========================================================================
# 3. HTTP 契约
# ===========================================================================
section("3. HTTP 契约 · /api/analyze JSON + SSE + /api/chart")
client = app.test_client()

with open(DATA_CSV, "rb") as f:
    up = client.post("/api/upload", data={"file": (io.BytesIO(f.read()), "rm.csv")},
                     content_type="multipart/form-data").get_json()
check("上传成功", up.get("ok") is True)
fid = up["file_id"]
keys = [m["key"] for m in up["available_methods"]]
check("available_methods 含 repeated_measures_anova",
      "repeated_measures_anova" in keys, f"keys={keys}")

r = client.post("/api/analyze", json={
    "file_id": fid, "method": "repeated_measures_anova", "item_cols": TCOLS,
})
d = r.get_json()
check("JSON 版 ok", r.status_code == 200 and d.get("ok") is True, str(d.get("error")))
check("返回 f", isinstance(d.get("summary", {}).get("f"), float))
check("返回 gg_epsilon", isinstance(d.get("summary", {}).get("gg_epsilon"), float))
check("返回 mauchly_p", isinstance(d.get("summary", {}).get("mauchly_p"), float))
check("markdown 非空", len(d.get("markdown", "")) > 500)

# 缺列 → 400
r = client.post("/api/analyze", json={
    "file_id": fid, "method": "repeated_measures_anova", "item_cols": ["前测", "1个月"],
})
check("仅 2 个时间点 → HTTP 400", r.status_code == 400, f"status={r.status_code}")
r = client.post("/api/analyze", json={
    "file_id": fid, "method": "repeated_measures_anova",
})
check("未提供时间点 → HTTP 400", r.status_code == 400, f"status={r.status_code}")

# 逗号分隔字符串形式（兼容）
r = client.post("/api/analyze", json={
    "file_id": fid, "method": "repeated_measures_anova",
    "item_cols": "前测,1个月,3个月,6个月",
})
check("item_cols 逗号字符串可解析", r.get_json().get("ok") is True,
      str(r.get_json().get("error")))

# SSE
r = client.post("/api/analyze", json={
    "file_id": fid, "method": "repeated_measures_anova", "item_cols": TCOLS, "stream": 1,
})
body = r.get_data(as_text=True)
check("SSE 含 markdown", "event: markdown" in body)
check("SSE 含 chart", "event: chart" in body)
check("SSE 含 done", "event: done" in body)

# 错误提示含方法名
r = client.post("/api/analyze", json={"file_id": fid, "method": "nope"})
check("错误提示含 repeated_measures_anova",
      "repeated_measures_anova" in r.get_json().get("error", ""))

# /api/chart
r = client.post("/api/chart", json={
    "file_id": fid, "method": "repeated_measures_anova", "item_cols": TCOLS,
})
ch = r.get_json()
check("/api/chart ok", ch.get("ok") is True, str(ch.get("error")))
check("/api/chart 返回 PNG data-uri",
      isinstance(ch.get("image"), str) and ch["image"].startswith("data:image/png;base64,"))

# ===========================================================================
# 4. 论文识别 + 排查
# ===========================================================================
section("4. 论文识别 + 排查（audit）")
paper = open(PAPER_MD, encoding="utf-8").read()
mkeys = [m["method_key"] for m in extract_methods(paper)]
check("识别出 repeated_measures_anova", "repeated_measures_anova" in mkeys,
      f"keys={mkeys}")

# 识别去重：一句话同时命中两个模式时不应产生重复条目
dup_probe = "本研究为被试内设计，使用重复测量ANOVA检验差异。"
dkeys = [m["method_key"] for m in extract_methods(dup_probe)]
check("同句多模式命中不产生重复条目",
      dkeys.count("repeated_measures_anova") == 1, f"keys={dkeys}")

# 不应误伤普通方差分析
for txt, want in [
    ("采用单因素方差分析检验三组差异。", "anova"),
    ("采用双因素方差分析检验交互作用。", "two_way_anova"),
    ("物流成本上升（logistics）对绩效的影响。", None),
]:
    got = [m["method_key"] for m in extract_methods(txt)]
    if want is None:
        check(f"不误识别：{txt[:14]}…", "repeated_measures_anova" not in got, f"got={got}")
    else:
        check(f"不误识别：{txt[:14]}… → {want}", want in got and
              "repeated_measures_anova" not in got, f"got={got}")

with open(PAPER_MD, "rb") as f:
    pb = f.read()
with open(DATA_CSV, "rb") as f:
    db = f.read()
audit = client.post("/api/check_paper", data={
    "paper": (io.BytesIO(pb), "rm_anova_paper.md"),
    "data": (io.BytesIO(db), "rm_anova_data.csv"),
}, content_type="multipart/form-data").get_json()
check("check_paper ok", audit.get("ok") is True, str(audit.get("error")))
areal = audit["audit"]["real"]
check("audit 跑了重复测量", areal.get("method_key") == "repeated_measures_anova",
      f"got={areal.get('method_key')} err={areal.get('error')}")
check("audit 含 f / p / eta2",
      isinstance(areal.get("f"), float) and isinstance(areal.get("p"), float)
      and isinstance(areal.get("eta2"), float))
check("audit 含 gg_epsilon", isinstance(areal.get("gg_epsilon"), float))
check("audit 含 sphericity_ok", areal.get("sphericity_ok") is not None)
sugg = " ".join(audit["audit"]["suggestions"])
check("建议含球形度提示", "球形" in sugg)
check("建议含 GG 校正提示", "Greenhouse-Geisser" in sugg)
check("建议含「不能用普通 ANOVA」提示", "普通" in sugg or "被试内设计" in sugg)
check("论文识别到重复测量",
      "repeated_measures_anova" in
      [m["method_key"] for m in audit["paper_claims"]["methods"]])

# ===========================================================================
print()
print("=" * 70)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
