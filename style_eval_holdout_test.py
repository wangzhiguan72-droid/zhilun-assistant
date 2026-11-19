# -*- coding: utf-8 -*-
"""留出集的构造自检 + 规则泛化边界测试（零 API 调用）。

与 style_eval_corpus_test 的分工：
    那份测的是**推导集**上的决策边界（规则就是从它归因出来的）。
    这份测的是**留出集**——条目在两条新规则定稿之后才写，措辞与截断位置全不同。
    规则没有为这里任何一条让路；这里过了，才说明收窄的是机制而不是记住了答案。

最有力的一组是「成对条目」：同一段文字只差收尾三个字，
完整的必须放行、截断的必须挂起。规则若只是记住了推导集的答案，这里就会露馅。

标签是构造式的，本文件不声称任何真实世界准确率。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

import style_eval_holdout as H  # noqa: E402
import style_triage as st  # noqa: E402


def _real28_here():
    import style_eval as se
    return se.real28_available()


#: real28 是仓库外私有语料，新克隆上不存在 → 显式跳过，不静默当成通过。
needs_real28 = unittest.skipUnless(
    _real28_here(), '真实语料 real28 不在本机（仓库外私有数据），跳过')

#: 段落类型按构造事实给定，不由模型猜。
SECTION = {
    "h-cover-statement": "封面", "h-ref-list": "参考文献",
    "h-method-passive-single": "方法",
    "h-method-passive-conn-complete": "方法",
    "h-results-terms-complete": "结果", "h-hype-with-numbers": "结果",
    "h-quote-interview": "引语或访谈", "h-limitation-bounded": "讨论",
    "h-discussion-single-conn": "讨论", "h-hype-no-numbers": "讨论",
    "h-ai-residue": "其他", "h-unbounded-generalize": "讨论",
    "h-platitude-chain-conclusion": "结论",
    "h-platitude-chain-discussion": "讨论",
    "h-redlist-unjustified": "方法", "h-hype-truncated": "讨论",
    "h-residue-truncated": "其他", "h-cut-before-table": "其他",
    "h-cut-mid-claim": "其他", "h-fragment-unknown-section": "其他",
    "h-cut-method-tail-density": "方法", "h-cut-results-density": "结果",
    "h-cut-conclusion-density": "结论",
    # 原先按构造意图给「其他」（引号没闭合，构造者以为模型认不出段落）。
    # 实测 8 个模型 × 5 重复共 39 个有效单元**全部**识别为「引语或访谈」——
    # 它们看得见「访谈中一位教师提到：「。一个错误的桩假设把真实缺陷挡在测试之外：
    # 线上 39/39 判假，离线却全绿。桩必须按实测模型行为给，不按构造者的期望给。
    "h-cut-quote-unclosed": "引语或访谈",
}


def stub(case, needs, sufficient, visible=None, basis="「示例」处可见该现象"):
    payload = {"section_type": SECTION[case], "needs_revision": needs,
               "evidence_sufficient": sufficient,
               "visible": visible if visible is not None else [],
               "basis": basis}
    return lambda p, s: json.dumps(payload, ensure_ascii=False)


class ConstructionTest(unittest.TestCase):
    def test_audit_clean(self):
        self.assertEqual(H.audit(), [])

    def test_every_case_has_section(self):
        missing = [it["case"] for it in H.items() if it["case"] not in SECTION]
        self.assertEqual(missing, [], "新增条目必须同时登记段落类型")

    def test_labels_balanced(self):
        n = {k: sum(1 for i in H.items() if i["human"] == k)
             for k in ("真", "假", "存疑")}
        self.assertGreaterEqual(n["真"], 8, "真类分母必须撑起来")
        self.assertGreaterEqual(min(n.values()), 5)

    def test_reasons_nonempty_and_valid(self):
        vocab = set(st.AUTHOR_CONTROLLED) | set(st.DISCIPLINARY)
        for it in H.items():
            self.assertTrue(it["reasons"], it["case"])
            self.assertTrue(set(it["reasons"]) <= vocab,
                            f'{it["case"]} 出现未知维度 {it["reasons"]}')

    def test_excerpt_matches_product_window(self):
        for it in H.items():
            self.assertLessEqual(len(it["excerpt"]), H.EXCERPT_MAX + 1, it["case"])

    def test_no_real_identifiers(self):
        import privacy_gate as pg
        blob = "\n".join(t for *_, t in H.CASES)
        self.assertEqual(pg.scan_text(blob), [], "留出集不得含真实隐私串或密钥")

    def test_disjoint_from_synthetic(self):
        """holdout 与合成语料的 case id 不得相交，否则结果会被误合并。"""
        import style_eval_corpus as syn
        ho = {it["case"] for it in H.items()}
        self.assertEqual(ho & {it["case"] for it in syn.items()}, set())

    @needs_real28
    def test_disjoint_from_real28(self):
        """holdout 与真实语料的 case id 不得相交。real28 在仓库外，缺则跳过。"""
        import style_eval as se
        ho = {it["case"] for it in H.items()}
        self.assertEqual(ho & {it["case"] for it in se.load_real28()}, set())

    def test_registered_in_eval_corpora(self):
        import style_eval as se
        self.assertIn("holdout", se.CORPORA)
        self.assertEqual(len(se.CORPORA["holdout"]()), len(H.CASES))


class GateBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.items = {it["case"]: it for it in H.items()}

    def run_case(self, case, complete):
        it = self.items[case]
        return st.triage_passage(it["reasons"], it["excerpt"], complete)

    def test_exempt_cases_need_no_model(self):
        def boom(p, s):
            raise AssertionError("豁免段落不该调模型")
        for case in ("h-cover-statement", "h-ref-list", "h-method-passive-single"):
            r = self.run_case(case, boom)
            self.assertEqual(r["verdict"], "假", case)
            self.assertFalse(r["model_called"], case)
            self.assertEqual(r["source"], "local_gate", case)

    def test_hard_positives_have_local_evidence(self):
        for it in H.items():
            if it["layer"] != "hard_positive":
                continue
            self.assertTrue(st.local_signals(it["excerpt"])["present"],
                            f'{it["case"]} 本地无证据，构造无效')

    def test_hard_positives_can_reach_true(self):
        for it in H.items():
            if it["layer"] != "hard_positive":
                continue
            case = it["case"]
            r = self.run_case(case, stub(case, "是", "是", it["reasons"]))
            self.assertEqual(r["verdict"], "真", f'{case} → {r["source"]}')

    def test_adversarial_model_cannot_force_true_on_negatives(self):
        """核心不退让性质：模型对合法文本坚持「需要修改」，护栏也不得判真。"""
        bad = []
        for it in H.items():
            if it["human"] != "假":
                continue
            case = it["case"]
            r = self.run_case(case, stub(case, "是", "是", it["reasons"]))
            if r["verdict"] == "真":
                bad.append(f'{case} → {r["source"]}')
        self.assertEqual(bad, [], "合法文本被误判为真：\n" + "\n".join(bad))

    def test_insufficient_never_collapses_to_false(self):
        bad = []
        for it in H.items():
            if it["layer"] != "insufficient":
                continue
            case = it["case"]
            r = self.run_case(case, stub(case, "无法判断", "否", []))
            if r["verdict"] != "存疑":
                bad.append(f'{case} → {r["verdict"]} ({r["source"]})')
        self.assertEqual(bad, [], "信息不足塌缩成非存疑：\n" + "\n".join(bad))

    def test_unclosed_quote_never_exempted_in_any_corpus(self):
        """跨语料不变量：引号未闭合的条目，模型识别为引语/访谈也不得豁免成「假」。

        只约束「未闭合引号」这个窗口内可判定的事实，**不**约束「截断就降级」：
        后者会把真实语料 #2（无引号的第三人称转述、人工假）37 个正确判假改成存疑，
        假族覆盖 −9.64pt。反例见 style_triage_test.QuoteBoundaryTest。
        """
        import style_eval_corpus as C
        found, bad = set(), []
        for name, items in (("holdout", H.items()), ("synthetic", C.items())):
            for it in items:
                if not st._unclosed_quote(it["excerpt"]):
                    continue
                found.add((name, it["case"]))
                payload = {"section_type": "引语或访谈", "needs_revision": "否",
                           "evidence_sufficient": "是", "visible": it["reasons"],
                           "basis": "「示例」处可见该现象"}
                r = st.triage_passage(
                    it["reasons"], it["excerpt"],
                    lambda p, s, _p=payload: json.dumps(_p, ensure_ascii=False))
                if r["verdict"] == "假":
                    bad.append(f'{name}/{it["case"]} → {r["source"]}')
        # 防止语料改动后本测试静默变成空转
        self.assertIn(("holdout", "h-cut-quote-unclosed"), found)
        self.assertIn(("synthetic", "truncated-quote-start"), found)
        self.assertEqual(bad, [], "未闭合引语被豁免成假：\n" + "\n".join(bad))

    def test_no_model_means_doubt_not_false(self):
        for it in H.items():
            if it["layer"] != "insufficient":
                continue
            r = self.run_case(it["case"], None)
            self.assertEqual(r["verdict"], "存疑", it["case"])
            self.assertFalse(r["model_called"], it["case"])


class RuleGeneralizationTest(unittest.TestCase):
    """两条新规则在**新写的**条目上是否仍按机制生效。"""

    def setUp(self):
        self.items = {it["case"]: it for it in H.items()}

    def run_case(self, case, needs, sufficient="是", visible=None):
        it = self.items[case]
        return self.run_raw(case, stub(case, needs, sufficient,
                                       it["reasons"] if visible is None else visible))

    def run_raw(self, case, complete):
        it = self.items[case]
        return st.triage_passage(it["reasons"], it["excerpt"], complete)

    # ---- v3：完整摘录 + 方法/结果段 + 仅密度型 + 模型放行 → 假 ----
    def test_v3_complete_side_clears_to_false(self):
        for case in ("h-method-passive-conn-complete", "h-results-terms-complete"):
            sig = st.local_signals(self.items[case]["excerpt"])
            self.assertFalse(sig["truncated"], case)
            self.assertEqual(sig["present"], ["空泛套话"], case)
            r = self.run_case(case, "否")
            self.assertEqual(r["verdict"], "假", case)
            self.assertEqual(r["source"], "soft_signal_cleared", case)

    def test_v3_truncated_side_never_clears(self):
        case = "h-cut-method-tail-density"
        self.assertTrue(st.local_signals(self.items[case]["excerpt"])["truncated"])
        r = self.run_case(case, "否")
        self.assertEqual(r["verdict"], "存疑", case)
        self.assertNotEqual(r["source"], "soft_signal_cleared", case)

    def test_v3_pair_differs_only_by_ending(self):
        """成对条目：同一段文字只差收尾，判定必须一边放行一边挂起。"""
        full = self.items["h-method-passive-conn-complete"]["excerpt"]
        cut = self.items["h-cut-method-tail-density"]["excerpt"]
        self.assertTrue(full.startswith(cut[:40]), "两条必须是同一段文字")
        self.assertNotEqual(full, cut)
        a = self.run_case("h-method-passive-conn-complete", "否")
        b = self.run_case("h-cut-method-tail-density", "否")
        self.assertEqual((a["verdict"], b["verdict"]), ("假", "存疑"))

    # ---- v4：截断 + 仅密度型 → 存疑；完整空话连发 → 真 ----
    def test_v4_truncated_side_stays_uncertain(self):
        for case in ("h-cut-conclusion-density", "h-fragment-unknown-section",
                     "h-cut-results-density", "h-cut-before-table"):
            r = self.run_case(case, "是")
            self.assertEqual(r["verdict"], "存疑", case)
            self.assertIn(r["source"], ("soft_signal_truncated", "soft_signal_only"),
                          case)

    def test_v4_complete_side_still_true(self):
        for case in ("h-platitude-chain-conclusion", "h-platitude-chain-discussion"):
            sig = st.local_signals(self.items[case]["excerpt"])
            self.assertFalse(sig["truncated"], case)
            self.assertEqual(sig["present"], ["空泛套话"], case)
            r = self.run_case(case, "是")
            self.assertEqual(r["verdict"], "真", case)
            self.assertEqual(r["source"], "local_plus_model", case)

    def test_v4_pair_differs_only_by_completeness(self):
        """结论段：完整空话连发判真，只剩指代的截断片段判存疑。
        规则若按段落名一刀切，这一对就会同时判错。"""
        a = self.run_case("h-platitude-chain-conclusion", "是")
        b = self.run_case("h-cut-conclusion-density", "是")
        self.assertEqual(SECTION["h-platitude-chain-conclusion"],
                         SECTION["h-cut-conclusion-density"])
        self.assertEqual((a["verdict"], b["verdict"]), ("真", "存疑"))

    def test_hard_fact_survives_truncation(self):
        """截断收窄只针对密度型信号，绝不能误伤窗口内可判定的硬事实。"""
        for case in ("h-hype-truncated", "h-residue-truncated"):
            sig = st.local_signals(self.items[case]["excerpt"])
            self.assertTrue(sig["truncated"], case)
            self.assertTrue(set(sig["present"]) & st.HARD_FACT_DIMENSIONS, case)
            r = self.run_case(case, "是")
            self.assertEqual(r["verdict"], "真", case)

    def test_truncation_detector_on_holdout(self):
        """留出集的截断例全部无「…」标记——只认省略号的旧检测器会全漏。"""
        marked = unmarked = 0
        for it in H.items():
            trunc = st.local_signals(it["excerpt"])["truncated"]
            if it["layer"] == "insufficient" or it["case"].endswith("truncated"):
                self.assertTrue(trunc, f'{it["case"]} 应判截断')
                if it["excerpt"].rstrip().endswith(("…", "...")):
                    marked += 1
                else:
                    unmarked += 1
        self.assertGreaterEqual(unmarked, 6, "留出集必须覆盖无标记截断")
        self.assertEqual(marked, 0, "本语料的截断全靠句法位置，不靠省略号")


if __name__ == "__main__":
    unittest.main(verbosity=2)