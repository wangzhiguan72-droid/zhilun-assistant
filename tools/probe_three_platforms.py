#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三平台真调探针（v0.5.2）
========================
用实际 Key 实测三个平台能否调通 + 隐式前缀缓存是否命中。

    1. 智谱 BigModel      glm-4.7-flash      （免费档，限 1 并发）
    2. 阿里云百炼          qwen3.7-flash      （DashScope OpenAI 兼容模式）
    3. DeepSeek 官方       deepseek-v4-flash  （模型名候选探测）

对每个平台：
    Phase A  拉模型列表（确认真实模型 ID —— 模型名写错时能立刻看出来）
    Phase B  两次"同冻结前缀 + 不同问题"调用
             第 1 次建立缓存 → 等 4s → 第 2 次看 cached_tokens 是否命中

Key 只从环境变量读，**不落盘、不写 .env**：
    ZHIPU_API_KEY / DASHSCOPE_API_KEY / DEEPSEEK_API_KEY

用法（Git Bash，一行传三个 Key）：
    ZHIPU_API_KEY=xxx DASHSCOPE_API_KEY=yyy DEEPSEEK_API_KEY=zzz \\
        .venv/Scripts/python.exe probe_three_platforms.py
"""
from __future__ import annotations

import os
import sys
import time

# 本脚本位于 tools/ 下，项目根在其上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Key 来源：项目根 .env（v0.5.3 落盘）或真实环境变量
from env_loader import load_dotenv  # noqa: E402
load_dotenv()

from agents import AgentError, DeepSeekAgent, QwenAgent, ZhipuAgent  # noqa: E402


# ---------------------------------------------------------------------------
# 冻结前缀（字节级稳定：无时间戳 / 无随机数 / 无文件路径）
# 模拟 paper_check 场景，约 1800 字符，足够超过各平台缓存块阈值
# ---------------------------------------------------------------------------
FROZEN_PREFIX = """你是论文统计部分的审稿专家。以下是待审计的论文文本，请只依据该文本回答问题。

<<<PAPER
第三章 数据分析方法
3.1 研究对象：本研究招募了某高校两个年级共 180 名本科生，剔除无效问卷后得到有效样本 172 份，其中男生 84 人，女生 88 人，平均年龄 19.6 岁（SD=1.12）。
3.2 研究工具：学业自我效能感量表采用 Likert 五点计分，共 22 题，内部一致性系数 Cronbach's α = 0.87；学业成绩以期末综合成绩（百分制）为准。
3.3 统计方法：采用独立样本 t 检验比较不同性别学生的学业自我效能感差异；采用 Pearson 积差相关分析自我效能感与学业成绩的关系；采用卡方检验分析不同年级学生的通过率差异。所有分析均在 SPSS 26.0 中完成，显著性水平设定为 α = 0.05。
3.4 结果：男生自我效能感得分 M = 3.62, SD = 0.54；女生 M = 3.48, SD = 0.61；t(170) = 2.15, p = 0.033, Cohen's d = 0.24。自我效能感与学业成绩的相关 r = 0.42, p < 0.001。卡方检验 χ²(1) = 3.51, p = 0.061。
3.5 讨论部分指出"自我效能感显著预测学业成绩"，并在结论中建议"高校应开设自我效能感提升课程以改善学业表现"。

（方法学背景材料，供审计参考。）独立样本 t 检验的前提假设包括：观测值相互独立、两组数据分别服从正态分布、两组方差齐性。当方差齐性不满足时，应报告 Welch 校正的 t 检验结果。Cohen's d 的计算在两组样本量不等时建议使用合并标准差的校正公式（Hedges' g）。Pearson 相关的前提是两变量均为连续变量且关系近似线性；若存在明显偏态，可考虑 Spearman 等级相关。卡方检验要求期望频数不小于 5，否则应采用 Fisher 精确检验或合并类别。p 值的常见误读包括：p 值不是零假设为真的概率、统计显著不等于实际效应量大、p = 0.051 与 p = 0.049 的差异不应被夸大。报告统计量时应同时给出效应量与置信区间，避免仅以 p 值作为结论依据。相关系数的显著性检验自由度为 n-2；t 检验的自由度为 n1+n2-2。重复测量方差分析适用于同一批被试在多个时间点的测量，需满足球形假设（Mauchly 检验），不满足时用 Greenhouse-Geisser 校正。多元方差分析（MANOVA）用于多个因变量同时检验，可控制第一类错误膨胀。效应量的常用标准：d = 0.2 小效应，0.5 中效应，0.8 大效应；η² = 0.01 / 0.06 / 0.14 分别对应小 / 中 / 大。置信区间越窄说明估计越精确；样本量翻倍，置信区间宽度约缩小为原来的 1/√2 倍。回归分析中 R² 表示因变量方差被自变量解释的比例，调整 R² 对加入无关自变量有惩罚；标准化回归系数（Beta）可用于比较不同量纲自变量的相对重要性。多重共线性用 VIF 诊断，VIF > 10 视为严重。Logistic 回归报告 OR 值及其 95% 置信区间，OR = 1 表示无关联。

（统计报告规范补充材料。）在报告独立样本 t 检验时，标准写法应完整包含：两组样本量 n1、n2，两组均值 M 与标准差 SD，t 值，自由度 df，p 值，以及效应量 Cohen's d 及其 95% 置信区间。若采用 Welch 校正，需明确标注 df 为小数（如 t(158.3) = 2.20）。方差齐性检验推荐 Levene 检验或 Brown-Forsythe 检验，p > 0.05 方可认为方差齐性成立；若方差不齐，必须改用 Welch t 检验，否则第一类错误率会被高估。正态性检验推荐 Shapiro-Wilk（n ≤ 50）或 Kolmogorov-Smirnov（n > 50）；对于 n > 30 的分组，依据中心极限定理可近似认为抽样分布正态，但若原始分布严重偏态或存在极端离群值，仍建议改用 Mann-Whitney U 检验并报告秩均值与秩和。配对设计应使用配对样本 t 检验，报告均值差及其置信区间，并说明差值分布是否近似正态；不满足时改用 Wilcoxon 符号秩检验。相关分析中若两变量存在明显非线性但单调的关系，应改用 Spearman 秩相关；若数据存在大量并列秩次，需使用并列校正公式。卡方检验的报告应包含卡方值、自由度、p 值、样本量以及每个单元格的期望频数；当 2×2 表中任一期望频数小于 5 时，应使用连续性校正的卡方或 Fisher 精确检验；当理论频数过小而表格大于 2×2 时，可采用合并类别或增加样本量。多重比较必须进行校正，常用方法包括 Bonferroni（最保守）、Holm-Bonferroni（逐步法，功效更高）、Benjamini-Hochberg（控制错误发现率 FDR）；不校正会显著抬高族错误率，三次独立比较时族错误率已接近 14%。效应量报告不可省略：t 检验用 Cohen's d，方差分析用 η² 或偏 η²，相关用 r，回归用 R² 与标准化 Beta；仅报告 p 值无法说明实际意义，大样本下极小的差异也可能达到统计显著。置信区间的解释需谨慎：95% 置信区间指的是重复抽样下区间包含真值的长期频率为 95%，而非当前区间有 95% 概率包含真值。样本量估算应在研究设计阶段完成，基于预期效应量、显著性水平、检验功效（通常 0.80）与脱落率；事后功效分析（post-hoc power）与观测到的 p 值一一对应，不提供额外信息，不应作为解释不显著结果的依据。缺失数据需说明处理方式：完整案例分析、成对删除、均值填补、多重插补或最大似然；不同处理方式会影响估计的无偏性与标准误，均值填补会人为压缩方差、低估标准误，推荐多重插补。异常值处理需报告判定标准（如 ±3SD、箱线图 1.5 倍四分位距、Mahalanobis 距离）与处理方式（剔除、缩尾、稳健方法），并做敏感性分析说明结论是否稳健。测量信度报告 Cronbach's α 的同时建议报告 McDonald's ω，因为 α 假设所有题目等负荷，ω 更稳健。
PAPER>>>

输出格式（严格遵守，中文）：用不超过三句话直接回答问题，不输出寒暄。
"""

Q1 = "该研究最值得质疑的一处统计报告遗漏是什么？"
Q2 = "3.4 节的 t 检验报告有什么格式上的不完整？"


# ---------------------------------------------------------------------------
# 待测目标：models 是候选模型名（按优先级），第一个成功的即采用
# ---------------------------------------------------------------------------
TARGETS = [
    dict(
        label="智谱 BigModel",
        provider="zhipu",
        env="ZHIPU_API_KEY",
        factory=ZhipuAgent,
        models=["glm-4.7-flash"],
    ),
    dict(
        label="阿里云百炼",
        provider="dashscope",
        env="DASHSCOPE_API_KEY",
        factory=QwenAgent,
        models=["qwen3.7-flash", "qwen3.5-9b"],
    ),
    dict(
        label="DeepSeek 官方",
        provider="deepseek",
        env="DEEPSEEK_API_KEY",
        factory=DeepSeekAgent,
        # 官方模型列表实测只有这两个（deepseek-chat/reasoner 疑已退役，一并探测）
        models=["deepseek-flash", "deepseek-v4-flash",
                "deepseek-chat", "deepseek-reasoner"],
    ),
]

# 缓存测试用的 max_tokens：推理模型（deepseek-flash / qwen3.7 混合思考）
# 思考 token 也计入 completion，给太小会导致 content 为空（实测 1200 仍不够）
CACHE_MAX_TOKENS = 4096


def mask(key: str) -> str:
    return f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***"


def fmt_usage(u) -> str:
    if not u:
        return "（无 usage）"
    total = u.get("prompt_tokens", 0)
    cached = u.get("cached_tokens", 0)
    pct = (cached / total * 100) if total else 0.0
    return (f"prompt={total}, cached={cached} ({pct:.1f}%), "
            f"completion={u.get('completion_tokens', 0)}")


def list_models(agent) -> list[str]:
    """拉平台模型列表（失败返回空表，不影响后续调用测试）。"""
    try:
        resp = agent.client.models.list()
        return [m.id for m in getattr(resp, "data", [])]
    except Exception as e:  # noqa: BLE001 - 列表接口不一定支持
        print(f"    （模型列表接口不可用：{type(e).__name__}: {str(e)[:120]}）")
        return []


def run_target(t: dict) -> dict:
    """返回 {label, provider, status, model, note, cached_pct}"""
    raw = os.environ.get(t["env"], "").strip()
    print("=" * 72)
    print(f"▶ {t['label']}（{t['provider']}）")
    if not raw:
        print(f"  [SKIP] 未设置 {t['env']}")
        return dict(label=t["label"], status="SKIP", model="-", note="缺 Key")
    key = raw.split(",")[0].strip()
    print(f"  Key: {mask(key)}  环境变量: {t['env']}")

    # --- Phase A：探测可用模型 ---
    agent = None
    chosen = None
    last_err = None
    probe = t["factory"](model=t["models"][0], api_key=key,
                         default_max_tokens=64)
    ids = list_models(probe)
    if ids:
        # 只打印含关键词的模型，避免刷屏
        hits = [i for i in ids if any(k in i.lower() for k in ("glm", "qwen", "deepseek"))]
        print(f"  可用模型（{len(ids)} 个，节选）：{', '.join((hits or ids)[:12])}")

    # --- 选模型：先按候选顺序实调 ---
    for m in t["models"]:
        try:
            agent = t["factory"](model=m, api_key=key, default_max_tokens=256)
            text = agent.complete("只回答两个字：收到")
            chosen = m
            print(f"  [OK] 模型「{m}」可用 → 返回：{text.strip()[:40]}")
            break
        except AgentError as e:
            last_err = str(e)
            print(f"  [×] 模型「{m}」不可用：{str(e)[:160]}")
            agent = None
    if agent is None:
        return dict(label=t["label"], status="FAIL", model="-",
                    note=f"全部候选模型失败；最后错误：{last_err}")

    # --- Phase B：前缀缓存实证（同前缀多次调用） ---
    print(f"\n  ── 前缀缓存实证（{chosen}）──")
    prompt1 = FROZEN_PREFIX + "\n本次任务：" + Q1 + "\n"
    try:
        ans1 = agent.complete(prompt1, max_tokens=CACHE_MAX_TOKENS)
        print(f"  [调用 1] 建立缓存 → {ans1.strip()[:80]}")
    except AgentError as e:
        print(f"  [调用 1] 内容异常（思考耗尽 token，缓存仍可能已建立）："
              f"{str(e)[:110]}")
    u1 = agent.last_usage
    print(f"           usage: {fmt_usage(u1)}")

    cached = 0
    total = 0
    for attempt, (wait, q) in enumerate(
            [(4, Q2), (15, "该研究的相关分析自由度是多少？")], start=2):
        print(f"  等待 {wait}s（缓存异步写入）...")
        time.sleep(wait)
        prompt = FROZEN_PREFIX + "\n本次任务：" + q + "\n"
        try:
            ans = agent.complete(prompt, max_tokens=CACHE_MAX_TOKENS)
        except AgentError as e:
            print(f"  [调用 {attempt}] 失败：{str(e)[:120]}")
            continue
        u = agent.last_usage
        print(f"  [调用 {attempt}] 验证命中 → {ans.strip()[:80] or '（content 为空）'}")
        print(f"           usage: {fmt_usage(u)}")
        cached = (u or {}).get("cached_tokens", 0)
        total = (u or {}).get("prompt_tokens", 1) or 1
        if cached:
            break

    pct = cached / total * 100 if total else 0.0
    note = (f"缓存命中 {cached}/{total} = {pct:.1f}%" if cached
            else "未见命中（前缀未达阈值 / 异步写入更慢 / 该模型不支持）")
    print(f"  → {note}")
    return dict(label=t["label"], status="PASS", model=chosen, note=note,
                cached_pct=pct)


def main() -> int:
    print(f"冻结前缀长度：{len(FROZEN_PREFIX)} 字符")
    print()
    results = [run_target(t) for t in TARGETS]

    print()
    print("=" * 72)
    print("汇总")
    print("=" * 72)
    for r in results:
        mark = {"PASS": "✓", "SKIP": "-", "FAIL": "✗"}[r["status"]]
        print(f"  {mark} {r['label']:<16} {r['model']:<22} {r['note']}")
    ok = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n  {ok}/{len(results)} 个平台调通")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
