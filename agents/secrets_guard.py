"""
敏感信息擦除（v0.5.4）
======================
目标：API Key **永不**出现在任何可能回传到前端的字符串里。

背景（为什么需要这层）：
    LLM 调用失败时，异常链可能是
        openai SDK 异常 → AgentError → llm_audit/_short → jsonify(error=...)
    直接把 `str(e)` 送到浏览器。SDK 的报错正文（尤其是鉴权失败）有时会
    回显凭据，一旦发生就是不可逆的泄露。所以**所有对外字符串必须过一遍擦除**。

两层防护：
    1. **已知 Key 精确替换**（最可靠）
       Agent 初始化时把用到的 Key 注册进本模块，redact() 把文本里出现的
       Key 原样替换为 `***`。不管 Key 从哪条路径漏出来都能兜住。
    2. **形态兜底**
       即使某把 Key 没被注册（例如从别处拼出来的），也按常见形态擦除：
       百炼/OpenAI 的 `sk-...`、智谱的 `32位hex.随机串`。

不做的事：
    - 不读取、不落盘、不打印任何 Key；只在内存里留一份用于比对
    - 不用它做鉴权，纯输出过滤

用法：
    from .secrets_guard import register_secret, redact
    register_secret(api_key)      # Agent 构造时登记
    safe = redact(str(exc))       # 任何对外文案之前调一次
"""
from __future__ import annotations

import re

MASK = "***"

# 已知 Key 集合（内存内，进程级；太短的不登记，避免误伤正常文本）
_KNOWN: set[str] = set()
_MIN_SECRET_LEN = 12

# 形态兜底：即便 Key 未登记也擦掉
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 百炼 / OpenAI / DeepSeek 系：sk-xxx（含 sk-ws-xxx.yyy 这种带点的）
    re.compile(r"sk-[A-Za-z0-9._\-]{12,}"),
    # 智谱系：32 位小写 hex + '.' + 随机串
    re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9_\-]{8,}\b"),
)


def register_secret(value: str | None) -> None:
    """登记一把 Key，后续 redact() 会精确擦除它。"""
    v = (value or "").strip()
    if len(v) >= _MIN_SECRET_LEN:
        _KNOWN.add(v)


def register_many(raw: str | None, sep: str = ",") -> None:
    """登记逗号分隔的多把 Key（多 Key 轮转场景）。"""
    for part in (raw or "").split(sep):
        register_secret(part)


def redact(text: object) -> str:
    """把文本里所有已知 / 疑似 Key 替换为 `***`。输入 None 返回空串。"""
    if text is None:
        return ""
    s = str(text)
    if not s:
        return s
    for key in _KNOWN:
        if key in s:
            s = s.replace(key, MASK)
    for pattern in _PATTERNS:
        s = pattern.sub(MASK, s)
    return s


def clear_registry() -> None:
    """清空已登记 Key（仅供测试使用）。"""
    _KNOWN.clear()
