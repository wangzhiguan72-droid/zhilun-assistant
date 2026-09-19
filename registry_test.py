"""v1.3 · METHODS 注册表一致性测试（总线 seam 守卫）
======================================================
这个测试的存在理由：本项目已**多次**出现"加了功能但忘了同步某一处"的漂移
（文档停在旧版本、前端漏 optgroup、audit 漏别名…）。注册表的真正价值不是
少写几行 if/elif，而是让"方法清单"有**单一真源**，并把这个不变式钉死在测试里。

三层验证：
  1) 注册表内部自洽：key 唯一、alias 合法、fn 可调用、picker_label 非空
  2) 跨模块一致性：注册表 ↔ available_methods ↔ method_label ↔ 前端 optgroup
       ↔ extract_paper ↔ audit._METHOD_ALIASES ↔ paper_writer._method_label
       ↔ methods_graph（①知识图谱，v1.4 起纳入）
  3) 行为等价：12 个方法逐个真跑（用示例数据），确认表驱动后结果不变

「跨模块一致性」之所以是本节重点：每加一个方法，上面 8 个地方都要同步，
漏一处就是静默降级（前端选不到 / 论文识别不出 / 图谱画不出）。测试钉死它。

跑法：.venv/Scripts/python.exe registry_test.py
"""
import io
import os
import re
import sys
from pathlib import Path

import pandas as pd

# 本套件对 /api/analyze 连打 12+ 次；v2.26 起 analyze 计入 LLM 限流桶（8/分），
# 不关限流必然误报 429（security_guard.disabled 专为回归测试提供此开关）。
os.environ["RATE_LIMIT_DISABLE"] = "1"

from app import app, method_label, _dispatch_analysis
from methods_registry import (
    MissingField, available_methods, call_method, get_methods, get_spec,
    method_keys, method_labels,
)

ROOT = Path(__file__).resolve().parent
EX = ROOT / "examples"

PASS = 0
FAIL = 0
WARN = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def warn(name, detail=""):
    global WARN
    WARN += 1
    print(f"  ⚠ {name}  {detail}")


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


REG = get_methods()
KEYS = method_keys()

# ===========================================================================
section("1. 注册表内部自洽")
# ===========================================================================
check("注册表非空", len(KEYS) > 0, f"keys={KEYS}")
check("key 无重复", len(KEYS) == len(set(KEYS)))
check("key 命名规范（小写+下划线）",
      all(re.fullmatch(r"[a-z][a-z0-9_]*", k) for k in KEYS),
      f"违规={[k for k in KEYS if not re.fullmatch(r'[a-z][a-z0-9_]*', k)]}")

valid_aliases = {"group_value", "chi_square", "two_cols", "x_cols",
                 "two_way", "item_cols", "time_cols"}
for k, s in REG.items():
    check(f"{k}：alias 合法（{s.alias}）", s.alias in valid_aliases,
          f"alias={s.alias}")
    check(f"{k}：fn 可调用", callable(s.fn))
    check(f"{k}：label / picker_label 非空",
          bool(s.label.strip()) and bool(s.display_picker_label().strip()))
    check(f"{k}：fn 名与 key 语义一致",
          s.fn.__name__ == f"run_{k}", f"fn={s.fn.__name__} key={k}")

# ===========================================================================
section("2. 跨模块一致性（防漂移）")
# ===========================================================================
# 2a) available_methods（/api/upload 下发给前端）
am = available_methods()
check("available_methods 数量 == 注册表", len(am) == len(KEYS),
      f"{len(am)} vs {len(KEYS)}")
check("available_methods 的 key 集合 == 注册表",
      {m["key"] for m in am} == set(KEYS))
check("available_methods 顺序 == 注册表注册顺序",
      [m["key"] for m in am] == KEYS)

# 2b) method_label
labels = method_labels()
for k in KEYS:
    check(f"method_label('{k}') 与注册表一致",
          method_label(k) == labels[k], f"{method_label(k)!r} vs {labels[k]!r}")

# 2c) 前端 optgroup（templates/index.html）
html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
# 方法下拉框里的 <option value="xxx">
opt_vals = set(re.findall(r'<option value="([a-z][a-z0-9_]*)"', html))
# 只取形如方法 key 的（排除空串与纯英文占位）
fe_keys = {v for v in opt_vals if v in set(KEYS) or v in {
    "independent_t", "anova", "correlation", "chi_square", "paired_t",
    "mann_whitney", "wilcoxon", "linear_regression", "logistic_regression",
    "cronbach_alpha", "two_way_anova", "repeated_measures_anova"}}
missing_fe = set(KEYS) - fe_keys
check("前端 optgroup 覆盖全部注册方法", not missing_fe,
      f"前端缺={sorted(missing_fe)}")

# 2d) CPL_METHODS（副驾驶页方法清单）
cpl = set(re.findall(r"\{\s*v:\s*'([a-z][a-z0-9_]*)'", html))
missing_cpl = set(KEYS) - cpl
check("副驾驶 CPL_METHODS 覆盖全部注册方法", not missing_cpl,
      f"副驾驶缺={sorted(missing_cpl)}")

# 2e) extract_paper 方法识别模式
from extract_paper import _METHOD_PATTERNS
ep_keys = {key for _, key, _ in _METHOD_PATTERNS}
# 识别层还有 anova 的细粒度变体（one_sample_t / spearman / regression 等未实装的方法），
# 所以只要求「注册表 → 识别层」方向覆盖
missing_ep = set(KEYS) - ep_keys
check("extract_paper 识别层覆盖全部注册方法", not missing_ep,
      f"识别层缺={sorted(missing_ep)}")

# 2f) audit 的方法别名
import audit
alias_vals = set(audit._METHOD_ALIASES.values())
unimplemented = {"one_sample_t", "spearman", "kruskal_wallis",
                 "regression", "non_parametric"}
missing_alias = set(KEYS) - alias_vals
check("audit._METHOD_ALIASES 覆盖全部注册方法", not missing_alias,
      f"别名缺={sorted(missing_alias)}")
check("audit 的『未实装』清单不含任何已注册方法",
      not (set(KEYS) & unimplemented),
      f"冲突={sorted(set(KEYS) & unimplemented)}")

# 2g) audit 优先级表
missing_pri = set(KEYS) - set(audit._METHOD_PRIORITY)
check("audit._METHOD_PRIORITY 覆盖全部注册方法", not missing_pri,
      f"优先级缺={sorted(missing_pri)}")

# 2h) paper_writer 中文名表（已单源化到注册表，验证派生一致）
import paper_writer
pw_labels = {k: paper_writer._method_label(k) for k in KEYS}
reg_labels = method_labels()
mismatch_pw = {k for k in KEYS if pw_labels[k] != reg_labels.get(k, k)}
check("paper_writer._method_label 与注册表 method_labels() 一致", not mismatch_pw,
      f"不一致={sorted(mismatch_pw)}")

# 2i) _dispatch_analysis 里不应该再出现方法名字面量
import inspect
src = inspect.getsource(_dispatch_analysis)
hardcoded = [k for k in KEYS if f'"{k}"' in src or f"'{k}'" in src]
check("_dispatch_analysis 已无硬编码方法分支（表驱动）", not hardcoded,
      f"仍有硬编码={hardcoded}")

# 2j) v1.4 方法学知识图谱（①知识图谱）
# 图谱是第 8 处可能漂移的地方：加方法时要同步决策树，否则前端画不出新方法的路径。
# 这里把「树结构自洽 + 与注册表一致 + 端点可用」全部钉死。
import methods_graph as mg

graph = mg.build_graph()
check("图谱 uncovered 为空（决策树覆盖全部注册方法）",
      not graph["uncovered"], f"未覆盖={graph['uncovered']}")

node_ids = {n["id"] for n in graph["nodes"]}
bad_edges = [(n["id"], o["to"]) for n in graph["nodes"]
             for o in (n.get("options") or []) if o["to"] not in node_ids]
check("图谱所有边都指向存在的节点", not bad_edges, f"坏边={bad_edges}")

# 从 root 做 BFS：每个方法节点都必须可达，否则前端高亮不到它
method_nodes = [n for n in graph["nodes"] if n.get("kind") == "method"]
_adj = {n["id"]: [o["to"] for o in (n.get("options") or [])] for n in graph["nodes"]}
_seen, _q = {graph["root"]}, [graph["root"]]
while _q:
    _cur = _q.pop(0)
    for _nx in _adj.get(_cur, []):
        if _nx not in _seen:
            _seen.add(_nx)
            _q.append(_nx)
unreachable = [n["id"] for n in method_nodes if n["id"] not in _seen]
check("每个方法节点都从 root 可达（前端能高亮）", not unreachable,
      f"不可达={unreachable}")

# 方法节点中文名必须与注册表一致（防止图谱自己维护一份标签）
_reg_labels = method_labels()
bad_g_label = [n["id"] for n in method_nodes
               if n.get("label") != _reg_labels.get(n.get("method"))]
check("图谱方法节点中文名与注册表一致", not bad_g_label, f"标签不符={bad_g_label}")

# 决策路径：每步 node 必须存在，且末节点是该方法的终点节点
bad_path = []
for _k, _steps in mg._PATH_BY_METHOD.items():
    if not _steps:
        continue
    if any(s.get("node") not in node_ids for s in _steps):
        bad_path.append((_k, "含无效 node"))
        continue
    _mn = next((n for n in method_nodes if n.get("method") == _k), None)
    if _mn is None or _steps[-1].get("node") != _mn["id"]:
        bad_path.append((_k, _steps[-1].get("node")))
check("每个方法的决策路径终止于自己的方法节点", not bad_path, f"末节点错={bad_path}")

missing_path = [k for k in KEYS if k not in mg._PATH_BY_METHOD]
check("每个注册方法都有决策路径", not missing_path, f"缺路径={missing_path}")

# 端点可用性（含 uncovered 为空）
_gj = app.test_client().get("/api/methods_graph").get_json()
check("/api/methods_graph 返回 ok", bool(_gj.get("ok")), f"resp={_gj}")
check("/api/methods_graph 的 uncovered 为空",
      not (_gj.get("graph", {}).get("uncovered") or []),
      f"uncovered={_gj.get('graph', {}).get('uncovered')}")

# ===========================================================================
section("3. 字段解析（alias 契约）")
# ===========================================================================
df_demo = pd.DataFrame({
    "g": ["a"] * 6 + ["b"] * 6,
    "y": [1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "y2": [2.0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
    "g2": ["x", "y"] * 6,
    "t1": [1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "t2": [2.0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
    "t3": [3.0, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
})

# 逗号字符串 → list（历史前端格式）
try:
    r = call_method("cronbach_alpha", df_demo, {"item_cols": "t1,t2,t3"})
    check("item_cols 逗号字符串可解析", r["summary"]["k"] == 3)
except Exception as e:  # noqa: BLE001
    check("item_cols 逗号字符串可解析", False, str(e))

# 未知方法 → MissingField，且消息列出全部支持方法
try:
    call_method("no_such_method", df_demo, {})
    check("未知方法抛 MissingField", False, "未抛异常")
except MissingField as e:
    check("未知方法抛 MissingField", True)
    check("未知方法提示列出全部方法",
          all(k in str(e) for k in KEYS))

# 缺字段的具体报错（抽 3 个代表）
for key, payload, want_sub in [
    ("independent_t", {}, "分组列"),
    ("cronbach_alpha", {"item_cols": ["t1"]}, "至少 2 个题项"),
    ("repeated_measures_anova", {"item_cols": ["t1", "t2"]}, "至少 3 个时间点"),
]:
    try:
        call_method(key, df_demo, payload)
        check(f"{key} 缺字段应抛异常", False, "未抛异常")
    except MissingField as e:
        check(f"{key} 缺字段提示正确", want_sub in str(e), f"got={e}")

# ===========================================================================
section("4. 行为等价：12 个方法逐个真跑")
# ===========================================================================
client = app.test_client()


def upload(path):
    with open(path, "rb") as f:
        return client.post("/api/upload", data={"file": (io.BytesIO(f.read()), Path(path).name)},
                           content_type="multipart/form-data").get_json()


# 用示例数据跑一个「每方法一次」的冒烟矩阵
# 说明：示例数据里没有二分类因变量，logistic_regression 用一个临时构造的
# DataFrame 走 call_method（不依赖 examples/），保证 12 个方法都被真实执行。
smoke = [
    ("student_scores.csv", "independent_t", {"group_col": "gender", "value_col": "score"}),
    ("student_scores.csv", "anova", {"group_col": "study_intensity", "value_col": "score"}),
    ("student_scores.csv", "correlation", {"value_col": "score", "value_col2": "study_hours"}),
    ("student_scores.csv", "chi_square", {"group_col": "gender", "value_col": "pass"}),
    ("student_scores.csv", "paired_t", {"value_col": "anxiety_pre", "value_col2": "anxiety_post"}),
    ("student_scores.csv", "mann_whitney", {"group_col": "gender", "value_col": "reaction_time_ms"}),
    ("student_scores.csv", "wilcoxon", {"value_col": "anxiety_pre", "value_col2": "anxiety_post"}),
    ("sample_regression_data.csv", "linear_regression",
     {"value_col": "年收入", "x_cols": ["受教育年限", "年龄", "工作经验"]}),
    ("questionnaire_data.csv", "cronbach_alpha", {"item_cols": ["q1", "q2", "q3", "q4", "q5", "q6"]}),
    ("two_way_data.csv", "two_way_anova", {"group_col": "gender", "value_col2": "teaching_method", "value_col": "score"}),
    ("rm_anova_data.csv", "repeated_measures_anova", {"item_cols": ["前测", "1个月", "3个月", "6个月"]}),
]

hit = set()
for fname, method, extra in smoke:
    fpath = EX / fname
    if not fpath.exists():
        warn(f"{method} 示例数据缺失（{fname}），跳过", "")
        continue
    up = upload(fpath)
    if not up.get("ok"):
        check(f"{method} 上传失败", False, str(up.get("error")))
        continue
    r = client.post("/api/analyze", json={"file_id": up["file_id"], "method": method, **extra})
    d = r.get_json()
    ok = (r.status_code == 200 and d.get("ok") is True
          and d.get("method") == method
          and isinstance(d.get("markdown"), str) and len(d["markdown"]) > 100)
    check(f"{method} 真跑通过（HTTP）", ok,
          f"status={r.status_code} err={d.get('error')}")
    if ok:
        hit.add(method)

# logistic_regression：示例数据无二分类因变量，用合成数据走注册表路径
import numpy as _np
_rng = _np.random.default_rng(3)
_n = 80
_logit_df = pd.DataFrame({
    "x1": _rng.normal(0, 1, _n),
    "x2": _rng.normal(0, 1, _n),
})
_lp = 1.4 * _logit_df["x1"] - 0.8 * _logit_df["x2"]
_logit_df["y"] = (_rng.random(_n) < 1 / (1 + _np.exp(-_lp))).astype(int)
try:
    _r = call_method("logistic_regression", _logit_df, {"value_col": "y", "x_cols": ["x1", "x2"]})
    check("logistic_regression 真跑通过（注册表路径）",
          _r["method"] == "logistic_regression" and len(_r["markdown"]) > 100)
    hit.add("logistic_regression")
except Exception as e:  # noqa: BLE001
    check("logistic_regression 真跑通过（注册表路径）", False, str(e))

check("12 个方法全部真跑成功", hit == set(KEYS),
      f"未跑通={sorted(set(KEYS) - hit)}")

# 表驱动路径 与 直接调 run_* 结果一致（抽查 3 个）
import app as app_mod
for key, payload, direct, csv_name in [
    ("independent_t",
     {"group_col": "gender", "value_col": "score"},
     lambda df: app_mod.run_independent_t(df, "gender", "score"),
     "student_scores.csv"),
    ("correlation",
     {"value_col": "score", "value_col2": "study_hours"},
     lambda df: app_mod.run_correlation(df, "score", "study_hours"),
     "student_scores.csv"),
    ("two_way_anova",
     {"group_col": "gender", "value_col2": "teaching_method", "value_col": "score"},
     lambda df: app_mod.run_two_way_anova(df, "gender", "teaching_method", "score"),
     "two_way_data.csv"),
]:
    dfx = pd.read_csv(EX / csv_name)
    a = call_method(key, dfx, payload)
    b = direct(dfx)
    check(f"{key}：注册表路径 == 直接调用（summary + markdown 全等）",
          a["summary"] == b["summary"] and a["markdown"] == b["markdown"])


# ===========================================================================
print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败 / {WARN} 警告")
print("=" * 72)
sys.exit(1 if FAIL else 0)
