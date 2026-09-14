"""可插拔方法市场测试（v2.14 · 纯本地，零 API）
================================================
插件层是**外来代码进主程序**的唯一入口，所以这套测试的重心不是"能跑通"，
而是**三道护栏真的拦得住**：

  1. **契约校验**：SCHEMA 缺字段 / key 非法 / 没有 run → 该插件加载失败，
     且**不拖垮其它插件**（市场模型的基本容错）。
  2. **沙箱超时**：插件死循环 → 被杀，而不是冻住整个界面（真跑一个 sleep 30 的插件）。
  3. **结果校验**：p=2 / df<0 / n=1 → **绝不进报告**。
     尤其要覆盖**嵌套 dict**：本项目结果约定是 `{"summary": {"p": ...}}`，
     只查顶层等于什么都没查。

外加：注册表合并（插件不得顶掉内置方法）、分发端到端、TOST 数值不变量
（等价判定 ⟺ 置信区间完全落在 ±Δ 内，这是 TOST 的定义性恒等式）。

跑法：.venv/Scripts/python.exe plugin_registry_test.py
"""
import os
import shutil
import sys
import tempfile
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

import methods_registry  # noqa: E402
import plugin_registry as pr  # noqa: E402

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


def _write_plugin(d: str, name: str, body: str) -> str:
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))
    return p


def _df(diff=0.0, n=60, seed=5):
    rng = np.random.default_rng(seed)
    v1 = rng.normal(3.0, 1.0, n)
    v2 = rng.normal(3.0 + diff, 1.0, n)
    return pd.DataFrame({
        "组别": ["实验"] * n + ["对照"] * n,
        "得分": np.r_[v1, v2],
    })


# ===========================================================================
section("1. 内置插件能被扫出来")
# ===========================================================================
_plugins, _errors = pr.scan_plugins()
_keys = [p.key for p in _plugins]
check("扫描到示例插件 tost", "tost" in _keys, str(_keys))
check("示例插件零加载错误", _errors == [], str(_errors))
check("插件目录存在", pr.PLUGIN_DIR.is_dir(), str(pr.PLUGIN_DIR))

_tost = next((p for p in _plugins if p.key == "tost"), None)
check("tost 有中文名", _tost is not None and _tost.label == "TOST 等价性检验")
check("tost 的 ui 形状是 group_value", _tost.ui == "group_value", _tost.ui)
check("tost 声明了 margin 与 alpha 两个可选参数",
      [n for n, _ in _tost.params] == ["margin", "alpha"], str(_tost.params))
check("tost 的 margin 默认值是 0.5",
      dict(_tost.params).get("margin") == 0.5, str(dict(_tost.params)))
check("执行方式为 subprocess（有超时保护）", pr.executor() == "subprocess",
      pr.executor())


# ===========================================================================
section("2. 契约校验：坏插件被拦下，且不拖垮好插件")
# ===========================================================================
_tmp = tempfile.mkdtemp(prefix="zl_plugin_test_")
try:
    _GOOD = """
        SCHEMA = {"key": "good_one", "label": "好插件", "needs": ["value_col"]}
        def run(df, value_col, **kw):
            return {"p": 0.5}
    """
    _CASES = [
        ("缺 run（只有 SCHEMA）", "no_run.py",
         'SCHEMA = {"key": "x", "label": "x"}\n'),
        ("缺 SCHEMA", "no_schema.py",
         'def run(df, **kw):\n    return {"p": 0.1}\n'),
        ("key 含大写", "bad_key1.py",
         'SCHEMA = {"key": "BadKey", "label": "x"}\ndef run(df, **kw): return {}\n'),
        ("key 含连字符", "bad_key2.py",
         'SCHEMA = {"key": "my-method", "label": "x"}\ndef run(df, **kw): return {}\n'),
        ("key 为空", "bad_key3.py",
         'SCHEMA = {"key": "", "label": "x"}\ndef run(df, **kw): return {}\n'),
        ("label 缺失", "bad_label.py",
         'SCHEMA = {"key": "x1"}\ndef run(df, **kw): return {}\n'),
        ("ui 非法", "bad_ui.py",
         'SCHEMA = {"key": "x2", "label": "x", "ui": "whatever"}\n'
         'def run(df, **kw): return {}\n'),
        ("needs 不是列表", "bad_needs.py",
         'SCHEMA = {"key": "x3", "label": "x", "needs": "value_col"}\n'
         'def run(df, **kw): return {}\n'),
        ("params 结构错", "bad_params.py",
         'SCHEMA = {"key": "x4", "label": "x", "params": ["alpha"]}\n'
         'def run(df, **kw): return {}\n'),
        ("import 就抛异常", "boom.py",
         'raise RuntimeError("我坏了")\n'),
        ("SCHEMA 不是 dict", "schema_str.py",
         'SCHEMA = "nope"\ndef run(df, **kw): return {}\n'),
    ]
    _write_plugin(_tmp, "good_one.py", _GOOD)
    for _name, _fn, _body in _CASES:
        _write_plugin(_tmp, _fn, _body)
    # 下划线开头的文件应被跳过
    _write_plugin(_tmp, "_private.py",
                  'SCHEMA = {"key": "hidden", "label": "x"}\n'
                  'def run(df, **kw): return {}\n')

    _ps, _es = pr.scan_plugins(_tmp)
    _ok_keys = [p.key for p in _ps]
    _err_mods = {e["module"] for e in _es}

    check("好插件仍然加载成功（坏插件不拖垮它）",
          "good_one" in _ok_keys, str(_ok_keys))
    check("下划线开头的文件被跳过", "hidden" not in _ok_keys, str(_ok_keys))
    check("坏插件数量与预期一致", len(_es) == len(_CASES), f"{len(_es)} vs {len(_CASES)}")
    for _name, _fn, _body in _CASES:
        check(f"拦下：{_name}", _fn in _err_mods)
    check("每条失败都带中文原因",
          all(e.get("error") for e in _es), str(_es[:2]))
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

check("目录不存在时扫描不崩", pr.scan_plugins("/nonexistent/dir") == ([], []))


# ===========================================================================
section("3. 结果合理性校验（p∈[0,1] / df>0 / n≥2）")
# ===========================================================================
check("p=0.5 通过", pr.validate_result({"p": 0.5})[0])
check("p=0 通过", pr.validate_result({"p": 0.0})[0])
check("p=1 通过", pr.validate_result({"p": 1.0})[0])
check("p=2 被拒（这是计划里的验收项）", not pr.validate_result({"p": 2})[0])
_why = pr.validate_result({"p": 2})[1]
check("p 越界的提示里带数值", "2" in _why, _why)
check("p=-0.1 被拒", not pr.validate_result({"p": -0.1})[0])
check("p=NaN 被拒", not pr.validate_result({"p": float("nan")})[0])
check("p=inf 被拒", not pr.validate_result({"p": float("inf")})[0])
check("p='abc' 被拒", not pr.validate_result({"p": "abc"})[0])
check("df=0 被拒", not pr.validate_result({"df": 0})[0])
check("df=-3 被拒", not pr.validate_result({"df": -3})[0])
check("df=58 通过", pr.validate_result({"df": 58})[0])
check("n=1 被拒", not pr.validate_result({"n": 1})[0])
check("n=30 通过", pr.validate_result({"n": 30})[0])

# 本项目结果约定：数字都在 summary 里 —— 只查顶层等于没查
check("嵌套 summary.p=2 也会被拒（关键）",
      not pr.validate_result({"summary": {"p": 2, "df": 58}})[0],
      str(pr.validate_result({"summary": {"p": 2, "df": 58}})))
check("嵌套 summary 合法时通过",
      pr.validate_result({"summary": {"p": 0.03, "df": 58, "n": 60}})[0])
check("非 dict 被拒", not pr.validate_result([1, 2])[0])
check("空 dict 被拒", not pr.validate_result({})[0])
check("None 被拒", not pr.validate_result(None)[0])


# ===========================================================================
section("4. 沙箱：死循环插件必须被杀，而不是冻住界面")
# ===========================================================================
_tmp2 = tempfile.mkdtemp(prefix="zl_plugin_sandbox_")
try:
    _write_plugin(_tmp2, "slow.py", """
        SCHEMA = {"key": "slow", "label": "死循环插件"}
        def run(df, **kw):
            import time
            time.sleep(30)
            return {"p": 0.5}
    """)
    _write_plugin(_tmp2, "crash.py", """
        SCHEMA = {"key": "crash", "label": "必崩插件"}
        def run(df, **kw):
            raise ValueError("我就是要崩")
    """)
    _write_plugin(_tmp2, "unpicklable.py", """
        SCHEMA = {"key": "unpicklable", "label": "返回烂结果"}
        def run(df, **kw):
            return {"fn": lambda x: x}
    """)
    _write_plugin(_tmp2, "empty.py", """
        SCHEMA = {"key": "empty", "label": "返回空"}
        def run(df, **kw):
            return {}
    """)
    _ps2, _ = pr.scan_plugins(_tmp2)
    _by = {p.key: p for p in _ps2}

    _t0 = time.time()
    try:
        pr.run_plugin(_by["slow"], _df(), {}, timeout=2)
        check("死循环插件被超时拦下", False, "竟然返回了")
    except pr.PluginError as e:
        check("死循环插件被超时拦下", "超时" in str(e), str(e))
    _dt = time.time() - _t0
    check(f"超时确实只等了约 2s（实测 {_dt:.1f}s）", _dt < 8, f"{_dt:.1f}s")

    try:
        pr.run_plugin(_by["crash"], _df(), {})
        check("崩溃插件变成友好错误", False, "竟然返回了")
    except pr.PluginError as e:
        check("崩溃插件变成友好错误", "我就是要崩" in str(e), str(e))

    try:
        pr.run_plugin(_by["unpicklable"], _df(), {})
        check("不可序列化的结果被拦下", False, "竟然返回了")
    except pr.PluginError as e:
        check("不可序列化的结果被拦下", True)

    try:
        pr.run_plugin(_by["empty"], _df(), {})
        check("返回空 dict 的插件被拦下", False, "竟然返回了")
    except pr.PluginError as e:
        check("返回空 dict 的插件被拦下", "合理性校验" in str(e), str(e))

    check("PluginError 是 ValueError（分发层会转成中文提示）",
          issubclass(pr.PluginError, ValueError))
finally:
    shutil.rmtree(_tmp2, ignore_errors=True)


# ===========================================================================
section("5. 参数构造 build_kwargs")
# ===========================================================================
_kw = pr.build_kwargs(_tost, {"group_col": "组别", "value_col": "得分",
                              "margin": 0.4})
check("needs 全部取到", _kw["group_col"] == "组别" and _kw["value_col"] == "得分")
check("显式传的 param 优先", _kw["margin"] == 0.4, str(_kw.get("margin")))
check("未传的 param 用默认值", _kw["alpha"] == 0.05, str(_kw.get("alpha")))
try:
    pr.build_kwargs(_tost, {"group_col": "组别"})
    check("缺 needs 时抛错", False, "竟然没抛")
except pr.PluginError as e:
    check("缺 needs 时抛错", True)
    # 插件自己写了 error_hint 时优先用它（比模板文案更具体）
    check("缺参提示用插件自己写的 error_hint",
          "TOST" in str(e) and "2 组" in str(e), str(e))

# 没写 error_hint 的插件回落到模板文案
_bare = pr.PluginInfo(key="bare", label="裸插件", path="", module="bare.py",
                      needs=("value_col",))
try:
    pr.build_kwargs(_bare, {})
    check("无 error_hint 时抛错", False, "竟然没抛")
except pr.PluginError as e:
    check("无 error_hint 时回落到模板文案",
          "缺少必需参数" in str(e) and "value_col" in str(e), str(e))


# ===========================================================================
section("6. 注册表与插件市场职责分离（插件绝不能混进内置表）")
# ===========================================================================
# 为什么插件**不**注册进 methods_registry：那张表是**内置方法**的真源，
# registry_test 会断言「前端下拉 / 副驾驶 CPL_METHODS / extract_paper 识别层 /
# audit 别名 / 方法图谱」全部覆盖它。插件一进来这些断言全崩（实测 12 项）。
# 因此插件走"注册表不认识 → 才查插件市场"的兜底分发（app._run_plugin_method）。
_all_keys = methods_registry.method_keys()
check("tost **不**在 method_keys（内置表保持纯净）",
      "tost" not in _all_keys, str(_all_keys))
check("内置 12 个方法一个没少", len(_all_keys) == 12, str(len(_all_keys)))
check("内置方法都在",
      all(k in _all_keys for k in
          ("independent_t", "anova", "cronbach_alpha", "repeated_measures_anova")))
check("available_methods 不含 tost",
      not any(m["key"] == "tost" for m in methods_registry.available_methods()))
try:
    methods_registry.call_method("tost", _df(), {"group_col": "组别",
                                                 "value_col": "得分"})
    check("call_method('tost') 抛 MissingField（交给兜底）", False, "竟然成功")
except methods_registry.MissingField:
    check("call_method('tost') 抛 MissingField（交给兜底）", True)

# 冒名插件（与内置方法同 key）永远劫持不了内置方法：
# 注册表先命中内置，压根走不到插件市场。
_tmp3 = tempfile.mkdtemp(prefix="zl_plugin_conflict_")
_saved_dir, _saved_cache = pr.PLUGIN_DIR, pr._SCAN_CACHE
try:
    _write_plugin(_tmp3, "hijack.py", """
        SCHEMA = {"key": "independent_t", "label": "冒名插件",
                  "needs": ["group_col", "value_col"]}
        def run(df, group_col, value_col, **kw):
            return {"p": 0.999, "我是冒名的": True}
    """)
    pr.PLUGIN_DIR = __import__("pathlib").Path(_tmp3)
    pr._SCAN_CACHE = None

    import app as _app

    _r, _e = _app._dispatch_analysis(
        _df(diff=1.5), {"method": "independent_t",
                        "group_col": "组别", "value_col": "得分"})
    check("冒名插件劫持不了内置方法", _e is None and (_r or {}).get("method")
          == "independent_t", str(_e))
    check("内置方法结果不含插件痕迹",
          "我是冒名的" not in str(_r), str(_r)[:120])
    check("插件市场里确实有这个 key（但会被内置挡住）",
          any(p.key == "independent_t" for p in pr.load_plugins()[0]))
finally:
    pr.PLUGIN_DIR = _saved_dir
    pr._SCAN_CACHE = _saved_cache
    shutil.rmtree(_tmp3, ignore_errors=True)

check("恢复后内置表不受影响", len(methods_registry.method_keys()) == 12)


# ===========================================================================
section("7. 分发端到端（/api/analyze 与副驾驶共用同一入口）")
# ===========================================================================
import app  # noqa: E402

_res, _err = app._dispatch_analysis(
    _df(), {"method": "tost", "group_col": "组别", "value_col": "得分",
            "margin": 0.5})
check("插件方法能被分发", _err is None and isinstance(_res, dict), str(_err))
check("返回 method=tost", (_res or {}).get("method") == "tost")
_r2, _e2 = app._dispatch_analysis(
    _df(), {"method": "tost", "group_col": "组别"})
check("缺参数 → 中文提示而非崩溃", _e2 is not None and "TOST" in _e2, str(_e2))
_r3, _e3 = app._dispatch_analysis(_df(), {"method": "不存在的插件"})
check("未知方法 → 提示当前支持列表", "不被识别" in (_e3 or ""), str(_e3))

_c = app.app.test_client()
_resp = _c.get("/api/plugins")
check("/api/plugins 返回 200", _resp.status_code == 200, str(_resp.status_code))
_d = _resp.get_json()
check("/api/plugins 报出插件数", _d.get("count") == 1, str(_d.get("count")))
check("/api/plugins 报出执行方式", _d.get("sandbox") in ("subprocess", "inprocess"),
      str(_d.get("sandbox")))
check("/api/plugins 含参数声明",
      _d["plugins"][0]["params"][0]["name"] == "margin", str(_d["plugins"][0]))


# ===========================================================================
section("8. TOST 数值不变量（等价判定 ⟺ 置信区间完全落在 ±Δ 内）")
# ===========================================================================
def _tost_run(diff, margin, n=60, seed=5):
    r, e = app._dispatch_analysis(
        _df(diff=diff, n=n, seed=seed),
        {"method": "tost", "group_col": "组别", "value_col": "得分",
         "margin": margin})
    assert e is None, e
    return r["summary"]


for _diff, _margin in ((0.0, 0.5), (0.1, 0.5), (0.6, 0.5), (0.0, 0.1), (0.3, 1.0)):
    s = _tost_run(_diff, _margin)
    _ci_ok = (s["ci_low"] > -_margin) and (s["ci_high"] < _margin)
    check(f"diff={_diff} Δ={_margin}：等价判定 ⟺ CI 落在界内（判={s['equivalent']}）",
          s["equivalent"] == _ci_ok,
          f"CI=[{s['ci_low']:.3f},{s['ci_high']:.3f}]")
    check(f"diff={_diff} Δ={_margin}：p<α ⟺ 判等价",
          (s["p"] < s["alpha"]) == s["equivalent"])
    check(f"diff={_diff} Δ={_margin}：p = max(p₁, p₂)",
          abs(s["p"] - max(s["p_lower"], s["p_upper"])) < 1e-12)

_s_wide = _tost_run(0.3, 0.2)
_s_narrow = _tost_run(0.3, 1.0)
check("边界放宽更容易判等价（单调性）",
      (not _s_wide["equivalent"]) and _s_narrow["equivalent"])

# 与手算交叉验证：90% CI 应等于 diff ± t(0.95, df) × SE
_s = _tost_run(0.2, 0.5, n=40, seed=9)
_t = float(stats.t.ppf(1 - _s["alpha"], _s["df"]))
check("90% CI 与手算一致",
      abs(_s["ci_low"] - (_s["diff"] - _t * _s["se"])) < 1e-9
      and abs(_s["ci_high"] - (_s["diff"] + _t * _s["se"])) < 1e-9)
check("自由度为正、样本量正确",
      _s["df"] > 0 and _s["n1"] == 40 and _s["n2"] == 40, str(_s["df"]))

_r4, _e4 = app._dispatch_analysis(
    pd.DataFrame({"g": ["a", "b", "c"] * 10, "v": np.arange(30.0)}),
    {"method": "tost", "group_col": "g", "value_col": "v", "margin": 0.5})
check("分组不是 2 组时明确报错", _e4 is not None and "2 个" in _e4, str(_e4))

_r5, _e5 = app._dispatch_analysis(
    _df(), {"method": "tost", "group_col": "组别", "value_col": "得分",
            "margin": -1})
check("等价边界为负时被拒", _e5 is not None, str(_e5))


# ===========================================================================
section("9. 数据不落盘")
# ===========================================================================
_up = "uploads"
_before = set(os.listdir(_up)) if os.path.isdir(_up) else set()
_tost_run(0.1, 0.5)
_after = set(os.listdir(_up)) if os.path.isdir(_up) else set()
check("插件执行未在 uploads/ 留下任何文件", _before == _after,
      str(_after - _before))


# ===========================================================================
print()
print("=" * 72)
print(f"插件市场测试：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
