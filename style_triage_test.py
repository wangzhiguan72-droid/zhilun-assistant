"""风格提示护栏复核的离线回归（不调任何 API）。

夹具取自 24 路由 × 28 题双臂实验里实测的 31 条「接入后变差」样本：
15 条单项被动判真、10 条方法/结果段多项触发判真、5 条漏真、1 条判词理由矛盾。
每条退化模式在这里都有一个必须通过的断言，防止提示词或合成逻辑回退。
"""
import unittest

import style_triage as st

# 实测样本的结构复刻：C45 封面页（MiMo-V2.6-Pro 旧垂直臂 verdict=真，理由却写
# 「论文封面页，无内容段落，无可判定的风格问题」——判词与理由自相矛盾的那一条）。
# 姓名/校名/题名/学号全部合成脱敏，只保留字段族、字距与分行排版——门判的是结构不是内容。
COVER_C45 = (
    "2025 届 硕 士 学 位 论 文\n合成视角下样例群体示例观念状况与改进路径考察\n"
    "——以样例省若干院校的抽样为据\n作者姓名 测试甲\n指导教师 测试乙 教授\n"
    "学科专业 示例学\n研究方向 示例方向\n培养单位 示例学院\n"
    "学习年限 2021年9月至2024年6 月\n二〇二五年六月\n示例大学"
)
COVER_C08 = (
    "单位代码： 10000 密 级：\n硕 士 学 位 论 文\n论文题目： 合成计划样例院校学生示例能力"
    "及其改进路径考察——以若干院校抽样为据\n1000000000\n测试甲\n测试乙\n示例学\n"
    "示例二级学\n示例学硕士\n二〇二四年五月\n学 号\n姓 名\n导 师\n学 科 专 业\n研 究 方 向"
)
# 实测样本：C19 测度段，单项「被动句偏多」触发（旧垂直臂 GLM-5.3-Flash / MiMo 判真）
# 以下五段正文同样合成脱敏，不入库任何真实论文原句。保留的是检测器真正依赖的结构：
# 段落标记、拔高词后有无数字、术语型套话、截断位置——门判的是这些，不是选题内容。
METHOD_PASSIVE = (
    "3.2 变量测度\n示例能力的测度由被试在问卷中自行报告完成，题项询问其近一学年"
    "参与示例类活动的次数。此处口径限定为：该次数仅计入最高学历在读阶段的有效记录。"
)
# 实测样本：C44 讨论段，唯一人工「真」——本地检出「显著提升」无量化支撑
DISCUSSION_TRUE = (
    "本研究的分析表明，示例平台的信息公开程度与用户满意度之间存在正向关联。"
    "进一步分析发现，主动披露的影响更为稳定，而被动回应的作用在不同群体间波动较大。"
    "这一差异说明，平台治理水平的改善会显著提升用户的使用意愿，也提示后续研究需要"
    "区分披露方式加以考察。结合样例信息理论，平台与用户各自掌握的信息并不对等，"
    "披露质量因此成为影响双方判断的关键变量，后续…"
)
# 实测样本：J03 结果段，触发只有「高频过渡套话密集；被动句偏多」
RESULT_CONN_PASSIVE = (
    "4.1 描述性统计\n首先，报告了各变量的均值与标准差。其次，比较了两组被试在"
    "示例任务上的得分差异。随后，按性别与年级分组重复了上述比较。最后，汇报了"
    "各题项的分布情况，全部数据以表格形式呈现。"
)
QUOTED_INTERVIEW = (
    "他觉得示例平台的模拟练习相当有用，特别是在口语模拟环节里，能够让他反复练习"
    "做错的题目，能够在模拟过程中不断纠正自己的发音问题，最终达到比较理想的效果。"
)
# 实测样本：C44 实验设计段（第 14 题，人工「存疑」）。本地把术语表述「起到中介
# 作用」检成空泛套话——MiniMax / GPT-OSS 的护栏臂曾据此判真，方向反而变差。
METHOD_TONE_ONLY = (
    "5.2 实验设计 \n5.2.1 实验整体安排 \n"
    "预实验确认了流程可以跑通。第二，抽样框覆盖了目标群体。所用示例调查平台\n"
    "注册人数超过 300 万， 其中在校学生占比较高， 能够为本实验提供\n"
    "充足样本。 \n"
    "本节检验示例信息披露与用户满意度之间是否存在因果关联， 并且\n"
    "考察平台信任在其中是否起到中介作用。 为此， 本节选用双因子组间\n"
    "设计…"
)


def opinion(section="其他", visible=(), needs="是", sufficient="是", basis="摘录含空泛拔高"):
    return {"section_type": section, "visible": list(visible),
            "needs_revision": needs, "evidence_sufficient": sufficient, "basis": basis}


class LocalGateTest(unittest.TestCase):
    def test_cover_pages_are_exempt_without_model_call(self):
        for text in (COVER_C45, COVER_C08):
            gate = st.local_gate(text, ["对话式 AI 输出残留", "被动句偏多", "空泛套话"])
            self.assertTrue(gate["exempt"], text[:30])
            self.assertEqual(gate["category"], "封面或版权页")
            calls = []
            r = st.triage_passage(["被动句偏多"], text,
                                  lambda p, s: calls.append(1) or "{}")
            self.assertEqual(r["verdict"], "假")
            self.assertFalse(r["model_called"])
            self.assertEqual(calls, [])

    def test_single_cover_marker_does_not_exempt_real_prose(self):
        text = "本研究的学位论文写作遵循学术规范，采用问卷调查法收集数据，样本量为 307 人。"
        self.assertLess(len(st.cover_evidence(text)), 3)
        self.assertFalse(st.local_gate(text, ["空泛套话"])["exempt"])

    def test_prose_mentioning_three_cover_families_is_not_exempt(self):
        """摘要谈「学位论文/研究方向/培养单位」能凑够三族，但它是长行论述段。"""
        text = ("本研究以学位论文质量为切入点，按中图分类号归类了 307 篇文献，系统考察了研究方向"
                "与培养单位之间的匹配关系，并在此基础上显著提升了对研究生培养过程的理解。"
                "首先，研究结论有利于改善平台的服务环境。其次，本文的研究成果得到了验证。")
        self.assertGreaterEqual(len(st.cover_evidence(text)), 3)
        self.assertFalse(st._is_field_layout(text))
        self.assertFalse(st.local_gate(text, ["空泛套话", "拔高词没有数字支撑"])["exempt"])

    def test_cover_variants_from_the_matrix_still_exempt(self):
        """实测 6 条封面摘录的结构复刻（201 字符截断、含换行、字距拉开）必须仍被豁免。

        内容已合成脱敏：真实姓名/学号/校名/题名不入库。保留的是门真正依赖的东西——
        字段族数量、字距（「硕 士 学 位 论 文」）、短行清单排版、英文题名与截断位置。
        """
        excerpts = [
            "单位代码： 10000 密 级：\n硕 士 学 位 论 文\n论文题目： 合成计划样例院校学生示例能力及其改进\n路径考察——以若干院校抽样为据\n1000000000\n测试甲\n测试乙\n示例学\n示例二级学\n示例学硕士\n二〇二四年五月\n学 号\n姓 名\n导 师",
            "中图分类号： H000 学校代码： 10000\n学号： 2022000000 密级： 无\n硕 士 学 位 论 文\n合成大学生样例语使用状况抽样考察\n——以样例国样例大学为据\nA Synthetic Inquiry into Placeholder Wording among Placeholder Campus Respondents",
            "分类号 密级 学校代码： 10000\nUDC 编号 学 号： 2221000000\n硕 士 学 位 论 文\n大学生样例体育参与和样例心态的关联考察\n——以样例地区院校的抽样为据",
            "2025 届 硕 士 学 位 论 文\n合成视角下样例群体示例观念状况与\n改进路径考察\n——以样例省若干院校的抽样为据\n作者姓名 测试甲\n指导教师 测试乙 教授\n学科专业 示例学\n研究方向 示例方向\n培养单位 示例学院\n学习年限 2021年9月至2024年6 月\n二〇二五年六月\n示例大学",
            "分类号 学号 M202200000\n学校代码 1 0 0 0 0 密级\n硕士学位论文\n（学术型☑ 专业型□）\n本科生示例素养的状况抽样与改\n进路径考察——以样例大学为据\n学位申请人：测试甲\n学 科 专 业：示例学\n指 导 教 师：测试乙教授\n答 辩 日 期：2025 年 6 月 1 日",
            "单 位 代 码 10000\n学 号 2022000000\n分 类 号 J000.0\n密 级 公开\n硕士学位论文\n合成教育视角下样例院校示例教育抽样考察\n——以样例高校为据",
        ]
        for e in excerpts:
            gate = st.local_gate(e, ["对话式 AI 输出残留", "被动句偏多", "空泛套话"])
            self.assertTrue(gate["exempt"], e[:40])
            self.assertEqual(gate["category"], "封面或版权页")

    def test_method_single_passive_is_exempt(self):
        gate = st.local_gate(METHOD_PASSIVE, ["被动句偏多"])
        self.assertTrue(gate["exempt"])
        self.assertEqual(gate["category"], "方法段单项被动")

    def test_method_multi_dimension_is_not_blanket_exempt(self):
        gate = st.local_gate(METHOD_PASSIVE, ["被动句偏多", "拔高词没有数字支撑"])
        self.assertFalse(gate["exempt"])

    def test_reference_entry_is_exempt(self):
        text = ("[2] 访谈的成功取决于双方能否交流经验，"
                "而不是由访问者单方面讲述自身经历。")
        self.assertTrue(st.local_gate(text, ["被动句偏多"])["exempt"])


class DisciplinaryDimensionTest(unittest.TestCase):
    def test_passive_alone_never_survives_in_any_section(self):
        for section in ("方法", "结果", "讨论", "结论", "其他"):
            self.assertEqual(st.effective_dimensions(["被动句偏多"], section), [])

    def test_connectives_exempt_in_method_and_result_only(self):
        rs = ["高频过渡套话密集", "被动句偏多"]
        for section in ("方法", "结果", "实验程序", "测度"):
            self.assertEqual(st.effective_dimensions(rs, section), [])
        self.assertEqual(st.effective_dimensions(rs, "结论"), ["高频过渡套话密集"])

    def test_result_section_conn_passive_is_false_not_true(self):
        r = st.reconcile(opinion(section="结果", visible=["被动句偏多"], needs="是"),
                         ["高频过渡套话密集", "被动句偏多"], RESULT_CONN_PASSIVE)
        self.assertEqual(r["verdict"], "假")
        self.assertEqual(r["source"], "disciplinary")

    def test_author_controllable_dimension_survives(self):
        self.assertEqual(
            st.effective_dimensions(["被动句偏多", "拔高词没有数字支撑"], "方法"),
            ["拔高词没有数字支撑"])


class NoDegradationGuardTest(unittest.TestCase):
    """核心契约：模型单独说「真」不足以定性为真。"""

    def test_model_true_without_local_evidence_becomes_uncertain(self):
        r = st.reconcile(opinion(visible=["空泛套话"], needs="是"),
                         ["被动句偏多", "空泛套话"], METHOD_PASSIVE)
        self.assertEqual(r["verdict"], "存疑")
        self.assertEqual(r["source"], "no_local_evidence")
        self.assertIn("模型判真但本地无作者可控证据", r["guards"])

    def test_model_opinion_is_preserved_not_overwritten(self):
        op = opinion(visible=["空泛套话"], needs="是", basis="套话密集")
        r = st.reconcile(op, ["空泛套话"], METHOD_PASSIVE)
        self.assertEqual(r["model_opinion"]["needs_revision"], "是")
        self.assertEqual(r["model_opinion"]["basis"], "套话密集")

    def test_contradictory_basis_is_downgraded(self):
        r = st.reconcile(opinion(needs="是", basis="论文封面页，无内容段落，无可判定的风格问题"),
                         ["空泛套话"], DISCUSSION_TRUE)
        self.assertEqual(r["verdict"], "存疑")
        self.assertEqual(r["source"], "contradiction")
        self.assertIn("判词与理由自相矛盾", r["guards"])

    def test_model_identified_exempt_section_overrides_its_own_true(self):
        r = st.reconcile(opinion(section="封面", needs="是"),
                         ["空泛套话"], "某段正文，含显著提升等表述，样本量为 307。")
        self.assertEqual(r["verdict"], "假")
        self.assertEqual(r["source"], "section_gate")

    def test_true_requires_local_evidence_property(self):
        """无本地作者可控证据时，任何模型意见组合都不能产出「真」。"""
        text = METHOD_PASSIVE
        self.assertEqual(st.local_signals(text)["present"], [])
        for section in ("方法", "结果", "讨论", "结论", "其他"):
            for needs in ("是", "否", "无法判断"):
                for visible in ([], ["空泛套话"], ["被动句偏多"]):
                    r = st.reconcile(opinion(section, visible, needs),
                                     ["被动句偏多", "空泛套话"], text)
                    self.assertNotEqual(r["verdict"], "真", (section, needs, visible))


class MissedTrueRegressionTest(unittest.TestCase):
    """5 条漏真全在第 12 题：绝对化门槛 + 200 字窗口把不确定塌缩成「假」。"""

    def test_local_hype_plus_visible_survives_truncation(self):
        sig = st.local_signals(DISCUSSION_TRUE)
        self.assertIn("拔高词没有数字支撑", sig["present"])
        self.assertTrue(sig["truncated"])
        r = st.reconcile(opinion(section="讨论", visible=["拔高词没有数字支撑"],
                                 needs="无法判断", sufficient="否",
                                 basis="摘录被截断，量化数据不在摘录中"),
                         ["高频过渡套话密集", "拔高词没有数字支撑", "被动句偏多", "空泛套话"],
                         DISCUSSION_TRUE)
        self.assertEqual(r["verdict"], "真")
        self.assertEqual(r["source"], "local_plus_model")

    def test_model_veto_with_local_evidence_is_disagreement_not_false(self):
        r = st.reconcile(opinion(section="讨论", visible=["拔高词没有数字支撑"],
                                 needs="否", sufficient="是", basis="属正常学术论述"),
                         ["拔高词没有数字支撑"], DISCUSSION_TRUE)
        self.assertEqual(r["verdict"], "存疑")
        self.assertEqual(r["source"], "disagreement")

    def test_unconfirmed_local_evidence_stays_uncertain(self):
        r = st.reconcile(opinion(section="方法", visible=[], needs="无法判断",
                                 sufficient="否"),
                         ["被动句偏多", "空泛套话"],
                         "本研究起到中介作用，样本被随机分配到两个组别中。")
        self.assertEqual(r["verdict"], "存疑")
        self.assertEqual(r["source"], "unconfirmed")


class SoftSignalGuardTest(unittest.TestCase):
    """第 14 题回归：方法段的术语表述被检成「空泛套话」，不足以定性为真。"""

    reasons = ["被动句偏多", "空泛套话"]

    def test_method_section_tone_only_stays_uncertain(self):
        sig = st.local_signals(METHOD_TONE_ONLY)
        self.assertEqual(sig["present"], ["空泛套话"])
        for needs in ("是", "无法判断"):
            r = st.reconcile(opinion(section="方法", visible=["空泛套话"], needs=needs,
                                     sufficient="否", basis="「起到中介作用」属术语表述"),
                             self.reasons, METHOD_TONE_ONLY)
            self.assertEqual(r["verdict"], "存疑", needs)
            self.assertEqual(r["source"], "soft_signal_only")
            self.assertIn("方法/结果段仅有密度型信号", r["guards"])

    def test_hard_fact_in_method_section_still_can_be_true(self):
        text = "本实验设计显著提升了被试的反应时表现，样本被随机分配到两组。"
        self.assertIn("拔高词没有数字支撑", st.local_signals(text)["present"])
        r = st.reconcile(opinion(section="方法", visible=["拔高词没有数字支撑"], needs="是"),
                         ["被动句偏多", "拔高词没有数字支撑"], text)
        self.assertEqual(r["verdict"], "真")

    def test_discussion_section_tone_only_is_not_downgraded(self):
        text = "综上所述，本研究起到中介作用，结论得到了验证，因而也是重要的参考。"
        r = st.reconcile(opinion(section="讨论", visible=["空泛套话"], needs="是"),
                         ["空泛套话"], text)
        self.assertEqual(r["verdict"], "真")

    def test_hard_fact_set_excludes_only_tone(self):
        self.assertEqual(set(st.AUTHOR_CONTROLLED) - st.HARD_FACT_DIMENSIONS, {"空泛套话"})


class RobustnessTest(unittest.TestCase):
    def test_unparseable_output_is_uncertain_never_dropped(self):
        for raw in ("", "我觉得成立", '{"verdict":"真"}', "```json\n{}\n```"):
            r = st.triage_passage(["空泛套话"], DISCUSSION_TRUE, lambda p, s: raw)
            self.assertEqual(r["verdict"], "存疑")
            self.assertTrue(r["model_called"])
            self.assertIn("模型输出无法解析", r["guards"])

    def test_parse_picks_object_with_section_type(self):
        raw = '前置说明 {"note":"x"} 然后 {"section_type":"讨论","visible":[],"needs_revision":"否","evidence_sufficient":"是","basis":"具体"}'
        parsed = st.parse_triage(raw)
        self.assertEqual(parsed["section_type"], "讨论")

    def test_no_model_available_falls_back_to_local_gate(self):
        r = st.triage_passage(["被动句偏多"], METHOD_PASSIVE, None)
        self.assertFalse(r["model_called"])
        self.assertEqual(r["verdict"], "假")

    def test_ai_off_is_not_reported_as_unparseable_model_output(self):
        """关了 AI 却显示「模型输出无法解析」是假话——根本没调模型。"""
        r = st.triage_passage(["拔高词没有数字支撑"], DISCUSSION_TRUE, None)
        self.assertFalse(r["model_called"])
        self.assertEqual(r["source"], "local_only")
        self.assertEqual(r["verdict"], "存疑")
        self.assertNotIn("模型输出无法解析", r["guards"])
        self.assertIn("未调用模型，仅本地判定", r["guards"])

    def test_call_failure_fallback_also_says_local_only(self):
        def boom(prompt, system):
            raise RuntimeError("HTTPError 502: Bad Gateway")
        d = st.triage_batch([{"excerpt": DISCUSSION_TRUE, "score": 64,
                              "reasons": ["拔高词没有数字支撑"]}], boom)
        item = d["items"][0]
        self.assertEqual(item["source"], "local_only")
        self.assertEqual(item["verdict"], "存疑")
        self.assertTrue(item["model_called"])
        self.assertIn("502", item["error"])

    def test_unparseable_and_local_only_are_distinct_sources(self):
        reasons, text = ["拔高词没有数字支撑"], DISCUSSION_TRUE
        called = st.triage_passage(reasons, text, lambda p, s: "不是 JSON")
        silent = st.triage_passage(reasons, text, None)
        self.assertEqual(called["source"], "unparseable")
        self.assertEqual(silent["source"], "local_only")
        self.assertEqual(called["verdict"], silent["verdict"])

    def test_prompt_has_no_trigger_list_fewshot(self):
        self.assertNotIn("例1｜触发", st.STYLE_TRIAGE_SYSTEM)
        self.assertNotIn("必须与这些口径保持一致", st.STYLE_TRIAGE_SYSTEM)
        self.assertIn("现象可见", st.STYLE_TRIAGE_SYSTEM)
        self.assertIn("无法判断", st.STYLE_TRIAGE_SYSTEM)

    def test_prompt_separates_identification_from_judgement(self):
        p = st.build_triage_prompt(["被动句偏多"], METHOD_PASSIVE)
        self.assertIn("语言现象清单：被动句偏多", p)
        self.assertIn("section_type", st.STYLE_TRIAGE_SYSTEM)
        self.assertIn("needs_revision", st.STYLE_TRIAGE_SYSTEM)


class ReportGateTest(unittest.TestCase):
    def report(self, passages):
        return {"ok": True, "passages": passages, "suggestions": [
            {"priority": "P1",
             "issue": f"定位到 {len(passages)} 处「AI 风格较集中」的段落"
                      f"（最高的一处风险分 {passages[0]['score']}）",
             "action": "报告下方逐段给出了原文…"}]}

    def test_cover_passage_moves_to_exempted_with_basis(self):
        rep = st.apply_gate(self.report([
            {"excerpt": COVER_C45, "score": 71, "reasons": ["被动句偏多", "空泛套话"]},
            {"excerpt": DISCUSSION_TRUE, "score": 64,
             "reasons": ["拔高词没有数字支撑", "被动句偏多"]},
        ]))
        self.assertEqual(len(rep["passages"]), 1)
        self.assertEqual(len(rep["passages_exempted"]), 1)
        self.assertEqual(rep["passages_exempted"][0]["gate"]["category"], "封面或版权页")
        self.assertEqual(rep["gate_version"], st.GATE_VERSION)
        self.assertIn("定位到 1 处", rep["suggestions"][0]["issue"])
        self.assertIn("另有 1 处", rep["suggestions"][0]["issue"])
        self.assertEqual(rep["suggestions"][-1]["priority"], "P2")
        self.assertIn("本地豁免门", rep["suggestions"][-1]["issue"])

    def test_all_exempt_downgrades_suggestion_not_silently_empty(self):
        rep = st.apply_gate(self.report([
            {"excerpt": COVER_C08, "score": 80, "reasons": ["空泛套话"]},
            {"excerpt": METHOD_PASSIVE, "score": 55, "reasons": ["被动句偏多"]},
        ]))
        self.assertEqual(rep["passages"], [])
        self.assertEqual(len(rep["passages_exempted"]), 2)
        self.assertEqual(rep["suggestions"][0]["priority"], "P2")
        self.assertIn("全部判定不适用", rep["suggestions"][0]["issue"])

    def test_no_exemption_leaves_report_untouched(self):
        passages = [{"excerpt": DISCUSSION_TRUE, "score": 64,
                     "reasons": ["拔高词没有数字支撑"]}]
        rep = st.apply_gate(self.report(passages))
        self.assertEqual(rep["passages"], passages)
        self.assertEqual(rep["passages_exempted"], [])
        self.assertEqual(len(rep["suggestions"]), 1)

    def test_gate_version_stamped_even_when_nothing_exempted(self):
        """没豁免也要盖戳，否则分不清「门判了没豁免」和「这版根本没门」。"""
        rep = st.apply_gate(self.report([
            {"excerpt": DISCUSSION_TRUE, "score": 64, "reasons": ["拔高词没有数字支撑"]}]))
        self.assertEqual(rep["gate_version"], st.GATE_VERSION)
        self.assertEqual(st.apply_gate({"ok": True})["gate_version"], st.GATE_VERSION)

    def test_empty_report_is_safe(self):
        rep = st.apply_gate({"ok": True})
        self.assertEqual(rep["passages"], [])
        self.assertEqual(rep["passages_exempted"], [])


class BatchTest(unittest.TestCase):
    passages = [
        {"excerpt": COVER_C45, "score": 71, "reasons": ["被动句偏多"]},
        {"excerpt": DISCUSSION_TRUE, "score": 64, "reasons": ["拔高词没有数字支撑"]},
    ]

    def test_local_only_batch_makes_zero_calls(self):
        d = st.triage_batch(self.passages, None)
        self.assertEqual(d["model_calls"], 0)
        self.assertEqual(len(d["items"]), 2)
        self.assertEqual(d["items"][0]["verdict"], "假")

    def test_exempt_item_skips_model_even_when_available(self):
        calls = []
        d = st.triage_batch(self.passages, lambda p, s: calls.append(p) or "{}")
        self.assertEqual(d["model_calls"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("显著提升", calls[0])
        self.assertFalse(d["items"][0]["model_called"])

    def test_batch_keeps_excerpt_reasons_score_for_rendering(self):
        d = st.triage_batch(self.passages, None)
        self.assertEqual(d["items"][1]["excerpt"], DISCUSSION_TRUE)
        self.assertEqual(d["items"][1]["reasons"], ["拔高词没有数字支撑"])
        self.assertEqual(d["items"][1]["score"], 64)

    def test_model_failure_degrades_one_item_never_drops_it(self):
        def boom(prompt, system):
            raise RuntimeError("401 invalid key sk-SECRET-TOKEN-1234567890")
        d = st.triage_batch(self.passages, boom)
        self.assertEqual(len(d["items"]), 2)
        self.assertEqual(d["model_calls"], 1)
        self.assertEqual(len(d["errors"]), 1)
        self.assertNotIn("sk-SECRET-TOKEN", d["items"][1]["error"])
        self.assertIn(d["items"][1]["verdict"], ("真", "假", "存疑"))

    def test_total_model_outage_equals_local_only_result(self):
        """实测：Gemini-3.1 通道整条 404，护栏臂仍 28/28 出判词（纯本地兜底）。"""
        def dead(prompt, system):
            raise RuntimeError("HTTPError 404: Not Found")
        outage = st.triage_batch(self.passages, dead)
        local = st.triage_batch(self.passages, None)
        self.assertEqual([i["verdict"] for i in outage["items"]],
                         [i["verdict"] for i in local["items"]])
        self.assertEqual(len(outage["items"]), len(self.passages))
        self.assertTrue(all(i["verdict"] in ("真", "假", "存疑") for i in outage["items"]))

    def test_empty_batch(self):
        d = st.triage_batch([], None)
        self.assertTrue(d["ok"])
        self.assertEqual(d["items"], [])
        self.assertEqual(d["model_calls"], 0)


class DimensionContractTest(unittest.TestCase):
    def test_tuples_cover_every_label_ai_audit_can_emit(self):
        """标签漂移会让某个维度既不算作者可控也不算学科惯例，从而静默漏判。"""
        from ai_audit import _passage_reasons
        keys = ("residue", "conn", "hype", "generic", "passive", "tone", "redlist")
        labels = set(_passage_reasons({k: 1 for k in keys}))
        self.assertEqual(len(labels), 7)
        covered = set(st.AUTHOR_CONTROLLED) | set(st.DISCIPLINARY)
        self.assertEqual(labels, covered)
        self.assertFalse(set(st.AUTHOR_CONTROLLED) & set(st.DISCIPLINARY))

    def test_local_signal_keys_are_exactly_author_controlled(self):
        self.assertEqual(set(st.local_signals("")["hits"]), set(st.AUTHOR_CONTROLLED))


class OldVersionPreservedTest(unittest.TestCase):
    """新版本另起，旧版本保留：统计审计链路的提示词资产不得被本次改动触碰。"""

    def test_paper_check_system_unchanged(self):
        from agents.prompts import PAPER_CHECK_SYSTEM, _DOMAIN_CALIBRATION
        self.assertIn(_DOMAIN_CALIBRATION, PAPER_CHECK_SYSTEM)
        self.assertIn("方法/实验程序段的被动语态是学科惯例", PAPER_CHECK_SYSTEM)
        self.assertIn("你是论文统计部分的审稿专家", PAPER_CHECK_SYSTEM)

    def test_llm_audit_prompt_version_not_reset(self):
        import llm_audit
        self.assertTrue(llm_audit.PROMPT_VERSION.startswith("v"))


if __name__ == "__main__":
    unittest.main()