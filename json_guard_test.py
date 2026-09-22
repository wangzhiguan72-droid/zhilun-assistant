"""agents/json_guard.safe_parse_json 测试套件（v2.30 起）

按 *AGENTS.md* §四：测试命名 *_test.py 放根目录，CI 自动发现。
按 *CLAUDE.md* §四：与回归脚本兼容，OK/FAIL 输出 + 最后一行汇总。
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from agents.json_guard import safe_parse_json, MAX_JSON_CHARS

PASS = 0
FAIL = 0


def _check(label: str, got: Any, want: Any) -> None:
    global PASS, FAIL
    if got == want:
        print(f"  [PASS] {label}")
        PASS += 1
    else:
        print(f"  [FAIL] {label} got={got!r} want={want!r}")
        FAIL += 1


# === A. 顶层类型校验 ===
print("[A] 顶层类型校验")
val, status, raw = safe_parse_json('{"k":1}', expect="object")
_check("object 顶层 OK", (type(val).__name__, status), ("dict", "ok"))

val, status, _ = safe_parse_json('[1,2,3]', expect="array")
_check("array 顶层 OK", (type(val).__name__, len(val), status), ("list", 3, "ok"))

val, status, _ = safe_parse_json('{"k":1}', expect="array")
_check("object 当 array 用 → type_mismatch", status, "type_mismatch")

val, status, _ = safe_parse_json('[1,2,3]', expect="object")
_check("array 当 object 用 → type_mismatch", status, "type_mismatch")

# === B. 必填字段校验 ===
print("\n[B] 必填字段校验（仅 object 生效）")
val, status, _ = safe_parse_json('{"a":1,"b":2}', expect="object", required=["a", "b"])
_check("必填都在 → ok", status, "ok")

val, status, _ = safe_parse_json('{"a":1}', expect="object", required=["a", "b"])
_check("必填缺一个 → missing_field", status, "missing_field")

val, status, _ = safe_parse_json('[1,2]', expect="array", required=["a"])
_check("array 不校验必填 → ok（透传）", status, "ok")

# === C. markdown 代码块提取 ===
print("\n[C] markdown 代码块提取")
text_md = "好的，按你说的：\n```json\n[{\"a\":1},{\"a\":2}]\n```\n以上。"
val, status, _ = safe_parse_json(text_md, expect="array")
_check("markdown fence → 解析成功", status, "ok")
_check("markdown fence → 长度正确", len(val), 2)

text_md_no_lang = "结果：\n```\n{\"k\":1}\n```\n完。"
val, status, _ = safe_parse_json(text_md_no_lang, expect="object", required=["k"])
_check("无语言 fence 也能取", status, "ok")

# === D. inline 反引号 ===
print("\n[D] inline 反引号包裹")
text_inline = "结果是 `[1,2,3]` 这就是答案。"
val, status, _ = safe_parse_json(text_inline, expect="array")
_check("inline backtick → ok", status, "ok")
_check("inline backtick → 值正确", val, [1, 2, 3])

# === E. 前后废话 + JSON 段落 ===
print("\n[E] 前后废话夹 JSON 段落")
text_chatter = (
    "好的，我分析了这段文本：\n"
    "以下是审查报告（请忽略前文）：\n"
    "[{\"type\":\"冗余\",\"original\":\"您好\",\"suggestion\":\"删除寒暄\"}]\n"
    "以上。"
)
val, status, _ = safe_parse_json(text_chatter, expect="array")
_check("提取首段 array → ok", status, "ok")
_check("提取首段 array → 内容正确", val[0]["type"], "冗余")

# === F. 空 / 太长 / 坏 JSON ===
print("\n[F] 空 / 太长 / 坏 JSON")
val, status, raw = safe_parse_json("", expect="array")
_check("空字符串 → empty", (val, status, raw), (None, "empty", 0))

val, status, raw = safe_parse_json("   \n  ", expect="array")
_check("纯空白 → empty", status, "empty")

val, status, raw = safe_parse_json("not json at all", expect="array")
_check("纯废话 → decode_failed", (val, status), (None, "decode_failed"))

big = '{"a":' + "x" * (MAX_JSON_CHARS + 10) + "}"
val, status, raw = safe_parse_json(big, expect="object")
_check("超长 → too_long", (val, status), (None, "too_long"))

# === G. 与 llm_review 老格式完全兼容 ===
print("\n[G] 与 llm_review 老格式完全兼容")
old_style = '[\n  {"type": "已优化", "original": "ok", "suggestion": "已优化"}\n]'
val, status, _ = safe_parse_json(old_style, expect="array")
_check("旧 llm_review 格式 → ok", status, "ok")
_check("旧 llm_review 格式 → 内容", val[0]["type"], "已优化")

# === H. 不抛异常（异常路径也不外泄） ===
print("\n[H] 不抛异常（异常路径也不外泄）")
try:
    # 触发编程错误：expect 传非法值
    val, status, _ = safe_parse_json("{}", expect="tuple")  # type: ignore[arg-type]
    _check("非法 expect → type_mismatch 兜底", status, "type_mismatch")
except Exception as e:
    _check("非法 expect 反而抛了", f"EXC:{e}", "no exception")


print(
    "\n==================================================\n"
    f"json_guard 测试：{PASS} 通过 / {FAIL} 失败\n"
    "=================================================="
)
sys.exit(0 if FAIL == 0 else 1)