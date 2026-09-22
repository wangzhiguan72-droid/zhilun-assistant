"""
LLM 输出审查工具（v0.5.1）
===========================
参考 Ponytail 的 `/ponytail-review` 命令设计，用于识别 LLM 输出中的
冗余内容（寒暄、重复说明、过度解释）。

设计目标：
    - 主动识别输出中的"过度工程化"（AI 腔、废话、重复）
    - 给出可操作的简化建议
    - 作为可选功能，不影响主流程（用户主动触发）

接口：
    review_output(text: str) -> list[dict]
        返回 [{"type": str, "original": str, "suggestion": str}, ...]
        type: 冗余 / 重复 / 可简化 / 已优化
"""
from __future__ import annotations

from typing import Any

from agents import AgentError, get_router


def review_output(text: str) -> list[dict[str, str]]:
    """识别 LLM 输出中的冗余内容（寒暄、重复、过度解释）。

    返回审查报告列表：
        - type: 冗余 / 重复 / 可简化 / 已优化
        - original: 原文片段
        - suggestion: 简化建议（或"已优化"）
    """
    if not text or not text.strip():
        return []

    prompt = f"""以下是一段 AI 生成的学术文本：

<<<OUTPUT
{text}
OUTPUT>>>

请识别其中的冗余部分（寒暄、重复说明、过度解释）并给出简化建议。

严格按以下格式输出（JSON 数组，不要有其他文字）：
[
  {{
    "type": "冗余|重复|可简化|已优化",
    "original": "原文片段",
    "suggestion": "简化建议（已优化时写「已优化」）"
  }}
]

标注规则：
- 冗余：寒暄语、客套话、与结论无关的铺垫
- 重复：相同意思重复说、数字重复列举
- 可简化：可以用更简洁的方式表达（如两句话合并）
- 已优化：这部分已经很简洁，无需改进
"""

    try:
        router = get_router()
        result = router.complete(
            "write_text",
            prompt=prompt,
            system="你是学术写作审查专家，只输出 JSON 数组，不要任何解释。",
            max_tokens=800,
        )
        return _parse_review(result)
    except AgentError:
        return [{"type": "error", "original": "", "suggestion": "LLM 不可用"}]
    except Exception:  # noqa: BLE001
        return [{"type": "error", "original": "", "suggestion": "解析失败"}]


def _parse_review(text: str) -> list[dict[str, str]]:
    """解析 LLM 返回的 JSON 数组（v2.30 起走 agents.json_guard.safe_parse_json 统一兜底）。

    三层降级：直接 parse → 提取首段 [] → markdown 代码块提取。
    失败时返回兜底提示，**绝不**把 JSONDecodeError 原文透出去。
    """
    from agents.json_guard import safe_parse_json

    parsed, status, raw_len = safe_parse_json(text, expect="array")
    if parsed is None:
        # 兜底提示统一收敛到这里——上游 review_output 会包成 error 项。
        # raw_len > 0 但 status != ok 时附上原文前 100 字符，方便排查。
        preview = text[:100] if raw_len else ""
        reason = {
            "empty": "LLM 返回为空",
            "too_long": f"LLM 返回过长（{raw_len} > 安全上限）",
            "type_mismatch": "LLM 返回顶层类型不符（期望 JSON 数组）",
            "missing_field": "LLM 返回缺少必填字段",
            "decode_failed": "LLM 返回无法解析为 JSON",
        }.get(status, f"解析失败（{status}）")
        return [
            {
                "type": "error",
                "original": preview,
                "suggestion": f"{reason}，请检查输出格式",
            }
        ]
    return parsed


# ---------------------------------------------------------------------------
# 离线自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_text = """
    您好！这是一段学术解读。首先，我需要说明一下统计检验的结果。
    根据我们计算的统计量，t = -14.09，p < 0.001。这个结果说明两组之间的
    差异非常显著。另外，效应量也很重要，效应量 d = 2.31，表明差异很大。
    建议你在论文中这样写：「t 检验显示两组差异显著（t = -14.09, p < 0.001）」。
    最后，写作提示：记住要报告效应量。
    """

    print("=== 测试文本 ===")
    print(test_text)
    print("\n=== 审查报告 ===")
    for item in review_output(test_text):
        print(f"[{item['type']}] {item['original'][:50]}… → {item['suggestion']}")