"""风格提示护栏复核（v2.61）
================================
背景：24 路由 × 28 题双臂实验里，「垂直校准」提示词让 16 个模型方向一致率上升，
却让 5 个下降（最差 MiMo-V2.6-Pro 57.1%→28.6%）。逐条归因 31 条变差：

    15 条  单项「被动句偏多」被判真——校准文明写豁免，模型仍答「触发成立→真」
    11 条  方法/结果段多项触发被判真——理由仍是「套话+被动式密集」
     5 条  唯一人工「真」样本被漏判——绝对化门槛把不确定塌缩成「假」
     1 条  判词与理由自相矛盾（理由写「封面页，无可判定的风格问题」，verdict=真）

共同机制：**旧提示把「触发词描述的现象是否存在」当成了「风格问题是否成立」**，
并把豁免规则交给模型自觉执行。few-shot 的「触发：X｜摘录：Y → 判 Z」格式
进一步强化了触发词匹配。

本模块的分工原则（与红线「计算本地跑、模型输出与本地结果并列互不覆盖」一致）：

    模型负责识别，本地负责定性。

    - 豁免（封面/版权页、方法段单项被动、引语与参考文献）由代码确定性判定；
    - 「真」必须有本地作者可控证据（拔高无量化 / AI 残留 / 无边界推广 /
      空泛套话 / 高风险模型未说明），模型单独说「真」不足以定性；
    - 模型的独立意见原样保留在 model_opinion，不被覆盖，交由用户核对；
    - 证据不足一律「存疑」，绝不塌缩成「假」。

旧 agents.prompts.PAPER_CHECK_SYSTEM 与 _DOMAIN_CALIBRATION 原样保留，
本模块是新增能力，不改动统计审计链路。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from ai_audit import (
    _METHOD_MARK_RE,
    _generic_hits,
    _hype_hits,
    _redlist_hits,
    _residue_hits,
    _tone_find,
)

GATE_VERSION = "v1"
TRIAGE_PROMPT_VERSION = "v1"

#: 作者可控维度：命中才可能构成「需要作者修改」的风格问题。
AUTHOR_CONTROLLED = (
    "对话式 AI 输出残留",
    "拔高词没有数字支撑",
    "无边界推广",
    "空泛套话",
    "高风险模型未说明适配性",
)

#: 学科惯例维度：中文方法学写作天然高频，单独或联合都不构成风格问题。
DISCIPLINARY = ("被动句偏多", "高频过渡套话密集")

#: 硬事实维度：在摘录窗口内就能判定（拔高词后有没有数字、有没有那句残留原文），
#: 不依赖整段密度。「空泛套话」不在其中——它是密度/语域型信号。
HARD_FACT_DIMENSIONS = frozenset(AUTHOR_CONTROLLED) - {"空泛套话"}

#: 方法/程序/结果段里，常规过渡词与被动语态同属学科惯例。
_METHOD_SECTIONS = ("方法", "结果", "实验", "程序", "测度", "测量")

#: 模型识别出这些段落类型时，本地同样按豁免处理（模型负责识别）。
_EXEMPT_SECTIONS = ("封面", "参考文献", "引语或访谈")

# ---------------------------------------------------------------------------
# 确定性豁免门
# ---------------------------------------------------------------------------
#: 封面/版权页字段族。每族只计一次，≥3 族才判豁免——单族命中（如正文提到
# 「学位论文」）不足以豁免，避免把真实论述段误放过。
_COVER_FAMILIES = (
    ("密级", re.compile(r"密\s*级")),
    ("学号", re.compile(r"学\s*号")),
    ("分类号", re.compile(r"分\s*类\s*号|中图分类号|UDC")),
    ("单位学校代码", re.compile(r"(?:单\s*位|学\s*校)\s*代\s*码")),
    ("学位论文题名页", re.compile(r"[硕博]\s*士\s*学\s*位\s*论\s*文|学位论文")),
    ("作者导师字段", re.compile(r"学位申请人|作者姓名|指导教师|导\s*师")),
    ("学科方向培养单位", re.compile(r"学\s*科\s*专\s*业|研究方向|培养单位|申请学位类别")),
    ("日期字段", re.compile(r"答辩日期|论文提交日期|学习年限|二〇[一二三四五六七八九〇]{2}\s*年")),
    ("英文题名页", re.compile(r"A\s+(?:Dissertation|Thesis)\s+Submitted|in\s+Partial\s+Fulfil",
                             re.I)),
)
_COVER_MIN_FAMILIES = 3

#: 参考文献条目特征：行首 [n] / 作者. 题名[J]. 刊名, 年, 卷(期): 页.
_REF_ENTRY_RE = re.compile(r"^\s*\[\d+\]\s*\S|\.\s*[JMCDNP]\s*[\.．]\s*|etal\.", re.M)


def cover_evidence(text: str) -> list[str]:
    """返回命中的封面字段族名（去重、保序）。"""
    t = text or ""
    return [name for name, rx in _COVER_FAMILIES if rx.search(t)]


def _is_field_layout(text: str) -> bool:
    """封面是字段清单：多行且每行短；正文论述段是长行。

    只看字段族会误伤——摘要里谈「学位论文质量 / 研究方向 / 培养单位」也能凑够
    三族。加上排版条件，论述段就不会被当成封面豁免掉。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 4:
        return False
    return sum(1 for ln in lines if len(ln) <= 40) * 2 >= len(lines)


def local_gate(text: str, reasons: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """确定性豁免门。exempt=True 时该提示不成立，且**不需要调用模型**。

    三条豁免全部来自既有校准资产，不是新发明的规则：
      - 领域校准第 1 条：封面/版权页不是论述段落；
      - v2.51 P1b：方法/程序段「被动句偏多」单项降权（ai_audit._passage_scan）；
      - v2.51 P1a：直接引语与参考文献不算作者文风（ai_audit._style_view）。
    """
    t = text or ""
    rs = list(reasons or [])
    cover = cover_evidence(t)
    if len(cover) >= _COVER_MIN_FAMILIES and _is_field_layout(t):
        return {"exempt": True, "category": "封面或版权页",
                "reason": "封面/版权页字段（" + "、".join(cover[:4]) + "）不是作者论述段落",
                "evidence": cover}
    if rs == ["被动句偏多"] and _METHOD_MARK_RE.search(t):
        return {"exempt": True, "category": "方法段单项被动",
                "reason": "方法/实验程序段的被动语态是学科惯例，单项被动不构成风格问题",
                "evidence": ["被动句偏多"]}
    if _REF_ENTRY_RE.search(t) and not _hype_hits(t):
        return {"exempt": True, "category": "参考文献条目",
                "reason": "参考文献条目不是作者文风",
                "evidence": ["参考文献格式"]}
    return {"exempt": False, "category": "", "reason": "", "evidence": []}


def local_signals(text: str) -> dict[str, Any]:
    """本地作者可控证据。全部复用 ai_audit 既有检测器，口径与全文自查一致。"""
    t = text or ""
    hype = [h["phrase"] for h in _hype_hits(t) if not h["quantified"]]
    residue = [h["phrase"] for h in _residue_hits(t)]
    generic = [h["phrase"] for h in _generic_hits(t)]
    tone = [h["phrase"] for h in _tone_find(t)]
    redlist = [h["model"] for h in _redlist_hits(t) if not h["justified"]]
    hits = {"拔高词没有数字支撑": hype, "对话式 AI 输出残留": residue,
            "无边界推广": generic, "空泛套话": tone,
            "高风险模型未说明适配性": redlist}
    return {"hits": hits,
            "present": [k for k, v in hits.items() if v],
            "truncated": t.rstrip().endswith(("…", "..."))}


# ---------------------------------------------------------------------------
# 分解式提问：识别与判断分开问，不再用「触发：X → 判 Z」式 few-shot
# ---------------------------------------------------------------------------
STYLE_TRIAGE_SYSTEM = (
    "你是论文风格复核员。系统给出一段论文摘录和自动检测到的语言现象清单。"
    "你的工作分两步，必须分别回答，不得合并成一个结论。\n"
    "第一步 识别：这段摘录属于论文的哪个部分？清单里的现象在摘录中是否真的可见？\n"
    "第二步 判断：作者是否需要为这段文字做修改？\n\n"
    "回答第二步时遵守以下纪律（本地程序还会独立复核一遍，你不必替它下最终结论）：\n"
    "- 封面、版权页、参考文献、直接引语与访谈转述不是作者论述，不需要作者修改。\n"
    "- 方法、实验程序、结果报告段的被动语态与常规过渡词是学科惯例，不需要作者修改。\n"
    "- 「现象可见」不等于「需要修改」。只有空泛拔高且无量化支撑、无边界推广、"
    "对话式 AI 残留、模板化空话连发这类作者可控的问题，才需要修改。\n"
    "- 引用了理论或文献不等于有量化支撑；判断拔高是否有支撑，只看摘录里有没有数字。\n"
    "- 摘录被截断、看不到支撑数据时，needs_revision 与 evidence_sufficient 都回答"
    "「无法判断」/「否」，不要回答「否」来否定一个你看不全的段落。\n\n"
    "只输出 JSON，不要输出别的：\n"
    '{"section_type": "封面|方法|结果|讨论|结论|引语或访谈|参考文献|其他", '
    '"visible": ["清单中确实在摘录里出现的现象名，原样照抄"], '
    '"needs_revision": "是|否|无法判断", '
    '"evidence_sufficient": "是|否", '
    '"basis": "不超过40字，必须引用摘录中的具体词句"}'
)

_TRIAGE_TEMPLATE = (
    "语言现象清单：{reasons}\n"
    "论文摘录：{excerpt}\n\n"
    "请按两步分别回答，只输出规定格式的 JSON。"
)

#: 判词为「真」但理由本身在说「这是封面/不构成问题」——自相矛盾，降级为存疑。
_CONTRADICTION_RE = re.compile(
    r"封面|版权页|元数据|元信息|密级|学号|分类号|单位代码|学校代码"
    r"|不构成|无问题|无可判定|不适用|非作者文风|不是论述|非论述"
    r"|学科惯例|正常学术|无需修改|不需要修改|无风格问题|无明显"
)


def build_triage_prompt(reasons: list[str] | tuple[str, ...], excerpt: str) -> str:
    return _TRIAGE_TEMPLATE.format(reasons="；".join(reasons or []),
                                   excerpt=(excerpt or "").strip())


def parse_triage(out: str) -> dict[str, Any] | None:
    """取第一个含 section_type 的 JSON 对象；解析失败返回 None（不猜测）。"""
    if not out:
        return None
    for m in re.finditer(r"\{.*?\}", out, re.S):
        try:
            j = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(j, dict) and "section_type" in j:
            return j
    return None


def effective_dimensions(reasons: list[str] | tuple[str, ...],
                         section_type: str) -> list[str]:
    """扣掉学科惯例维度后，剩下的才参与定性。

    被动句在任何段落都不单独构成问题（校准第 2 条）；
    常规过渡词只在方法/程序/结果段额外豁免。
    """
    rs = [r for r in (reasons or []) if r != "被动句偏多"]
    if any(k in (section_type or "") for k in _METHOD_SECTIONS):
        rs = [r for r in rs if r != "高频过渡套话密集"]
    return rs


def reconcile(parsed: dict[str, Any] | None, reasons: list[str] | tuple[str, ...],
              text: str, signals: dict[str, Any] | None = None,
              model_available: bool = True) -> dict[str, Any]:
    """本地合成最终判词。模型意见原样保留，不被覆盖。

    返回 verdict ∈ {真, 假, 存疑}；source 说明是谁定的性；guards 记录拦截理由。
    model_available=False 表示根本没调模型（用户关了 AI，或调用失败回落本地），
    与「调了但输出解析不出来」是两件事，不能共用一个来源标签。
    """
    rs = list(reasons or [])
    sig = signals if signals is not None else local_signals(text)
    guards: list[str] = []
    opinion = None
    if parsed:
        opinion = {"section_type": parsed.get("section_type", ""),
                   "needs_revision": parsed.get("needs_revision", ""),
                   "evidence_sufficient": parsed.get("evidence_sufficient", ""),
                   "visible": parsed.get("visible") or [],
                   "basis": str(parsed.get("basis", ""))[:120]}

    def out(verdict: str, source: str, reason: str) -> dict[str, Any]:
        return {"verdict": verdict, "source": source, "reason": reason,
                "guards": guards, "model_opinion": opinion,
                "local_evidence": {k: sig["hits"][k] for k in sig["present"]},
                "gate_version": GATE_VERSION}

    gate = local_gate(text, rs)
    if gate["exempt"]:
        return out("假", "local_gate", gate["reason"])

    section = (opinion or {}).get("section_type", "")
    if section in _EXEMPT_SECTIONS:
        guards.append("模型识别为豁免段落类型")
        return out("假", "section_gate", f"摘录属{section}，不是作者论述文风")

    eff = effective_dimensions(rs, section)
    if not eff:
        return out("假", "disciplinary",
                   "命中的全是学科惯例维度（被动语态/常规过渡词），不构成风格问题")

    if parsed is None:
        if not model_available:
            guards.append("未调用模型，仅本地判定")
            return out("存疑", "local_only",
                       "未启用模型复核，本地证据不足以定性，保留未判定")
        guards.append("模型输出无法解析")
        return out("存疑", "unparseable", "模型未给出可解析的识别结果，保留未判定")

    visible = {str(v) for v in (opinion or {}).get("visible", [])}
    corroborated = [d for d in eff if d in sig["present"]]
    needs = (opinion or {}).get("needs_revision", "")
    sufficient = (opinion or {}).get("evidence_sufficient", "")
    basis = (opinion or {}).get("basis", "")

    if needs == "是" and _CONTRADICTION_RE.search(basis):
        guards.append("判词与理由自相矛盾")
        return out("存疑", "contradiction",
                   f"模型认为需要修改，但理由本身在说「{basis[:40]}」，交人工核对")

    if not corroborated:
        # 模型说「真」也不定性为真：本地找不到作者可控证据。意见并列保留。
        if needs == "是":
            guards.append("模型判真但本地无作者可控证据")
            return out("存疑", "no_local_evidence",
                       "模型认为成立，本地未检出拔高无量化/残留/推广/空话证据，请人工核对")
        if needs == "否" and sufficient == "是" and not (visible & set(eff)):
            return out("假", "not_visible", "模型确认有效现象在摘录中不可见，且证据充分")
        return out("存疑", "no_local_evidence",
                   "本地未检出作者可控证据，且摘录不足以判定，保留未判定")

    if needs == "否" and sufficient == "是":
        guards.append("本地有证据但模型认为无需修改")
        return out("存疑", "disagreement",
                   "本地检出" + "、".join(corroborated) + "，模型认为无需修改，两说并列交用户核对")

    # 本地证据是窗口内可判定的事实（拔高词后有没有数字），模型也确认现象可见时，
    # 摘录被截断不构成否定理由——否则唯一的人工「真」样本会被系统性漏判。
    confirmed = [d for d in corroborated if d in visible]
    if not confirmed and needs != "是":
        return out("存疑", "unconfirmed",
                   "本地检出" + "、".join(corroborated) + "，模型未在摘录中确认该现象，保留未判定")

    # 方法/结果/程序段里，「空泛套话」会把术语表述（如「起到中介作用」）误当空话。
    # 这类密度型信号在这些段落不足以定性为真，必须有窗口内可判定的硬事实。
    if (any(k in section for k in _METHOD_SECTIONS)
            and not set(corroborated) & HARD_FACT_DIMENSIONS):
        guards.append("方法/结果段仅有密度型信号")
        return out("存疑", "soft_signal_only",
                   "本地仅检出" + "、".join(corroborated)
                   + "（密度型信号），摘录属方法/结果段，不足以定性，保留未判定")

    return out("真", "local_plus_model",
               "本地检出" + "、".join(corroborated) + "，模型确认现象可见且不属豁免段落")


#: audit_ai_traces 里那条段落定位建议的识别标记（本地豁免后要同步改口径）。
_PASSAGE_SUGGESTION_MARK = "「AI 风格较集中」"


def apply_gate(report: dict[str, Any]) -> dict[str, Any]:
    """把确定性豁免门套到 audit_ai_traces 的报告上（零 LLM）。

    被豁免的段落不是悄悄丢掉，而是移到 passages_exempted 并附豁免依据——
    用户能看到「为什么这条不算」，与红线「只报可疑、请核对」一致。
    """
    kept: list[dict] = []
    exempted: list[dict] = []
    for p in report.get("passages") or []:
        gate = local_gate(p.get("excerpt", ""), p.get("reasons") or [])
        if gate["exempt"]:
            exempted.append(dict(p, gate=gate))
        else:
            kept.append(p)
    report["passages"] = kept
    report["passages_exempted"] = exempted
    # 没豁免任何段落也要盖版本戳：否则审计者分不清「门判了但没豁免」和「这版根本没门」。
    report["gate_version"] = GATE_VERSION
    if not exempted:
        return report
    for s in report.get("suggestions") or []:
        if _PASSAGE_SUGGESTION_MARK not in str(s.get("issue", "")):
            continue
        if kept:
            s["issue"] = (f"定位到 {len(kept)} 处{_PASSAGE_SUGGESTION_MARK}的段落"
                          f"（最高的一处风险分 {kept[0]['score']}；"
                          f"另有 {len(exempted)} 处经本地豁免门判定不适用）")
        else:
            s["priority"] = "P2"
            s["issue"] = (f"自动定位的 {len(exempted)} 处段落经本地豁免门全部判定不适用")
            s["action"] = ("这些段落属封面/版权页、方法段单项被动或参考文献条目，"
                           "不是作者论述文风；明细见下方「本地豁免」，请自行核对。")
    categories: dict[str, int] = {}
    for p in exempted:
        categories[p["gate"]["category"]] = categories.get(p["gate"]["category"], 0) + 1
    report.setdefault("suggestions", []).append({
        "priority": "P2",
        "issue": "本地豁免门：" + "、".join(f"{k} {v} 处" for k, v in categories.items()),
        "action": "封面/版权页不是论述段落；方法/实验程序段的单项被动语态是学科惯例；"
                  "参考文献条目不是作者文风。这三类由代码确定性判定，不交给模型自觉执行。",
    })
    return report


def triage_passage(reasons: list[str] | tuple[str, ...], excerpt: str,
                   complete: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """单条风格提示的护栏复核。

    complete(prompt, system) -> 模型原始输出；为 None 时只跑本地门（零调用）。
    豁免命中直接返回，不调模型——既省 token，也让该类退化在结构上不可能发生。
    """
    rs = list(reasons or [])
    text = excerpt or ""
    sig = local_signals(text)
    gate = local_gate(text, rs)
    if gate["exempt"]:
        res = reconcile(None, rs, text, sig)
        res["model_called"] = False
        return res
    if complete is None:
        res = reconcile(None, rs, text, sig, model_available=False)
        res["model_called"] = False
        return res
    prompt = build_triage_prompt(rs, text)
    raw = complete(prompt, STYLE_TRIAGE_SYSTEM) or ""
    parsed = parse_triage(raw)
    res = reconcile(parsed, rs, text, sig)
    res["model_called"] = True
    res["raw"] = raw[:600]
    return res


def triage_batch(passages: list[dict] | None,
                 complete: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """整份报告的护栏复核。complete=None 时全本地、零调用。

    单条模型调用失败只降级该条（回落本地门），绝不丢条、绝不阻断整份报告。
    """
    items = []
    errors = []
    for p in passages or []:
        reasons = p.get("reasons") or []
        excerpt = p.get("excerpt", "")
        try:
            r = triage_passage(reasons, excerpt, complete)
        except Exception as exc:  # noqa: BLE001
            r = triage_passage(reasons, excerpt, None)
            r["model_called"] = True
            try:
                from agents.secrets_guard import redact
                msg = redact(str(exc))
            except Exception:  # noqa: BLE001
                msg = str(exc)
            r["error"] = msg.replace("\n", " ")[:120]
            errors.append(r["error"])
        r["score"] = p.get("score")
        r["excerpt"] = excerpt
        r["reasons"] = reasons
        items.append(r)
    counts: dict[str, int] = {}
    for r in items:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"ok": True, "items": items, "verdicts": counts,
            "model_calls": sum(1 for r in items if r["model_called"]),
            "errors": errors,
            "gate_version": GATE_VERSION, "prompt_version": TRIAGE_PROMPT_VERSION}