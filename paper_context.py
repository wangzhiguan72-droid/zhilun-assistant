"""Local, bounded context selection. Never calls a model or reads raw data rows.

Offsets refer to the extracted text supplied by the caller, not rendered pages.
Selection is a retrieval heuristic, not a claim that every section was understood.
"""
from __future__ import annotations

import hashlib
import re

CONTEXT_VERSION = "v1"
LIMITS = {"head": 2000, "evidence": 24000, "full": 120000}
_SECTION = re.compile(
    r"(?im)^[ \t#]*(?:(?:第[一二三四五六七八九十百\d]+[章节]|\d+(?:\.\d+)*[.、]?)\s*)?"
    r"(?:摘要|研究方法|材料与方法|方法|结果|讨论|结论|abstract|methods?|results?|discussion|conclusions?)"
    r"[^\n]{0,60}$")


def build_paper_context(text: str, *, mode: str = "head", directive: str = "",
                        comparisons: list | None = None) -> dict:
    """Return exact, non-overlapping excerpts within the user's selected budget.

    Legacy callers remain head-only. Full mode rejects oversized documents rather
    than silently presenting a partial input as a complete review.
    """
    if mode not in LIMITS:
        raise ValueError("AI 审阅范围无效，请重新选择。")
    text = text or ""
    total, limit = len(text), LIMITS[mode]
    if mode == "full" and total > limit:
        raise ValueError(f"全文共 {total} 字符，超过全文审阅上限 {limit}；请选择跨章节摘录或拆分文档。")
    intervals: list[tuple[int, int, str]] = []
    if total and (mode != "evidence" or total <= limit):
        intervals = [(0, min(total, limit), "全文" if total <= limit else "开头")]
    elif total:
        # Interleave anchors by purpose so repeated keywords cannot spend the
        # whole budget before methods/results/conclusions have a chance to enter.
        groups: list[list[tuple[int, str]]] = [[], [], [], []]
        for record in (comparisons or [])[:64]:
            needle = record.get("paper") if isinstance(record, dict) else None
            if isinstance(needle, str) and 4 <= len(needle) <= 1200:
                pos = text.find(needle)
                if pos >= 0:
                    groups[0].append((pos, "比对原句"))
        terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,12}", directive or "")[:12]
        for term in terms:
            # Search one first and one last occurrence, both bounded operations.
            for pos in dict.fromkeys((text.find(term), text.rfind(term))):
                if pos >= 0:
                    groups[1].append((pos, "用户关注"))
        # Keep late matches too: a TOC often repeats the first section titles.
        section_hits = []
        for match in _SECTION.finditer(text):
            section_hits.append(match.start())
            if len(section_hits) > 128:
                section_hits.pop(0)
        groups[2] = [(pos, "章节标题邻域") for pos in section_hits[-16:]]
        groups[3] = [(int((total-1)*fraction), "全文分布抽取")
                     for fraction in (0, 1, .25, .5, .75)]
        used = 0
        # 16 primary windows maximum; remainder can extend the distribution.
        for index in range(max(map(len, groups))):
            for group in groups:
                if index >= len(group) or used >= limit:
                    continue
                pos, reason = group[index]
                start, end = max(0, pos-500), min(total, pos+1300)
                # Extend the last window backwards to include enough tail context.
                if end == total:
                    start = max(0, total-1800)
                uncovered = [(start, end)]
                for old_start, old_end, _ in intervals:
                    next_parts = []
                    for left, right in uncovered:
                        if old_end <= left or old_start >= right:
                            next_parts.append((left, right))
                        else:
                            if left < old_start:
                                next_parts.append((left, old_start))
                            if old_end < right:
                                next_parts.append((old_end, right))
                    uncovered = next_parts
                needed = sum(right-left for left, right in uncovered)
                if needed <= limit-used:
                    intervals.extend((left, right, reason) for left, right in uncovered)
                    used += needed
                if len(intervals) >= 24:
                    break
            if used >= limit or len(intervals) >= 24:
                break
    # Merge touching windows to avoid double counting and preserve continuity.
    merged: list[list] = []
    for start, end, reason in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
            if reason not in merged[-1][2]:
                merged[-1][2] += "、" + reason
        else:
            merged.append([start, end, reason])
    passages = [{"id": f"P{index:03d}", "start": start, "end": end,
                 "text": text[start:end], "reason": reason}
                for index, (start, end, reason) in enumerate(merged, 1)]
    included = sum(p["end"]-p["start"] for p in passages)
    prompt_text = "\n\n".join(
        f"[{p['id']}] 提取文本字符 [{p['start']}, {p['end']})；{p['reason']}\n{p['text']}"
        for p in passages)
    return {"mode": mode, "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "total_chars": total, "included_chars": included, "complete": included == total,
            "passages": passages, "prompt_text": prompt_text, "version": CONTEXT_VERSION}
