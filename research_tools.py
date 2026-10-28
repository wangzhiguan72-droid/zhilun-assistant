"""Bounded, in-memory tools for evidence-led paper review.

Only the caller-supplied extracted paper is accessible. Requests cannot name a
file, submit observations, execute code, or supply replacement test statistics.
Character offsets use the original input, without whitespace normalization.
"""
from __future__ import annotations

from stat_recheck import recheck_stats

TOOLS_VERSION = "paper-tools-v2"  # Includes negation-aware tail declarations.


class ResearchTools:
    """One review's read-only scope and cumulative outgoing excerpt budget."""

    def __init__(self, text: str, mode: str = "head"):
        limits = {"head": 2000, "evidence": 24000, "full": 120000}
        if not isinstance(mode, str) or mode not in limits:
            raise ValueError("论文工具范围无效。")
        if not isinstance(text, str):
            raise ValueError("论文工具只接受提取后的文本。")
        if mode == "full" and len(text) > limits[mode]:
            raise ValueError("全文超过120000字符，请选择跨章节范围。")
        self._text = text[:2000] if mode == "head" else text
        self._budget = limits[mode]
        self.sent_chars = 0
        self._sequence = 0

    def _next_id(self) -> str:
        self._sequence += 1
        return f"T{self._sequence:03d}"

    def _spend(self, length: int) -> None:
        if self.sent_chars + length > self._budget:
            raise ValueError("本次论文工具摘录预算已用尽；请基于已有证据审阅。")
        self.sent_chars += length

    def _interval(self, request: dict, maximum: int) -> tuple[int, int]:
        start, end = request.get("start"), request.get("end")
        if type(start) is not int or type(end) is not int:
            raise ValueError("字符位置必须是整数。")
        if not 0 <= start < end <= len(self._text):
            raise ValueError("字符区间超出本次授权论文范围。")
        if end - start > maximum:
            raise ValueError(f"本工具单次最多读取{maximum}字符。")
        return start, end

    def execute(self, request: dict) -> dict:
        """Return JSON-safe evidence or a bounded error; never raise for a request.

        ``search`` accepts only a literal ``query``. ``read``/``stat_recheck``
        accept only integer ``start`` and exclusive ``end``. Every result and
        search passage gets a unique evidence ID within this session.
        """
        record = {"id": self._next_id(), "tool": "unknown", "ok": False,
                  "start": None, "end": None}
        try:
            if not isinstance(request, dict):
                raise ValueError("工具请求必须是JSON对象。")
            tool = request.get("tool")
            if not isinstance(tool, str) or tool not in {"search", "read", "stat_recheck"}:
                raise ValueError("仅支持search、read、stat_recheck只读工具。")
            record["tool"] = tool
            keys = {"tool", "query"} if tool == "search" else {"tool", "start", "end"}
            if set(request) != keys:
                raise ValueError("工具参数缺失或包含未支持的字段。")
            if tool == "search":
                query = request["query"]
                if not isinstance(query, str) or not query.strip() or len(query) > 80:
                    raise ValueError("检索词须为1至80字符的非空文本。")
                intervals = []
                cursor = 0
                for _ in range(4):
                    pos = self._text.find(query, cursor)
                    if pos < 0:
                        break
                    start, end = max(0, pos - 300), min(len(self._text), pos + 900)
                    intervals.append((start, end))
                    # Skip already displayed neighborhoods, bounding work even
                    # when a document repeats a keyword millions of times.
                    cursor = end
                self._spend(sum(end - start for start, end in intervals))
                passages = [{"id": self._next_id(), "start": start, "end": end,
                             "text": self._text[start:end]} for start, end in intervals]
                record.update(ok=True, passages=passages)
                if passages:
                    record.update(start=passages[0]["start"], end=passages[-1]["end"])
            else:
                start, end = self._interval(request, 2400 if tool == "read" else 6000)
                source = self._text[start:end]
                if tool == "read":
                    self._spend(len(source))
                    record.update(ok=True, start=start, end=end, text=source)
                else:
                    # Compute before spending so unexpected local calculation
                    # errors neither consume budget nor leak a partial result.
                    if self.sent_chars + len(source) > self._budget:
                        raise ValueError("本次论文工具摘录预算已用尽。")
                    result = recheck_stats(source)
                    for rows in (result.get("items", []), result.get("conflicts", [])):
                        for item in rows:
                            if isinstance(item.get("pos"), int):
                                item["pos"] += start
                    self._spend(len(source))
                    record.update(ok=True, start=start, end=end, result=result,
                                  note="仅复算所选区间内报告的一致性，不证明研究真实性；"
                                       "片段外的单侧检验、校正与显著性水平声明须另行补读核对。")
        except ValueError as exc:
            record["error"] = str(exc)
        except Exception:
            record["error"] = "本地工具执行失败，请核对论文文本或字符区间。"
        return record
