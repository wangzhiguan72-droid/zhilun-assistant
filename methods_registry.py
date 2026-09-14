"""METHODS 注册表 —— 可插拔统计方法总线（v1.3，总线 seam）
================================================================
设计目标（见《进一步完善计划.md》§二「总线蓝图 + 关键 seam」）：

把原先散落在 `app.py::_dispatch_analysis` 里的 12 个 `if method == "..."`
分支，抽成**表驱动**的注册表。加一个统计方法 = 往 `METHODS` 里插一条，
而不是再改一处控制流。

为什么这是"总线的第一刀"：
  - 后端：`_dispatch_analysis` 只需查表 → 校验 → 调用，零分支
  - 论文排查 `audit._run_real_analysis`、副驾驶 `paper_writer` 都能复用同一张表
  - 后续的插件市场（扫 `plugins/` 目录注册）、跨端 CLI（直接调表）无需重写
  - `available_methods`（前端下拉）、`method_label` 也统一由本表派生，
    从根上消除"加了方法但忘了同步某一处"的漂移（本项目已多次踩到）

## 契约

每个方法一条 `MethodSpec`：

    spec = MethodSpec(
        key          = "independent_t",              # 唯一标识（前端/论文识别/API 共用）
        fn           = run_independent_t,            # 纯函数 run_*(df, **field_values)
        fields       = (Field("group_col"), Field("value_col")),  # 必需的 payload 字段
        label        = "独立样本 T 检验",              # 报告用中文名
        picker_label = "独立样本 T 检验（2 个分组）",   # 前端下拉文案
        alias        = "group_value",                # 字段组合模式（见下）
    )

`alias` 描述"payload 字段 → run_* 位置参数"的映射模式，覆盖全部 12 个方法：

    "group_value"   (group_col, value_col)              → fn(df, g, v)
    "two_cols"      (value_col, value_col2)             → fn(df, a, b)
    "x_cols"        (value_col, x_cols[])               → fn(df, y, xs)
    "chi_square"    (group_col, value_col)              → fn(df, row, col)   # 同 group_value，语义不同
    "two_way"       (group_col, value_col2, value_col)  → fn(df, a, b, y)
    "item_cols"     (item_cols[], 至少 2)                → fn(df, items)
    "time_cols"     (item_cols[]/time_cols[], 至少 3)    → fn(df, times, subject_col?)

这样 `_dispatch_analysis` 里不再出现任何方法名。

## 加新方法的清单（应只剩 1 处）

1. 写 `run_xxx(df, ...) -> {method, summary, markdown, variables}`
2. 在 `METHODS` 里加一条 `MethodSpec`
3. 跑 `registry_test.py`（会检查与前端/论文识别/audit 的一致性并提示漏接线）

前端下拉、`/api/upload` 的 `available_methods`、`method_label` 均由本表派生，无需手改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 字段描述
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """一个 payload 字段的契约。

    name       : payload 里的键名（同时是 run_* 的来源）
    candidate  : 允许的候选键（按序回退，用于兼容历史前端命名）
    min_items  : 若字段是列表，最少几项（None = 不限制）
    numeric    : 预留标记（当前不做类型校验，校验在 run_* 内部，故解耦）
    """

    name: str
    candidate: tuple[str, ...] = ()
    min_items: int | None = None
    numeric: bool = False

    def lookup_keys(self) -> tuple[str, ...]:
        return (self.name,) + self.candidate


@dataclass(frozen=True)
class MethodSpec:
    """一个统计方法在总线上的完整描述。"""

    key: str
    fn: Callable[..., dict]
    label: str
    alias: str
    fields: tuple[Field, ...] = ()
    picker_label: str = ""
    #: 当字段缺失/不合法时的友好报错（覆盖默认生成的提示）
    error_hint: str = ""

    def display_picker_label(self) -> str:
        return self.picker_label or self.label


# ---------------------------------------------------------------------------
# 字段模式：把 payload 解析成 run_* 的位置参数
# ---------------------------------------------------------------------------
def _as_list(v: Any) -> list[str]:
    """把 "a,b" / ["a","b"] / None 统一成 list[str]（保序、去空）。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [c.strip() for c in v.split(",") if c.strip()]
    if isinstance(v, (list, tuple)):
        return [str(c).strip() for c in v if str(c).strip()]
    return []


def _get_first(payload: dict, keys: tuple[str, ...]) -> Any:
    """按序取第一个"非空"的键值。空字符串/空列表视为未提供。"""
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (list, tuple)) and len(v) == 0:
            continue
        return v
    return None


class MissingField(ValueError):
    """字段校验失败。消息即给用户看的中文提示。"""


def _resolve(spec: MethodSpec, payload: dict) -> list[Any]:
    """按 spec.alias 从 payload 解析出 run_* 的位置参数。

    解析不出来时抛 MissingField（消息已含方法中文名与缺口说明）。
    """
    alias = spec.alias
    name = spec.label

    def need(f: Field) -> Any:
        v = _get_first(payload, f.lookup_keys())
        if v is None:
            raise MissingField(spec.error_hint or f"「{name}」缺少必需参数：{f.name}")
        return v

    if alias == "group_value":
        return [need(Field("group_col")), need(Field("value_col"))]

    if alias == "chi_square":
        # 历史约定：group_col = 行，value_col = 列
        return [need(Field("group_col")), need(Field("value_col"))]

    if alias == "two_cols":
        return [need(Field("value_col")), need(Field("value_col2"))]

    if alias == "x_cols":
        y = need(Field("value_col"))
        xs = _get_first(payload, ("x_cols",))
        if not xs:
            # 兜底：单自变量走 value_col2（兼容老前端 / 副驾驶）
            v2 = _get_first(payload, ("value_col2",))
            xs = [v2] if v2 else None
        xs = _as_list(xs)
        if not xs:
            raise MissingField(spec.error_hint or f"「{name}」需要至少 1 个自变量列。")
        return [y, xs]

    if alias == "two_way":
        a = need(Field("group_col"))
        b = need(Field("value_col2"))
        y = need(Field("value_col"))
        return [a, b, y]

    if alias == "item_cols":
        items = _as_list(_get_first(payload, ("item_cols", "x_cols", "columns")))
        if len(items) < 2:
            raise MissingField(
                spec.error_hint or f"「{name}」需要至少 2 个题项（数值列）。"
            )
        return [items]

    if alias == "time_cols":
        times = _as_list(_get_first(payload, ("item_cols", "time_cols", "x_cols", "columns")))
        if len(times) < 3:
            raise MissingField(
                spec.error_hint
                or (f"「{name}」需要至少 3 个时间点/条件列"
                    f"（仅 2 个时间点时请改用配对样本 T 检验）。")
            )
        subject = payload.get("subject_col") or None
        return [times, subject]

    raise MissingField(f"「{name}」的字段模式 {alias} 未实现（注册表配置有误）。")


# ---------------------------------------------------------------------------
# 注册表本体
# ---------------------------------------------------------------------------
def _build_methods() -> dict[str, MethodSpec]:
    """延迟构造注册表：需要 import app 里的 run_* 函数。

    用函数包一层而不是模块级 import，是为了避免 `app` ↔ `methods_registry`
    的循环导入（app 需要 METHODS，METHODS 需要 app 的 run_*）。
    """
    from app import (
        run_anova,
        run_chi_square,
        run_correlation,
        run_cronbach_alpha,
        run_independent_t,
        run_linear_regression,
        run_logistic_regression,
        run_mann_whitney,
        run_paired_t,
        run_repeated_measures_anova,
        run_two_way_anova,
        run_wilcoxon,
    )

    specs = [
        MethodSpec(
            key="independent_t", fn=run_independent_t, alias="group_value",
            label="独立样本 T 检验",
            picker_label="独立样本 T 检验（2 个分组）",
            fields=(Field("group_col"), Field("value_col")),
            error_hint="独立样本 T 检验需要分组列和数值列。",
        ),
        MethodSpec(
            key="anova", fn=run_anova, alias="group_value",
            label="单因素方差分析",
            picker_label="单因素方差分析 ANOVA（3+ 个分组）",
            fields=(Field("group_col"), Field("value_col")),
            error_hint="方差分析需要分组列和数值列。",
        ),
        MethodSpec(
            key="correlation", fn=run_correlation, alias="two_cols",
            label="Pearson 相关分析",
            picker_label="Pearson 相关分析（2 个连续变量）",
            fields=(Field("value_col"), Field("value_col2")),
            error_hint="Pearson 相关需要两个数值列。",
        ),
        MethodSpec(
            key="chi_square", fn=run_chi_square, alias="chi_square",
            label="卡方检验",
            picker_label="卡方检验（2 个分类变量）",
            fields=(Field("group_col"), Field("value_col")),
            error_hint="卡方检验需要两个分类列（分组列 + 数值列字段）。",
        ),
        MethodSpec(
            key="paired_t", fn=run_paired_t, alias="two_cols",
            label="配对样本 T 检验",
            picker_label="配对样本 T 检验（同一对象前后测）",
            fields=(Field("value_col"), Field("value_col2")),
            error_hint="配对 T 检验需要两个数值列（前测 + 后测）。",
        ),
        MethodSpec(
            key="mann_whitney", fn=run_mann_whitney, alias="group_value",
            label="Mann-Whitney U 检验",
            picker_label="Mann-Whitney U 检验（2 组非参数）",
            fields=(Field("group_col"), Field("value_col")),
            error_hint="Mann-Whitney U 检验需要分组列和数值列。",
        ),
        MethodSpec(
            key="wilcoxon", fn=run_wilcoxon, alias="two_cols",
            label="Wilcoxon 符号秩检验",
            picker_label="Wilcoxon 符号秩检验（配对非参数）",
            fields=(Field("value_col"), Field("value_col2")),
            error_hint="Wilcoxon 符号秩检验需要两个数值列（前测 + 后测）。",
        ),
        MethodSpec(
            key="linear_regression", fn=run_linear_regression, alias="x_cols",
            label="多元线性回归",
            picker_label="多元线性回归（1 个数值因变量 + 多个自变量）",
            fields=(Field("value_col"), Field("x_cols", min_items=1)),
            error_hint="回归分析需要因变量列和至少 1 个自变量列。",
        ),
        MethodSpec(
            key="logistic_regression", fn=run_logistic_regression, alias="x_cols",
            label="二元 Logistic 回归",
            picker_label="二元 Logistic 回归（1 个二分类因变量 + 多个自变量）",
            fields=(Field("value_col"), Field("x_cols", min_items=1)),
            error_hint="回归分析需要因变量列和至少 1 个自变量列。",
        ),
        MethodSpec(
            key="cronbach_alpha", fn=run_cronbach_alpha, alias="item_cols",
            label="Cronbach's α 信度分析",
            picker_label="信度分析 Cronbach's α（多个量表题项）",
            fields=(Field("item_cols", candidate=("x_cols", "columns"), min_items=2),),
            error_hint="信度分析需要至少 2 个题项（数值列）。",
        ),
        MethodSpec(
            key="two_way_anova", fn=run_two_way_anova, alias="two_way",
            label="双因素方差分析",
            picker_label="双因素方差分析（2 个分类因素 + 1 个数值因变量）",
            fields=(Field("group_col"), Field("value_col2"), Field("value_col")),
            error_hint="双因素方差分析需要：因素 A 列、因素 B 列、数值因变量列。",
        ),
        MethodSpec(
            key="repeated_measures_anova", fn=run_repeated_measures_anova,
            alias="time_cols",
            label="重复测量方差分析",
            picker_label="重复测量 ANOVA（同一批被试 ≥3 个时间点）",
            fields=(Field("item_cols",
                          candidate=("time_cols", "x_cols", "columns"), min_items=3),),
            error_hint=("重复测量方差分析需要至少 3 个时间点/条件列"
                        "（仅 2 个时间点时请改用配对样本 T 检验）。"),
        ),
    ]

    return {s.key: s for s in specs}
    # ⚠️ 不要把 plugins/ 的插件并进这张表。
    # 本表是**内置方法**的真源，registry_test 会断言「前端下拉 / 副驾驶
    # CPL_METHODS / extract_paper 识别层 / audit 别名 / 方法图谱」全部覆盖它
    # ——插件一进来这些断言全崩（实测 12 项）。插件走 `app._run_plugin_method`
    # 的兜底分发（注册表不认识的方法才查插件市场），两者职责不重叠。


_METHODS_CACHE: dict[str, MethodSpec] | None = None


def get_methods() -> dict[str, MethodSpec]:
    """返回 METHODS 注册表（惰性构造 + 缓存）。"""
    global _METHODS_CACHE
    if _METHODS_CACHE is None:
        _METHODS_CACHE = _build_methods()
    return _METHODS_CACHE


def get_spec(method_key: str) -> MethodSpec | None:
    return get_methods().get(method_key)


def method_keys() -> list[str]:
    """注册表里的方法 key（注册顺序）。"""
    return list(get_methods().keys())


def available_methods() -> list[dict[str, str]]:
    """给 /api/upload 的 available_methods（前端下拉数据源）。"""
    return [{"key": s.key, "label": s.display_picker_label()}
            for s in get_methods().values()]


def method_labels() -> dict[str, str]:
    """key → 中文名（报告/日志用）。"""
    return {s.key: s.label for s in get_methods().values()}


def call_method(method_key: str, df, payload: dict) -> dict:
    """查表 → 解析字段 → 调用 run_*。

    抛 MissingField（字段缺口）或 run_* 自身抛的 ValueError。
    """
    spec = get_spec(method_key)
    if spec is None:
        raise MissingField(
            f"方法 {method_key} 不被识别。当前支持：{' / '.join(method_keys())}。"
        )

    args = _resolve(spec, payload)
    return spec.fn(df, *args)
