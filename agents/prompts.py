"""
缓存友好的 prompt 构建器（v0.4.1）
==================================
背景（2026-09-09 实测验证，硅基流动 DeepSeek-V4-Flash）：

    1. 硅基流动**透传** DeepSeek 前缀缓存计费：usage 里返回
       prompt_cache_hit_tokens / prompt_cache_miss_tokens，命中价 ≈ 未命中价的 1/10
       （V4-Flash 闲时 ¥0.15/M vs ¥1.5/M，忙时 ¥0.3 vs ¥3；V4-Pro 命中 ¥1/M）
    2. 实测：冻结前缀 2048 token 全量命中，仅变量后缀 20 token 按未命中计费
    3. 缓存按 512 token 块组织；写入是**异步**的（调用后几秒到几分钟才生效）
    4. 官方公告：2026-09-01 起 V4-Flash 分时段收费，"完全免费"已成历史

契约（接线 /api/analyze 时必须遵守）：

    prompt = [冻结区：字节级稳定] + [变量区：每次不同，放最后]

    - 冻结区 = system 提示 + 论文全文 + 输出 schema
    - 变量区 = 用户本次的指令 / 追问
    - 冻结区内**严禁**时间戳、随机数、文件路径、临时 ID —— 任何一个字节
      变化都会让后面几千 token 的论文文本全部缓存失效（按最贵价计费）
    - 论文文本必须做规范化（strip 尾部空白），保证同一篇论文 → 同一个前缀

用法（paper_check 状态）：

    from agents.prompts import PAPER_CHECK_SYSTEM, build_paper_check_prompt

    prefix = build_paper_check_prefix(paper_text)     # 同篇论文只算一次，可缓存
    prompt = build_paper_check_prompt(prefix, "审计第 3.2 节的统计方法选择")
    router.complete("paper_check", prompt, system=PAPER_CHECK_SYSTEM)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# paper_check：论文审计（路由到 sf/deepseek-v4-pro，付费 → 缓存收益最大）
# ---------------------------------------------------------------------------
# v1.7：注入 tone_guide 的明确语气禁令。旧版只写"不输出寒暄和免责声明"，
# 对"AI 腔"没有可判定标准，模型只能猜；现在改为逐条具体禁令。
# ⚠️ 改动本常量 = 改变冻结前缀 → llm_audit.PROMPT_VERSION 必须 bump。
from tone_guide import TONE_RULES_REVIEW as _TONE_RULES_REVIEW  # noqa: E402

PAPER_CHECK_SYSTEM = (
    "你是论文统计部分的审稿专家。你的任务是核查统计方法、统计量与结论的"
    "一致性，并给出可操作的改进建议。只输出与统计审计相关的内容，"
    "不输出寒暄和免责声明。\n\n"
    + _TONE_RULES_REVIEW
)

_PAPER_HEADER = "以下是待审计的论文文本：\n<<<PAPER\n"
_PAPER_FOOTER = "\nPAPER>>>\n"

_OUTPUT_SCHEMA = (
    "输出格式（严格遵守，中文）：\n"
    "1. 识别的方法：<论文实际使用的统计方法>\n"
    "2. 发现的问题：<逐条列出，标注严重程度（高/中/低）>\n"
    "3. 改进建议：<逐条对应问题，给出具体可执行的修改>\n"
)


def build_paper_check_prefix(paper_text: str) -> str:
    """构建冻结前缀（论文全文 + 输出 schema）。

    同一篇论文的多次审计 / 追问共用同一个前缀 → 前缀缓存全量命中，
    论文文本只在第一次按未命中计费。
    """
    # strip() 规范化：避免尾部空白差异导致缓存失效
    return _PAPER_HEADER + paper_text.strip() + _PAPER_FOOTER + _OUTPUT_SCHEMA


def build_paper_check_prompt(frozen_prefix: str, question: str) -> str:
    """冻结前缀 + 变量后缀 → 完整 prompt。变量部分永远在最后。"""
    return frozen_prefix + "\n本次任务：" + question.strip() + "\n"


# ---------------------------------------------------------------------------
# write_text / recommend：prompt 较短，暂无前缀缓存需求（v0.5 响应缓存覆盖）
# ---------------------------------------------------------------------------
