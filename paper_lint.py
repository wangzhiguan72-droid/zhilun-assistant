"""论文提交前规范体检（v2.34）· Claude 线

只看论文文本本身，查「交稿前最容易被导师 / 编辑部打回」的硬伤：

1. 参考文献（GB/T 7714—2015）：类型标识 [J][M][D]…、出版年、期刊卷期页码、
   专著出版项、电子文献 URL 与引用日期、编号连续、疑似重复条目、作者超 3 人未加「等」
2. 正文引用 ↔ 文献表：顺序编码制查越界引用、未被引用条目、首次引用顺序；
   著者-出版年制查「（张三，2020）」在文献表里有没有对应条目
3. 图表编号：图题 / 表题重号、跳号，正文提到的图表是否存在，图表是否被正文引用
4. 中文排版（GB/T 15834）：中文间半角标点、半角括号括中文、重复标点、省略号、
   括号引号不成对、全角数字、中文间多余空格（PDF 抽取会改写标点，PDF 跳过本项）
5. 结构：摘要 / 关键词个数 / 英文摘要、章节与小节编号连续
6. 统计复算：并入 stat_recheck（statcheck 式 p 值复算）

纪律：纯函数、零 LLM、不落盘、不 import app；所有结论只说「请核对」，
不替学生改写正文（红线 4：不做代写），也不对作者身份或学术诚信下判断。
"""
from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from datetime import date
from typing import Any

from stat_recheck import normalize, recheck_stats

_MAX_LIST = 12
_CJK = "一-鿿"
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# ---------------------------------------------------------------------------
# 1) 分节：正文 / 参考文献 / 文末附属部分
# ---------------------------------------------------------------------------
_REF_HEAD = re.compile(
    r"^[ \t]*(?:[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*[ \t]*|第[一二三四五六七八九十\d]+[章节部分][ \t]*)?"
    r"(?:参[ \t]*考[ \t]*文[ \t]*献|主要参考文献|引用文献|References|REFERENCES|Bibliography)[ \t]*[:：]?[ \t]*$",
    re.M)
_TAIL_HEAD = re.compile(
    r"^[ \t]*(?:致[ \t]*谢|谢[ \t]*辞|后[ \t]*记|附[ \t]*录|作者简介|个人简历|攻读.{0,12}(?:期间|成果)"
    r"|Acknowledg(?:e)?ments?|Appendix)[^\n]{0,30}$", re.M)
_FIRST_ENTRY = re.compile(r"^[ \t]*(?:\[\s*1\s*\]|1[.、])|\[(?:J|M|D|C|EB/OL)\]", re.M)
_PAGE_NOISE = re.compile(r"^\s*(?:\d{1,3}|参\s*考\s*文\s*献|万方数据|References)\s*$")   # 页码、页眉、水印
_TABLE_ROW = " | "   # read_paper_text 把 docx 表格行拼成「单元格 | 单元格」附在文末
_TOC_LINE = re.compile(r"(?:[.…·]{3,}|\t)[ \t]*\d{1,3}[ \t]*$")


def _is_toc(line: str) -> bool:
    """目录行「标题……12」。只看行尾 40 字：整行 search 遇到超长点线会 O(n²) 回溯。"""
    return bool(_TOC_LINE.search(line.rstrip("\n")[-40:]))


def split_sections(text: str) -> dict[str, Any]:
    """返回各部分的下标区间（normalize 是逐字符映射，同一组下标可直接切原文）。"""
    heads = list(_REF_HEAD.finditer(text))
    n = len(text)
    if not heads:
        return {"body": (0, n), "refs": (n, n), "tail": (n, n), "ref_heading": False}
    # 学位论文 PDF 每页页眉都会重复「参考文献」：取第一个「后面紧跟条目」的标题；都不像就取最后一个
    h = next((x for x in heads if _FIRST_ENTRY.search(text[x.end():x.end() + 400])), heads[-1])
    tail_m = _TAIL_HEAD.search(text, h.end())
    ref_end = tail_m.start() if tail_m else n
    return {"body": (0, h.start()), "refs": (h.end(), ref_end), "tail": (ref_end, n), "ref_heading": True}


# ---------------------------------------------------------------------------
# 2) 参考文献条目解析
# ---------------------------------------------------------------------------
_TYPE_CODE = re.compile(r"\[\s*(J|M|D|C|N|R|S|P|G|Z|A|K|CM|DS|DB|CP|EB)(?:\s*/\s*(OL|MT|DK|CD))?\s*\]")
# \s 覆盖 PDF 常见的   / 　 / \xa0 等特殊空格
_ENTRY_NUM = [re.compile(r"^[^\S\n]*[\[［](\d{1,3})[\]］][^\S\n]*(?=\S)", re.M),
              re.compile(r"^[^\S\n]*(\d{1,3})[.．、][^\S\n]*(?=[^\d\s])", re.M)]
_YEAR = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")


def _join(a: str, b: str) -> str:
    """PDF 断行拼接：中文直接连，英文补空格，行尾连字符去掉。"""
    if not a:
        return b
    if a.endswith("-") and b[:1].islower():
        return a[:-1] + b
    return a + ("" if re.match(f"[{_CJK}，。：；、）》]", b[:1]) or re.search(f"[{_CJK}（《/]$", a) else " ") + b


def _looks_like_ref(line: str) -> bool:
    return bool(_TYPE_CODE.search(line) or (_YEAR.search(line) and len(line) >= 15 and re.search(r"[.．,，]", line)))


_INLINE_NUM = re.compile(r"[.。\]]\s*(\[\s*(\d{1,3})\s*\])[^\S\n]*(?=\S)")


def _with_inline(block: str, ms: list[re.Match[str]]) -> list[tuple[int, int, int]]:
    """PDF 双栏 / 断行会把下一条 [n] 并到上一条行尾：只接受「上一条编号 + 1」的行内编号。"""
    marks = {m.start(): (m.start(), m.end(), int(m.group(1))) for m in ms}
    for m in _INLINE_NUM.finditer(block):
        pos = m.start(1)
        if not any((pos + d) in marks for d in range(-3, 4)):     # O(1) 查邻近，别对 marks 全表扫
            marks[pos] = (pos, m.end(), int(m.group(2)))
    out: list[tuple[int, int, int]] = []
    line_starts = {m.start() for m in ms}
    for pos in sorted(marks):
        mk = marks[pos]
        if pos in line_starts or (out and mk[2] == out[-1][2] + 1):
            out.append(mk)
    return out


def _joined(seg: str) -> str:
    txt = ""
    for ln in seg.split("\n"):
        txt = _join(txt, ln.strip())
    return txt.strip()


def parse_references(refs: str, source: str, raw: str | None = None) -> dict[str, Any]:
    """refs 为 normalize 后文本（用于判断），raw 为同位置原文（用于展示）。"""
    raw = refs if raw is None else raw
    pairs = [(a.strip(), b.strip()) for a, b in zip(refs.split("\n"), raw.split("\n"))
             if a.strip() and _TABLE_ROW not in a and not _PAGE_NOISE.match(a)]
    if not pairs:
        return {"mode": "none", "entries": []}
    block, block_r = "\n".join(p[0] for p in pairs), "\n".join(p[1] for p in pairs)
    for idx, rx in enumerate(_ENTRY_NUM):
        ms = list(rx.finditer(block))
        if len(ms) >= 2 or (ms and len(pairs) == 1):
            if idx == 0:
                ms = _with_inline(block, ms)
            else:
                ms = [(m.start(), m.end(), int(m.group(1))) for m in ms]
            entries = []
            for i, m in enumerate(ms):
                end = ms[i + 1][0] if i + 1 < len(ms) else len(block)
                entries.append({"num": m[2], "text": _joined(block[m[1]:end]), "raw": _joined(block_r[m[1]:end])})
            return {"mode": "explicit", "entries": entries}
    # Word 自动编号在抽取文本里会丢失：docx / txt 一段就是一条
    if source != "pdf":
        cand = [p for p in pairs if _looks_like_ref(p[0])]
        if len(cand) >= max(2, int(0.6 * len(pairs))):
            return {"mode": "implicit",
                    "entries": [{"num": i + 1, "text": a, "raw": b} for i, (a, b) in enumerate(cand)]}
    return {"mode": "unparsed", "entries": []}


# ---------------------------------------------------------------------------
# 3) 单条文献的 GB/T 7714 规则
# ---------------------------------------------------------------------------
# 「2020, 36(4)」「2020, 36」「2020(04)」都算有卷期（国内期刊常只有期号）
#（知网导出常见「2022,No.781(09)」也算）
_JOURNAL_VOL = re.compile(r"(?:1[89]|20)\d{2}\s*(?:[,，]\s*(?:No\.?\s*)?\d+\s*(?:[(（]\s*[\dA-Za-z\-/ ]{1,20}\s*[)）])?"
                          r"|[,，]?\s*[(（]\s*[\dA-Za-z\-/ ]{1,20}\s*[)）])")
# 「: 12-18」或网络首发常见的「2025, 1-10.」
_PAGES = re.compile(r"[:：]\s*[A-Za-z]?\d+(?:\s*[-–—~]\s*[A-Za-z]?\d+)?(?:\s*[,，]\s*\d+(?:\s*[-–—~]\s*\d+)?)*"
                    r"|(?:1[89]|20)\d{2}\s*[,，]\s*\d+\s*[-–—~]\s*\d+\s*[.。]?\s*$"
                    r"|[)）]\s*[,，]\s*[A-Za-z]?\d{2,}")          # 外文期刊的文章号：130(3), 034504
_PUBLISHER = re.compile(r"[:：]\s*[^,，:：.。\[\]]{2,40}[,，]\s*(?:1[89]|20)\d{2}")
_URL = re.compile(r"https?://|www\.|doi\s*[:：]|\b10\.\d{4,9}/", re.I)
_CITE_DATE = re.compile(r"\[\s*(?:19|20)\d{2}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}\s*\]")
_CN_NAME = re.compile(f"[{_CJK}·]{{2,6}}")
_EN_NAME = re.compile(r"[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)*\s+[A-Z]{1,3}(?:\s*[A-Z])?")
_ETAL = re.compile(r"(?:等|et\s*al\.?|etc\.?)\s*$", re.I)


def _author_segment(t: str) -> str:
    m = re.search(r"[.。](?!\d)", t)
    return t[:m.start()] if m else ""


def _author_count(t: str) -> tuple[int, bool] | None:
    """(人名个数, 是否已加「等 / et al」)；识别不了（机构作者、APA 写法）返回 None。"""
    seg = _author_segment(t).strip()
    if not seg:
        return None
    etal = bool(_ETAL.search(seg))
    parts = [x.strip() for x in re.split(r"[,，;；]", _ETAL.sub("", seg)) if x.strip()]
    if not parts or not all(_CN_NAME.fullmatch(x) or _EN_NAME.fullmatch(x) for x in parts):
        return None
    return len(parts), etal


def _title_key(t: str) -> str:
    code = _TYPE_CODE.search(t)
    seg = _author_segment(t)
    title = t[len(seg):code.start()] if code else t[len(seg):]
    return re.sub(r"[\W_]+", "", title.lower())


def check_entry(text: str, current_year: int) -> tuple[str | None, list[str]]:
    """返回 (类型码, 问题清单)。"""
    probs: list[str] = []
    text = re.sub(r"(?<!\d)(19|20) (\d{2})(?!\d)", r"\1\2", text)   # PDF 抽取把「2016」拆成「20 16」
    code_m = _TYPE_CODE.search(text)
    code = code_m.group(1) + ("/" + code_m.group(2) if code_m.group(2) else "") if code_m else None
    base = code_m.group(1) if code_m else None
    years = [int(y) for y in _YEAR.findall(text)]
    if not years and base not in ("S", "P"):
        probs.append("缺出版年")
    elif years and min(years) > current_year:
        probs.append("出版年晚于今年")
    online = bool(code and code.endswith("/OL"))
    if base == "J" and not online:                  # 网络首发 [J/OL] 尚无卷期页码，GB/T 7714 允许省略
        miss = [n for n, rx in (("卷(期)", _JOURNAL_VOL), ("页码", _PAGES)) if not rx.search(text[code_m.end():])]
        if miss:
            probs.append("期刊缺" + "、".join(miss))
    elif base in ("M", "D") and years and not _PUBLISHER.search(text[code_m.end():]):
        probs.append("专著缺「出版地: 出版者, 年」" if base == "M" else "学位论文缺「保存地: 保存单位, 年」")
    if online or base == "EB":
        if not _URL.search(text):
            probs.append("网络资源缺网址 / DOI" if base == "EB" else "网络首发文献缺网址 / DOI")
        if not _CITE_DATE.search(text):
            probs.append("电子文献缺引用日期 [YYYY-MM-DD]")
    ac = _author_count(text)
    if ac and ac[0] > 3 and not ac[1]:
        probs.append("作者超过 3 人未写「等 / et al.」")
    return code, probs


def _finding(priority: str, category: str, issue: str, action: str, context: str = "") -> dict:
    return {"priority": priority, "category": category, "issue": issue, "action": action, "context": context}


def _nums_str(nums: list[int], limit: int = 20) -> str:
    s = "、".join(f"[{n}]" for n in nums[:limit])
    return s + (f" 等 {len(nums)} 条" if len(nums) > limit else "")


_PROB_RULE = {
    "缺出版年": ("P1", "补上出版年（标准 [S]、专利 [P] 可不写）"),
    "出版年晚于今年": ("P1", "出版年晚于今年，请核对是否录错（网络首发文献可能标注次年卷期）"),
    "专著缺「出版地: 出版者, 年」": ("P1", "专著须写出版项，如「北京: 高等教育出版社, 2019」"),
    "学位论文缺「保存地: 保存单位, 年」": ("P1", "学位论文须写保存地与学位授予单位，如「武汉: 武汉大学, 2020」"),
    "网络资源缺网址 / DOI": ("P1", "[EB/OL] 须给出获取和访问路径（URL 或 DOI）"),
    "网络首发文献缺网址 / DOI": ("P2", "GB/T 7714 要求 /OL 文献写获取路径；知网网络首发可补 DOI 或知网链接"),
    "电子文献缺引用日期 [YYYY-MM-DD]": ("P2", "电子资源须写引用日期，如「[2026-09-30]」"),
    "作者超过 3 人未写「等 / et al.」": ("P2", "GB/T 7714—2015：作者超过 3 人时只列前 3 人，其后加「等」或「et al.」"),
}
_JOURNAL_RULE = ("P1", "期刊须写「年, 卷(期): 起止页码」，如「2020, 36(4): 12-18」；只有期号的写「2020(4): 12-18」")


def _show(e: dict) -> str:
    t = e.get("raw") or e["text"]
    return f"[{e['num']}] " + (t[:80] + ("…" if len(t) > 80 else ""))


def _check_references(parsed: dict, current_year: int) -> tuple[list[dict], dict]:
    entries = parsed["entries"]
    fs: list[dict] = []
    info: dict[str, Any] = {"total": len(entries), "mode": parsed["mode"], "with_code": 0, "types": {}}
    if not entries:
        return fs, info
    by_prob: dict[str, list[dict]] = {}
    no_code = []
    for e in entries:
        code, probs = check_entry(e["text"], current_year)
        if code:
            info["with_code"] += 1
            info["types"][code] = info["types"].get(code, 0) + 1
        else:
            no_code.append(e)
        for pb in probs:
            by_prob.setdefault(pb, []).append(e)
    if no_code and info["with_code"] == 0:
        fs.append(_finding("P1", "参考文献", f"全部 {len(entries)} 条文献都没有文献类型标识（如 [J] [M] [D]）",
                           "国内学位论文与多数中文期刊采用 GB/T 7714—2015：期刊 [J]、专著 [M]、学位论文 [D]、"
                           "会议 [C]、电子资源 [EB/OL]；若目标期刊要求 APA 等格式可忽略本条", _show(no_code[0])))
    elif no_code:
        fs.append(_finding("P1", "参考文献", f"{_nums_str([e['num'] for e in no_code])} 缺文献类型标识",
                           "其余条目已使用 GB/T 7714 类型标识，请补齐（期刊 [J]、专著 [M]、学位论文 [D]、电子资源 [EB/OL] 等）",
                           _show(no_code[0])))
    for pb, es in by_prob.items():
        pri, act = _JOURNAL_RULE if pb.startswith("期刊缺") else _PROB_RULE.get(pb, ("P2", "请按 GB/T 7714—2015 核对"))
        fs.append(_finding(pri, "参考文献", f"{_nums_str([e['num'] for e in es])} {pb}", act, _show(es[0])))
    seen: dict[str, int] = {}
    dups = []
    for e in entries:
        k = _title_key(e["text"])
        if len(k) >= 8:
            if k in seen:
                dups.append((seen[k], e["num"]))
            else:
                seen[k] = e["num"]
    if dups:
        fs.append(_finding("P1", "参考文献", "疑似重复条目：" + "；".join(f"[{a}] 与 [{b}]" for a, b in dups[:8]),
                           "请核对是否同一文献。顺序编码制下同一文献只列一次，再次引用沿用首次编号；"
                           "引用页码不同时在正文角标外注页码，如 [7]⁹⁶"))
    if parsed["mode"] == "explicit":
        nums = [e["num"] for e in entries]
        gaps = sorted(set(range(1, max(nums) + 1)) - set(nums))
        reps = sorted(n for n, c in Counter(nums).items() if c > 1)
        if gaps or reps:
            desc = "；".join(x for x in ((f"缺 {_nums_str(gaps)}" if gaps else ""),
                                          (f"重复 {_nums_str(reps)}" if reps else "")) if x)
            fs.append(_finding("P1", "参考文献", f"文献表编号不连续：{desc}", "请按正文首次引用顺序重新连续编号"))
    return fs, info


# ---------------------------------------------------------------------------
# 4) 正文引用 ↔ 文献表
# ---------------------------------------------------------------------------
# 「Smith[9]」是常见引用写法，要认；「x[1]」「a[i]」这种短小写变量下标不认
_CITE_NUM = re.compile(r"(?<![0-9_])(?<!(?<![A-Za-z])[a-z])(?<!(?<![A-Za-z])[a-z]{2})"
                       r"\[\s*(\d{1,3}(?:\s*[-,~]\s*\d{1,3})*)\s*\]")
_RANGE_BEFORE = re.compile(r"(?:区间|范围|取值|∈|矩阵|数组|向量|索引)\s*$")
_RANGE_AFTER = re.compile(r"^\s*(?:区间|范围|之间)")
_AY_PAREN = re.compile(r"\(([^()\n]{2,120}?)\)")
_AY_CN = re.compile(f"^\\s*([{_CJK}·]{{2,12}}?)(?:等人?)?\\s*,\\s*((?:1[89]|20)\\d{{2}})[a-z]?\\s*$")
_AY_EN = re.compile(r"^\s*([A-Z][A-Za-z'\-]+)(?:\s+(?:and|&)\s+[A-Z][A-Za-z'\-]+|\s+et\s+al\.?)?\s*,?\s*"
                    r"((?:1[89]|20)\d{2})[a-z]?\s*$")


def _excerpt(raw: str, a: int, b: int, width: int = 30) -> str:
    lo, hi = max(0, a - width), min(len(raw), b + width)
    return ("…" if lo > 0 else "") + raw[lo:hi].replace("\n", " ").strip() + ("…" if hi < len(raw) else "")


def _cite_numbers(inner: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", inner.strip()):
        m = re.fullmatch(r"(\d+)\s*[-~]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if not (0 < a < b <= a + 50):
                return []
            out += range(a, b + 1)
        elif part.isdigit():
            out.append(int(part))
        else:
            return []
    return [] if not out or 0 in out else out


def find_numeric_cites(body: str) -> list[tuple[int, int, list[int]]]:
    """[1] [2,5] [3-6] [1]-[3] → [(起点, 终点, [编号…])]；排除 [0,1] 这类数学区间。"""
    cites: list[tuple[int, int, list[int]]] = []
    for m in _CITE_NUM.finditer(body):
        if _RANGE_BEFORE.search(body[max(0, m.start() - 8):m.start()]) or _RANGE_AFTER.search(body[m.end():m.end() + 4]):
            continue
        nums = _cite_numbers(m.group(1))
        if not nums:
            continue
        if cites and body[cites[-1][1]:m.start()].strip() in ("-", "~") and len(cites[-1][2]) == 1 and len(nums) == 1:
            a, b = cites[-1][2][0], nums[0]
            if a < b <= a + 50:                     # [1]-[3] 写法
                cites[-1] = (cites[-1][0], m.end(), list(range(a, b + 1)))
                continue
        cites.append((m.start(), m.end(), nums))
    return cites


def find_author_year(body: str) -> list[tuple[int, int, str, str]]:
    """（张三，2020；李四等，2021）/ (Smith et al., 2020) → [(起点, 终点, 作者, 年)]。"""
    out = []
    for m in _AY_PAREN.finditer(body):
        for part in m.group(1).split(";"):
            mm = _AY_CN.match(part) or _AY_EN.match(part)
            if mm:
                out.append((m.start(), m.end(), mm.group(1), mm.group(2)))
    return out


def _ay_found(name: str, year: str, entries: list[dict]) -> bool:
    name = re.sub(r"(?:等人?|et\s+al\.?)$", "", name).strip()
    cands = [name[i:] for i in range(max(1, len(name) - 1))] if re.search(f"[{_CJK}]", name) else [name]
    cands = [c.lower() for c in cands if len(c) >= 2]
    return any(year in e["text"] and any(c in e["text"].lower() for c in cands) for e in entries)


def _check_citations(body: str, raw_body: str, parsed: dict, ref_heading: bool) -> tuple[list[dict], dict]:
    nums_c = find_numeric_cites(body)
    ay_c = find_author_year(body)
    style = ("numeric" if len(nums_c) >= 3 and len(nums_c) >= len(ay_c) else "author_year" if len(ay_c) >= 3
             else "numeric" if nums_c else "author_year" if ay_c else "none")
    info = {"style": style, "numeric": len(nums_c), "author_year": len(ay_c)}
    fs: list[dict] = []
    entries, mode = parsed["entries"], parsed["mode"]
    if style == "none":
        if entries:
            fs.append(_finding("P2", "引用核对", f"文献表有 {len(entries)} 条，但正文里没识别到 [1] 或（作者，年）式引用",
                               "若用的是上标引用，抽取文本时上标会丢失，可忽略本条；否则请在正文相应位置标注引用"))
        return fs, info
    if not entries:
        if mode == "unparsed":
            fs.append(_finding("P2", "引用核对", "识别到「参考文献」标题，但条目格式无法解析，已跳过引用核对",
                               "请确认每条文献独占一段，并以 [1] 或 1. 开头"))
        elif not ref_heading and (len(nums_c) >= 3 or len(ay_c) >= 3):
            fs.append(_finding("P1", "引用核对", f"正文有 {len(nums_c) + len(ay_c)} 处引用，但没找到「参考文献」一节",
                               "请在文末添加「参考文献」标题及文献表；若标题写法特殊（如「文献」），可改为标准写法后再体检"))
        return fs, info
    if style == "numeric":
        have = {e["num"] for e in entries}
        cited: dict[int, int] = {}
        for a, b, ns in nums_c:
            for n in ns:
                cited.setdefault(n, a)
        over = sorted(n for n in cited if n not in have)
        # 双栏 PDF 会把后半截文献表排进正文：这些编号以「行首 [n] 作者.」形式出现在正文里，不算越界
        leaked = {int(m.group(1)) for m in _ENTRY_NUM[0].finditer(body)
                  if _TYPE_CODE.search(body[m.end():m.end() + 300]) or _YEAR.search(body[m.end():m.end() + 200])}
        over = [n for n in over if n not in leaked]
        if over:
            implicit = mode == "implicit"
            fs.append(_finding("P1" if implicit else "P0", "引用核对",
                               f"正文引用了 {_nums_str(over)}，文献表里没有对应编号"
                               + (f"（按文献表段落顺序计 {len(entries)} 条）" if implicit else ""),
                               "请补全文献表或修正正文引用编号", _excerpt(raw_body, cited[over[0]], cited[over[0]] + 6)))
        unused = sorted(have - set(cited))
        if unused and len(cited) >= 3:
            fs.append(_finding("P1", "引用核对", f"文献表中 {_nums_str(unused)} 在正文里没有被引用",
                               "顺序编码制要求文献表条目均在正文中引用：请补引用或删去该条目"))
        order = sorted(cited, key=lambda n: cited[n])
        bad, top = [], 0
        for n in order:
            if n < top:
                bad.append((n, top))
            top = max(top, n)
        if bad and mode == "explicit":
            n, t = bad[0]
            fs.append(_finding("P2", "引用核对", f"有 {len(bad)} 处编号未按首次引用顺序排列（如 [{n}] 在 [{t}] 之后才首次出现）",
                               "顺序编码制要求按正文首次引用的先后连续编号；若目标期刊不要求，可忽略",
                               _excerpt(raw_body, cited[n], cited[n] + 6)))
    else:
        miss = [(a, b, nm, y) for a, b, nm, y in ay_c if not _ay_found(nm, y, entries)]
        if miss:
            eg = "；".join(f"（{nm}，{y}）" for _, _, nm, y in miss[:6])
            fs.append(_finding("P1", "引用核对", f"有 {len(miss)} 处著者-出版年引用在文献表里找不到对应条目，如 {eg}",
                               "请核对作者姓名与年份是否与文献表一致，或补充缺失条目", _excerpt(raw_body, miss[0][0], miss[0][1])))
    return fs, info


# ---------------------------------------------------------------------------
# 5) 图表编号
# ---------------------------------------------------------------------------
_CAPTION = re.compile(r"^[ \t]*(图|表|Figure|Fig\.|Table)[ \t]*(\d{1,2}(?:[.\-]\d{1,2}){0,2})(?![\d.\-])[ \t:]*([^\n]{0,120})$",
                      re.M)
# 同一行并排两张图：「图 1-1 桥式起重机      图 1-2 门式起重机」
_CAPTION_INLINE = re.compile(r"(?:[ \t]{2,}|\t)(图|表|Figure|Fig\.|Table)[ \t]*(\d{1,2}(?:[.\-]\d{1,2}){0,2})(?![\d.\-])")
# 「图 1 所示」「表 2 中可以看出」是正文提及不是图表题；只拦正文腔的开头，别误伤「描述性统计」「中国…」这类标题
_NOT_CAPTION = re.compile(r"^(?:所示|中[,，的可所]|中$|可知|可以|可见|表明|说明了?[,，]|显示|反映了|给出了|列出了|为[,，]|是[,，]"
                          r"|和[图表]|与[图表]|及[图表]|至|~|-|、|,|的[结数]|展示了|描述了|对比|进一步|则|结果(?:可知|表明|显示))")
_MENTION = re.compile(r"(?:(?<![一-鿿A-Za-z])|(?<=[如见由据按参从在将对把至到即详依和与及、上下的]))(图|表)[ \t]*"
                      r"(\d{1,2}(?:[.\-]\d{1,2}){0,2})(?![\d]|[.\-]\d)"
                      r"(?:[ \t]*(?:~|至|到|—)[ \t]*(?:图|表)?[ \t]*(\d{1,2}(?:[.\-]\d{1,2}){0,2})(?![\d]|[.\-]\d))?")
# 宽松版（不限前一个字）：只用来判断「某图表有没有被提及」，误匹配的代价仅是少报一条 P2；
# 「提到了却没有图题」这种 P1 仍用严格版，避免「代表 3」「发表 2」之类被当成图表号
_MENTION_LOOSE = re.compile("(图|表)" + _MENTION.pattern.split("(图|表)", 1)[1])
_KIND = {"图": "图", "Figure": "图", "Fig.": "图", "表": "表", "Table": "表"}


def _numkey(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.split(r"[.\-]", s))


def _fmt_num(k: tuple[int, ...]) -> str:
    return "-".join(map(str, k))


def find_captions(body: str) -> dict[str, list[dict]]:
    """图题 / 表题。中英双语图题（图 1-1 + Fig. 1-1）只数中文那套，没有中文才用英文。"""
    found: dict[tuple[str, str], list[dict]] = {}
    for m in _CAPTION.finditer(body):
        line = m.group(0)
        if _TABLE_ROW in line or _is_toc(line):
            continue
        heads = [(m.start(1), m.group(1), m.group(2), m.end(2))]
        heads += [(m.start() + x.start(1), x.group(1), x.group(2), m.start() + x.end(2))
                  for x in _CAPTION_INLINE.finditer(line)]
        for i, (pos, word, num, nend) in enumerate(heads):
            stop = heads[i + 1][0] if i + 1 < len(heads) else m.end()
            title = body[nend:stop].strip(" \t:")
            title = re.split(r"[ \t]{2,}", title, maxsplit=1)[0]   # PDF 把表头并到表题同一行
            if _NOT_CAPTION.match(title) or len(title) > 60 or (title.endswith(("。", ".")) and len(title) > 25) \
                    or re.search(f"[{_CJK}][,，][^,，]{{8,}}", title) or title.endswith((",", "，")):
                continue                               # 带逗号长句、以逗号收尾的，是 PDF 断行的正文
            if re.match(r"^[(（]?续", title):          # 续表：编号存在，但不参与重号判断
                found.setdefault((_KIND[word], "cont"), []).append({"key": _numkey(num), "pos": pos, "end": stop})
                continue
            lang = "cn" if word in ("图", "表") else "en"
            found.setdefault((_KIND[word], lang), []).append(
                {"key": _numkey(num), "title": re.sub(r"\s*\d{1,3}$", "", title), "pos": pos, "end": stop})
    out: dict[str, list[dict]] = {k: found.get((k, "cn")) or found.get((k, "en")) or [] for k in ("图", "表")}
    for k in ("图", "表"):
        out[k + "续"] = found.get((k, "cont"), [])
    return out


def _gaps(keys: list[tuple[int, ...]]) -> list[tuple[int, ...]]:
    flat = sorted({k[0] for k in keys if len(k) == 1})
    out = [(n,) for n in range(1, flat[-1]) if n not in flat] if len(flat) >= 2 else []
    chap: dict[int, set[int]] = {}
    for k in keys:
        if len(k) >= 2:                                # 「表 4-4-1」拆分子表也算「表 4-4」在
            chap.setdefault(k[0], set()).add(k[1])
    for c, ns in chap.items():
        if len(ns) >= 2:
            out += [(c, n) for n in range(1, max(ns)) if n not in ns]
    return out


def _mention_keys(m: re.Match[str], chapter_style: bool) -> list[tuple[int, ...]]:
    a = _numkey(m.group(2))
    if len(a) == 2 and not chapter_style and "-" in m.group(2):
        return [(n,) for n in range(a[0], a[1] + 1)] if 0 < a[0] < a[1] <= a[0] + 20 else []   # 平铺编号下「图1-3」是范围
    if not m.group(3):
        return [a]
    b = _numkey(m.group(3))
    if len(a) == 1 and len(b) == 1 and a[0] < b[0] <= a[0] + 20:
        return [(n,) for n in range(a[0], b[0] + 1)]
    if len(a) == 2 and len(b) == 2 and a[0] == b[0] and a[1] < b[1] <= a[1] + 20:
        return [(a[0], n) for n in range(a[1], b[1] + 1)]
    return [a, b]


def _check_figures(body: str, raw_body: str) -> tuple[list[dict], dict]:
    caps = find_captions(body)
    cap_spans = sorted((c["pos"], c["end"]) for k in caps for c in caps[k])   # 各图表题互不重叠
    cap_starts = [a for a, _ in cap_spans]
    line_starts = [0] + [m.end() for m in re.finditer(r"\n", body)]
    row_lines = {i for i, ln in enumerate(body.split("\n")) if _TABLE_ROW in ln}

    def _skip(pos: int) -> bool:
        """落在图表题里、或落在 docx 表格行（read_paper_text 附在文末）里的「图 N」不算正文提及。
        用二分查找而非逐个比对：超长单行 / 上千图表题时仍是 O(log n)。"""
        i = bisect_right(cap_starts, pos) - 1
        if i >= 0 and pos < cap_spans[i][1]:
            return True
        return bisect_right(line_starts, pos) - 1 in row_lines

    fs: list[dict] = []
    info = {"figures": len(caps["图"]), "tables": len(caps["表"])}
    for kind in ("图", "表"):
        items = caps[kind]
        keys = [c["key"] for c in items]
        chapter_style = sum(len(k) >= 2 for k in keys) > len(keys) / 2
        by_key: dict[tuple[int, ...], set[str]] = {}
        for c in items:
            by_key.setdefault(c["key"], set()).add(re.sub(r"\s+", "", c["title"]))
        dup = [k for k, ts in by_key.items() if len(ts) > 1]
        if dup:
            fs.append(_finding("P1", "图表编号", f"{kind}题重号：" + "、".join(f"{kind} {_fmt_num(k)}" for k in dup[:8])
                               + " 对应了不同标题", f"请重新连续编号，并同步修改正文中的{kind}号引用"))
        gaps = _gaps(keys)
        if gaps:
            fs.append(_finding("P2", "图表编号", f"{kind}号不连续，缺 " + "、".join(f"{kind} {_fmt_num(k)}" for k in gaps[:10])
                               + (f" 等 {len(gaps)} 处" if len(gaps) > 10 else ""),
                               f"请核对是否漏了{kind}或编号跳号"))
        have = set(keys)

        def _collect(rx: re.Pattern[str]) -> dict[tuple[int, ...], int]:
            got: dict[tuple[int, ...], int] = {}
            for m in rx.finditer(body):
                if m.group(1) != kind or _skip(m.start()):
                    continue
                for k in _mention_keys(m, chapter_style):
                    got.setdefault(k, m.start())
            return got

        mentioned = _collect(_MENTION)
        mentioned_loose = _collect(_MENTION_LOOSE)
        if not items:
            if len(mentioned) >= 2:
                fs.append(_finding("P2", "图表编号", f"正文提到了 {len(mentioned)} 个{kind}号，但没识别到{kind}题",
                                   f"若{kind}题放在文本框 / 图片里，抽取时会丢失，可忽略；否则请补{kind}题"))
            continue
        have |= {c["key"] for c in caps[kind + "续"]}
        prefixes = {k[:i] for k in have for i in range(1, len(k))}         # 有「表 4-4-1」就算「表 4-4」存在
        lens = {len(k) for k in keys}
        missing = sorted(k for k in mentioned if len(k) in lens and k not in have and k not in prefixes)
        if missing:
            k0 = missing[0]
            fs.append(_finding("P1", "图表编号", "正文提到了 " + "、".join(f"{kind} {_fmt_num(k)}" for k in missing[:8])
                               + f"，但没找到对应的{kind}题", f"请核对{kind}号是否写错，或补上缺失的{kind}",
                               _excerpt(raw_body, mentioned[k0], mentioned[k0] + 6)))
        unref = sorted(k for k in set(keys) if k not in mentioned_loose
                       and (len(k) < 3 or k[:-1] not in mentioned_loose))
        if unref and mentioned_loose:
            more = f" 等 {len(unref)} 个{kind}" if len(unref) > 10 else ""
            fs.append(_finding("P2", "图表编号", "、".join(f"{kind} {_fmt_num(k)}" for k in unref[:10])
                               + more + " 在正文里没有被提及", f"学位论文一般要求「先见文、后见{kind}」，请在正文中引用并说明"))
    return fs, info


# ---------------------------------------------------------------------------
# 6) 中文排版（GB/T 15834—2011），只查原文，不查参考文献
# ---------------------------------------------------------------------------
_TYPO_RULES = [
    ("中文之间用了半角标点", re.compile(f"[{_CJK}][,;:?!][{_CJK}]"), "中文语境请用全角标点：，；：？！"),
    ("半角括号括中文", re.compile(f"\\([{_CJK}][^()\\n]{{0,40}}\\)"), "括号内是中文时请用全角括号（）"),
    ("重复标点", re.compile(r"([，。、；：])\1+"), "删去多余的重复标点"),
    ("省略号写法不规范", re.compile(f"(?<=[{_CJK}])(?:\\.{{3,}}|。。。|…(?!…))"), "中文省略号应为两个「…」即「……」"),
    ("全角数字", re.compile(r"[０-９]+"), "GB/T 15835：数字一般用半角阿拉伯数字"),
    ("中文之间有多余空格", re.compile(f"[{_CJK}][ \\u3000]+[{_CJK}]"), "删去中文字符之间的空格（排版对齐请用段落缩进 / 表格）"),
]
_PAIRS = [("（", "）"), ("“", "”"), ("‘", "’"), ("《", "》"), ("【", "】")]
_SKIP_LINE = re.compile(r"^\s*(?:关键词|关键字|Key\s*words?|摘\s*要|目\s*录|第.{1,3}章)", re.I)


def _check_typography(raw_body: str) -> list[dict]:
    hits: dict[str, list[str]] = {}
    for line in raw_body.split("\n"):
        s = line.strip()
        if len(s) < 20 or _TABLE_ROW in s or _is_toc(s) or _SKIP_LINE.match(s):
            continue
        for name, rx, _ in _TYPO_RULES:
            for m in rx.finditer(s):
                hits.setdefault(name, []).append(_excerpt(s, m.start(), m.end(), 12))
        for a, b in _PAIRS:
            if a == "“" and s.startswith("“") and s.count("“") == s.count("”") + 1:
                continue                               # 跨段引文：每段开头有前引号、末段才收
            if s.count(a) != s.count(b):
                hits.setdefault("括号 / 引号不成对", []).append(s[:40] + ("…" if len(s) > 40 else ""))
                break
    fs = []
    rule_action = {n: act for n, _, act in _TYPO_RULES}
    rule_action["括号 / 引号不成对"] = "请检查该段括号、引号、书名号是否成对"
    for name, egs in hits.items():
        if name == "中文之间有多余空格" and len(egs) < 3:
            continue                                   # 零星一两处多是排版对齐，不打扰
        fs.append(_finding("P2", "中文排版", f"{name}：{len(egs)} 处", rule_action[name], "例：" + egs[0]))
    return fs


# ---------------------------------------------------------------------------
# 7) 结构：摘要 / 关键词 / 英文摘要 / 章编号
# ---------------------------------------------------------------------------
_ABS_CN = re.compile(r"^[ \t]*[\[【]?\s*摘[ \t]*要\s*[\]】]?", re.M)
_ABS_EN = re.compile(r"^[ \t]*(?:\[|【)?\s*(?:Abstract|ABSTRACT)\b", re.M)
_KW_CN = re.compile(r"^[ \t]*[\[【]?\s*关键[词字]\s*[\]】]?\s*[:]?\s*([^\n]+)", re.M)
_CHAPTER = re.compile(r"^[ \t]*第[ \t]*([一二三四五六七八九十]{1,3}|\d{1,2})[ \t]*章(?=[ \t]*\S)[^\n]{0,40}$", re.M)
_CN_NUM = {c: i for i, c in enumerate("零一二三四五六七八九十")}


def _cn_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN_NUM[s[1]]
    if s.endswith("十"):
        return _CN_NUM[s[0]] * 10
    return _CN_NUM[s[0]] * 10 + _CN_NUM[s[-1]] if len(s) == 3 else _CN_NUM[s]


def _check_structure(body: str, has_refs: bool, pdf: bool = False) -> tuple[list[dict], dict]:
    fs: list[dict] = []
    abs_cn, abs_en = bool(_ABS_CN.search(body)), bool(_ABS_EN.search(body))
    kw = _KW_CN.search(body)
    parts = [x for x in re.split(r"[;、,，；]|\s{2,}", kw.group(1)) if x.strip()] if kw else []
    if len(parts) == 1 and re.search(f"[{_CJK}]\\s[{_CJK}]", parts[0]):
        parts = parts[0].split()                    # 「存货管理 供应链管理 光明乳业」单空格分隔
    n_kw = len(parts)
    info = {"abstract_cn": abs_cn, "abstract_en": abs_en, "keywords": n_kw, "chapters": 0}
    if kw and not 3 <= n_kw <= 8 and not (pdf and n_kw < 3):   # PDF 断行会把关键词切到下一行
        fs.append(_finding("P2", "论文结构", f"关键词识别到 {n_kw} 个", "GB/T 7713 建议关键词 3～8 个，词间用分号隔开",
                           kw.group(0).strip()[:60]))
    if abs_cn and not abs_en and len(body) > 15000:
        fs.append(_finding("P2", "论文结构", "有中文摘要，但没识别到英文摘要（Abstract）",
                           "学位论文和多数中文核心期刊要求中英文摘要，请按学校 / 期刊要求补充"))
    if not abs_cn and not abs_en and len(body) > 15000 and has_refs:
        fs.append(_finding("P1", "论文结构", "没识别到「摘要」", "学位论文须有中文摘要；若标题写法特殊，可改为「摘要」后再体检"))
    # 目录、页眉都会重复章标题，只看「出现过哪些章号」是否从 1 连续到最大
    chaps = sorted({_cn_int(m.group(1)) for m in _CHAPTER.finditer(body) if not _is_toc(m.group(0))})
    info["chapters"] = len(chaps)
    missing = [c for c in range(1, chaps[-1] + 1) if c not in chaps] if len(chaps) >= 2 else []
    if missing:
        fs.append(_finding("P1", "论文结构", "章编号不连续，缺 " + "、".join(f"第{c}章" for c in missing),
                           "请核对章标题编号是否跳号"))
    return fs, info


# ---------------------------------------------------------------------------
# 8) 汇总
# ---------------------------------------------------------------------------
DISCLAIMER = ("规范体检只看论文文本本身，按 GB/T 7714—2015、GB/T 15834—2011 与常见学位论文要求做规则核对，"
              "结果是「请核对」清单而非判定；学校 / 期刊另有格式要求时以其为准。本工具不改写正文。")
CATEGORY_ORDER = ["统计复算", "结论表述", "引用核对", "参考文献", "图表编号", "论文结构", "报告规范", "中文排版"]


def _md_escape(s: str) -> str:
    """export_docx 只认 **粗体** / 行首 # 与 |：拆掉 ** 并压成单行即可，别的原样保留。"""
    return re.sub(r"\s*\n\s*", " ", (s or "").replace("**", "*"))


def _markdown(res: dict) -> str:
    c = res["counts"]
    sec = res["sections"]
    lines = ["# 论文提交前规范体检", "", f"**结论**：{res['headline']}", "",
             f"- 参考文献：{sec['references']} 条（{sec['ref_mode_label']}），正文引用 {sec['citations']} 处（{sec['cite_style_label']}）",
             f"- 图题 {sec['figures']} 个，表题 {sec['tables']} 个；统计复算 {res['stats']['summary']['checked']} 处",
             f"- 问题：必改 {c['P0']} 条 · 建议改 {c['P1']} 条 · 可选 {c['P2']} 条", ""]
    for pri, label in (("P0", "必改（提交前务必处理）"), ("P1", "建议改"), ("P2", "可选 / 细节")):
        group = [f for f in res["findings"] if f["priority"] == pri]
        if not group:
            continue
        lines += [f"## {label}", ""]
        for f in group:
            lines.append(f"- **[{f['category']}]** {_md_escape(f['issue'])}")
            lines.append(f"  - 建议：{_md_escape(f['action'])}")
            if f.get("context"):
                lines.append(f"  - 位置：{_md_escape(f['context'])}")
        lines.append("")
    if res["notes"]:
        lines += ["## 说明", ""] + [f"- {_md_escape(n)}" for n in res["notes"]] + [""]
    lines += ["---", "", res["disclaimer"], "", res["stats"]["disclaimer"]]
    return "\n".join(lines)


_MODE_LABEL = {"explicit": "按编号解析", "implicit": "按段落解析（Word 自动编号）", "unparsed": "格式无法解析", "none": "未找到"}
_STYLE_LABEL = {"numeric": "顺序编码制", "author_year": "著者-出版年制", "none": "未识别"}


def lint_paper(text: str, source: str = "docx", current_year: int | None = None) -> dict:
    """论文规范体检主入口。source ∈ docx / pdf / txt / md（PDF 跳过排版项）。纯函数、零 LLM、不落盘。"""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    norm = normalize(raw)
    source = (source or "docx").lower().lstrip(".")
    year = current_year or date.today().year
    sec = split_sections(norm)
    b0, b1 = sec["body"]
    r0, r1 = sec["refs"]
    body, raw_body = norm[b0:b1], raw[b0:b1]
    parsed = parse_references(norm[r0:r1], source, raw[r0:r1])
    f_ref, i_ref = _check_references(parsed, year)
    f_cite, i_cite = _check_citations(body, raw_body, parsed, sec["ref_heading"])
    f_fig, i_fig = _check_figures(body, raw_body)
    f_str, i_str = _check_structure(body, bool(parsed["entries"]), pdf=source == "pdf")
    notes: list[str] = []
    if source == "pdf":
        f_typo = []
        # PDF 版面（页眉页脚、双栏、图片里的图题）会干扰解析：引用 / 图表类结论各降一级
        for f in f_cite + f_fig:
            f["priority"] = {"P0": "P1", "P1": "P2"}.get(f["priority"], f["priority"])
        notes.append("PDF 抽取会改写全角 / 半角标点和空格，中文排版项已跳过；页眉页脚与双栏排版可能干扰引用和图表核对，"
                     "相关结论已降一级。请上传 .docx 获得完整体检")
    else:
        f_typo = _check_typography(raw_body)
    stats = recheck_stats(raw[b0:b1] + "\n" + raw[r1:])
    findings = f_ref + f_cite + f_fig + f_str + f_typo + stats["findings"]
    findings.sort(key=lambda f: (_PRIORITY_ORDER.get(f["priority"], 9),
                                 CATEGORY_ORDER.index(f["category"]) if f["category"] in CATEGORY_ORDER else 99))
    counts = {p: sum(1 for f in findings if f["priority"] == p) for p in ("P0", "P1", "P2")}
    counts["total"] = len(findings)
    by_cat: dict[str, int] = {}
    for f in findings:
        by_cat[f["category"]] = by_cat.get(f["category"], 0) + 1
    if not counts["total"]:
        headline = "未发现规则可识别的问题。规则只能覆盖常见硬伤，提交前仍建议通读一遍。"
    else:
        headline = (f"发现 {counts['total']} 条待核对：必改 {counts['P0']} 条、建议改 {counts['P1']} 条、可选 {counts['P2']} 条。"
                    + ("请先处理「必改」项。" if counts["P0"] else ""))
    res: dict[str, Any] = {
        "ok": True, "source": source, "chars": len(raw), "counts": counts, "by_category": by_cat,
        "sections": {"references": i_ref["total"], "ref_mode": i_ref["mode"], "ref_mode_label": _MODE_LABEL[i_ref["mode"]],
                     "ref_types": i_ref["types"], "citations": i_cite["numeric"] + i_cite["author_year"],
                     "cite_style": i_cite["style"], "cite_style_label": _STYLE_LABEL[i_cite["style"]],
                     "figures": i_fig["figures"], "tables": i_fig["tables"], **i_str},
        "findings": findings, "headline": headline, "notes": notes,
        "stats": {k: stats[k] for k in ("alpha", "one_tailed_declared", "summary", "summary_text", "items",
                                        "method_note", "disclaimer")},
        "disclaimer": DISCLAIMER,
    }
    res["markdown"] = _markdown(res)
    return res


if __name__ == "__main__":  # 手动冒烟：python paper_lint.py 论文.txt
    import sys
    from pathlib import Path
    p = Path(sys.argv[1])
    print(lint_paper(p.read_text(encoding="utf-8"), source=p.suffix)["markdown"])
