"""论文内部自洽核查（v2.45 · 零数据依赖）
==========================================
这一层刻意**不碰用户数据**——只用论文自己的文字，就能挑出四类自相矛盾。
为什么单列一层：它既是最大的行为门槛（不用上传数据 = 不用犹豫），
也是最硬的隐私承诺（数据根本没进来）。四条纪律：

  1. 只报「对不上」，绝不给建议。说「此处措辞与所报结果不符，请核对」，
     不说「建议补充回归分析」——后者是代写，越线。
  2. 只描述，不定性。用词一律「可疑，请核对」，永不出现「造假 / 抄袭」。
  3. 宁可漏报，不可误报。任一条拿不准就整条不报——误报一次，
     用户对整份报告的信任就没了（与 extract_paper.find_abstract_span 同源）。
  4. 每条发现自带证据：原文摘录 + 字符偏移，用户能自己跳回去看。

四把刀：
  ① 结论措辞 ↔ 实际做了什么（只有相关却写「导致 / 促进」）
  ② 假设条目数 ↔ 实际报了几次检验（列了 4 条假设，只报了 1 个统计量）
  ③ 图表编号自洽（正文引「图 3」，全文最大图题只到 2）
  ④ 置信区间 ↔ 点估计（CI [8,12] 却把差值写成 5）

不做的事：不做章节切分。不同学校模板差别太大，靠标题猜「这是结论章」
非常脆，猜错比不猜更伤。改用**全文措辞扫描 + 结果章定位**——后者只用来
排序（结果章里的因果措辞更值得提醒），定位不到也照常扫全文。
"""
from __future__ import annotations

import re
from typing import Any

# -----------------------------------------------------------------------------
# 共用工具：中文数字 → 阿拉伯数字（图「一」与 图「1」是同一条）
# -----------------------------------------------------------------------------
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_CHARS = "一二三四五六七八九十"
# 编号的字符类：阿拉伯数字或中文数字（图 1 / 图一 / 图十三）
_NUM_ALT = r"(?:[0-9]{1,2}|[一二三四五六七八九十]{1,3})"


def _to_int(s: str) -> int | None:
    """'3' / '三' / '十三' / '三十二' → int；认不出返回 None（宁可不认，绝不猜）。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_NUM:      # 十三 → 13
        return 10 + _CN_NUM[s[1]]
    if len(s) == 2 and s[0] in _CN_NUM and s[1] == "十":      # 三十 → 30
        return _CN_NUM[s[0]] * 10
    if len(s) == 3 and s[0] in _CN_NUM and s[1] == "十" and s[2] in _CN_NUM:
        return _CN_NUM[s[0]] * 10 + _CN_NUM[s[2]]             # 三十二 → 32
    return None


def _snippet(text: str, start: int, end: int) -> str:
    """取命中处上下文（两侧各 22 字），压成一行。

    宽度是**可读性**约束不是技术约束：这些摘录会原样出现在建议列表里，
    太长会把一条建议撑成三行，用户反而看不到结论。22 字足够定位到句子。
    """
    s = max(0, start - 22)
    e = min(len(text), end + 22)
    return re.sub(r"\s+", " ", text[s:e]).strip()


# =============================================================================
# ① 结论措辞 ↔ 实际做了什么
# =============================================================================
# 动机：本科/硕士论文最常见的一处不自洽——方法章节只做了相关分析
# （Pearson / 问卷 / 横断面），结论却写「显著提高」「导致了」。
# 这是**论文内部**的矛盾，不需要任何数据就能指出来。
#
# 纪律：只在「只做了相关、没有任何实验/干预方法」时才报。
# 有 ANOVA / t 检验且论文自称实验组对照组时，因果措辞就是合法的，一律不报。
#
# 【为什么不给因果动词加 \b 或 lookaround】
# 中文没有词边界：`(?<![一-龥])` 会把「显著提高了」里的「提高了」整个挡掉
# （前一个字是汉字），而后置 \b 对中文完全失效（CLAUDE.md 六·平台注意事项
# 记过同一课：「采用LSTM」检测不到）。所以这里**直接抓动词**，
# 靠左邻文字的语义切片来排除「引用文献 / 假设陈述」——切片判比正则稳。
_CAUSAL_VERBS = ("导致", "引起", "引发", "致使", "造成", "促进",
                 "提升了", "提高了", "降低", "减少", "改善", "抑制", "增强")
_CAUSAL_RE = re.compile("|".join(re.escape(v) for v in _CAUSAL_VERBS))
# 左邻这些词 → 是引用别人的研究或做假设陈述，不是本文结论
_CAUSAL_LEFT_SKIP = re.compile(
    r"(假设|推测|猜想|据|文献|前人|已有研究|研究者|既往|相关研究|若|如果|可能|也许|或许|有望)")
# 右邻这些字 → 动词是名词用法（「影响力」「促进性」），不是因果断言
_CAUSAL_RIGHT_SKIP = re.compile(r"^[性力率度者物]")
# 句末标点：用来把左邻窗口截到"本句开头"（中文）或 30 字（英文/长句）
_SENT_START_RE = re.compile(r"[。！？；\n]")

# 结果章节标题（只用于排序，不裁剪文本）
_RESULTS_HEAD = re.compile(
    r"(第\s*[四五六4-6]\s*章[^\n]{0,20}(结果|分析|实证)|"
    r"^\s*[四五六4-6][\s、.．]\s*(结果|实证|数据分析)|"
    r"研究结果|实证结果|结果与分析|数据分析与结果)", re.MULTILINE)

# 纯相关类方法（做了这些且没有实验类方法 → 因果措辞才可疑）
_CORRELATIONAL_KEYS = {"correlation", "spearman", "cronbach_alpha",
                       "regression", "linear_regression", "logistic_regression"}
# 实验/干预类方法（出现任一 → 因果措辞合法，整条不报）
_EXPERIMENTAL_KEYS = {"independent_t", "paired_t", "one_sample_t", "anova",
                      "two_way_anova", "repeated_measures_anova", "mann_whitney",
                      "wilcoxon", "kruskal_wallis"}


def check_causal_wording(text: str, methods: list[dict] | None = None) -> dict[str, Any]:
    """只做了相关分析，正文却写因果 → 逐条列出（带原文摘录与偏移）。"""
    text = text or ""
    keys = {m.get("method_key") or "" for m in methods or []}
    if keys & _EXPERIMENTAL_KEYS:
        return {"applicable": False, "hits": [],
                "reason": "论文声明了实验/干预类方法（组间比较），因果措辞在本设计中成立，本项不核查。"}
    if not (keys & _CORRELATIONAL_KEYS):
        return {"applicable": False, "hits": [],
                "reason": "论文未声明相关/回归类方法，本项无从判断，不报。"}

    rm = _RESULTS_HEAD.search(text)
    results_at = rm.start() if rm else -1
    labels = sorted({m.get("method_label") or "" for m in methods or [] if m.get("method_label")})
    hits: list[dict[str, Any]] = []
    seen: set[int] = set()
    for m in _CAUSAL_RE.finditer(text):
        verb = m.group(0)
        if m.start() in seen:
            continue
        left = text[max(0, m.start() - 30):m.start()]
        # 截到"本句开头"：只判这个词所在的那句话
        # （窗口取 8~10 字会漏掉「前人研究发现，手机使用会」这种 12 字前缀，
        #   实测把文献综述句误判成本文结论——见 coherence_test 的反例）
        _brk = _SENT_START_RE.search(left)
        if _brk:
            left = left[_brk.end():]
        if _CAUSAL_LEFT_SKIP.search(left):
            continue
        if _CAUSAL_RIGHT_SKIP.search(text[m.end():m.end() + 1]):
            continue
        seen.add(m.start())
        hits.append({
            "verb": verb,
            "context": _snippet(text, m.start(), m.end()),
            "offset": m.start(),
            # 结果章里的更可能是本文结论；引言里的多半是文献综述
            "in_results": bool(results_at >= 0 and m.start() >= results_at),
        })
    hits.sort(key=lambda h: (not h["in_results"], h["offset"]))
    return {
        "applicable": True,
        "hits": hits,
        "reason": (f"论文使用的方法是「{'、'.join(labels)}」，属于**相关/预测**类，"
                   f"不建立因果。以下措辞与此不符，请核对是否表述过头。"),
    }


# =============================================================================
# ② 假设条目数 ↔ 实际报了几次检验
# =============================================================================
# 动机：论文第 3 章列了 H1/H2/H3/H4 四条假设，结果章只报了 1 个 t 值。
# 要么漏报，要么只在正文提了没做——两种都该自查。
#
# 纪律（防误报）：
#   · 条目数 < 3 不报（2 条假设只报 1 个检验太常见，误报率划不来）
#   · 「假设 3 个因素」这类量词短语排除（不是在列假设条目）
_HYP_RE = re.compile(
    r"(?:假设|假说)\s*(" + _NUM_ALT + r")"
    r"|(?<![A-Za-z])([Hh])\s*([0-9]{1,2})(?![A-Za-z0-9])")
# 编号后紧跟量词 → 是"假设 N 个/种/类"的叙述句，不是假设条目
_HYP_MEASURE = re.compile(r"^\s*[个种类条次项名位组部分]")


def check_hypothesis_coverage(text: str, quantities: list[dict] | None = None) -> dict[str, Any]:
    """数「列了几条假设」与「报了几次检验」，差距过大就提醒。"""
    text = text or ""
    # 【为什么自己再扫一遍正则，而不直接用传入的 quantities】
    # extract_quantities 按 (kind, raw) 去重——两处都写「p < 0.05」会合并成
    # 一条（count=2），位置信息丢失。数"几次检验"必须按位置扫。
    from extract_paper import _PATTERNS  # 延迟导入：与 extract_paper 共用同一套正则

    spans: list[tuple[int, int]] = []      # 非 p 的统计量（t/F/r/χ²/M/SD…）
    anchors: list[int] = []                # p 值位置（当"这次检验"的锚）
    for kind, regs in _PATTERNS.items():
        for reg in regs:
            for m in reg.finditer(text):
                if kind == "p":
                    anchors.append(m.start())
                else:
                    spans.append((m.start(), m.end()))

    test_groups: set[int] = set()
    for s, _e in spans:
        # 把统计量归到「它所属的那次检验」= 前后 150 字内最近的 p 值；
        # 找不到 p 值就自己算一次（有些论文只报 F/χ² 不报 p）。
        cand = [a for a in anchors if a - 150 <= s <= a + 150]
        test_groups.add(min(cand) if cand else s)
    # 独立的 p 值也算一次检验的痕迹——但只在全文 p 值不多时信它。
    # 表格脚注「*** p<0.01，** p<0.05」会一次抓几十条，那种情况不能当真。
    if len(anchors) <= 30:
        test_groups |= set(anchors)
    test_count = len(test_groups)

    declared: list[str] = []
    seen_pos: list[int] = []
    for m in _HYP_RE.finditer(text):
        raw_num = m.group(1) or m.group(3) or ""
        n = _to_int(raw_num)
        if n is None:
            continue
        if _HYP_MEASURE.search(text[m.end():m.end() + 3]):
            continue                      # 「假设 3 个因素」→ 不是假说条目
        # 「假设H1」会被两条模式重叠命中 → 按位置 ±12 字去重
        if any(abs(m.start() - p) <= 12 for p in seen_pos):
            continue
        seen_pos.append(m.start())
        label = "H" + str(n) if m.group(2) else f"假设{n}"
        declared.append((n, label))

    # 按编号去重 + 排序（报告里读起来是 H1/H2/H3 的顺序）
    uniq = sorted({n: lb for n, lb in declared}.items())
    declared_str = [f"{lb}（第 {n} 条）" for n, lb in uniq]
    n_decl = len(uniq)
    if n_decl < 3:
        return {"applicable": False, "declared": declared_str, "test_count": test_count,
                "missed": 0,
                "reason": f"论文中识别到的编号假设不足 3 条（{n_decl} 条），不核查覆盖率。"}
    missed = n_decl - test_count
    return {
        "applicable": True,
        "declared": declared_str,
        "test_count": test_count,
        "missed": max(0, missed),
        "reason": (f"论文列出 {n_decl} 条编号假设，全文识别到约 {test_count} 次检验/统计量报告。"
                   if missed >= 2 else
                   f"列出 {n_decl} 条编号假设，识别到约 {test_count} 次检验报告，数量基本相称。"),
    }


# =============================================================================
# ③ 图表编号自洽
# =============================================================================
# 动机：正文写「见图 3」，全文最大图题只到 2（删了图没改正文）。
#
# 纪律：**只看编号越界，不看数量差**——
#   实际图题【少于】正文引用，很可能是标题格式特殊没被解析到，
#   数量差一两个就报会误伤一大批。只有「引用编号 > 最大图题编号」是硬矛盾。
_FIG_CAP = re.compile(r"(?:^|\n)[ \t　]*(图|Fig\.?|Figure|图表)[ \t　]*(" + _NUM_ALT + r")")
_TAB_CAP = re.compile(r"(?:^|\n)[ \t　]*(表|Tab\.?|Table)[ \t　]*(" + _NUM_ALT + r")")
_FIG_REF = re.compile(r"(图|Fig\.?|Figure)[ \t　]*(" + _NUM_ALT + r")")
_TAB_REF = re.compile(r"(表|Tab\.?|Table)[ \t　]*(" + _NUM_ALT + r")")


def _scan_charts(text: str, cap_re: re.Pattern[str],
                 ref_re: re.Pattern[str]) -> dict[str, Any]:
    """扫一遍某一类（图或表）：图题编号集合 + 正文引用列表。

    【关键】引用必须**排除落在图题里的那些**——否则图题自己会被当成引用，
    引用编号永远 ≤ 最大图题编号，「越界」这项就永远查不出来（空转）。
    """
    cap_spans: list[tuple[int, int]] = []
    caps: set[int] = set()
    for m in cap_re.finditer(text):
        n = _to_int(m.group(2))
        if n is None:
            continue
        cap_spans.append(m.span())
        caps.add(n)

    def _in_caption(pos: int) -> bool:
        return any(a <= pos < b for a, b in cap_spans)

    refs: list[tuple[int, str, int]] = []
    for m in ref_re.finditer(text):
        if _in_caption(m.start()):
            continue
        n = _to_int(m.group(2))
        if n is None:
            continue
        refs.append((n, m.group(0).replace(" ", ""), m.start()))

    ref_nums = sorted({r[0] for r in refs})
    return {"caps": caps, "refs": refs, "ref_nums": ref_nums,
            "max_cap": max(caps) if caps else 0,
            "max_ref": max(ref_nums) if ref_nums else 0}


def check_figure_table_numbering(text: str) -> dict[str, Any]:
    """正文引用的图表编号 vs 全文实际出现的图表题编号。"""
    text = text or ""
    fig = _scan_charts(text, _FIG_CAP, _FIG_REF)
    tab = _scan_charts(text, _TAB_CAP, _TAB_REF)

    issues: list[dict[str, Any]] = []
    for label, kind, d in (("图", "figure", fig), ("表", "table", tab)):
        # 至少要认出 2 个图表题才敢判越界——只认出 1 个多半是解析没抓全，
        # 这时"越界"的结论不可靠，宁可不说。
        if len(d["caps"]) < 2:
            continue
        for n in [x for x in d["ref_nums"] if x > d["max_cap"]][:5]:
            raw, off = next(((r[1], r[2]) for r in d["refs"] if r[0] == n),
                            (f"{label}{n}", 0))
            issues.append({
                "kind": kind, "label_cn": label, "number": n,
                "caption_max": d["max_cap"], "caption_count": len(d["caps"]),
                "raw": raw, "context": _snippet(text, off, off + len(raw)),
                "offset": off,
            })
        # 反向：有图表题但正文一次都没引用（多数学校允许，只提一句）
        if d["ref_nums"] and len(d["caps"]) > 2:
            unreferenced = sorted(d["caps"] - set(d["ref_nums"]))
            if 0 < len(unreferenced) <= len(d["caps"]) // 2:
                issues.append({
                    "kind": kind, "label_cn": label, "number": None,
                    "unreferenced": unreferenced, "context": "", "offset": 0,
                })
    return {
        # caps 必须转成 list —— set 不能 JSON 序列化，直接放进报告会让
        # /api/paper_check 整个 500（v2.45 实测踩过：9 个套件跟着红）。
        "fig": {"max_cap": fig["max_cap"], "ref_nums": fig["ref_nums"],
                "caps": sorted(fig["caps"])},
        "tab": {"max_cap": tab["max_cap"], "ref_nums": tab["ref_nums"],
                "caps": sorted(tab["caps"])},
        "issues": issues,
    }


# =============================================================================
# ④ 置信区间 ↔ 点估计
# =============================================================================
# 动机：「差值为 5.2（95% CI [8.1, 12.3]）」——区间中点 10.2 ≠ 5.2，
# 也就是说这个区间根本不属于这个点估计（多半是抄了别处的数）。
# 纯算术，可以手验，所以归到「最硬的一类核查」。
# 只做均值型区间的中点核对——率差/OR 的 CI 不是算术对称的，不碰。
_CI_RE = re.compile(
    r"(?:95\s*%\s*(?:CI|置信区间)|置信区间|CI)\s*[:：=]?\s*[\[\(（]\s*"
    r"(-?\d+\.?\d*)\s*[,，]\s*(-?\d+\.?\d*)\s*[\]\)）]")
_TOL = 0.06      # 容差：报告位数少时（CI [8, 12] 中点 10.0）留够四舍五入余量

# 明确标记的点估计形式（只有这些能**触发**报错，见下）
_EXPLICIT_EST = (
    (r"(?:差值|均差|差异)\s*[为是=＝:：]?\s*(-?\d+\.?\d*)", "差值"),
    (r"\bM\s*[=＝]\s*(-?\d+\.?\d*)", "M"),
    (r"(?:均值|平均分|平均数)\s*[为是=＝:：]?\s*(-?\d+\.?\d*)", "均值"),
    (r"(?:β|beta)\s*[=＝]\s*(-?\d+\.?\d*)", "β"),
    (r"\bOR\s*[=＝]\s*(-?\d+\.?\d*)", "OR"),
    (r"\bCohen'?s\s*d\s*[=＝]\s*(-?\d+\.?\d*)", "d"),
)


def check_ci_vs_estimate(text: str) -> dict[str, Any]:
    """CI 中点与同处**明确标记**的点估计对不上 → 报。

    【为什么只有明确标记才能触发报错】兜底匹配（在 CI 前 25 字里随便挑个
    小数）会把年份、页码、样本量当点估计，报出去全是误报。所以兜底分支
    **只用于确认一致（算 checked），永不产生 mismatch** —— 宁可漏报。
    """
    text = text or ""
    out: list[dict[str, Any]] = []
    checked = 0
    for m in _CI_RE.finditer(text):
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo >= hi:
            continue
        mid = (lo + hi) / 2.0
        seg_s = max(0, m.start() - 60)
        seg = text[seg_s:m.end() + 60]
        ci_s, ci_e = m.start() - seg_s, m.end() - seg_s

        peer = None
        for pat, name in _EXPLICIT_EST:
            for pm in re.finditer(pat, seg, re.IGNORECASE):
                # 别把 CI 自身的两个边界当点估计
                if pm.start(1) >= ci_s and pm.end(1) <= ci_e:
                    continue
                peer = {"value": float(pm.group(1)), "raw": f"{name} = {pm.group(1)}",
                        "explicit": True}
                break
            if peer:
                break
        if peer is None:
            # 兜底：CI 前 25 字内若有个小数，只用来"确认一致"，不产生 mismatch
            for pm in re.finditer(r"(-?\d+\.\d+)", seg[max(0, ci_s - 25):ci_s]):
                if abs(float(pm.group(1)) - mid) <= _TOL:
                    peer = {"value": float(pm.group(1)), "raw": pm.group(1),
                            "explicit": False}
                    break
            if peer is None:
                continue
        checked += 1
        val = peer["value"]
        if abs(val - mid) <= _TOL or not peer["explicit"]:
            continue
        out.append({
            "estimated": val, "ci": [lo, hi], "midpoint": round(mid, 4),
            "delta": round(abs(val - mid), 4),
            "raw": m.group(0).replace(" ", ""), "peer_raw": peer["raw"],
            "context": _snippet(text, m.start(), m.end()), "offset": m.start(),
        })
    return {"checked": checked, "mismatches": out}


# =============================================================================
# 汇总
# =============================================================================
def run_coherence_checks(text: str, methods: list[dict] | None = None) -> dict[str, Any]:
    """跑齐四把刀；返回可直接进 API 的 dict（无 numpy / 无数据依赖）。"""
    text = text or ""
    causal = check_causal_wording(text, methods)
    hypo = check_hypothesis_coverage(text)
    charts = check_figure_table_numbering(text)
    ci = check_ci_vs_estimate(text)
    findings = (len(causal["hits"])
                + (1 if hypo.get("applicable") and hypo["missed"] >= 2 else 0)
                + len(charts["issues"]) + len(ci["mismatches"]))
    return {
        "causal": causal,
        "hypothesis": hypo,
        "chart_numbering": charts,
        "ci": ci,
        "findings": findings,
        "note": "" if text.strip() else "论文正文为空，本项无内容可查。",
    }


def summarize(coherence: dict[str, Any]) -> list[str]:
    """把发现压成「[论文自洽] …」建议行（进报告第六节）。

    措辞纪律：只描述「哪里对不上」，不给「你应该做什么」。
    """
    out: list[str] = []
    c = coherence or {}

    for h in (c.get("causal") or {}).get("hits", [])[:5]:
        where = "结果部分" if h.get("in_results") else "正文"
        out.append(f"[论文自洽] {where}出现因果措辞「{h['verb']}」"
                   f"（原文「{h['context']}」），但论文用的是相关/预测类方法。"
                   f"请核对该处表述是否与所用方法相称。")

    hypo = c.get("hypothesis") or {}
    if hypo.get("applicable") and hypo.get("missed", 0) >= 2:
        out.append(f"[论文自洽] 论文列出 {len(hypo['declared'])} 条编号假设"
                   f"（{'、'.join(hypo['declared'][:6])}），"
                   f"但全文只识别到约 {hypo['test_count']} 次检验/统计量报告。"
                   f"请核对是否有假设未做检验、或结果未写全。")

    for iss in (c.get("chart_numbering") or {}).get("issues", []):
        if iss.get("number"):
            out.append(f"[论文自洽] 正文引用「{iss['raw']}」"
                       f"（原文「{iss['context']}」），但全文只找到 "
                       f"{iss['caption_count']} 个{iss['label_cn']}题"
                       f"（最大编号 {iss['caption_max']}）。"
                       f"请核对是否删了{iss['label_cn']}而正文未改。")
        elif iss.get("unreferenced"):
            nums = "、".join(f"{iss['label_cn']}{n}" for n in iss["unreferenced"][:6])
            out.append(f"[论文自洽] 以下{iss['label_cn']}有题无引用：{nums}，"
                       f"请核对正文是否漏引。")

    for m in (c.get("ci") or {}).get("mismatches", []):
        out.append(f"[论文自洽] 置信区间「{m['raw']}」的中点约为 {m['midpoint']}，"
                   f"但同处写的点估计是 {m['peer_raw']}（差 {m['delta']}）。"
                   f"请核对该区间是否对应这个估计值。")
    return out
