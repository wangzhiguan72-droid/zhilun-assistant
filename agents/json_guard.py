"""LLM 返回 JSON 的统一安全解析器（v2.30 起）。

设计动机
--------
LLM 不是确定性的——返回经常混着：
- 前言/后语废话
- markdown 代码块包裹（```json ... ```）
- 行内 `` `{"...":"..."}` ``
- 嵌套乱码、长度爆掉

每个调用方各自手写"先 json.loads，崩了再 re.search 找 []"既重复又漏。
本模块把这一兜底统一起来，要求：
1. 三层降级：直接 parse → 提取首段 {} 或 [] → 提取 markdown 代码块
2. 结构校验：期望类型（object/array）、必填字段存在、字段值类型粗校
3. 超长截断：超过 MAX_JSON_CHARS 直接判失败（防 LLM 异常长输出撑爆内存）
4. 失败静默：返回 (None, reason_code, raw_len)，绝不把 JSONDecodeError 原文透出去

红线
----
- 不抛任何异常给调用方（业务路径走 AgentError 容灾链就够了）
- 不写文件、不读全局状态
- 不依赖外部包（只用标准库 + agents 已有依赖）
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

# 单条 LLM 返回超过这个长度直接判失败（防 OOM / 防把网页弄爆）
MAX_JSON_CHARS = 200_000

# markdown 代码块（```json ... ``` 或 ``` ... ```）
_MD_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)
# 任意 inline `` `...` ``
_INLINE_BACKTICK = re.compile(r"`([^`]+)`")
# 首段 JSON object
_OBJ = re.compile(r"\{.*\}", re.DOTALL)
# 首段 JSON array
_ARR = re.compile(r"\[.*\]", re.DOTALL)


def _try_load(blob: str) -> Any | None:
    """尝试 json.loads，失败返 None。不抛异常。"""
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_candidates(text: str, expect: str) -> list[str]:
    """按"成本由低到高"产出候选 JSON 串。"""
    candidates: list[str] = []

    # 第 1 层：原始文本
    candidates.append(text)

    # 第 2 层：markdown 代码块（取第一段）
    m = _MD_FENCE.search(text)
    if m:
        candidates.append(m.group(1))

    # 第 3 层：inline 反引号包裹（取第一段）
    m = _INLINE_BACKTICK.search(text)
    if m:
        candidates.append(m.group(1))

    # 第 4 层：截取首段 object / array
    pat = _OBJ if expect == "object" else _ARR
    m = pat.search(text)
    if m:
        candidates.append(m.group())

    return candidates


def safe_parse_json(
    text: str,
    *,
    expect: str = "array",
    required: Iterable[str] = (),
) -> tuple[Any | None, str, int]:
    """把 LLM 返回文本安全解析为 JSON。

    参数
    ----
    text : LLM 原始返回字符串
    expect : 期望顶层类型，"object" | "array"
    required : object 时要求这些 key 都存在（顶层 key 校验，不递归）

    返回
    ----
    (value, status, raw_len)
        - value: 解析成功 → 校验后的对象；失败 → None
        - status: "ok" | "empty" | "too_long" | "type_mismatch" |
                  "missing_field" | "decode_failed"
        - raw_len: 原始 text 字节数（让上层日志能看出"炸了一条大响应"）
    """
    if not text or not text.strip():
        return None, "empty", 0

    raw_len = len(text)
    if raw_len > MAX_JSON_CHARS:
        return None, "too_long", raw_len

    candidates = _extract_candidates(text, expect)

    parsed: Any | None = None
    for blob in candidates:
        parsed = _try_load(blob)
        if parsed is not None:
            break

    if parsed is None:
        return None, "decode_failed", raw_len

    # 类型校验
    if expect == "object":
        if not isinstance(parsed, dict):
            return None, "type_mismatch", raw_len
        for k in required:
            if k not in parsed:
                return None, "missing_field", raw_len
    elif expect == "array":
        if not isinstance(parsed, list):
            return None, "type_mismatch", raw_len
    else:
        return None, "type_mismatch", raw_len  # 编程错误也走兜底

    return parsed, "ok", raw_len


__all__ = ["safe_parse_json", "MAX_JSON_CHARS"]