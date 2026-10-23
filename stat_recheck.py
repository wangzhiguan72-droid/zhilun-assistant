"""论文统计报告一致性复算（v2.34 · statcheck 思路的中文论文本地化）
====================================================================
只需要论文文本、不需要原始数据：从正文抽出「检验统计量 + 自由度 + p 值」
（t / F / χ² / r / z），用 scipy 按统计量与自由度**重算 p 值**，再与论文
报告的 p 值对照。思路来自 statcheck（Nuijten et al., 2016, Behavior
Research Methods；R 包），本模块为独立 Python 实现，并适配中文论文写法：
全角括号/等号/小于号、「P 值为」「显著性 =」、χ2 / X² / x² 等变体、
Word 公式里的数学斜体字母（𝑡 𝑝 𝐹）。

判定口径：
    - 报告值按报告位数视为区间（t = 2.3 → [2.25, 2.35]；p = .02 →
      [.015, .025]），区间能对上即「一致」——舍入差异不报；
    - 对不上 → 「p 值与统计量对不上」；
    - 对不上且按 α 的显著性结论相反 → 「结论可能翻转」（最高优先级）；
    - 文中声明单侧检验、p 减半后对得上 → 「单侧一致」，不报；
    - 另查三类写法问题：写「显著」但 p ≥ α（或反之）、p = 0.000、
      t / F 只给统计量不给自由度（无法复算，如实说明覆盖面）。

纪律（与 datacheck / 取证模块同一条红线）：
    - 只说「对不上，请核对」，**永不推论造假**——绝大多数不一致是笔误、
      抄错行或舍入口径不同；
    - 宁漏勿误：统计量与 p 值须在同一句且相距 ≤ 40 字才配对；自由度缺失不猜；
      Spearman / Kendall 等秩相关不按 Pearson 公式复算；
    - 纯函数、零 LLM、不落盘、不 import app。
"""
from __future__ import annotations

import bisect
import re
from typing import Any

from scipy import stats as _st

DEFAULT_ALPHA = 0.05
_ALLOWED_ALPHA = (0.1, 0.05, 0.01, 0.001)
_PAIR_GAP = 40          # 统计量与 p 值最大间隔（字符）
_MAX_LIST = 12          # 每类问题最多逐条列出几条
_MAX_ITEMS_OUT = 200    # 返回的明细上限（防超长论文把 JSON 撑大）

# ---------------------------------------------------------------------------
# 1) 文本归一化：全角 → 半角、数学斜体 → 普通字母（逐字符 1:1，位置不变）
# ---------------------------------------------------------------------------
_CHAR_MAP: dict[str, str] = {
    "（": "(", "）": ")", "＝": "=", "＜": "<", "＞": ">", "≦": "≤", "≧": "≥",
    "，": ",", "．": ".", "：": ":", "；": ";", "－": "-", "−": "-", "–": "-",
    "‐": "-", "﹣": "-", "＋": "+", "［": "[", "］": "]", "～": "~",
    "Ｐ": "P", "ｐ": "p", "Ｔ": "T", "ｔ": "t", "Ｆ": "F", "ｆ": "f",
    "Ｒ": "R", "ｒ": "r", "Ｚ": "Z", "ｚ": "z", "Ｘ": "X", "ｘ": "x",
    "Ｎ": "N", "ｎ": "n", "²": "2", "Χ": "χ",
    # Word 公式的数学斜体（Mathematical Italic 区段，均为单个码点）
    "\U0001D45D": "p", "\U0001D443": "P", "\U0001D461": "t", "\U0001D447": "T",
    "\U0001D439": "F", "\U0001D45F": "r", "\U0001D467": "z", "\U0001D44D": "Z",
    "\U0001D45B": "n", "\U0001D441": "N", "\U0001D451": "d", "\U0001D453": "f",
    "\U0001D712": "χ", "\U0001D6FC": "α",
}
_CHAR_MAP.update({chr(0xFF10 + i): str(i) for i in range(10)})  # 全角数字
_TRANS = str.maketrans(_CHAR_MAP)


def normalize(text: str) -> str:
    """逐字符 1:1 替换，保证归一化后的下标仍能回指原文。"""
    return (text or "").translate(_TRANS)


# ---------------------------------------------------------------------------
# 2) 统计量与 p 值的正则
# ---------------------------------------------------------------------------
_NUM = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
_UNUM = r"(?:\d+(?:\.\d+)?|\.\d+)"
_PRE = r"(?<![A-Za-z0-9_])"          # 统计量字母前不能紧贴字母数字（中文可以）
_DFKEY = r"(?:df|DF|自由度|υ|ν)"

_STAT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("t", re.compile(_PRE + r"[tT]\s*\(\s*(?P<df1>\d+(?:\.\d+)?)\s*\)\s*=\s*(?P<val>" + _NUM + r")")),
    ("t", re.compile(_PRE + r"[tT]\s*=\s*(?P<val>" + _NUM + r")\s*[,;]?\s*" + _DFKEY
                     + r"\s*=\s*(?P<df1>\d+(?:\.\d+)?)(?!\s*,\s*\d)")),
    ("F", re.compile(_PRE + r"F\s*\(\s*(?P<df1>\d+(?:\.\d+)?)\s*[,/]\s*(?P<df2>\d+(?:\.\d+)?)\s*\)"
                     r"\s*=\s*(?P<val>" + _UNUM + r")")),
    ("F", re.compile(_PRE + r"F\s*=\s*(?P<val>" + _UNUM + r")\s*[,;]?\s*" + _DFKEY
                     + r"\s*=\s*\(?\s*(?P<df1>\d+(?:\.\d+)?)\s*[,/]\s*(?P<df2>\d+(?:\.\d+)?)\s*\)?")),
    ("chi2", re.compile(r"(?:χ\s*\^?\s*2|(?<![A-Za-z0-9_])[xX]\s*\^?\s*2|[Cc]hi[\s-]*square[d]?)"
                        r"\s*\(\s*(?P<df1>\d+)\s*(?:,\s*[Nn]\s*=\s*\d+\s*)?\)\s*=\s*(?P<val>" + _UNUM + r")")),
    ("chi2", re.compile(r"(?:χ\s*\^?\s*2|(?<![A-Za-z0-9_])[xX]\s*\^?\s*2)\s*=\s*(?P<val>" + _UNUM
                        + r")\s*[,;]?\s*" + _DFKEY + r"\s*=\s*(?P<df1>\d+)")),
    ("r", re.compile(_PRE + r"r\s*\(\s*(?P<df1>\d+)\s*\)\s*=\s*(?P<val>" + _NUM + r")")),
    ("r", re.compile(_PRE + r"r\s*=\s*(?P<val>" + _NUM + r")\s*[,;]?\s*[nN]\s*=\s*(?P<n>\d+)")),
    ("z", re.compile(_PRE + r"[zZ]\s*=\s*(?P<val>" + _NUM + r")")),
]

#: z 值只有在检验语境下才算（工科论文里 Z 常是阻抗/坐标）
_Z_CONTEXT = re.compile(r"检验|显著|test|Mann|Whitney|Wilcoxon|秩和|Sobel|中介|(?<![A-Za-z])[UW]\s*=")
#: 秩相关不按 Pearson 公式复算
_RANK_CORR = re.compile(r"Spearman|Kendall|斯皮尔曼|肯德尔|等级相关|秩相关|ρ|τ")

#: p 值：p / P / P值 / Sig. / 显著性（排除「显著性水平」）
_P_RE = re.compile(
    r"(?<![A-Za-z])(?:[pP](?:\s*值)?|[Ss]ig\.?(?:\s*值)?|显著性(?!水平))\s*(?:均|都|皆)?\s*"
    r"(?P<op>≤|≥|<=|>=|=|<|>|小于等于|大于等于|小于|大于|低于|高于|为|是)\s*"
    r"(?P<num>\d*\.?\d+(?:\s*[eE]\s*-?\s*\d+|\s*[×xX*]\s*10\s*\^?\s*-\s*\d+)?)"
    r"(?![\d.]*\d)(?!\s*(?:[kKMG]?Pa|kW|W\b|Hz|mm|cm|km|kg|mol|℃|°|%))"
)
_OP_MAP = {"≤": "<", "<=": "<", "<": "<", "小于等于": "<", "小于": "<", "低于": "<",
           "≥": ">", ">=": ">", ">": ">", "大于等于": ">", "大于": ">", "高于": ">",
           "=": "=", "为": "=", "是": "="}
_BOUNDARY = set("。！？!?\n")


# ---------------------------------------------------------------------------
# 3) 数值工具：报告位数 → 舍入区间；统计量 → p 值
# ---------------------------------------------------------------------------
def _decimals(s: str) -> int:
    s = s.strip()
    return len(s.split(".", 1)[1]) if "." in s else 0


def _parse_p(num: str) -> tuple[float, float] | None:
    """p 值文本 → (值, 舍入半宽)。支持 .05 / 0.001 / 1e-5 / 1.2×10^-3。"""
    s = re.sub(r"\s+", "", num)
    m = re.fullmatch(r"(\d*\.?\d+)(?:[eE](-?\d+)|[×xX*]10\^?(-\d+))?", s)
    if not m:
        return None
    mant, e1, e2 = m.group(1), m.group(2), m.group(3)
    exp = int(e1 or e2 or 0)
    try:
        val = float(mant) * (10.0 ** exp)
    except ValueError:
        return None
    tol = 0.5 * 10.0 ** (exp - _decimals(mant))
    return val, tol


def _p_from_stat(kind: str, v: float, df1: float | None, df2: float | None) -> float:
    """按统计量类型重算双侧 p（F / χ² 本身就是右尾）。"""
    if kind == "t":
        return float(2 * _st.t.sf(abs(v), df1))
    if kind == "z":
        return float(2 * _st.norm.sf(abs(v)))
    if kind == "F":
        return float(_st.f.sf(max(v, 0.0), df1, df2))
    if kind == "chi2":
        return float(_st.chi2.sf(max(v, 0.0), df1))
    if kind == "r":
        a = min(abs(v), 0.999999)
        tval = a * (df1 / (1 - a * a)) ** 0.5
        return float(2 * _st.t.sf(tval, df1))
    raise ValueError(kind)


def _consistent(op: str, rep: float, tol: float, lo: float, hi: float) -> bool:
    """报告的 p（含舍入区间）与重算区间 [lo, hi] 是否有交集。"""
    eps = 1e-12
    if op == "=":
        return hi >= rep - tol - eps and lo <= rep + tol + eps
    if op == "<":
        return lo < rep + eps
    return hi > rep - eps   # ">"


def _reported_sig(op: str, rep: float, alpha: float) -> bool | None:
    """论文报告的 p 在 α 下的结论；落在边界上（如 p = .05）视为不可判。"""
    if op == "=":
        if rep < alpha - 1e-12:
            return True
        if rep > alpha + 1e-12:
            return False
        return None
    if op == "<":
        return True if rep <= alpha + 1e-12 else None
    return False if rep >= alpha - 1e-12 else None


def _calc_sig(lo: float, hi: float, alpha: float) -> bool | None:
    if hi < alpha:
        return True
    if lo >= alpha:
        return False
    return None


# ---------------------------------------------------------------------------
# 4) 语境：α 声明、单侧检验、「显著 / 不显著」措辞
# ---------------------------------------------------------------------------
_ALPHA_DECL = [
    re.compile(r"显著性?水平\s*(?:设定?为|定为|取|为|=|α\s*=)?\s*(?:α\s*=\s*)?(0?\.\d+)"),
    re.compile(r"(?:以|按|取)\s*[pP]\s*(?:<|≤)\s*(0?\.\d+)\s*(?:为|作为|表示)"),
    re.compile(r"(?<![A-Za-z'’\s])α\s*=\s*(0?\.\d+)"),
]
_SENT_LEVEL = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:的)?\s*(?:显著性)?\s*水平|(0?\.\d+)\s*(?:的)?\s*(?:显著性)?\s*水平")
# 只认「单侧检验 / 单侧 p」这类明确说法——医学论文里的「单侧肢体」不算
_ONE_TAILED = re.compile(r"单[侧尾]\s*(?:检验|[pP]|假设|t\s*检验)|one[\s-]*(?:tailed|sided)|directional\s+(?:test|hypothes)",
                         re.IGNORECASE)
_CORRECTED = re.compile(r"校正|矫正|Bonferroni|Holm|FDR|Tukey|LSD|Scheff|S-N-K|事后|多重比较|Greenhouse|Huynh|Welch|"
                        r"Satterthwaite|稳健标准误|聚类标准误|robust", re.IGNORECASE)
_QUESTION = re.compile(r"是否|能否|有无|是不是|若|如果|假如")
_NEG_SIG = re.compile(
    r"(?:并不|均不|都不|尚不|尚未|未能|没有达到|未达到|未达|没有|未见|不|无|未|没)"
    r"\s*(?:统计学?上的?|统计学?)?\s*(?:的)?\s*(?:显著|统计学?意义)"
    r"|差异不大|未见(?:明显)?差异|无差异"
)
_POS_SIG = re.compile(r"显著(?!性\s*(?:水平|检验|[=<>≤≥]|为)|水平)|有统计学?意义|差异有统计学")
_MARGINAL = re.compile(r"边缘显著|临界显著|接近显著|趋于显著|marginal", re.IGNORECASE)


def detect_alpha(norm: str) -> float:
    """论文若明确声明唯一的显著性水平（如 α = 0.01）就用它，否则用 0.05。"""
    found: set[float] = set()
    for rx in _ALPHA_DECL:
        for m in rx.finditer(norm):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if v in _ALLOWED_ALPHA:
                found.add(v)
    return found.pop() if len(found) == 1 else DEFAULT_ALPHA


def _sentence_alpha(sent: str, default: float) -> float:
    """经管论文常见「在 10% 的水平上显著」：取句中声明的最宽水平。"""
    vals = []
    for m in _SENT_LEVEL.finditer(sent):
        try:
            v = float(m.group(1)) / 100 if m.group(1) else float(m.group(2))
        except ValueError:
            continue
        if v in _ALLOWED_ALPHA:
            vals.append(v)
    return max(vals) if vals else default


def _polarity(sent: str) -> str | None:
    """句子对显著性的表态：pos / neg / None（混合或未表态都返回 None）。"""
    if _MARGINAL.search(sent):
        return None
    neg = bool(_NEG_SIG.search(sent))
    pos = bool(_POS_SIG.search(_NEG_SIG.sub("", sent)))
    if neg and not pos:
        return "neg"
    if pos and not neg:
        return "pos"
    return None


# ---------------------------------------------------------------------------
# 5) 抽取与配对
# ---------------------------------------------------------------------------
def _find_ps(norm: str) -> list[dict]:
    out = []
    for m in _P_RE.finditer(norm):
        parsed = _parse_p(m.group("num"))
        if parsed is None or parsed[0] > 1:   # p > 1 多半是压强/功率等物理量，不当 p 值
            continue
        out.append({"start": m.start(), "end": m.end(), "op": _OP_MAP[m.group("op")],
                    "value": parsed[0], "tol": parsed[1], "num": m.group("num")})
    return out


def _find_stats(norm: str) -> list[tuple[str, re.Match[str]]]:
    cands = []
    for kind, rx in _STAT_PATTERNS:
        for m in rx.finditer(norm):
            if kind == "z" and not _Z_CONTEXT.search(norm[max(0, m.start() - 60):m.end() + 60]):
                continue
            if kind == "r" and _RANK_CORR.search(norm[max(0, m.start() - 40):m.start() + 1]):
                continue
            cands.append((kind, m))
    cands.sort(key=lambda km: (km[1].start(), -(km[1].end() - km[1].start())))
    kept: list[tuple[str, re.Match[str]]] = []
    last_end = -1
    for kind, m in cands:                 # 去重叠：同一段文字只认最早、最长的那条
        if m.start() >= last_end:
            kept.append((kind, m))
            last_end = m.end()
    return kept


def _pair(norm: str, stats_: list, ps: list[dict]) -> list[tuple[str, re.Match[str], dict]]:
    """每个统计量配它之后最近的 p 值：同一句、间隔 ≤ 40 字、中间没有别的统计量。"""
    starts = [p["start"] for p in ps]
    pairs = []
    for i, (kind, m) in enumerate(stats_):
        j = bisect.bisect_left(starts, m.end())
        if j >= len(ps):
            continue
        p = ps[j]
        gap = norm[m.end():p["start"]]
        if len(gap) > _PAIR_GAP or any(ch in _BOUNDARY for ch in gap):
            continue
        if i + 1 < len(stats_) and stats_[i + 1][1].start() < p["start"]:
            continue
        pairs.append((kind, m, p))
    return pairs


_SENT_END = set("。！？!?\n")
_CLAUSE_END = set("。！？!?\n；;")


def _span(norm: str, a: int, b: int, ends: set[str], limit: int = 300) -> tuple[int, int]:
    s, e = a, b
    while s > 0 and a - s < limit and norm[s - 1] not in ends:
        s -= 1
    while e < len(norm) and e - b < limit and norm[e] not in ends:
        e += 1
    return s, e


def _p_bounds(kind: str, v: float, dec: int, df1: float | None, df2: float | None) -> tuple[float, float, float]:
    """统计量按报告位数取舍入区间 → 重算 p 的 (下界, 点值, 上界)。p 随 |统计量| 单调递减。"""
    h = 0.5 * 10.0 ** (-dec)
    a = abs(v)
    lo_s, hi_s = max(a - h, 0.0), a + h
    if kind == "r":
        a, lo_s, hi_s = min(a, 0.999999), min(lo_s, 0.999999), min(hi_s, 0.999999)
    return (_p_from_stat(kind, hi_s, df1, df2), _p_from_stat(kind, a, df1, df2),
            _p_from_stat(kind, lo_s, df1, df2))


def _g(x: float) -> float:
    return float(f"{x:.6g}")


def _fmt_p(x: float) -> str:
    if x < 0.0001:
        return "< 0.0001"
    if x < 0.001:
        return f"≈ {x:.4f}"
    return f"≈ {x:.3f}"


_KIND_LABEL = {"t": "t 检验", "F": "F 检验", "chi2": "χ² 检验", "r": "相关系数", "z": "z 检验"}


def _excerpt(norm: str, a: int, b: int, s0: int, s1: int, width: int = 50) -> str:
    lo, hi = max(s0, a - width), min(s1, b + width)
    return ("…" if lo > s0 else "") + norm[lo:hi].strip() + ("…" if hi < s1 else "")


def _invalid_reason(kind: str, v: float, df1: float | None, df2: float | None) -> str:
    if kind in ("t", "F", "chi2", "r") and (df1 is None or df1 <= 0):
        return "自由度应为正数"
    if kind == "F" and (df2 is None or df2 <= 0):
        return "F 检验的分母自由度应为正数"
    if kind == "r" and abs(v) > 1:
        return "相关系数的绝对值不可能超过 1"
    return ""


def _evaluate(norm: str, kind: str, m: re.Match[str], p: dict,
              alpha_doc: float, one_tailed_doc: bool) -> dict | None:
    gd = m.groupdict()
    try:
        v = float(gd["val"])
        df1 = float(gd["df1"]) if gd.get("df1") else None
        df2 = float(gd["df2"]) if gd.get("df2") else None
        n = int(gd["n"]) if gd.get("n") else None
    except (TypeError, ValueError):
        return None
    if kind == "r" and df1 is None and n is not None:
        df1 = float(n - 2)                       # r 报告 n 时，df = n − 2
    s0, s1 = _span(norm, m.start(), p["end"], _SENT_END)
    sent = norm[s0:s1]
    alpha = _sentence_alpha(sent, alpha_doc)
    item: dict[str, Any] = {
        "kind": kind, "label": _KIND_LABEL[kind], "pos": m.start(),
        "text": norm[m.start():p["end"]].strip()[:80], "stat": v, "df1": df1, "df2": df2,
        "reported_op": p["op"], "reported_p": p["value"], "reported_text": p["num"],
        "computed_p": None, "computed_range": None, "alpha": alpha,
        "status": "consistent", "corrected": bool(_CORRECTED.search(sent)), "note": "",
        "context": _excerpt(norm, m.start(), p["end"], s0, s1),
    }
    reason = _invalid_reason(kind, v, df1, df2)
    if reason:
        item.update(status="invalid", note=reason)
        return item
    try:
        lo, pt, hi = _p_bounds(kind, v, _decimals(gd["val"]), df1, df2)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if not all(x == x for x in (lo, pt, hi)):   # NaN
        return None
    item.update(computed_p=_g(pt), computed_range=[_g(lo), _g(hi)])
    op, rep, tol = p["op"], p["value"], p["tol"]
    if _consistent(op, rep, tol, lo, hi):
        return item
    one_ok = kind in ("t", "z", "r") and _consistent(op, rep, tol, lo / 2, hi / 2)
    if one_ok and (one_tailed_doc or _ONE_TAILED.search(sent)):
        item.update(status="one_tailed", note="按单侧检验解读时一致（文中声明了单侧检验）")
        return item
    rs, cs = _reported_sig(op, rep, alpha), _calc_sig(lo, hi, alpha)
    decision = rs is not None and cs is not None and rs != cs
    item["status"] = "decision_error" if decision else "inconsistent"
    if not decision:
        item["gap_note"] = ("不一致（重算值贴近 α，显著性结论请一并核对）" if cs is None
                            else "不一致" if rs is None else "不一致（不影响显著性结论）")
    notes = []
    if one_ok:
        notes.append("若按单侧检验则一致；如确为单侧，请在文中写明")
    if item["corrected"]:
        notes.append("句中提到校正/事后比较，若报告的是校正后 p，出现差异属正常，请注明校正方法")
    item["note"] = "；".join(notes)
    return item


_P_NOT_PVALUE = re.compile(r"(?:概率|参数|系数|比例|比率|功率|压强|压力|取值|设|令|记)\s*$")


def _claim_conflicts(norm: str, ps: list[dict], alpha_doc: float) -> list[dict]:
    """文字说「显著 / 不显著」，但同一分句里报告的 p 值在 α 下给出相反结论。只看 p 值本身，不重算。"""
    groups: dict[tuple[int, int], list[dict]] = {}
    for p in ps:
        if _P_NOT_PVALUE.search(norm[max(0, p["start"] - 6):p["start"]]):
            continue
        groups.setdefault(_span(norm, p["start"], p["end"], _CLAUSE_END), []).append(p)
    out = []
    for (c0, c1), plist in groups.items():
        clause = norm[c0:c1]
        if _QUESTION.search(clause):
            continue                                  # 「若 P<0.05 则认为…」是方法说明，不是结论
        pol = _polarity(clause)
        if pol is None:
            continue
        s0, s1 = _span(norm, c0, c1, _SENT_END)
        alpha = _sentence_alpha(norm[s0:s1], alpha_doc)
        sigs = {_reported_sig(p["op"], p["value"], alpha) for p in plist}
        if len(sigs) != 1 or None in sigs:
            continue                                  # 一句里有显著有不显著，交给人看
        sig = sigs.pop()
        if (pol == "pos") == sig:
            continue
        if pol == "neg" and sig and _CORRECTED.search(clause):
            continue                                  # 校正后不显著：原始 p 小于 α 很正常
        p = plist[0]
        out.append({"pos": p["start"], "claim": "显著" if pol == "pos" else "不显著",
                    "reported": f"p {p['op']} {p['num']}", "alpha": alpha,
                    "context": _excerpt(norm, p["start"], p["end"], c0, c1, 60)})
    return out


# t = 2.35, P = 0.021 这种没给自由度的写法：无法复算，只做提示
_MISSING_DF = re.compile(_PRE + r"(?P<k>[tTF])\s*=\s*" + _NUM + r"\s*[,;]?\s*(?=[pP]\s*(?:值)?\s*[<=>≤≥])")


# ---------------------------------------------------------------------------
# 6) 组装结论（措辞统一为「请核对」，不下定性结论）
# ---------------------------------------------------------------------------
def _finding(priority: str, category: str, issue: str, action: str, context: str = "") -> dict:
    return {"priority": priority, "category": category, "issue": issue, "action": action, "context": context}


def _capped(rows: list, make, category: str) -> list[dict]:
    out = [make(r) for r in rows[:_MAX_LIST]]
    if len(rows) > _MAX_LIST:
        out.append(_finding("P2", category, f"另有 {len(rows) - _MAX_LIST} 处同类问题未逐条列出",
                            "修改后可重新体检，查看剩余条目"))
    return out


def _stat_issue(it: dict, tail: str) -> str:
    return (f"{it['label']}「{it['text']}」：按报告的统计量与自由度重算 p {_fmt_p(it['computed_p'])}，"
            f"与报告的 p {it['reported_op']} {it['reported_text']} {tail}")


_ACTION_RECHECK = "请回到原始输出（SPSS / Stata / R）核对统计量、自由度与 p 值是否抄录一致"


def _build_findings(items: list[dict], conflicts: list[dict], zero_ps: list[dict], missing: int) -> list[dict]:
    fs: list[dict] = []
    dec = [i for i in items if i["status"] == "decision_error"]
    inc = [i for i in items if i["status"] == "inconsistent"]
    inv = [i for i in items if i["status"] == "invalid"]
    fs += _capped(dec, lambda it: _finding(
        "P1" if it["corrected"] else "P0", "统计复算",
        _stat_issue(it, f"在 α = {it['alpha']:g} 下的显著性结论相反"),
        _ACTION_RECHECK + (f"；{it['note']}" if it["note"] else ""), it["context"]), "统计复算")
    fs += _capped(inv, lambda it: _finding(
        "P1", "统计复算", f"{it['label']}「{it['text']}」：{it['note']}",
        "请核对该处数值或自由度是否录入有误", it["context"]), "统计复算")
    fs += _capped(inc, lambda it: _finding(
        "P1", "统计复算", _stat_issue(it, it.get("gap_note", "不一致")),
        _ACTION_RECHECK + (f"；{it['note']}" if it["note"] else ""), it["context"]), "统计复算")
    fs += _capped(conflicts, lambda c: _finding(
        "P1", "结论表述", f"文字表述为「{c['claim']}」，但同一分句报告的 {c['reported']} 在 α = {c['alpha']:g} 下给出相反结论",
        "请核对结论措辞与 p 值是否对应；若采用了其他显著性水平，请在方法部分写明", c["context"]), "结论表述")
    if zero_ps:
        fs.append(_finding("P2", "报告规范", f"有 {len(zero_ps)} 处 p 值写作「p = 0.000」",
                           "p 值不可能恰为 0，这是软件显示位数所限，建议改写为「p < 0.001」",
                           f"首处：p = {zero_ps[0]['num']}"))
    if missing:
        fs.append(_finding("P2", "报告规范", f"有 {missing} 处 t / F 检验只给了统计量和 p 值，没有自由度，无法复算",
                           "如目标期刊采用 APA 格式，建议写成 t(df) = …、F(df1, df2) = …；国内医学期刊不强制，可按期刊要求取舍"))
    return fs


METHOD_NOTE = ("复算规则参照 statcheck（Nuijten et al., 2016）：统计量按报告位数取舍入区间，"
               "报告的 p 值按其位数取舍入区间，两区间有交集即视为一致；t / z / r 默认双侧，F / χ² 为右尾。")
DISCLAIMER = ("统计复算只检验论文里「写出来的数字」彼此是否自洽，不接触原始数据。"
              "不一致可能源于抄录笔误、单侧检验、校正后 p 值或软件舍入差异，请逐条核对；本结果不构成任何学术不端判定。")


def recheck_stats(text: str) -> dict:
    """从论文全文里抽取 APA / 国标写法的检验结果，重算 p 值并与报告值比对。纯函数、零 LLM、不落盘。"""
    norm = normalize(text)
    alpha = detect_alpha(norm)
    one_tailed = bool(_ONE_TAILED.search(norm))
    ps = _find_ps(norm)
    stats_ = _find_stats(norm)
    items = [it for it in (_evaluate(norm, k, m, p, alpha, one_tailed) for k, m, p in _pair(norm, stats_, ps)) if it]
    conflicts = _claim_conflicts(norm, ps, alpha)
    zero_ps = [p for p in ps if p["op"] == "=" and p["value"] == 0]
    missing = sum(1 for _ in _MISSING_DF.finditer(norm))
    count = {s: sum(1 for i in items if i["status"] == s)
             for s in ("consistent", "one_tailed", "inconsistent", "decision_error", "invalid")}
    summary = {"p_values_found": len(ps), "checked": len(items), **count,
               "claim_conflicts": len(conflicts), "p_zero": len(zero_ps), "missing_df": missing}
    if not items:
        summary_text = (f"识别到 {len(ps)} 个 p 值，但没有找到可复算的「统计量 + 自由度 + p 值」组合"
                        "（如 t(58) = 2.35, p = .022）。")
    else:
        bad = count["decision_error"] + count["inconsistent"] + count["invalid"]
        summary_text = (f"复算 {len(items)} 处检验结果：{count['consistent'] + count['one_tailed']} 处一致，"
                        f"{bad} 处需核对（其中 {count['decision_error']} 处可能改变显著性结论）。")
    if conflicts:
        summary_text += f"另有 {len(conflicts)} 处「显著 / 不显著」措辞与 p 值不符。"
    return {
        "ok": True, "alpha": alpha, "one_tailed_declared": one_tailed,
        "summary": summary, "summary_text": summary_text,
        "items": sorted(items, key=lambda i: i["pos"])[:_MAX_ITEMS_OUT],
        "conflicts": conflicts[:_MAX_ITEMS_OUT],
        "findings": _build_findings(items, conflicts, zero_ps, missing),
        "method_note": METHOD_NOTE, "disclaimer": DISCLAIMER,
    }


if __name__ == "__main__":  # 手动冒烟：python stat_recheck.py 论文.txt
    import json
    import sys
    from pathlib import Path
    src = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else sys.stdin.read()
    print(json.dumps(recheck_stats(src), ensure_ascii=False, indent=2))
