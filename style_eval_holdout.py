# -*- coding: utf-8 -*-
"""风格评测的**留出集**（holdout）：与 synthetic / real28 三份分开报告，绝不合并。

为什么需要它（这是本语料存在的唯一理由）：
    v2.62 的两条判定规则——
      v3 `soft_signal_cleared`：方法/结果段 + 完整摘录 + 仅密度型信号 + 模型放行 → 假
      v4 `soft_signal_truncated`：截断摘录 + 仅密度型信号 → 存疑（不得判真）
    ——是从 synthetic 那 30 条里逐条归因推导出来的，又用同一批条目验证。
    **推导集与验证集重合，等于把参数调到了测试集上**，分数再高也不能说明泛化。

    本语料的条目全部新写：机制与 synthetic 对应，但选题、措辞、句式、截断位置
    都不同，且**在规则定稿之后才写**。规则没有为这里的任何一条让路。
    如果护栏在留出集上仍然胜过旧垂直臂，才说明收窄的是机制而不是记住了答案。

标签口径与 synthetic 完全一致：
    来自「这段文字是怎么造出来的」，不是「读起来像不像 AI」。
    构造依据逐条写在 why 里。这不是真实世界准确率基准。

检测链路与产品口径完全复用 style_eval_corpus：
    ai_audit._passage_score + _passage_reasons 在**全文**上算 reasons，
    excerpt 按 EXCERPT_MAX=200 截断。所以 reasons 可能含窗口外信号，与真实报告一致。

case id 一律加 `h-` 前缀，避免与 synthetic / 真实台账撞名被误合并。
全部文本为合成，不含任何真实学号、单位代码、姓名或单位名。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import ai_audit as A  # noqa: E402
from style_eval_corpus import EXCERPT_MAX  # noqa: E402  口径单一真源，不另立常量

#: (id, kind, expect, why, text)
CASES: list[tuple[str, str, str, str, str]] = [
    # ---------------- 假：豁免类 ----------------
    ("h-cover-statement", "hard_negative", "假",
     "版权页字段清单：多族字段 + 每行短，另有授权声明触发被动命中。"
     "不是作者论述，无风格可判。",
     "密级：公开\n分类号：G434\n单位代码：10000\n学号：2024000000\n"
     "硕士学位论文\n论文题目：示例平台使用行为研究\n学位申请人：示例甲\n"
     "指导教师：示例乙 教授\n学科专业：示例教育学\n培养单位：示例教育学院\n"
     "答辩日期：二〇二六年五月\n本论文被授权由示例大学图书馆收录与检索。"),

    ("h-ref-list", "hard_negative", "假",
     "参考文献条目：格式特征命中，不是作者文风。",
     "[1] 示例甲. 在线学习投入的测量偏差[J]. 示例教育研究, 2024, 31(2): 88-101.\n"
     "[2] 示例乙, 示例丙. 平台治理与用户信任[M]. 示例出版社, 2023: 55-79.\n"
     "[3] 示例丁. 问卷回收率被高估的几种情形[J]. 示例方法学刊, 2025, 9(1): 12-27."),

    ("h-method-passive-single", "hard_negative", "假",
     "方法段单项被动：中文方法学写作天然高频，单项不构成风格问题。",
     "3.1 数据收集\n访谈录音被逐段转写为文本。转写稿被两名编码员独立阅读，"
     "分歧条目被提交第三人裁定。编码结果被录入数据库，原始录音被单独归档。"),

    ("h-method-passive-conn-complete", "hard_negative", "假",
     "**v3 正例**：方法段被动+过渡词+套话，摘录完整。唯一作者可控命中是"
     "密度型的「空泛套话」，模型放行时应判假，不得降级成存疑。",
     "此外，被试被按年级分成三组。进一步地，每组内的座位被随机打乱。"
     "与此同时，主试被要求保持同一套指导语。值得注意的是，练习时长被统一记录。"
     "由此可见，实验流程被完整留痕，便于事后核查。"),

    ("h-results-terms-complete", "hard_negative", "假",
     "**v3 正例**：结果段术语表述形似套话（「起到调节作用」），摘录完整。"
     "密度型信号在结果段不足以撑起两说并列。",
     "4.2 回归结果\n检验结果表明，家庭支持在投入与成绩之间起到调节作用，"
     "交互项系数被报告为 0.18（p=0.021）。此外，分组斜率差异被检验为显著，"
     "简单斜率被一并列于表 5。"),

    ("h-hype-with-numbers", "hard_negative", "假",
     "拔高词后窗口内有数字：有量化支撑就不是拔高无据。",
     "干预后实验组平均分显著提升 9.6 分（t=2.87，p<0.01），"
     "优秀率大幅提高至 41.3%。此外，后测的组内相关系数达到 0.72。"),

    ("h-quote-interview", "hard_negative", "假",
     "直接引语：受访者原话不是作者文风，引号内的套话不该记到作者头上。",
     "一位受访学生谈道：「说实话这个平台具有重要意义，对我帮助很大，"
     "总体来说我还是愿意继续用的。」另有三位受访者表达了相近看法，此处不再引述。"),

    ("h-limitation-bounded", "hard_negative", "假",
     "诚实的局限说明：明确写出边界与未验证范围，是无边界推广的反面。",
     "本研究只覆盖两所城区中学的高一学生，结论不宜外推到职高与县域学校。"
     "此外，追踪期只有一个学期，长期效应被留待后续研究检验。"),

    ("h-discussion-single-conn", "hard_negative", "假",
     "讨论段单个过渡词承接具体结论：不构成套话密集。",
     "相比之下，高投入组的回落幅度小于低投入组，这与自我决定理论的预测方向一致。"
     "该差异在控制基线成绩后仍然保留，系数为 0.14。"),

    # ---------------- 真：作者可控类 ----------------
    ("h-hype-no-numbers", "hard_positive", "真",
     "拔高词后无任何数字：作者可控，需要补量化或删拔高。",
     "本方案显著改善了学生的自主学习能力，有效解决了长期存在的动机不足问题。"
     "此外，课堂改革大幅提高了参与水平，对区域教学质量提升具有重要意义。"),

    ("h-ai-residue", "hard_positive", "真",
     "对话式 AI 输出残留：整句是助手口吻，必须删。最强信号。",
     "好的，下面为您梳理本文的研究不足部分。本研究围绕平台使用行为展开讨论。"
     "希望以上整理对您有帮助。如果您需要补充实证细节，可以随时告诉我。"),

    ("h-unbounded-generalize", "hard_positive", "真",
     "无边界推广：把单一样本的结论推到所有场景，作者可控。"
     "措辞对准 ai_audit._GENERIC_PROMO 的实际词表，不靠自造近义短语。",
     "该模式可推广到各个学段与各类学校，广泛应用于城乡不同背景的教育场景，"
     "普适性强，在任何地区场景都能取得同样效果，值得全面复制。"),

    ("h-platitude-chain-conclusion", "hard_positive", "真",
     "**v4 完整侧**：结论段整段空话连发、摘录完整。删掉这些句子读者对结论的"
     "判断不变。密度型信号在完整摘录上必须仍能定性为真，否则该维度再无正例。",
     "综上所述，本研究具有重要意义，兼具理论价值与实践价值，"
     "为后续工作提供了有力支撑，奠定了坚实基础。总体来看，本研究填补了相关空白，"
     "取得了显著成果，达到了预期目标。"),

    ("h-platitude-chain-discussion", "hard_positive", "真",
     "**v4 完整侧**：讨论段空话连发、摘录完整。与上一条同机制、不同段落类型，"
     "检验收窄不是只认「结论」这一个段落名。措辞取自 tone_guide 实际词表，"
     "但与结论段那条用不同的套话组合，避免两条变成同一句话的复制。",
     "本文的发现具有重要意义，在一定程度上弥补了既有文献对使用行为测量的不足，"
     "为未来实践指明了方向，也为后续工作提供了有力支撑。"
     "这一分析奠定了良好基础，其参考价值值得广泛借鉴。"),

    ("h-redlist-unjustified", "hard_positive", "真",
     "高风险模型未说明适配性：点名模型却不交代为什么适用于本数据。",
     "本研究采用 XGBoost 与 LSTM 对学业成绩进行预测，二者均为当前最先进的模型，"
     "能够充分捕捉数据中的复杂关系，因而被选为本文的核心方法。"),

    ("h-hype-truncated", "hard_positive", "真",
     "**硬事实 + 截断**：拔高词无量化在窗口内就能判定，截断不构成否定理由。"
     "与 real28 唯一人工「真」同机制，检验 v4 不会误伤硬事实正例。",
     "本研究的干预显著提升了学生的综合素养，有效解决了长期存在的城乡差距问题，"
     "对区域教育公平具有重要意义。此外，教学模式改革大幅提高了"),

    ("h-residue-truncated", "hard_positive", "真",
     "**硬事实 + 截断**：AI 残留原句在窗口内可见，属窗口内可判定事实。",
     "好的，以下是为您整理的讨论部分。本研究围绕平台使用行为展开分析，"
     "希望以上内容对您有帮助。如果您需要进一步补充"),

    # ---------------- 存疑：窗口内信息不足 ----------------
    ("h-cut-before-table", "insufficient", "存疑",
     "截断点落在数据表之前：支撑数据在窗口外，无法定性。",
     "与此同时，稳健性检验的结果需要单独交代。值得注意的是，"
     "更换聚类层级后系数符号保持一致，具体数值见下表"),

    ("h-cut-mid-claim", "insufficient", "存疑",
     "主张被截断在一半：看不出作者是否给了边界或量化。",
     "此外，该结论在抽样框覆盖的范围内成立，但在更一般的总体中"),

    ("h-fragment-unknown-section", "insufficient", "存疑",
     "孤立片段：判不出段落类型，也没有完整的可判定事实。",
     "由此可见，这一设定方式的合理性还需要更多证据支持。相比之下，"
     "另一种加权方案下的表现"),

    ("h-cut-method-tail-density", "insufficient", "存疑",
     "**v3 截断侧**：方法段尾部被切，唯一命中是密度型套话。"
     "与 h-method-passive-conn-complete 只差收尾——看不全就不许放行成「假」。",
     "此外，被试被按年级分成三组。进一步地，每组内的座位被随机打乱。"
     "与此同时，主试被要求保持同一套指导语。值得注意的是，练习时长被统一记录，便于"),

    ("h-cut-results-density", "insufficient", "存疑",
     "**v4 正例**：结果段开头被切，只剩过渡词与指代，没有结果本身。"
     "密度型信号 + 截断 → 存疑，不得判真。",
     "可以看出，各组之间的差异需要结合效应量一并解读。值得注意的是，"),

    ("h-cut-conclusion-density", "insufficient", "存疑",
     "**v4 正例**：结论段只剩指代、切在过渡词后。与 h-platitude-chain-conclusion"
     "同段落类型但摘录不完整，检验 v4 判的是截断而不是段落名。",
     "综上所述，表 6 给出的结果支持了前文的判断，也印证了本文的基本立场。进一步地，"),

    ("h-cut-quote-unclosed", "insufficient", "存疑",
     "引语起始被截、引号未闭合：分不清是受访者原话还是作者转述。",
     "访谈中一位教师提到：「我们这边的情况比较复杂，说实话"),
]


def _reasons(text: str) -> list[str]:
    _, hit = A._passage_score(text)
    return A._passage_reasons(hit)


def _excerpt(text: str) -> str:
    return text if len(text) <= EXCERPT_MAX else text[:EXCERPT_MAX] + "…"


def items() -> list[dict]:
    """字段与 load_real28 / style_eval_corpus.items 完全一致，另附构造依据。"""
    out = []
    for i, (cid, kind, expect, why, text) in enumerate(CASES, 1):
        out.append({"序号": i, "case": cid, "layer": kind, "human": expect,
                    "reasons": _reasons(text), "excerpt": _excerpt(text),
                    "construction": why})
    return out


def audit() -> list[str]:
    """构造自检。除 synthetic 那三条外，另加留出集特有的两条。"""
    problems = []
    its = items()
    for it in its:
        if not it["reasons"]:
            problems.append(f'{it["case"]}：零命中，不会出现在真实报告里，构造无效')
        if it["human"] not in ("真", "假", "存疑"):
            problems.append(f'{it["case"]}：标签非法 {it["human"]!r}')
        if len(it["excerpt"]) > EXCERPT_MAX + 1:
            problems.append(f'{it["case"]}：摘录超出产品口径')
    counts = {k: sum(1 for it in its if it["human"] == k) for k in ("真", "假", "存疑")}
    if counts["真"] < 8:
        problems.append(f'真类只有 {counts["真"]} 条，分母太小')
    if min(counts.values()) < 5:
        problems.append(f"标签分布过于失衡：{counts}")
    ids = [it["case"] for it in its]
    if len(set(ids)) != len(ids):
        problems.append("case id 有重复")
    if not all(i.startswith("h-") for i in ids):
        problems.append("留出集 case id 必须加 h- 前缀，避免与另两份语料撞名")
    import style_eval_corpus as syn
    if set(ids) & {it["case"] for it in syn.items()}:
        problems.append("留出集与 synthetic 存在同名 case，会被误合并")
    return problems


if __name__ == "__main__":
    import style_triage as st
    for it in items():
        sig = st.local_signals(it["excerpt"])
        print(f'{it["序号"]:>2} {it["human"]}  {it["case"]:<32} '
              f'截断={str(sig["truncated"]):<5} 本地={sig["present"] or "[]"}')
    probs = audit()
    counts = {k: sum(1 for it in items() if it["human"] == k) for k in ("真", "假", "存疑")}
    print(f"\n合计 {len(CASES)} 条，标签分布 {counts}")
    if probs:
        print("\n[FAIL] 构造自检：")
        for p in probs:
            print("  -", p)
        sys.exit(1)
    print("[PASS] 构造自检通过")