"""方法学知识图谱（v1.4 · ①知识图谱）
================================================================
把 12 个统计方法 + 前提假设 + 适用数据类型，织成**可点击决策图**。
用户顺着「几组？连续/分类？配对吗？」往下走，前端 SVG 高亮通往正确方法的路径。

## 为什么从注册表派生（而不是再硬编码一份清单）

刚做完 `methods_registry` 单源化。如果图谱再维护一份方法清单，就凭空多出
**第 8 处漂移源**——加方法时又得记得同步这里，而这正是本项目反复踩过的坑。

所以本模块**只负责注册表管不了的东西**：
  - 决策树结构（问什么、怎么分叉）
  - 每个方法的前提假设 / 适用时机文案

而**方法的中文名、key 全集一律从注册表派生**：

    method_labels()   → 方法中文名
    method_keys()     → 方法全集（build_graph 会校验覆盖）

## 数据契约（给前端 SVG）

build_graph() 返回：

    {
      "version":   "1.0",
      "root":      "q_goal",
      "nodes":     [ {id, label, kind, depth, options:[{to,label}], method?} ],
      "methods":   { key: {label, when, assumptions:[str]} },
      "keys":      [ ...注册表 key 全集，前端可按此校验 ]
    }

- `kind`：`q`（问题）/ `a`（分支答案）/ `method`（终点方法）
- `options`：出边；`to` 指向另一个 node id
- 终点节点额外带 `method`（= 注册表 key），前端据此反查 `methods[key]`

## 用法

    from methods_graph import build_graph, enrich_recommendation

    g   = build_graph()                       # 静态图谱，给 /api/methods_graph
    rec = enrich_recommendation(rec, columns) # 给 _recommend_method 补 decision_path/candidates

**纯数据 + 纯函数，零 LLM**（护栏：图谱数据硬编码可审，永不白屏）。
"""
from __future__ import annotations

from typing import Any

from methods_registry import method_keys, method_labels

GRAPH_VERSION = "1.0"


# ---------------------------------------------------------------------------
# 每个方法的「什么时候用」——一句话，进候选卡片
# ---------------------------------------------------------------------------
_WHEN: dict[str, str] = {
    "independent_t": "比较 2 个独立组的均值差异",
    "paired_t": "同一批对象前后测的均值差异",
    "anova": "比较 3 个及以上独立组的均值差异",
    "two_way_anova": "同时考察 2 个分类因素及其交互作用",
    "repeated_measures_anova": "同一批对象在 3+ 个时间点的变化",
    "correlation": "考察 2 个连续变量的线性关联强度",
    "linear_regression": "用 1 个或多个自变量预测连续因变量",
    "logistic_regression": "用 1 个或多个自变量预测二分类结局",
    "chi_square": "考察 2 个分类变量的关联性",
    "mann_whitney": "2 个独立组、不满足正态时的差异检验",
    "wilcoxon": "配对样本、不满足正态时的差异检验",
    "cronbach_alpha": "检验量表多个题项的内部一致性",
}


# ---------------------------------------------------------------------------
# 每个方法的前提假设——硬编码可审，不依赖 LLM
# ---------------------------------------------------------------------------
_ASSUMPTIONS: dict[str, list[str]] = {
    "independent_t": [
        "两组观测相互独立",
        "因变量为连续变量",
        "各组近似正态（每组 n ≥ 30 时可放宽）",
        "方差齐性（不满足时自动改用 Welch 校正）",
    ],
    "paired_t": [
        "同一批对象的两次测量（一一配对）",
        "**差值**近似正态（不是原始值正态）",
        "配对观测之间相互独立",
    ],
    "anova": [
        "3 个及以上独立组别",
        "因变量为连续变量",
        "各组近似正态",
        "方差齐性（Levene 检验）",
    ],
    "two_way_anova": [
        "2 个分类自变量（各含 ≥2 个水平）",
        "因变量为连续变量",
        "各单元格近似正态",
        "方差齐性",
        "本实现采用 Type III 平方和（含交互项）",
    ],
    "repeated_measures_anova": [
        "同一批对象在 3 个及以上时间点/条件被测量",
        "因变量为连续变量",
        "球形度假定（不满足时自动做 Greenhouse-Geisser 校正）",
        "缺失采用完整案例策略（缺任一时间点则剔除该对象）",
    ],
    "correlation": [
        "2 个连续变量",
        "线性关系（先看散点图）",
        "双变量近似正态",
        "无显著异常值（Pearson 对异常值敏感）",
    ],
    "linear_regression": [
        "因变量为连续变量",
        "至少 1 个自变量",
        "残差独立、近似正态、方差齐性",
        "自变量间无严重多重共线性",
    ],
    "logistic_regression": [
        "因变量为二分类（0/1）",
        "至少 1 个自变量",
        "观测之间相互独立",
        "自变量与 logit 呈线性关系",
        "无完全分离（存在时本工具会标注 ⚠️ 提示）",
    ],
    "chi_square": [
        "2 个分类变量",
        "观测之间相互独立",
        "期望频数 ≥ 5 的单元格占比 > 80%",
        "单元格期望频数不宜 < 1",
    ],
    "mann_whitney": [
        "2 个独立组别",
        "不满足正态性，或样本量很小",
        "因变量至少为有序（ordinal）尺度",
    ],
    "wilcoxon": [
        "配对 / 重复测量设计",
        "差值分布大致对称",
        "不满足正态性时替代配对 T 检验",
    ],
    "cronbach_alpha": [
        "至少 2 个量表题项",
        "题项同属一个维度（单维性）",
        "题项均为数值型（Likert 1–5 亦可）",
    ],
}


# ---------------------------------------------------------------------------
# 决策树
# ---------------------------------------------------------------------------
# 约定：
#   kind="q"      问题节点，options 是出边
#   kind="a"      中间分支节点（答案），options 继续往下
#   kind="method" 终点，带 method 字段（= 注册表 key）
#
# depth 仅用于前端布局分列（本模块不强制，前端可自行递归计算）

_TREE_NODES: list[dict[str, Any]] = [
    {
        "id": "q_goal", "kind": "q", "depth": 0,
        "label": "你想回答什么问题？",
        "options": [
            {"to": "a_diff", "label": "比较组间差异"},
            {"to": "a_rel", "label": "看变量间关系"},
            {"to": "a_scale", "label": "检验量表信度"},
        ],
    },

    # --- 分支 A：比较组间差异 ---
    {
        "id": "a_diff", "kind": "a", "depth": 1,
        "label": "比较组间差异",
        "options": [
            {"to": "q_ngroups", "label": "只有一个分组因素"},
            {"to": "q_two_factor", "label": "有两个分组因素"},
        ],
    },
    {
        "id": "q_ngroups", "kind": "q", "depth": 2,
        "label": "分组变量有几个水平？",
        "options": [
            {"to": "q_paired", "label": "2 个水平"},
            {"to": "q_repeated", "label": "3 个及以上"},
        ],
    },
    {
        "id": "q_paired", "kind": "q", "depth": 3,
        "label": "这两组是同一批人测两次吗？",
        "options": [
            {"to": "q_paired_normal", "label": "是（配对/前后测）"},
            {"to": "q_indep_normal", "label": "否（两组独立）"},
        ],
    },
    {
        "id": "q_paired_normal", "kind": "q", "depth": 4,
        "label": "差值近似正态吗？",
        "options": [
            {"to": "m_paired_t", "label": "是（或 n ≥ 30）"},
            {"to": "m_wilcoxon", "label": "否 / 样本量很小"},
        ],
    },
    {
        "id": "q_indep_normal", "kind": "q", "depth": 4,
        "label": "各组近似正态吗？",
        "options": [
            {"to": "m_independent_t", "label": "是（或 n ≥ 30）"},
            {"to": "m_mann_whitney", "label": "否 / 样本量很小"},
        ],
    },
    {
        "id": "q_repeated", "kind": "q", "depth": 3,
        "label": "这 3+ 个水平是同一批人的多次测量吗？",
        "options": [
            {"to": "m_repeated_measures_anova", "label": "是（重复测量）"},
            {"to": "m_anova", "label": "否（不同组的人）"},
        ],
    },
    {
        "id": "q_two_factor", "kind": "q", "depth": 2,
        "label": "需要看两个因素的交互作用吗？",
        "options": [
            {"to": "m_two_way_anova", "label": "是（双因素 ANOVA）"},
            {"to": "q_ngroups", "label": "否（退回单因素）"},
        ],
    },

    # --- 分支 B：看变量间关系 ---
    {
        "id": "a_rel", "kind": "a", "depth": 1,
        "label": "看变量间关系",
        "options": [
            {"to": "q_var_types", "label": "两个都是连续变量"},
            {"to": "q_cat_types", "label": "两个都是分类变量"},
            {"to": "q_predict", "label": "想做预测（有自变量）"},
        ],
    },
    {
        "id": "q_var_types", "kind": "q", "depth": 2,
        "label": "确认一下：只是看关联强度？",
        "options": [
            {"to": "m_correlation", "label": "是，只要相关系数"},
            {"to": "q_predict", "label": "不是，还想做预测"},
        ],
    },
    {
        "id": "q_cat_types", "kind": "q", "depth": 2,
        "label": "分类变量的关联性",
        "options": [
            {"to": "m_chi_square", "label": "检验是否独立"},
        ],
    },
    {
        "id": "q_predict", "kind": "q", "depth": 2,
        "label": "因变量是什么类型？",
        "options": [
            {"to": "m_linear_regression", "label": "连续（如收入、分数）"},
            {"to": "m_logistic_regression", "label": "二分类（如是否、患病/未患病）"},
        ],
    },

    # --- 分支 C：量表信度 ---
    {
        "id": "a_scale", "kind": "a", "depth": 1,
        "label": "检验量表信度",
        "options": [
            {"to": "m_cronbach_alpha", "label": "多个题项测同一维度"},
        ],
    },

    # --- 终点方法节点 ---
    {"id": "m_independent_t", "kind": "method", "depth": 5,
     "label": None, "method": "independent_t", "options": []},
    {"id": "m_paired_t", "kind": "method", "depth": 5,
     "label": None, "method": "paired_t", "options": []},
    {"id": "m_anova", "kind": "method", "depth": 4,
     "label": None, "method": "anova", "options": []},
    {"id": "m_two_way_anova", "kind": "method", "depth": 3,
     "label": None, "method": "two_way_anova", "options": []},
    {"id": "m_repeated_measures_anova", "kind": "method", "depth": 4,
     "label": None, "method": "repeated_measures_anova", "options": []},
    {"id": "m_mann_whitney", "kind": "method", "depth": 5,
     "label": None, "method": "mann_whitney", "options": []},
    {"id": "m_wilcoxon", "kind": "method", "depth": 5,
     "label": None, "method": "wilcoxon", "options": []},
    {"id": "m_correlation", "kind": "method", "depth": 3,
     "label": None, "method": "correlation", "options": []},
    {"id": "m_chi_square", "kind": "method", "depth": 3,
     "label": None, "method": "chi_square", "options": []},
    {"id": "m_linear_regression", "kind": "method", "depth": 3,
     "label": None, "method": "linear_regression", "options": []},
    {"id": "m_logistic_regression", "kind": "method", "depth": 3,
     "label": None, "method": "logistic_regression", "options": []},
    {"id": "m_cronbach_alpha", "kind": "method", "depth": 2,
     "label": None, "method": "cronbach_alpha", "options": []},
]


# ---------------------------------------------------------------------------
# 决策路径：由推荐结果 method 反推「我为什么走到这」
# ---------------------------------------------------------------------------
# 每步的 `node` = 该答案在决策树里指向的节点 id，前端据此高亮 SVG。
# （最后一 node 必为终点方法节点，前端可反查 methods[key] 展示前提假设）
_PATH_BY_METHOD: dict[str, list[dict[str, str]]] = {
    "independent_t": [
        {"q": "研究目的", "a": "比较组间差异", "next": "是否只有一个分组因素", "node": "a_diff"},
        {"q": "分组水平数", "a": "2 个水平", "next": "是否配对", "node": "q_paired"},
        {"q": "是否配对", "a": "否（两组独立）", "next": "独立样本 T 检验", "node": "m_independent_t"},
    ],
    "paired_t": [
        {"q": "研究目的", "a": "比较组间差异", "next": "是否只有一个分组因素", "node": "a_diff"},
        {"q": "分组水平数", "a": "2 个水平", "next": "是否配对", "node": "q_paired"},
        {"q": "是否配对", "a": "是（前后测）", "next": "配对样本 T 检验", "node": "m_paired_t"},
    ],
    "anova": [
        {"q": "研究目的", "a": "比较组间差异", "next": "是否只有一个分组因素", "node": "a_diff"},
        {"q": "分组水平数", "a": "3 个及以上", "next": "是否重复测量", "node": "q_repeated"},
        {"q": "是否重复测量", "a": "否（不同组的人）", "next": "单因素方差分析", "node": "m_anova"},
    ],
    "two_way_anova": [
        {"q": "研究目的", "a": "比较组间差异", "next": "是否只有一个分组因素", "node": "a_diff"},
        {"q": "分组因素", "a": "两个分类因素", "next": "是否看交互作用", "node": "q_two_factor"},
        {"q": "交互作用", "a": "是", "next": "双因素方差分析", "node": "m_two_way_anova"},
    ],
    "repeated_measures_anova": [
        {"q": "研究目的", "a": "比较组间差异", "next": "是否只有一个分组因素", "node": "a_diff"},
        {"q": "分组水平数", "a": "3 个及以上", "next": "是否重复测量", "node": "q_repeated"},
        {"q": "是否重复测量", "a": "是（同一批人多次测）", "next": "重复测量方差分析", "node": "m_repeated_measures_anova"},
    ],
    "mann_whitney": [
        {"q": "研究目的", "a": "比较组间差异", "next": "是否只有一个分组因素", "node": "a_diff"},
        {"q": "分组水平数", "a": "2 个水平", "next": "是否配对", "node": "q_paired"},
        {"q": "是否配对", "a": "否，且不满足正态", "next": "Mann-Whitney U 检验", "node": "m_mann_whitney"},
    ],
    "wilcoxon": [
        {"q": "研究目的", "a": "比较组间差异", "next": "是否只有一个分组因素", "node": "a_diff"},
        {"q": "分组水平数", "a": "2 个水平", "next": "是否配对", "node": "q_paired"},
        {"q": "是否配对", "a": "是，且不满足正态", "next": "Wilcoxon 符号秩检验", "node": "m_wilcoxon"},
    ],
    "correlation": [
        {"q": "研究目的", "a": "看变量间关系", "next": "变量类型", "node": "a_rel"},
        {"q": "变量类型", "a": "两个连续变量", "next": "只要关联强度", "node": "q_var_types"},
        {"q": "是否预测", "a": "否，只要相关系数", "next": "Pearson 相关分析", "node": "m_correlation"},
    ],
    "chi_square": [
        {"q": "研究目的", "a": "看变量间关系", "next": "变量类型", "node": "a_rel"},
        {"q": "变量类型", "a": "两个分类变量", "next": "检验独立性", "node": "q_cat_types"},
        {"q": "分析目标", "a": "检验是否独立", "next": "卡方检验", "node": "m_chi_square"},
    ],
    "linear_regression": [
        {"q": "研究目的", "a": "看变量间关系 / 做预测", "next": "变量类型", "node": "a_rel"},
        {"q": "是否有自变量", "a": "是", "next": "因变量类型", "node": "q_predict"},
        {"q": "因变量类型", "a": "连续变量", "next": "多元线性回归", "node": "m_linear_regression"},
    ],
    "logistic_regression": [
        {"q": "研究目的", "a": "看变量间关系 / 做预测", "next": "变量类型", "node": "a_rel"},
        {"q": "是否有自变量", "a": "是", "next": "因变量类型", "node": "q_predict"},
        {"q": "因变量类型", "a": "二分类", "next": "二元 Logistic 回归", "node": "m_logistic_regression"},
    ],
    "cronbach_alpha": [
        {"q": "研究目的", "a": "检验量表信度", "next": "题项情况", "node": "a_scale"},
        {"q": "题项数量", "a": "多个题项", "next": "是否同一维度", "node": "m_cronbach_alpha"},
    ],
}


# ---------------------------------------------------------------------------
# 候选方法组：推荐某个方法时，一并给出「为什么不是另一个」
# ---------------------------------------------------------------------------
_CANDIDATE_GROUPS: dict[str, list[tuple[str, str]]] = {
    "independent_t": [
        ("independent_t", "2 个独立组，因变量连续"),
        ("paired_t", "需为同一批对象的前后测"),
        ("mann_whitney", "2 个独立组但不满足正态"),
    ],
    "paired_t": [
        ("paired_t", "同一批对象前后测，差值正态"),
        ("wilcoxon", "配对设计但不满足正态"),
        ("independent_t", "需为两组互不相关的对象"),
    ],
    "anova": [
        ("anova", "3 个及以上独立组"),
        ("repeated_measures_anova", "若是同一批人的多次测量"),
        ("two_way_anova", "若同时有第 2 个分组因素"),
    ],
    "two_way_anova": [
        ("two_way_anova", "2 个分类因素 + 交互作用"),
        ("anova", "若只考察 1 个因素"),
    ],
    "repeated_measures_anova": [
        ("repeated_measures_anova", "同一批对象 3+ 个时间点"),
        ("anova", "若各时间点是不相关的不同人"),
        ("paired_t", "若只有 2 个时间点"),
    ],
    "mann_whitney": [
        ("mann_whitney", "2 个独立组，不满足正态"),
        ("independent_t", "若满足正态可获更高检验效能"),
        ("wilcoxon", "需为配对设计"),
    ],
    "wilcoxon": [
        ("wilcoxon", "配对设计，不满足正态"),
        ("paired_t", "若差值满足正态"),
        ("mann_whitney", "需为两组独立对象"),
    ],
    "correlation": [
        ("correlation", "2 个连续变量的线性关联"),
        ("linear_regression", "若要用一个变量预测另一个"),
        ("chi_square", "需两个都是分类变量"),
    ],
    "chi_square": [
        ("chi_square", "2 个分类变量的关联性"),
        ("correlation", "需两个都是连续变量"),
        ("logistic_regression", "若有明确的二分类因变量"),
    ],
    "linear_regression": [
        ("linear_regression", "连续因变量 + 1 个或多个自变量"),
        ("logistic_regression", "需因变量为二分类"),
        ("correlation", "若只要关联强度不做预测"),
    ],
    "logistic_regression": [
        ("logistic_regression", "二分类因变量 + 自变量"),
        ("linear_regression", "需因变量为连续变量"),
        ("chi_square", "若只是检验两个分类变量是否独立"),
    ],
    "cronbach_alpha": [
        ("cronbach_alpha", "多个题项测同一维度"),
        ("correlation", "若只要题项两两相关"),
    ],
}


# ---------------------------------------------------------------------------
# 图谱构造
# ---------------------------------------------------------------------------
def _build_methods_section() -> dict[str, dict[str, Any]]:
    """方法清单：中文名从注册表派生，文案由本模块提供。"""
    labels = method_labels()
    out: dict[str, dict[str, Any]] = {}
    for key in method_keys():
        out[key] = {
            "label": labels.get(key, key),
            "when": _WHEN.get(key, ""),
            "assumptions": list(_ASSUMPTIONS.get(key, [])),
        }
    return out


def build_graph() -> dict[str, Any]:
    """返回给前端 SVG 的决策图谱（JSON-serializable）。"""
    methods = _build_methods_section()

    # 终点节点的 label 由注册表填充（保证与下拉框/报告一致）
    nodes: list[dict[str, Any]] = []
    for n in _TREE_NODES:
        node = dict(n)
        if node["kind"] == "method":
            key = node["method"]
            node["label"] = methods.get(key, {}).get("label", key)
        nodes.append(node)

    # 自检：图谱覆盖全部注册方法（漏了说明加方法时忘了补决策树）
    tree_keys = {n["method"] for n in _TREE_NODES if n["kind"] == "method"}
    uncovered = [k for k in method_keys() if k not in tree_keys]
    if uncovered:
        # 不抛异常（图谱是可降级的可视化），但暴露给调用方/测试
        pass

    return {
        "version": GRAPH_VERSION,
        "root": "q_goal",
        "nodes": nodes,
        "methods": methods,
        "keys": list(method_keys()),
        "uncovered": uncovered,
    }


def graph_covers_all_methods() -> list[str]:
    """返回「注册表里有、但决策树没画」的方法 key（空 list = 完全覆盖）。"""
    tree_keys = {n["method"] for n in _TREE_NODES if n["kind"] == "method"}
    return [k for k in method_keys() if k not in tree_keys]


# ---------------------------------------------------------------------------
# 给 _recommend_method 用：补 decision_path / candidates
# ---------------------------------------------------------------------------
def enrich_recommendation(rec: dict[str, Any]) -> dict[str, Any]:
    """在 `rec` 上补 `decision_path` 与 `candidates`，原样返回。

    输入 rec 至少含 `method`。未识别的方法返回空路径/空候选（不抛异常，
    保证推荐功能永远可用——图谱是锦上添花，不该让主流程挂掉）。
    """
    method = rec.get("method")
    if not method:
        rec.setdefault("decision_path", [])
        rec.setdefault("candidates", [])
        return rec

    rec["decision_path"] = [dict(step) for step in _PATH_BY_METHOD.get(method, [])]

    labels = method_labels()
    cands: list[dict[str, Any]] = []
    for key, needs in _CANDIDATE_GROUPS.get(method, []):
        cands.append({
            "key": key,
            "label": labels.get(key, key),
            "needs": needs,
            "ok": key == method,
        })
    rec["candidates"] = cands
    return rec
