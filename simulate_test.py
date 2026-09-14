"""⑥ 模拟数据生成器（v2.17）回归测试。

核心思路只有一个：**truth 必须能被 `run_*` 复算出来**。
`simulate.py` 声称"这份数据应得 t = X / p = Y"，那就拿同一份数据喂给
`app.run_*`，看它是不是真的给出 X / Y。对不上，说明要么真值算错，要么工具
算错——不管哪种，都是必须被抓住的 bug。

本文件也是 v2.17 四处修复的护栏：
  1. `independent_t` 的 `expected_d` 与 `expected_t` 同号；
  2. `two_way_anova` 的交互**真的显著**（旧版 20 个 seed 只中 5 个）；
  3. 三个方法（two_way / rm_anova / logistic）补上 `expected_*`；
  4. `cronbach_alpha` 不再 clip，负 α 照实报。

跑法：python simulate_test.py
"""
from __future__ import annotations

import io
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import simulate

PASS = 0
FAIL = 0
_LINES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        _LINES.append(f"  [PASS] {name}")
    else:
        FAIL += 1
        _LINES.append(f"  [FAIL] {name}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    _LINES.append("")
    _LINES.append("=" * 72)
    _LINES.append(title)
    _LINES.append("=" * 72)


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 延迟 import app（simulate 不 import app，这里为 oracle 才需要）
# ---------------------------------------------------------------------------
def _app():
    import app
    return app


# ---------------------------------------------------------------------------
# 一、覆盖度：12 个注册方法都得能生成
# ---------------------------------------------------------------------------
section("一、12 个方法全覆盖 + truth 非空")

METHODS = simulate.available_methods()
check("available_methods 覆盖 12 个注册方法", len(METHODS) == 12, str(METHODS))
for m in METHODS:
    try:
        df, truth = simulate.generate(m, effect_size=0.5, n_per_group=30, seed=1, noise=0.1)
        ok = (isinstance(df, pd.DataFrame) and not df.empty
              and isinstance(truth, dict) and truth.get("method") == m)
        check(f"{m} 生成成功且 truth 带 method", ok, str(truth)[:120])
    except Exception as e:  # noqa: BLE001
        check(f"{m} 生成成功", False, f"{type(e).__name__}: {e}")

section("二、每个方法都有可验证的 expected_*（旧版 3 个方法完全没有）")
for m in METHODS:
    _, truth = simulate.generate(m, seed=1)
    has = [k for k in truth if k.startswith("expected_")]
    check(f"{m} 至少 1 个 expected_*", len(has) >= 1, f"只有 {sorted(truth.keys())}")


# ---------------------------------------------------------------------------
# 三、oracle 交叉验证：truth vs run_*
# ---------------------------------------------------------------------------
section("三、oracle 交叉验证：生成数据重跑 run_* 应得到 truth")

app = _app()

# (方法, 调用, [(run_ 的键, truth 的键), ...])
ORACLE = [
    ("independent_t",
     lambda d: app.run_independent_t(d, "group", "value"),
     [("t", "expected_t"), ("p", "expected_p"), ("d", "expected_d")]),
    ("paired_t",
     lambda d: app.run_paired_t(d, "pre_test", "post_test"),
     [("t", "expected_t"), ("p", "expected_p")]),
    ("anova",
     lambda d: app.run_anova(d, "group", "value"),
     [("F", "expected_F"), ("p", "expected_p")]),
    ("two_way_anova",
     lambda d: app.run_two_way_anova(d, "factor_a", "factor_b", "value"),
     [("f_a", "expected_f_a"), ("f_b", "expected_f_b"), ("f_ab", "expected_f_ab"),
      ("p_a", "expected_p_a"), ("p_b", "expected_p_b"), ("p_ab", "expected_p_ab")]),
    ("repeated_measures_anova",
     lambda d: app.run_repeated_measures_anova(d, ["T1", "T2", "T3"]),
     [("f", "expected_F"), ("p", "expected_p")]),
    ("correlation",
     lambda d: app.run_correlation(d, "x_var", "y_var"),
     [("r", "expected_r"), ("p", "expected_p")]),
    ("chi_square",
     lambda d: app.run_chi_square(d, "row_var", "col_var"),
     [("chi2", "expected_chi2"), ("p", "expected_p")]),
    ("mann_whitney",
     lambda d: app.run_mann_whitney(d, "group", "value"),
     [("U", "expected_U"), ("p", "expected_p")]),
    ("wilcoxon",
     lambda d: app.run_wilcoxon(d, "pre_test", "post_test"),
     [("W", "expected_stat"), ("p", "expected_p")]),
    ("cronbach_alpha",
     lambda d: app.run_cronbach_alpha(d, list(d.columns)),
     [("alpha", "expected_alpha")]),
    ("logistic_regression",
     lambda d: app.run_logistic_regression(d, "y_binary", ["x_var"]),
     [("n", "expected_n"), ("n_event", "expected_n_events")]),
]

for method, runner, pairs in ORACLE:
    # 多个 seed 都验一遍：只对一个 seed 成立说明是巧合
    for seed in (1, 7, 42):
        df, truth = simulate.generate(method, effect_size=0.5, n_per_group=30,
                                      seed=seed, noise=0.1)
        try:
            summary = runner(df).get("summary", {})
        except Exception as e:  # noqa: BLE001
            check(f"{method} seed={seed} 能跑 run_*", False, f"{type(e).__name__}: {e}")
            continue
        for run_key, truth_key in pairs:
            if truth_key not in truth:
                check(f"{method} seed={seed} truth 有 {truth_key}", False,
                      str(sorted(truth.keys())))
                continue
            got, want = summary.get(run_key), truth[truth_key]
            ok = got is not None and close(got, want, 1e-6)
            check(f"{method} seed={seed} {run_key} == {truth_key}", ok,
                  f"run={got} truth={want}")


# ---------------------------------------------------------------------------
# 四、四处修复的专项护栏
# ---------------------------------------------------------------------------
section("四、修复①：independent_t 的 d 与 t 同号（旧版反号）")
for seed in (1, 42, 99):
    df, truth = simulate.generate("independent_t", effect_size=0.5, seed=seed)
    s = app.run_independent_t(df, "group", "value")["summary"]
    check(f"seed={seed} truth 的 d 与 t 同号",
          truth["expected_d"] * truth["expected_t"] > 0,
          f"d={truth['expected_d']} t={truth['expected_t']}")
    check(f"seed={seed} truth 的 d == run 的 d（含符号）",
          close(s["d"], truth["expected_d"]), f"run={s['d']} truth={truth['expected_d']}")

section("五、修复②：two_way_anova 的交互真的显著（旧版 20 seed 只中 5 个）")
sig = 0
SEEDS = list(range(20))
for seed in SEEDS:
    df, _ = simulate.generate("two_way_anova", effect_size=0.5, n_per_group=30,
                              seed=seed, noise=0.1)
    s = app.run_two_way_anova(df, "factor_a", "factor_b", "value")["summary"]
    if s["p_ab"] < 0.05:
        sig += 1
check("默认参数下 20 个 seed 至少 18 个交互显著", sig >= 18, f"实际 {sig}/20")
check("默认参数下交互显著比例 ≥ 90%", sig / len(SEEDS) >= 0.9, f"{sig}/{len(SEEDS)}")
# note 不能再说谎
_, tw_truth = simulate.generate("two_way_anova", effect_size=0.5)
# 旧 note 无条件写"有交互效应"——实测 20 个 seed 只有 5 个真的显著，那句是在说谎
check("note 不再无条件宣称'有交互效应'",
      "有交互效应" not in str(tw_truth.get("note", "")), str(tw_truth.get("note")))
check("note 说明显著与否取决于 effect_size",
      "effect_size" in str(tw_truth.get("note", "")), str(tw_truth.get("note")))

section("六、修复③：三个方法补上了 expected_*")
_, t_tw = simulate.generate("two_way_anova")
for k in ("expected_f_a", "expected_f_b", "expected_f_ab",
          "expected_p_a", "expected_p_b", "expected_p_ab"):
    check(f"two_way 有 {k}", k in t_tw and isinstance(t_tw[k], float), str(sorted(t_tw)))
_, t_rm = simulate.generate("repeated_measures_anova")
check("rm_anova 有 expected_F", "expected_F" in t_rm)
check("rm_anova 有 expected_p", "expected_p" in t_rm)
_, t_lg = simulate.generate("logistic_regression")
check("logistic 有 expected_n", "expected_n" in t_lg)
check("logistic 有 expected_n_events", "expected_n_events" in t_lg)
check("logistic 的 true_beta 标注为生成参数而非精确 oracle",
      "true_beta" in t_lg and "生成参数" in str(t_lg.get("note", "")),
      str(t_lg.get("note"))[:120])
# rm_anova 也应稳定显著
sig_rm = sum(
    1 for s in range(10)
    if app.run_repeated_measures_anova(
        simulate.generate("repeated_measures_anova", effect_size=0.5, seed=s, noise=0.1)[0],
        ["T1", "T2", "T3"])["summary"]["p"] < 0.05)
check("rm_anova 10 个 seed 至少 9 个显著", sig_rm >= 9, f"{sig_rm}/10")

section("七、修复④：cronbach α 不再 clip（负 α 照实报）")
rng = np.random.default_rng(0)
lat = rng.normal(0, 1, 40)
neg = pd.DataFrame({f"i{i}": (lat if i % 2 == 0 else -lat) + rng.normal(0, 0.2, 40)
                    for i in range(5)})
run_alpha = app.run_cronbach_alpha(neg, list(neg.columns))["summary"]["alpha"]
check("负相关量表确实得到负 α", run_alpha < 0, f"alpha={run_alpha}")
# simulate 的公式若再 clip 就会与 run 分叉 —— 用同款数据过一遍 simulate 的公式
iv = neg.var(ddof=1).values
tv = neg.sum(axis=1).var(ddof=1)
formula = (5 / 4) * (1 - iv.sum() / tv)
check("simulate 的公式不 clip（与 run 同值）", close(run_alpha, formula),
      f"run={run_alpha} formula={formula}")
# 常规场景仍一致
df, t_ca = simulate.generate("cronbach_alpha", seed=3)
check("常规场景 truth == run",
      close(app.run_cronbach_alpha(df, list(df.columns))["summary"]["alpha"],
            t_ca["expected_alpha"]))


# ---------------------------------------------------------------------------
# 八、可复现性
# ---------------------------------------------------------------------------
section("八、同 seed 可复现 / 换 seed 有变化")
for m in METHODS:
    a, ta = simulate.generate(m, seed=2026)
    b, tb = simulate.generate(m, seed=2026)
    check(f"{m} 同 seed 数据完全一致", a.equals(b))
    check(f"{m} 同 seed truth 一致",
          {k: v for k, v in ta.items() if isinstance(v, (int, float))}
          == {k: v for k, v in tb.items() if isinstance(v, (int, float))})
    c, _ = simulate.generate(m, seed=2027)
    check(f"{m} 换 seed 数据有变化", not a.equals(c))


# ---------------------------------------------------------------------------
# 九、脏输入
# ---------------------------------------------------------------------------
section("九、脏输入：要么 ValueError，要么正常生成，绝不 NaN / 空表")
try:
    simulate.generate("__不存在的方法__")
    check("未知方法抛 ValueError", False, "没抛")
except ValueError as e:
    check("未知方法抛 ValueError", True)
    check("错误信息带可用清单", "当前支持" in str(e), str(e)[:120])

# 9.1 非法入参必须明确报错（绝不产出 NaN 真值 —— NaN 不是合法 JSON）
for kw in ({"n_per_group": 1}, {"n_per_group": -10}, {"n_per_group": 0},
           {"n_per_group": "abc"}, {"effect_size": float("nan")},
           {"noise": float("inf")}, {"seed": float("nan")}):
    try:
        _, t = simulate.generate("independent_t", **kw)
        vals = [v for v in t.values() if isinstance(v, float)]
        check(f"非法入参 {kw} 不应静默通过", False, f"truth={vals}")
    except ValueError as e:
        check(f"非法入参 {kw} 抛 ValueError（不产 NaN）", True)
    except Exception as e:  # noqa: BLE001
        check(f"非法入参 {kw} 抛 ValueError 而非其它异常", False,
              f"{type(e).__name__}: {e}")

# 9.2 合法但极端的参数：必须能生成，且数据与 truth 都有限
for kw in ({"n_per_group": 3}, {"n_per_group": 500}, {"effect_size": 0.0},
           {"noise": 0.0}, {"noise": 5.0}, {"effect_size": -0.8}, {"seed": 0}):
    for m in ("independent_t", "two_way_anova", "cronbach_alpha"):
        try:
            df, truth = simulate.generate(m, **kw)
            finite = bool(np.isfinite(df.select_dtypes("number").to_numpy()).all())
            vals = [v for v in truth.values() if isinstance(v, float)]
            check(f"{m} {kw} 数据有限", finite, str(kw))
            check(f"{m} {kw} truth 无 NaN/Inf", all(math.isfinite(v) for v in vals),
                  f"{[v for v in vals if not math.isfinite(v)]}")
        except Exception as e:  # noqa: BLE001
            check(f"{m} 极端参数 {kw} 不崩溃", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 十、接口端到端
# ---------------------------------------------------------------------------
section("十、/api/simulate 端到端")
client = app.app.test_client()

r = client.post("/api/simulate", json={"method": "two_way_anova", "effect_size": 0.5,
                                       "n_per_group": 30, "seed": 42, "noise": 0.1})
check("POST 200", r.status_code == 200, str(r.status_code))
d = r.get_json()
check("返回 ok", d.get("ok") is True)
for key in ("file_id", "filename", "rows", "columns", "csv", "truth", "note"):
    check(f"返回 {key}", key in d, str(sorted(d.keys())))
check("标了 simulated", d.get("simulated") is True)
check("文件名带'模拟'", "模拟" in str(d.get("filename")), str(d.get("filename")))
check("note 警告不可用于论文", "不能当真实研究数据" in str(d.get("note")),
      str(d.get("note"))[:120])
check("csv 有表头", str(d.get("csv", "")).splitlines()[0] == "factor_a,factor_b,value",
      str(d.get("csv", ""))[:60])
check("列名与数据一致",
      [c["name"] for c in d["columns"]] == ["factor_a", "factor_b", "value"])
check("入参合法时 adjusted 为空", d.get("adjusted") == {}, json.dumps(d.get("adjusted")))
# 入参被钳制必须如实回报：静默把 n=1 变成 3 行数据，用户只会以为生成器坏了
d_adj = client.post("/api/simulate",
                    json={"method": "independent_t", "n_per_group": 1}).get_json()
check("n=1 被钳成 3", (d_adj.get("adjusted") or {}).get("n_per_group") == 3,
      json.dumps(d_adj.get("adjusted")))
check("n=1 实际生成 6 行", d_adj.get("rows") == 6, str(d_adj.get("rows")))
check("非数字 n 回落默认 30 并回报",
      (client.post("/api/simulate", json={"method": "independent_t",
                                          "n_per_group": "abc"}).get_json()
       .get("adjusted") or {}).get("n_per_group") == 30)
# 生成的 file_id 能直接进分析流程（与真实上传同池）
r2 = client.post("/api/datacheck", json={"file_id": d["file_id"]})
check("生成的数据可直接体检（同 _SESSION）", r2.status_code == 200, str(r2.status_code))
# 复现性（走接口）
r3 = client.post("/api/simulate", json={"method": "independent_t", "seed": 42})
check("同 seed 接口结果一致", r3.get_json()["csv"] ==
      client.post("/api/simulate", json={"method": "independent_t", "seed": 42}).get_json()["csv"])

section("十一、接口脏输入：不 500")
r4 = client.get("/api/simulate")
check("GET 返回方法清单", r4.status_code == 200
      and len(r4.get_json().get("methods", [])) == 12, str(r4.get_json())[:120])
r5 = client.post("/api/simulate", json={"method": "no_such"})
check("未知方法 400（不是 500）", r5.status_code == 400, str(r5.status_code))
check("400 时带可用清单", len((r5.get_json() or {}).get("available_methods", [])) == 12)
for bad in ({"method": "independent_t", "n_per_group": "abc"},
            {"method": "independent_t", "n_per_group": -5},
            {"method": "independent_t", "effect_size": None},
            {"method": "independent_t", "noise": 1e9},
            {}, None):
    rr = client.post("/api/simulate", json=bad)
    check(f"脏输入 {str(bad)[:40]} 不是 500", rr.status_code != 500, str(rr.status_code))


# ---------------------------------------------------------------------------
out = "\n".join(_LINES)
io.open("_sim_test_out.txt", "w", encoding="utf-8").write(out)
print(out)
print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
