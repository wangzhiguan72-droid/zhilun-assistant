"""
Ponytail 轻量级优化测试（v0.5.1）
=================================
测试三个优化点：
1. llm_enhance.py 的决策层 prompt（写解读前先判断是否需要详细解释）
2. llm_audit.py 的决策层 prompt（审计前先判断是否值得深度审计）
3. llm_review.py 的输出审查工具（识别冗余内容）

预期：
- 决策层 prompt 会影响 LLM 的输出风格（更简洁）
- llm_review.py 能识别出冗余内容并给出建议
- 缓存版本 bump 后旧缓存自动失效（PROMPT_VERSION 从 v1→v2）

运行：python ponytail_test.py
"""
from __future__ import annotations

from llm_enhance import enhance_analysis, PROMPT_VERSION as ENHANCE_VERSION
from llm_audit import audit_paper_with_llm, PROMPT_VERSION as AUDIT_VERSION
from llm_review import review_output


def test_decision_layer_versions():
    """确认两个模块的 PROMPT_VERSION 已 bump 到 v2 之后（当前 v3）。

    契约（比"等于某个字面量"更重要）：
      · 版本号必须是 vN 形式；
      · 两个模块的冻结 prompt 都改过，故两者版本应**同步**（本项目一直同号）；
      · 必须 ≥ v2（Ponytail 决策层的首次 bump）。
    ⚠️ 不要写死成某个具体版本 —— 每次按契约改 prompt 都必须 bump，
       写死会让"正确升级"反而测失败（本测试曾因 v3 升级而假红）。
    """
    import re as _re
    for name, ver in (("llm_enhance", ENHANCE_VERSION), ("llm_audit", AUDIT_VERSION)):
        m = _re.fullmatch(r"v(\d+)", ver or "")
        assert m, f"{name} 的 PROMPT_VERSION 应为 vN 形式，实际为 {ver!r}"
        assert int(m.group(1)) >= 2, f"{name} 版本应 ≥ v2，实际为 {ver}"
    assert ENHANCE_VERSION == AUDIT_VERSION, (
        f"两个模块的冻结 prompt 需同步升版：llm_enhance={ENHANCE_VERSION} "
        f"vs llm_audit={AUDIT_VERSION}")
    print(f"[✓] 决策层优化：PROMPT_VERSION 已 bump 至 {ENHANCE_VERSION}（两者同步）")


def test_llm_review_basic():
    """测试 llm_review.py 的基本功能（离线，不调真 API）。"""
    # 简单文本（不调 API 直接返回 error 模式）
    text = "这是一段测试文本。"
    result = review_output(text)

    # 无 Key 时应该返回 error 提示
    assert isinstance(result, list)
    if result:
        assert "type" in result[0]
        print(f"[✓] llm_review 基本功能：返回 {result[0]['type']}")
    else:
        print("[✓] llm_review 基本功能：返回空列表（无 LLM 时）")


def test_llm_review_parse():
    """测试 _parse_review 的解析逻辑（不调 LLM，直接测 JSON 解析）。"""
    from llm_review import _parse_review

    # 正常 JSON
    json_input = '[{"type": "冗余", "original": "您好", "suggestion": "删除"}]'
    result = _parse_review(json_input)
    assert len(result) == 1
    assert result[0]["type"] == "冗余"

    # 带废话的 JSON（提取 [] 部分）
    mixed_input = '以下是审查结果：\n[{"type": "重复", "original": "...", "suggestion": "..."}]\n希望对您有帮助。'
    result = _parse_review(mixed_input)
    assert len(result) == 1
    assert result[0]["type"] == "重复"

    # 无法解析时返回兜底
    bad_input = "不是 JSON"
    result = _parse_review(bad_input)
    assert result[0]["type"] == "error"

    print("[✓] llm_review._parse_review：正常 / 混杂 / 失败 三种场景都覆盖")


def test_enhance_decision_prompt():
    """验证 enhance_analysis 的 FROZEN_SYSTEM 包含决策层提示。"""
    from llm_enhance import FROZEN_SYSTEM

    assert "Ponytail" in FROZEN_SYSTEM or "决策" in FROZEN_SYSTEM, \
        "FROZEN_SYSTEM 应包含决策层提示"
    assert "生成前决策" in FROZEN_SYSTEM, "应明确标注「生成前决策」"
    assert "ponytail" in FROZEN_SYSTEM.lower(), "应支持「ponytail:」标记"
    print("[✓] enhance_analysis：决策层 prompt 已嵌入 FROZEN_SYSTEM")


def test_audit_decision_prompt():
    """验证 audit_paper_with_llm 的决策层提示已加载。"""
    from llm_audit import _PONYTAIL_DECISION

    assert "Ponytail" in _PONYTAIL_DECISION or "决策" in _PONYTAIL_DECISION, \
        "决策层提示应该存在"
    assert "审计前决策" in _PONYTAIL_DECISION, "应明确标注「审计前决策」"
    assert "ponytail" in _PONYTAIL_DECISION.lower(), "应支持「ponytail:」标记"
    print("[✓] audit_paper_with_llm：决策层提示已定义")


def test_cache_version_invalidates_old():
    """验证 PROMPT_VERSION bump 后旧缓存自动失效。"""
    from llm_cache import llm_cache, MAX_ENTRIES

    # 清空缓存
    llm_cache.clear()

    # 模拟旧版本的 key（v1）
    old_key = llm_cache.make_key("test-model", "v1", "independent_t", {})
    llm_cache.put(old_key, "旧版本结果")

    # 模拟新版本的 key（v2）
    new_key = llm_cache.make_key("test-model", "v2", "independent_t", {})
    llm_cache.put(new_key, "新版本结果")

    # 验证两者是不同的 key
    assert old_key != new_key, "版本 bump 后缓存 key 应该不同"

    # 验证缓存里有两条记录
    assert llm_cache.stats()["entries"] == 2, "应该有两个版本的缓存"

    print("[✓] 缓存版本控制：v1 和 v2 的 key 不同，旧缓存不会被误命中")


def test_decision_layer_output_style():
    """（可选）真调 API，观察决策层是否让输出更简洁。
    需要 SILICONFLOW_API_KEY / ZHIPU_API_KEY。
    """
    import os

    if not (os.getenv("SILICONFLOW_API_KEY") or os.getenv("ZHIPU_API_KEY")):
        print("[⊘] 决策层输出风格：跳过（无 API Key，需要真调才能验证）")
        return

    # 对比同一个统计量，有无决策层的输出差异
    summary = {
        "test": "independent_t",
        "t_statistic": -14.09,
        "p_value": 0.00001,
        "degrees_of_freedom": 28,
    }
    label = "独立样本 t 检验"

    # 调用 enhance_analysis（已嵌入决策层）
    section, error, meta = enhance_analysis("independent_t", summary, label)

    if error:
        print(f"[⊘] 决策层输出风格：LLM 调用失败 - {error}")
        return

    # 简单检查：输出应该包含统计量数字
    if section:
        assert "-14.09" in section or "14.09" in section, "应包含 t 值"
        print(f"[✓] 决策层输出风格：生成成功（{meta['model']}）")
        print(f"    预览：{section[:100]}…")
    else:
        print("[⊘] 决策层输出风格：返回空内容")


if __name__ == "__main__":
    print("=== Ponytail 轻量级优化测试 ===\n")

    test_decision_layer_versions()
    test_llm_review_basic()
    test_llm_review_parse()
    test_enhance_decision_prompt()
    test_audit_decision_prompt()
    test_cache_version_invalidates_old()
    test_decision_layer_output_style()

    print("\n=== 测试完成 ===")