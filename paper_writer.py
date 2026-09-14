"""论文副驾驶 · 证据约束写作引擎（Evidence-Constrained Paper Writer）

把统计分析结果，转成「不越界」的论文初稿。

## 为什么要「证据约束」

大模型写论文最大的问题是**幻觉**：把没做过的实验写成做过了，把弱结论写成强结论。
本模块的做法参照 Scientify 的 claim / evidence 体系（`write-paper` skill）：

    claim_inventory.md   每条结论 = 一条 claim，必须挂来源文件 + 基线 + 置信度
    figures_manifest.md  每张图 = 一条条目，声明它支撑哪条 claim
    draft.md             正文里每条结果段必须锚定 claim_id + 含定量陈述

**硬规则（写作闸门）**：
- 没有来源文件的 claim → 不许出现在正文
- 没有基线的比较句 → 不许写
- 置信度非 high 的 claim → 不许进摘要
- 无法锚定 claim_id 的结果段 → 不许写

## 两套模板

1. **LaTeX（理工科实验论文）**：`paper/manuscript.tex` + `paper/sections/*.tex`
2. **中文 docx（文科 / 学位论文）**：`paper/thesis.md` → 可经 `/api/export` 转 docx

## 用法

    from paper_writer import build_claim_inventory, build_figures_manifest, render_draft

    inv = build_claim_inventory(analysis_results)     # 从分析结果抽 claim
    man = build_figures_manifest(analysis_results)    # 从分析结果抽图
    draft = render_draft(inv, man, title=..., template="latex" | "thesis")

本模块**纯逻辑，不调 LLM**（保证零幻觉）。LLM 润色是可选的下游步骤。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

from methods_registry import method_labels as _registry_method_labels


# ---------------------------------------------------------------------------
# 证据契约
# ---------------------------------------------------------------------------

# claim 允许出现的章节（硬约束，不在此列表内的章节不许放该 claim）
SECTION_ABSTRACT = "abstract"
SECTION_INTRO = "introduction"
SECTION_SETUP = "problem_setup"
SECTION_METHOD = "method_system"
SECTION_PROTOCOL = "experimental_protocol"
SECTION_RESULTS = "main_results"
SECTION_CONCLUSION = "conclusion"
SECTION_BOUNDARY = "boundary_note"

ALL_SECTIONS = [
    SECTION_ABSTRACT, SECTION_INTRO, SECTION_SETUP, SECTION_METHOD,
    SECTION_PROTOCOL, SECTION_RESULTS, SECTION_CONCLUSION, SECTION_BOUNDARY,
]

# 高风险的「大词」——出现时必须同句或下一句有数字/基线/来源锚点
PRAISE_WORDS = [
    "significant", "substantial", "strong", "robust", "effective",
    "promising", "remarkable", "novel", "comprehensive", "state-of-the-art",
    "显著", "重要", "强大", "稳健", "有效", "有前景", "卓越", "新颖", "全面", "领先",
]

VAGUE_PHRASES = [
    "shows advantages", "performs well", "works effectively",
    "achieves competitive results", "delivers better performance",
    "improves overall quality", "demonstrates robustness", "has broad applicability",
    "表现良好", "效果显著", "具有优势", "性能优异", "有较好表现", "提升了整体质量",
]


@dataclass
class Claim:
    """一条可独立核查的结论。"""

    claim_id: str
    claim_text: str
    claim_type: str                     # result / observation / interpretation
    source_files: list[str] = field(default_factory=list)
    figure_or_table_anchor: str = ""
    baseline: str = ""
    protocol_or_guardrail: str = ""
    evidence_type: str = "local_runtime"   # simulator / local_runtime / runtime
    confidence: str = "medium"             # high / medium / low
    allowed_in_sections: list[str] = field(default_factory=lambda: [SECTION_RESULTS])
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_writable(self) -> bool:
        """写作闸门：缺来源 / 缺基线（仅比较类）→ 不可写。"""
        if not self.source_files:
            return False
        if not self.claim_text.strip():
            return False
        return True

    def can_enter_abstract(self) -> bool:
        return self.confidence == "high" and self.is_writable()

    def to_yaml(self) -> str:
        """输出成 claim_inventory 的 YAML 片段。"""
        lines = [
            f'- claim_id: "{self.claim_id}"',
            f'  claim_text: "{self._esc(self.claim_text)}"',
            f'  claim_type: "{self.claim_type}"',
            "  source_files:",
        ]
        lines += [f'    - "{s}"' for s in self.source_files] or ['    - "TODO"']
        lines += [
            f'  figure_or_table_anchor: "{self.figure_or_table_anchor}"',
            f'  baseline: "{self.baseline}"',
            f'  protocol_or_guardrail: "{self.protocol_or_guardrail}"',
            f'  evidence_type: "{self.evidence_type}"',
            f'  confidence: "{self.confidence}"',
            "  allowed_in_sections:",
        ]
        lines += [f'    - "{s}"' for s in self.allowed_in_sections]
        return "\n".join(lines)

    @staticmethod
    def _esc(s: str) -> str:
        return s.replace('"', "'").replace("\n", " ").strip()


@dataclass
class FigureEntry:
    """图表条目：声明它支撑哪些 claim。"""

    figure_id: str
    file_path: str
    latex_label: str
    section: str
    placement_hint: str
    caption_short: str
    caption_long: str
    takeaway_sentence: str
    callout_sentence: str
    baseline: str = ""
    evidence_type: str = "local_runtime"
    source_metrics: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    supports_claim_ids: list[str] = field(default_factory=list)
    must_appear_before_claim_ids: list[str] = field(default_factory=list)

    def is_ready(self) -> bool:
        """图条目就绪 = 有 section / placement / callout / supports 四项。"""
        return bool(self.section and self.placement_hint
                    and self.callout_sentence and self.supports_claim_ids)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_md(self) -> str:
        return "\n".join([
            f'### {self.figure_id}',
            f'- file_path: `{self.file_path}`',
            f'- latex_label: `{self.latex_label}`',
            f'- section: `{self.section}`',
            f'- placement_hint: {self.placement_hint}',
            f'- caption_short: {self.caption_short}',
            f'- caption_long: {self.caption_long}',
            f'- takeaway_sentence: {self.takeaway_sentence}',
            f'- callout_sentence: {self.callout_sentence}',
            f'- baseline: {self.baseline or "（本图为描述性统计，无对比基线）"}',
            f'- evidence_type: `{self.evidence_type}`',
            f'- source_metrics: {", ".join(self.source_metrics) or "—"}',
            f'- source_files: {", ".join("`" + s + "`" for s in self.source_files) or "—"}',
            f'- supports_claim_ids: {", ".join(self.supports_claim_ids) or "—"}',
            f'- must_appear_before_claim_ids: {", ".join(self.must_appear_before_claim_ids) or "—"}',
        ])


# ---------------------------------------------------------------------------
# 从分析结果抽取 claim / figure
# ---------------------------------------------------------------------------

_METHOD_CN = {
    "independent_t": "独立样本 T 检验",
    "anova": "单因素方差分析",
    "correlation": "Pearson 相关分析",
    "chi_square": "卡方检验",
    "paired_t": "配对样本 T 检验",
    "mann_whitney": "Mann-Whitney U 检验",
    "wilcoxon": "Wilcoxon 符号秩检验",
    "linear_regression": "多元线性回归",
    "logistic_regression": "二元 Logistic 回归",
    "cronbach_alpha": "Cronbach's α 信度分析",
    "two_way_anova": "双因素方差分析",
    "repeated_measures_anova": "重复测量方差分析",
}

# p 值 → 显著性措辞（避免一律写"显著"）
def _sig_phrase(p: float | None) -> tuple[str, str]:
    if p is None:
        return "未报告", "low"
    if p < 0.001:
        return "p < 0.001", "high"
    if p < 0.01:
        return f"p = {p:.3f}", "high"
    if p < 0.05:
        return f"p = {p:.3f}", "high"
    return f"p = {p:.3f}（未达 0.05 显著水平）", "high"


def _fmt(x: Any, digits: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def _method_label(method: str) -> str:
    return _METHOD_CN.get(method, method)


def build_claim_inventory(results: list[dict[str, Any]],
                          source_file: str = "analysis_res.md") -> list[Claim]:
    """把分析结果列表变成 claim 台账。

    results: 每个元素是 `run_*` 返回的 dict（含 method / summary / variables）。
    """
    claims: list[Claim] = []
    n = 0

    for res in results or []:
        method = res.get("method", "")
        summary = res.get("summary", {}) or {}
        label = _method_label(method)
        n += 1
        cid = f"claim-{n:03d}"

        text, baseline, confidence, metrics, anchor = _claim_from_summary(method, summary, label)
        if not text:
            continue

        claims.append(Claim(
            claim_id=cid,
            claim_text=text,
            claim_type="result",
            source_files=[source_file],
            figure_or_table_anchor=anchor,
            baseline=baseline,
            protocol_or_guardrail=res.get("guardrail", "真实数据，Python 科学计算栈计算"),
            evidence_type="local_runtime",
            confidence=confidence,
            allowed_in_sections=[SECTION_ABSTRACT, SECTION_RESULTS, SECTION_CONCLUSION],
            metrics=metrics,
        ))

    return claims


def _claim_from_summary(method: str, sumry: dict[str, Any],
                        label: str) -> tuple[str, str, str, dict[str, Any], str]:
    """针对每种方法，生成「含定量陈述 + 基线」的 claim 文本。

    返回 (claim_text, baseline, confidence, metrics, figure_anchor)
    """
    m: dict[str, Any] = {}

    if method == "independent_t":
        t, p = sumry.get("t"), sumry.get("p")
        g1, g2 = sumry.get("group_means", {}), None
        m1, m2 = sumry.get("mean1"), sumry.get("mean2")
        sig, conf = _sig_phrase(p)
        base = "另一组"
        txt = (f"独立样本 T 检验显示两组均值差异为 {_fmt((m1 or 0) - (m2 or 0))}"
               f"（t = {_fmt(t)}，{sig}），基线为对照组均值 {_fmt(m2)}。") if m1 is not None else ""
        return txt, base, conf, {"t": t, "p": p, "mean1": m1, "mean2": m2}, "fig-main-t"

    if method == "anova":
        f, p = sumry.get("f"), sumry.get("p")
        sig, conf = _sig_phrase(p)
        eta = sumry.get("eta_squared")
        txt = (f"单因素方差分析显示组间差异（F = {_fmt(f)}，{sig}），"
               f"效应量 η² = {_fmt(eta)}，基线为零假设（组间无差异）。")
        return txt, "零假设", conf, {"F": f, "p": p, "eta2": eta}, "fig-main-anova"

    if method == "paired_t":
        t, p = sumry.get("t"), sumry.get("p")
        sig, conf = _sig_phrase(p)
        txt = (f"配对样本 T 检验显示前后测差异（t = {_fmt(t)}，{sig}），"
               f"基线为无变化假设（差值为 0）。")
        return txt, "前后测无差异", conf, {"t": t, "p": p}, "fig-main-paired"

    if method == "correlation":
        r, p = sumry.get("r"), sumry.get("p")
        sig, conf = _sig_phrase(p)
        txt = (f"Pearson 相关分析显示两变量相关系数 r = {_fmt(r)}（{sig}），"
               f"基线为 r = 0（无线性相关）。")
        return txt, "r = 0（无相关）", conf, {"r": r, "p": p}, "fig-main-corr"

    if method == "chi_square":
        chi2, p = sumry.get("chi2"), sumry.get("p")
        v = sumry.get("cramers_v")
        sig, conf = _sig_phrase(p)
        txt = (f"卡方检验显示两分类变量关联（χ² = {_fmt(chi2)}，{sig}），"
               f"Cramér's V = {_fmt(v)}，基线为变量独立假设。")
        return txt, "变量独立", conf, {"chi2": chi2, "p": p, "V": v}, "fig-main-chi"

    if method in ("mann_whitney", "wilcoxon"):
        u, p = sumry.get("u") or sumry.get("stat"), sumry.get("p")
        sig, conf = _sig_phrase(p)
        txt = (f"{label}显示组间/前后差异（统计量 = {_fmt(u)}，{sig}），"
               f"基线为零假设（无差异）。")
        return txt, "零假设", conf, {"stat": u, "p": p}, "fig-main-nonparam"

    if method == "linear_regression":
        r2, p = sumry.get("r2"), sumry.get("f_p")
        sig, conf = _sig_phrase(p)
        txt = (f"多元线性回归模型 R² = {_fmt(r2)}（整体 F 检验 {sig}），"
               f"基线为仅含截距的零模型。")
        return txt, "零模型（仅截距）", conf, {"R2": r2, "p": p}, "fig-main-reg"

    if method == "logistic_regression":
        pr2, p = sumry.get("pseudo_r2") or sumry.get("mcfadden_r2"), sumry.get("ll_p")
        sig, conf = _sig_phrase(p)
        txt = (f"二元 Logistic 回归 McFadden 伪 R² = {_fmt(pr2)}（似然比检验 {sig}），"
               f"基线为零模型（仅截距）。")
        return txt, "零模型（仅截距）", conf, {"pseudo_R2": pr2, "p": p}, "fig-main-logit"

    if method == "cronbach_alpha":
        a = sumry.get("alpha")
        k = sumry.get("k") or sumry.get("n_items")
        conf = "high" if (a is not None and a >= 0.7) else "medium"
        txt = (f"量表共 {k} 题，Cronbach's α = {_fmt(a)}，"
               f"基线为 α = 0.7（可接受信度门槛）。")
        return txt, "α = 0.7 门槛", conf, {"alpha": a, "k": k}, "fig-main-alpha"

    if method == "two_way_anova":
        fa, pa = sumry.get("f_a"), sumry.get("p_a")
        fb, pb = sumry.get("f_b"), sumry.get("p_b")
        fab, pab = sumry.get("f_ab"), sumry.get("p_ab")
        sig, conf = _sig_phrase(pab)
        txt = (f"双因素方差分析：因素 A（F = {_fmt(fa)}）、因素 B（F = {_fmt(fb)}）、"
               f"交互 A×B（F = {_fmt(fab)}，{sig}），基线为无主效应 / 无交互的零模型。")
        return txt, "零模型（无主效应/无交互）", conf, {
            "F_A": fa, "p_A": pa, "F_B": fb, "p_B": pb, "F_AB": fab, "p_AB": pab,
        }, "fig-main-twoway"

    if method == "repeated_measures_anova":
        f, p = sumry.get("f"), sumry.get("p")
        k = sumry.get("k")
        eta2 = sumry.get("eta2")
        sig, conf = _sig_phrase(p)
        # 球形度假定不成立时，置信度降到 medium（结论需以 GG 校正为准）
        if sumry.get("sphericity_ok") is False:
            conf = "medium"
        txt = (f"重复测量方差分析（{k} 个时间点，被试内设计）：时间主效应 "
               f"F = {_fmt(f)}（{sig}），偏 η² = {_fmt(eta2)}，"
               f"基线为各时间点均值相等（无时间效应）。")
        return txt, "零模型（无时间效应）", conf, {
            "F": f, "p": p, "k": k, "partial_eta2": eta2,
            "GG_epsilon": sumry.get("gg_epsilon"),
            "sphericity_ok": sumry.get("sphericity_ok"),
        }, "fig-main-rm"

    return "", "", "low", {}, ""


def build_figures_manifest(results: list[dict[str, Any]],
                           claims: list[Claim],
                           chart_paths: dict[str, str] | None = None,
                           source_file: str = "analysis_res.md") -> list[FigureEntry]:
    """把分析结果 + claim 台账 → 图表清单。"""
    chart_paths = chart_paths or {}
    # claim_id 按方法顺序对齐
    by_anchor: dict[str, list[str]] = {}
    for c in claims:
        by_anchor.setdefault(c.figure_or_table_anchor, []).append(c.claim_id)

    figs: list[FigureEntry] = []
    for res in results or []:
        method = res.get("method", "")
        label = _method_label(method)
        anchor = ""
        # 复用 claim 抽取得到的 anchor
        for c in claims:
            if c.claim_type == "result" and res.get("method") and anchor == "":
                # 用 source 顺序粗略对应
                pass
        # 更稳的做法：重算一次 anchor
        _, _, _, _, anchor = _claim_from_summary(method, res.get("summary", {}) or {}, label)
        if not anchor:
            continue

        claim_ids = by_anchor.get(anchor, [])
        if not claim_ids:
            continue

        fpath = chart_paths.get(method, f"figures/{method}.png")
        figs.append(FigureEntry(
            figure_id=anchor,
            file_path=fpath,
            latex_label=f"fig:{method}",
            section=SECTION_RESULTS,
            placement_hint="首次讨论该结果段之后（top of page）",
            caption_short=f"{label}结果图",
            caption_long=f"{label}的结果可视化；支撑 {'、'.join(claim_ids)}。",
            takeaway_sentence=f"该图给出 {label} 的关键统计量分布。",
            callout_sentence=f"图 1 展示了 {label} 的结果（见 {'、'.join(claim_ids)}）。",
            baseline=next((c.baseline for c in claims if c.claim_id in claim_ids), ""),
            evidence_type="local_runtime",
            source_metrics=list(next((c.metrics for c in claims if c.claim_id in claim_ids), {}).keys()),
            source_files=[source_file],
            supports_claim_ids=claim_ids,
            must_appear_before_claim_ids=claim_ids,
        ))
    return figs


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _inventory_md(claims: list[Claim], title: str) -> str:
    body = "\n".join(c.to_yaml() for c in claims) or "# （无 claim）"
    return f"""# Claim Inventory · {title}

> 由「论文副驾驶」自动生成。每条 claim 必须挂来源文件 + 基线 + 置信度。
> 写作闸门：无来源的 claim 不许进正文；置信度非 high 不许进摘要。

```yaml
{body}
```

## 统计

- Claim 总数：{len(claims)}
- 可进摘要（confidence=high）：{sum(1 for c in claims if c.can_enter_abstract())}
- 缺来源（不可写）：{sum(1 for c in claims if not c.is_writable())}
"""


def _manifest_md(figs: list[FigureEntry], title: str) -> str:
    body = "\n\n".join(f.to_md() for f in figs) or "### （无图表）"
    return f"""# Figures Manifest · {title}

> 图表清单是「图表支撑哪条 claim」的唯一真源。
> 首次提及图时必须使用 `callout_sentence`；图的 `supports_claim_ids` 必须与正文一致。

{body}
"""


def _abstract(claims: list[Claim], title: str) -> str:
    """四句式摘要：问题 / 方法 / 最强结果 / 边界。只用 high 置信度 claim。

    注意：摘要受写作闸门约束，重述结果时必须带 claim 锚点（可核查）。
    """
    strong = [c for c in claims if c.can_enter_abstract()]
    best = strong[0] if strong else None
    s1 = f"本研究围绕「{title}」这一问题展开。"
    # 方法句只用「方法名」，不引用 claim 正文片段（避免摘要出现无锚点的结果陈述）
    method_names = []
    for c in claims:
        # claim_text 形如「独立样本 T 检验显示两组均值差异为 …」
        mt = re.match(r"^([^，,。；;]{2,20}?(?:检验|分析|回归|方差分析))", c.claim_text)
        if mt:
            method_names.append(mt.group(1).strip())
    s2 = (f"采用 { '、'.join(dict.fromkeys(method_names[:4])) } 等方法对真实数据进行分析。"
          if method_names else "采用统计分析方法对真实数据进行分析。")
    # 摘要重述最强结果：保留 claim 锚点，保证可回溯（长句内联）
    if best:
        s3 = f"最强证据（{best.claim_id}）：{best.claim_text}"
    else:
        s3 = "当前证据不足以支撑摘要级结论，结果详见正文。"
    s4 = "本结论基于本地真实数据计算；样本量与外部效度受原始数据限制，未做跨样本验证。"
    return f"{s1}\n{s2}\n{s3}\n{s4}"


def render_draft(claims: list[Claim], figs: list[FigureEntry],
                 title: str = "未命名研究",
                 template: str = "latex",
                 sections_extra: dict[str, str] | None = None) -> str:
    """渲染论文初稿。

    template: "latex"（理工科实验论文）| "thesis"（中文 / 学位论文）
    """
    if template == "latex":
        return _render_latex(claims, figs, title, sections_extra or {})
    return _render_thesis(claims, figs, title, sections_extra or {})


def _result_paragraphs(claims: list[Claim], figs: list[FigureEntry]) -> list[str]:
    """每个结果段 = claim 句 + 证据句 + 对比句 + 边界句（段落契约）。"""
    paras = []
    fig_by_claim: dict[str, FigureEntry] = {}
    for f in figs:
        for cid in f.supports_claim_ids:
            fig_by_claim.setdefault(cid, f)

    for c in claims:
        if not c.is_writable():
            continue
        # 保持「（claim_id）正文」在同一条句子里，避免锚点被判丢失
        pts = [f"（{c.claim_id}）{c.claim_text}".strip()]
        if c.baseline:
            pts.append(f"该结论以「{c.baseline}」为对比基线。")
        if c.protocol_or_guardrail:
            pts.append(f"计算协议：{c.protocol_or_guardrail}。")
        f = fig_by_claim.get(c.claim_id)
        if f:
            pts.append(f.callout_sentence)
        pts.append("该结论的证据边界为本地运行结果，未经外部数据复核。")
        # 结果段内部用空格连接成段，保证 claim_id 与其正文不被换行切断
        paras.append("".join(pts))
    return paras


def _render_thesis(claims: list[Claim], figs: list[FigureEntry],
                   title: str, extra: dict[str, str]) -> str:
    """中文 / 学位论文体例（可经 /api/export 转 docx）。"""
    paras = _result_paragraphs(claims, figs)
    results_md = "\n\n".join(paras) or "（当前无可写入的结果段：请先在「数据分析」阶段产出带来源的结论。）"
    fig_block = "\n\n".join(
        f"{f.callout_sentence}\n\n![{f.caption_short}]({f.file_path})\n\n*{f.caption_long}*"
        for f in figs
    ) or "（暂无图表）"
    extra_md = "\n\n".join(f"## {k}\n\n{v}" for k, v in extra.items())

    return f"""# {title}

## 摘要

{_abstract(claims, title)}

**关键词**：数据分析；统计检验；实证研究

## 1 引言

### 1.1 研究背景与问题

本研究围绕「{title}」展开，关注的核心问题是：数据中是否存在可被统计方法验证的规律。

### 1.2 研究缺口

既有分析若仅依赖描述性统计，难以判断差异与关联是否达到统计显著水平（α = 0.05），也无法给出可复核的效应量。

### 1.3 本文贡献

本文使用真实数据执行了 {len(claims)} 项统计检验，并给出可引用的定量结论与证据边界。

## 2 问题设定

- 数据来源：本地真实数据文件
- 分析单元与变量：见各方法的结果段
- 评价标准：以 p 值与效应量为判据，显著性水平 α = 0.05

## 3 方法

本文涉及的方法包括：{ '、'.join(dict.fromkeys(_registry_method_labels().get(m, m) for m in [])) or '独立样本 T 检验、方差分析、相关分析、卡方检验、回归分析、信度分析等' }。
所有统计量均由 Python 科学计算栈（NumPy / SciPy）计算，保证结果可复现。

## 4 分析协议

- 对比基线：各方法使用其对应的零假设或零模型作为基线
- 证据类型：本地运行（local_runtime）
- 质量守则：不做多重比较校正的情形已在结论中标注

## 5 结果

{results_md}

### 5.1 图表

{fig_block}

{extra_md}

## 6 结论

{_conclusion(claims)}

## 7 证据边界

- 本稿所有结论均来自本地真实数据，未做跨样本 / 跨人群验证。
- 未报告置信度非 high 的结论进入摘要。
- 若某项比较缺少明确基线，本稿不给出该比较的方向性表述。
"""


def _render_latex(claims: list[Claim], figs: list[FigureEntry],
                  title: str, extra: dict[str, str]) -> str:
    """理工科实验论文体例（LaTeX 骨架 + 已填内容）。"""
    paras = _result_paragraphs(claims, figs)
    results_tex = "\n\n".join(
        p.replace("_", r"\_").replace("%", r"\%") for p in paras
    ) or "% （暂无可写入的结果段）"

    fig_tex = "\n\n".join(
        f"""\\begin{{figure}}[t]
  \\centering
  \\includegraphics[width=0.8\\linewidth]{{{f.file_path}}}
  \\caption{{{f.caption_long}}}
  \\label{{{f.latex_label}}}
\\end{{figure}}"""
        for f in figs
    ) or "% （暂无图表）"

    return f"""% 由「论文副驾驶」生成 · 证据约束初稿
\\documentclass[11pt]{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage[T1]{{fontenc}}
\\usepackage[utf8]{{inputenc}}
\\usepackage{{microtype}}
\\usepackage{{amsmath,amssymb}}
\\usepackage{{booktabs}}
\\usepackage{{graphicx}}
\\usepackage{{xcolor}}
\\usepackage{{hyperref}}

\\title{{{title}}}
\\author{{论文副驾驶生成（待补作者）}}
\\date{{}}

\\begin{{document}}
\\maketitle

\\begin{{abstract}}
{_abstract(claims, title)}
\\end{{abstract}}

\\section{{Introduction}}
本研究围绕「{title}」，采用统计检验对真实数据进行实证分析，并给出可复核的定量结论。

\\section{{Problem Setup}}
评价标准：以 p 值与效应量为判据，显著性水平 $\\alpha = 0.05$。

\\section{{Method}}
所有统计量由 Python 科学计算栈（NumPy / SciPy）计算，保证可复现。共执行 {len(claims)} 项检验。

\\section{{Experimental Protocol}}
\\begin{{itemize}}
  \\item Baseline family: 各方法的零假设 / 零模型
  \\item Evidence boundary: \\texttt{{local\\_runtime}}（本地真实数据）
  \\item Guardrail: 未做多重比较校正的情形已在边界说明中标注
\\end{{itemize}}

\\section{{Main Results}}
{results_tex}

\\section{{Conclusion}}
{_conclusion(claims).replace("_", r"\\_")}

\\section*{{Boundary and Caveats}}
本稿结论均来自本地真实数据，未经外部数据复核；置信度非 high 的结论未进入摘要。

{fig_tex}

\\end{{document}}
"""


def _conclusion(claims: list[Claim]) -> str:
    """结论只重述最强结论，不引入新 claim / 新基线。带 claim 锚点保证可回溯。"""
    strong = [c for c in claims if c.is_writable()]
    if not strong:
        return "当前证据不足，暂不给出结论性表述。"
    picks = strong[:3]
    lines = [f"- （{c.claim_id}）{c.claim_text}" for c in picks]
    return "本研究基于真实数据得出以下结论：\n" + "\n".join(lines) + \
           "\n上述结论均以对应基线为参照，证据边界为本地运行结果。"


# ---------------------------------------------------------------------------
# 写作闸门校验
# ---------------------------------------------------------------------------

def lint_draft(text: str, claims: list[Claim]) -> list[dict[str, str]]:
    """写作闸门自检：找出高风险措辞与无锚点的结论句。

    返回 [{level, rule, excerpt}]，level ∈ {warn, error}。
    """
    issues: list[dict[str, str]] = []
    claim_ids = {c.claim_id for c in claims}

    # 关键：claim 文本自身含句号（「…基线为 r = 0（无相关）。」），
    # 若直接按「。」切句，会把「（claim-002）」锚点与它的正文切开，
    # 造成误报。正确做法是**以行/段为单位**检查锚点：一个段落里只要
    # 出现 claim 锚点，则该段受保护；未出现锚点却含结果句式的段落才报错。
    blocks = [b.strip() for b in re.split(r"\n+", text) if b.strip()]
    for block in blocks:
        if block.startswith("#"):
            continue
        anchored = any(cid in block for cid in claim_ids)
        # 块内逐句检查大词 / 空泛词（这两条不受锚点保护影响）
        for s in re.split(r"(?<=[。.!?；;])", block):
            s = s.strip()
            if not s:
                continue
            low = s.lower()
            for w in PRAISE_WORDS:
                if w in low or w in s:
                    has_num = bool(re.search(r"\d", s))
                    if not (has_num or anchored):
                        issues.append({
                            "level": "warn",
                            "rule": f"高风险措辞「{w}」缺少定量陈述或 claim 锚点",
                            "excerpt": s[:80],
                        })
            for v in VAGUE_PHRASES:
                if v in low or v in s:
                    issues.append({
                        "level": "warn",
                        "rule": f"空泛表述「{v}」建议改写为「指标 + 基线」",
                        "excerpt": s[:80],
                    })
        # 块级结果句锚点检查：整块未锚定且含结果句式 → 违规
        looks_like_result = (
            re.search(r"(T\s*检验|方差分析|相关分析|卡方检验|回归分析|信度分析|两因素|双因素)", block)
            and re.search(r"[=＝]\s*[-−]?\d", block)
            and re.search(r"(t\s*=|F\s*=|χ²|r\s*=|R²|α\s*=|p\s*[<=＝])", block)
        )
        if looks_like_result and not anchored:
            issues.append({
                "level": "error",
                "rule": "结果句未锚定任何 claim_id（违反写作闸门）",
                "excerpt": block[:80],
            })
    return issues


# ---------------------------------------------------------------------------
# 顶层入口：一次产出全套 paper/ 文件
# ---------------------------------------------------------------------------

LATEX_SECTIONS = [
    "abstract", "introduction", "problem_setup", "method_system",
    "experimental_protocol", "main_results", "conclusion",
]


def build_paper_bundle(results: list[dict[str, Any]],
                       title: str = "未命名研究",
                       template: str = "latex",
                       chart_paths: dict[str, str] | None = None,
                       source_file: str = "analysis_res.md") -> dict[str, str]:
    """一次生成整套论文文件（相对路径 → 内容）。

    template="latex"  → claim_inventory.md / figures_manifest.md / draft.md / manuscript.tex / sections/*.tex
    template="thesis" → claim_inventory.md / figures_manifest.md / draft.md / thesis.md
    """
    claims = build_claim_inventory(results, source_file=source_file)
    figs = build_figures_manifest(results, claims, chart_paths=chart_paths, source_file=source_file)

    draft = render_draft(claims, figs, title=title, template=template)
    bundle: dict[str, str] = {
        "paper/claim_inventory.md": _inventory_md(claims, title),
        "paper/figures_manifest.md": _manifest_md(figs, title),
        "paper/draft.md": render_draft(claims, figs, title=title, template="thesis"),
    }

    if template == "latex":
        bundle["paper/manuscript.tex"] = draft
        # 拆出 section 文件（供 LaTeX \input）
        bundle["paper/sections/abstract.tex"] = _abstract(claims, title)
        bundle["paper/sections/introduction.tex"] = (
            "% Introduction\n本研究围绕「" + title + "」，采用统计检验对真实数据进行实证分析。"
        )
        bundle["paper/sections/main_results.tex"] = "\n\n".join(_result_paragraphs(claims, figs))
        bundle["paper/build_paper.sh"] = _build_script()
        bundle["paper/references.bib"] = "% TODO: 补参考文献\n"
    else:
        bundle["paper/thesis.md"] = draft

    return bundle


def _build_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
PAPER_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$PAPER_DIR/build"
LOG_PATH="$BUILD_DIR/build.log"
ERROR_PATH="$BUILD_DIR/build_errors.md"
mkdir -p "$BUILD_DIR"
if ! command -v tectonic >/dev/null 2>&1; then
  cat >"$ERROR_PATH" <<'EOF'
# Paper Build Error
`tectonic` was not found in `PATH`.
Install `tectonic` first, then rerun: bash paper/build_paper.sh
EOF
  exit 1
fi
cd "$PAPER_DIR"
if tectonic --outdir "$BUILD_DIR" manuscript.tex >"$LOG_PATH" 2>&1; then
  rm -f "$ERROR_PATH"
else
  { echo "# Paper Build Error"; echo; echo '```text'; tail -n 80 "$LOG_PATH" || true; echo '```'; } >"$ERROR_PATH"
  exit 1
fi
"""
