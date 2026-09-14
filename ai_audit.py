"""论文 AI 痕迹自查（v2.15 · BZD 审计框架的规则化落地）
==============================================================
把《BZD 数模论文 AI 痕迹审计》两层框架里**纯文本可判定**的部分做成
本地自查：高频连接词密度分档、句长均匀性、被动句占比、拔高词无量化、
提示词残留、数模高风险模型清单、无边界推广。零 LLM、零成本、断网可用。

设计边界（与项目红线 + BZD 指南自身要求一致，务必遵守）：
    1. 只输出「**AI 风格风险**」与修改建议，**绝不判定"确系 AI 生成"**——
       规范句式和术语天然可能重复，单句不能定性（BZD 边界第二条）。
    2. 本模块是"体检报告"，不是"降 AI 率开关"：ROADMAP 明确禁止
       「绕过 AI 检测」类功能。定位是帮作者把空泛套话改成有据可依的表达。
    3. 纯函数、不 import app、无副作用（与 datacheck 同类先例）。
    4. 人工自查清单（来自《数学建模论文自查表.xlsx》291 项）只摘录
       与文本形态相关的条目作为提醒，**不能自动判定的不硬判**。

评分合成（可解释优先，权重参考 BZD「语言模板化占大头」）：
    连接词密度档（BZD 阈值 15/20/25%）+ 句长均匀性 + 被动句占比
    + 拔高词/空泛套话 + 提示词残留（最强信号）+ 数模专属（红名单模型
    + 无边界推广）→ 0-100 风险分 + 五档等级（BZD 分档）。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 词表（BZD 审计 Prompt + 《AI 率检测参考》合并整理）
# ---------------------------------------------------------------------------
#: 高频连接词（BZD 三档星级，越高越"AI 常用"）
CONNECTIVES: dict[str, list[str]] = {
    "5星": ["综上所述", "综合上述", "进一步", "此外", "与此同时",
           "值得注意的是", "可以看出", "由此可见"],
    "4星": ["一般来说", "不仅如此", "所以说", "因此可见", "不得不说",
           "需要指出的是", "不容否认"],
    "3星": ["毋庸置疑", "不言而喻", "众所周知", "相比之下"],
}

#: 被动句标记（保守：只认明确的被动形态，"被"字句 / "为…所" / "得以"；
#: 句子已由 _sentences 切好，无需再锚定句尾标点）
_PASSIVE_RE = re.compile(r"被|为[^，。；！？\n]{1,12}所|得以")

#: 拔高词（BZD：拔高词后无量化数据 → 风险）
_HYPE_WORDS = ["显著提高", "显著提升", "显著增强", "显著改善", "大幅提高", "大幅提升",
               "大幅降低", "有效解决", "完美实现", "取得显著成果", "极大提高",
               "极具潜力", "无与伦比", "至关重要"]
_HYPE_RES = [(w, re.compile(re.escape(w))) for w in _HYPE_WORDS]
#: 拔高词后 30 字内出现数字/百分号/统计量 → 视为"有量化支撑"，不扣分
_HAS_NUMBER_AFTER = re.compile(r"[0-9０-９%％]|[mpn]\s*=", re.IGNORECASE)

#: 提示词残留（对话式 AI 输出直接贴进论文的强信号，P0）
_PROMPT_RESIDUE = [
    r"作为一个?AI", r"作为人工智能", r"好的[，,]以下是", r"以下是?(我|为您)(整理|撰写|生成|编写)",
    r"希望(这|以上|以下)(对您|能|些)", r"如果(您|你)(需要|还有|想)", r"抱歉[，,]我",
    r"（?注：?以上内容由", r"本(回答|回复|内容)由(AI|人工智能)",
]
_PROMPT_RES_RES = [(p, re.compile(p)) for p in _PROMPT_RESIDUE]

#: 数模竞赛「AI 高频但不适合数模」模型红名单（BZD 2.1）
#: 命中不是错，但论文必须说明"为什么它适合本题"——没有说明就是高风险特征
#: 注意：不能用 \b 词边界——中文字符与字母相邻时 Python re 不认为有边界
#: （"采用LSTM"里 用 和 L 都是 \w），改用显式的字母数字 lookaround。
def _abbr(name: str) -> str:
    return rf"(?<![A-Za-z0-9]){name}(?![A-Za-z0-9])"


MODEL_REDLIST = [
    ("LSTM", _abbr("LSTM")), ("CNN", _abbr("CNN")), ("RNN", _abbr("RNN")),
    ("深度神经网络/DNN", r"深度神经网络|" + _abbr("DNN")),
    ("Transformer/注意力机制", r"[Tt]ransformer|注意力机制"),
    ("强化学习", r"强化学习"),
    ("遗传算法", r"遗传算法|" + _abbr("GA") + r"算法"),
    ("蚁群算法", r"蚁群(算法)?"), ("粒子群优化", r"粒子群|" + _abbr("PSO")),
    ("支持向量机", r"支持向量机|" + _abbr("SVM")),
    ("随机森林", r"随机森林"), ("XGBoost/LightGBM", r"XGBoost|LightGBM"),
]

#: 无边界推广（BZD 华而不实特征 4：模型通用化无边界）
_GENERIC_PROMO = ["广泛应用于", "适用范围广", "适用范围很广", "可推广到各个",
                  "普适性强", "在任何.{0,6}(领域|场景)(都|均)"]

#: 复用 tone_guide 的空泛套话词表（同一口径，不另起炉灶）
from tone_guide import find_ai_speak as _tone_find  # noqa: E402

#: 人工自查清单（摘自《数学建模论文自查表.xlsx》里文本形态相关、
#: 工具无法自动判定的条目——报告尾部提醒作者自查，不硬判）
MANUAL_CHECKS = [
    "摘要：脱离正文仍能知道研究问题、主要模型、求解方式与关键结果？",
    "摘要：是否只在确有依据时才谈创新（没有强行拔高）？",
    "模型假设：每条假设都在后文真正用到了吗（自证式假设 = 高风险）？",
    "模型求解：参数、初值、终止条件、随机种子写了吗（只写软件名 = 高风险）？",
    "结果与检验：每张图表都有文字解释吗？结果是否逐问作答？",
    "参数溯源：每个参数能追溯到赛题原文 / 前问推导 / 物理约束吗？",
    "问间对接：上一问的哪个输出进入下一问的哪个约束，写清楚了吗？",
    "参考文献：文献真实存在且与正文对应吗（幻觉文献 = 一票高危）？",
    "附录代码：有没有对话记录、本机路径等残留？",
]

_SENT_SPLIT = re.compile(r"[。！？；\n]+")
#: 句子有效性：排除公式 / 表格 / 代码行（含 = | 大量数字或过短）
_FORMULA_RE = re.compile(r"[=＝]|\|")


def _sentences(text: str) -> list[str]:
    """切句并过滤公式/表格/超短行（这些不适合按"句子"统计）。"""
    out: list[str] = []
    for raw in _SENT_SPLIT.split(text or ""):
        s = raw.strip()
        if len(s) < 6:
            continue
        if _FORMULA_RE.search(s):
            continue
        cn = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
        if cn < 4:  # 几乎不含中文的（数字串/英文缩写）不计
            continue
        out.append(s)
    return out


def _connection_stats(sents: list[str]) -> dict:
    """高频连接词：命中句占比（BZD 密度口径）+ 分档。"""
    hits: list[dict] = []
    hit_sent_ids: set[int] = set()
    for star, words in CONNECTIVES.items():
        for w in words:
            ids = [i for i, s in enumerate(sents) if w in s]
            if ids:
                hits.append({"phrase": w, "star": star, "count": len(ids)})
                hit_sent_ids.update(ids)
    total = len(sents)
    ratio = (len(hit_sent_ids) / total) if total else 0.0
    if ratio < 0.15:
        band = "正常"
    elif ratio < 0.20:
        band = "注意"
    elif ratio < 0.25:
        band = "需改"
    else:
        band = "高风险"
    hits.sort(key=lambda h: (-h["count"], h["star"]))
    return {"ratio": round(ratio, 4), "band": band, "hits": hits,
            "sentences_with": len(hit_sent_ids), "sentences_total": total}


def _sentence_stats(text: str, sents: list[str]) -> dict:
    """句长均匀性：《AI 率检测参考》——AI 句长集中（标准差小），人类波动大。"""
    lens = [len(s) for s in sents]
    n = len(lens)
    if n < 10:
        return {"n": n, "note": "有效句子过少，跳过句长分析"}
    mean = sum(lens) / n
    var = sum((x - mean) ** 2 for x in lens) / n
    std = var ** 0.5
    uniform = std < 6.0  # 人类中文行文 std 约 4-6 起步；更小 = 高度均匀
    return {"n": n, "mean_len": round(mean, 1), "std_len": round(std, 1),
            "uniform": uniform,
            "note": ("句长高度均匀（标准差 < 6 字），是 AI 生成文本的典型特征之一"
                     if uniform else "句长有自然起伏")}


def _passive_ratio(sents: list[str]) -> dict:
    n = len(sents)
    if not n:
        return {"ratio": 0.0, "band": "正常", "n": 0}
    hits = sum(1 for s in sents if _PASSIVE_RE.search(s))
    ratio = hits / n
    band = "低风险" if ratio <= 0.20 else ("中风险" if ratio <= 0.30 else "高风险")
    return {"ratio": round(ratio, 4), "band": band, "n": hits}


def _hype_hits(text: str) -> list[dict]:
    """拔高词命中（区分"后 30 字内有数字"——有量化支撑不扣分）。"""
    out: list[dict] = []
    for w, rx in _HYPE_RES:
        for m in rx.finditer(text):
            tail = text[m.end():m.end() + 30]
            out.append({"phrase": w, "quantified": bool(_HAS_NUMBER_AFTER.search(tail)),
                        "index": m.start()})
    out.sort(key=lambda h: h["index"])
    return out


def _residue_hits(text: str) -> list[dict]:
    out = []
    for p, rx in _PROMPT_RES_RES:
        ms = list(rx.finditer(text))
        if ms:
            out.append({"pattern": p, "phrase": ms[0].group(0)[:30], "count": len(ms)})
    return out


def _redlist_hits(text: str) -> list[dict]:
    out = []
    for name, rx in MODEL_REDLIST:
        ms = list(re.finditer(rx, text))
        if ms:
            # 红名单命中后 60 字内有没有"为什么适合本题"的解释线索
            ctx = text[max(0, ms[0].start() - 40):ms[0].end() + 60]
            justified = bool(re.search(r"(因为|由于|适用|适合|本题|该题|原因|缘于)", ctx))
            out.append({"model": name, "count": len(ms), "justified": justified})
    out.sort(key=lambda h: -h["count"])
    return out


def _generic_hits(text: str) -> list[dict]:
    out = []
    for w in _GENERIC_PROMO:
        cnt = len(re.findall(w, text))
        if cnt:
            out.append({"phrase": w, "count": cnt})
    return out


def audit_ai_traces(text: str) -> dict:
    """对论文全文做 AI 风格自查，返回可 JSON 的报告。

    报告口径：只谈「风格风险」，给等级与修改建议；**绝不判定作者身份**。
    """
    text = text or ""
    sents = _sentences(text)

    conn = _connection_stats(sents)
    sent = _sentence_stats(text, sents)
    passive = _passive_ratio(sents)
    hype = _hype_hits(text)
    hype_risky = [h for h in hype if not h["quantified"]]
    residue = _residue_hits(text)
    redlist = _redlist_hits(text)
    generic = _generic_hits(text)
    tone = _tone_find(text)

    # ── 风险分合成（0=低风险 … 100=高风险；可解释的加分制）──
    score = 0
    breakdown: dict[str, int] = {}

    add = {"正常": 0, "注意": 10, "需改": 25, "高风险": 45}.get(conn["band"], 0)
    breakdown["高频连接词"] = add
    score += add

    add = 15 if sent.get("uniform") else 0
    breakdown["句长高度均匀"] = add
    score += add

    add = {"低风险": 0, "中风险": 10, "高风险": 20}.get(passive["band"], 0)
    breakdown["被动句占比"] = add
    score += add

    add = min(len(hype_risky) * 4, 20)
    breakdown["拔高词无量化"] = add
    score += add

    tone_count = sum(h["count"] for h in tone)
    add = min(tone_count * 3, 15)
    breakdown["空泛套话"] = add
    score += add

    add = min(sum(h["count"] for h in residue) * 25, 50)
    breakdown["提示词残留"] = add
    score += add

    add = min(sum(h["count"] for h in redlist if not h["justified"]) * 10, 30)
    breakdown["高风险模型未说明"] = add
    score += add

    add = min(sum(h["count"] for h in generic) * 6, 18)
    breakdown["无边界推广"] = add
    score += add

    score = min(score, 100)
    if score <= 19:
        level, level_txt = "低", "主要为正常科技论文表达"
    elif score <= 34:
        level, level_txt = "中低", "存在局部套话，核心内容具体"
    elif score <= 54:
        level, level_txt = "中", "多个部分 AI 风格明显，仍可观察到实质工作"
    elif score <= 74:
        level, level_txt = "中高", "语言明显模板化，建议逐节改写"
    else:
        level, level_txt = "高", "大量空泛拼装表达，建议整体重写相关章节"

    # ── 修改建议（P0 > P1 > P2）──
    suggestions: list[dict] = []
    if residue:
        suggestions.append({"priority": "P0", "issue": "检测到对话式 AI 输出残留"
                            f"（如「{residue[0]['phrase']}」）",
                            "action": "删除这类语句；AI 辅助内容须按赛事规范披露并用自己的话重写"})
    if conn["band"] in ("需改", "高风险"):
        top = "、".join(h["phrase"] for h in conn["hits"][:5])
        suggestions.append({"priority": "P0", "issue": f"高频连接词密度过高（{conn['band']}，"
                            f"{conn['ratio']:.0%} 的句子含高频套话：{top}）",
                            "action": "删掉不影响理解的过渡词；保留的连接词换成具体因果表述"})
    if hype_risky:
        suggestions.append({"priority": "P1", "issue": f"{len(hype_risky)} 处拔高词后无量化数据"
                            f"（如「{hype_risky[0]['phrase']}」）",
                            "action": "改成具体数字（误差、指标、对比），或删掉该评价"})
    if sent.get("uniform"):
        suggestions.append({"priority": "P1", "issue": f"句长高度均匀"
                            f"（均值 {sent.get('mean_len')} 字，标准差 {sent.get('std_len')}）",
                            "action": "长短句交错：重要结论用短句，推导过程允许长句"})
    unexplained = [h["model"] for h in redlist if not h["justified"]]
    if unexplained:
        suggestions.append({"priority": "P1", "issue": f"使用高风险模型且未见适配性说明："
                            f"{'、'.join(unexplained)}",
                            "action": "补一段「为什么它适合本题」：数据特征、规模、为什么不用更简单的方法"})
    if passive["band"] != "低风险":
        suggestions.append({"priority": "P2", "issue": f"被动句占比 {passive['ratio']:.0%}"
                            f"（{passive['band']}）",
                            "action": "改为主动句式：「被采用」→「本文采用」"})
    if generic:
        suggestions.append({"priority": "P2", "issue": "存在无边界推广表述"
                            f"（如「{generic[0]['phrase']}」）",
                            "action": "写明适用条件与局限；没有边界的效果声明不可信"})
    if tone:
        top_tone = "、".join(h["phrase"] for h in tone[:4])
        suggestions.append({"priority": "P2", "issue": f"空泛套话 {tone_count} 处（{top_tone}）",
                            "action": "删掉后不影响判断的话就删掉；总结句必须带数字"})

    return {
        "ok": True,
        "score": score,
        "level": level,
        "level_text": level_txt,
        "breakdown": breakdown,
        "metrics": {
            "connection": conn,
            "sentence": sent,
            "passive": passive,
            "hype_risky": hype_risky,
            "hype_total": len(hype),
            "residue": residue,
            "redlist": redlist,
            "generic": generic,
            "tone_hits": tone,
        },
        "suggestions": suggestions,
        "manual_checks": MANUAL_CHECKS,
        "disclaimer": ("本报告只评估文本呈现出的「AI 风格风险」，不判定是否由 AI 生成，"
                       "不替代学校/赛事的正式检测；修改请保留事实、数据、公式与引用。"),
    }
