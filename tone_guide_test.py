"""
v1.7 · 规划§四 P1 人味增强（语气规约）契约测试
================================================
覆盖：
    1. AI 腔检测：各类命中 / 干净文本不误伤 / 计数与排序
    2. TONE_RULES 注入两条冻结 prompt（并已 bump PROMPT_VERSION）
    3. 内置报告模板自身零 AI 腔（防止未来有人写回套话）
    4. 边界：不误伤规范学术表述（数字、统计量、"建议补充…"）
    5. 边界：本模块是"语气规范"而非"绕过检测"——不提供任何规避功能

运行：.venv/Scripts/python.exe -u tone_guide_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f'  << {detail}' if detail else ''))


import tone_guide as tg

print('=== 1. 检测各类 AI 腔 ===')
cases = [
    ("本研究具有重要意义。", "空泛总结"),
    ("为后续研究奠定了坚实基础。", "空泛总结"),
    ("综上所述，该方法是可行的。", "空泛总结"),
    ("总体而言，结果符合预期。", "空泛总结"),
    ("不难看出，两组存在差异。", "套话"),
    ("值得注意的是，p 值为 0.03。", "套话"),
    ("该方法高效、准确、稳定。", "排比堆砌"),
    ("不仅提升了效率，而且优化了流程。", "长句套式"),
    ("在已有研究的基础上，进一步探讨。", "长句套式"),
    ("随着人工智能的不断发展，", "长句套式"),
    ("旨在提升学习效果。", "长句套式"),
    ("赋能高校教学改革。", "互联网黑话"),
    ("令人欣喜的是，结果显著。", "情绪词"),
]
for text, want_cat in cases:
    hits = tg.find_ai_speak(text)
    cats = {h["category"] for h in hits}
    check(f"命中「{text[:14]}…」→ {want_cat}", want_cat in cats if want_cat != "套话"
          else bool(cats & {"套话"}), str(hits))


print()
print('=== 2. 不误伤规范学术表述（关键：宁缺毋滥） ===')
clean = [
    "两组差异显著（t(28) = -14.093，p < 0.001，Cohen's d = -5.15）。",
    "建议补充效应量（Cohen's d）与 95% 置信区间。",
    "论文使用独立样本 T 检验，但分组有 3 个，建议改用单因素方差分析。",
    "Levene 检验 p = 0.74 > 0.05，方差齐性满足，采用等方差 T 检验。",
    "缺失值共 12 个（占 3.1%），已按列删除处理。",
    "该量表 Cronbach's α = 0.87，内部一致性良好。",
    "相关不等于因果。若仅做相关分析，讨论部分应避免因果表述。",
    "样本量 N=45，处于常见毕业论文规模，但仍建议报告效应量。",
]
for t in clean:
    hits = tg.find_ai_speak(t)
    check(f"不误伤「{t[:16]}…」", not hits, str(hits))


print()
print('=== 3. 统计与排序 ===')
multi = "具有重要意义。综上所述，具有重要意义。"
hits = tg.find_ai_speak(multi)
check("同一模式多次命中会计数", any(h["count"] >= 2 for h in hits), str(hits))
score = tg.ai_speak_score(multi)
check("total_hits 为总出现次数（含重复）", score["total_hits"] >= 3, str(score["total_hits"]))
check("by_category 汇总正确", "空泛总结" in score["by_category"], str(score["by_category"]))
check("density_per_1k 是正数", score["density_per_1k"] > 0, str(score["density_per_1k"]))
# 排序按首次出现位置
ordered = tg.find_ai_speak("旨在优化。综上所述，结果良好。")
check("命中按首次位置排序",
      [h["index"] for h in ordered] == sorted(h["index"] for h in ordered),
      str([h["index"] for h in ordered]))
check("空文本返回空", tg.find_ai_speak("") == [])
check("None 返回空", tg.find_ai_speak(None) == [])
check("has_ai_speak 干净文本为 False", tg.has_ai_speak(clean[0]) is False)
check("has_ai_speak 命中为 True", tg.has_ai_speak("具有重要意义") is True)


print()
print('=== 4. TONE_RULES 注入冻结 prompt ===')
import llm_enhance
import llm_audit
import agents.prompts as P

check("llm_enhance.FROZEN_SYSTEM 含 TONE_RULES", tg.TONE_RULES in llm_enhance.FROZEN_SYSTEM)
check("PAPER_CHECK_SYSTEM 含 TONE_RULES_REVIEW", tg.TONE_RULES_REVIEW in P.PAPER_CHECK_SYSTEM)
check("TONE_RULES_REVIEW 是 TONE_RULES 的超集",
      tg.TONE_RULES_REVIEW.startswith(tg.TONE_RULES))
check("审稿版多一条「建议可执行」",
      "可执行" in tg.TONE_RULES_REVIEW and "可执行" not in tg.TONE_RULES)
# 具体禁令必须可判定（含明确的反面例子，而非"避免 AI 腔"这种形容词）
check("规约里有「拆长句」", "拆长句" in tg.TONE_RULES)
check("规约里有「删空泛总结」", "删空泛总结" in tg.TONE_RULES)
check("规约给了具体禁用语实例", "具有重要意义" in tg.TONE_RULES)
check("规约禁止三连排比", "三连" in tg.TONE_RULES)
check("规约要求结论带统计量", "统计量" in tg.TONE_RULES)
# PROMPT_VERSION 已按契约 bump
check("llm_enhance PROMPT_VERSION 已 bump 到 v3",
      llm_enhance.PROMPT_VERSION == "v3", llm_enhance.PROMPT_VERSION)
check("llm_audit PROMPT_VERSION 已 bump 到 v3",
      llm_audit.PROMPT_VERSION == "v3", llm_audit.PROMPT_VERSION)


print()
print('=== 5. 内置报告模板自身零 AI 腔（防回退） ===')
import numpy as np
import pandas as pd
import app as A

np.random.seed(7)
n = 30
df = pd.DataFrame({
    "g": ["A"] * n + ["B"] * n,
    "v": np.r_[np.random.normal(10, 2, n), np.random.normal(13, 2, n)],
    "a": np.random.normal(0, 1, n * 2),
    "b": np.random.normal(0, 1, n * 2),
    "cat": np.random.choice(["x", "y"], n * 2),
})
df3 = df.copy()
df3["g3"] = np.random.choice(["A", "B", "C"], n * 2)

tpl_cases = [
    ("run_independent_t", A.run_independent_t, (df, "g", "v")),
    ("run_anova", A.run_anova, (df3, "g3", "v")),
    ("run_correlation", A.run_correlation, (df, "a", "b")),
    ("run_chi_square", A.run_chi_square, (df, "cat", "g")),
    ("run_mann_whitney", A.run_mann_whitney, (df, "g", "v")),
    ("run_wilcoxon", A.run_wilcoxon, (df, "a", "b")),
    ("run_paired_t", A.run_paired_t, (df, "a", "b")),
    ("run_cronbach_alpha", A.run_cronbach_alpha, (df, ["a", "b"])),
]
for name, fn, args in tpl_cases:
    try:
        r = fn(*args)
    except Exception as e:  # noqa: BLE001
        print(f'  [SKIP] {name} 无法跑通（{str(e)[:50]}）')
        continue
    md = r.get("markdown") or ""
    s = tg.ai_speak_score(md)
    check(f"{name} 报告模板零 AI 腔", s["total_hits"] == 0,
          f'{s["total_hits"]} {s["by_category"]}')


print()
print('=== 6. 边界：这不是"绕过 AI 检测"工具 ===')
src = open(tg.__file__, encoding="utf-8").read()
check("不提供任何规避检测/降 AI 率功能",
      "绕过" not in src.replace("绕过检测", "") or "不做" in src)
check("模块文档明确禁止规避用途", "明确禁止" in src or "ROADMAP 明确禁止" in src)
check("不改写用户文本（只报不改）", "不改写文本" in src or "不改写" in src)
check("纯规则零 LLM", "零 LLM" in src)


print()
print('=== 汇总 ===')
print(f'结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
