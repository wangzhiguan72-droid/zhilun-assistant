# -*- coding: utf-8 -*-
"""合成压力语料的构造自检 + 护栏边界测试（零 API 调用）。

测的是**护栏的决策边界**，不是模型准确率：
  - 豁免类必须零调用判假；
  - 作者可控类在模型确认时能判真（护栏不会把真问题一律压成存疑）；
  - **即使模型判错**（对合法文本坚持「需要修改」），护栏也不得跟着判真；
  - 信息不足时必须存疑，绝不塌缩成假。

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

import style_eval_corpus as C  # noqa: E402
import style_triage as st  # noqa: E402


def _real28_here():
    import style_eval as se
    return se.real28_available()


#: real28 是仓库外私有语料，新克隆上不存在 → 显式跳过，不静默当成通过。
needs_real28 = unittest.skipUnless(
    _real28_here(), '真实语料 real28 不在本机（仓库外私有数据），跳过')

#: 段落类型按构造事实给定，不由模型猜。
SECTION = {
    "cover-fields": "封面", "cover-english": "封面", "ref-entries": "参考文献",
    "method-passive-only": "方法", "method-passive-conn": "方法",
    "results-passive-conn": "结果", "hype-quantified": "讨论",
    "hype-stat-reported": "结果", "quoted-interview": "引语或访谈",
    "method-terms-not-platitudes": "结果", "discussion-normal-conn": "讨论",
    "limitation-honest": "讨论", "truncated-method-tail": "方法",
    "hype-unquantified": "讨论", "ai-residue": "其他", "ai-residue-subtle": "结论",
    "unbounded-promo": "讨论", "platitudes-chain": "结论",
    "redlist-unjustified": "方法", "hype-plus-promo": "方法",
    "residue-plus-platitude": "结论", "hype-in-conclusion": "结论",
    "promo-in-abstract": "其他",
    "truncated-mid-sentence": "其他", "truncated-before-numbers": "其他",
    "fragment-no-section": "其他", "truncated-results-head": "结果",
    "ambiguous-claim-cut": "其他", "truncated-quote-start": "其他",
    "truncated-table-caption": "结论",
}


def stub(case, needs, sufficient, visible=None, basis="「示例」处可见该现象"):
    payload = {"section_type": SECTION[case], "needs_revision": needs,
               "evidence_sufficient": sufficient,
               "visible": visible if visible is not None else [],
               "basis": basis}
    return lambda p, s: json.dumps(payload, ensure_ascii=False)


class ConstructionTest(unittest.TestCase):
    def test_audit_clean(self):
        self.assertEqual(C.audit(), [])

    def test_every_case_has_section(self):
        missing = [it["case"] for it in C.items() if it["case"] not in SECTION]
        self.assertEqual(missing, [], "新增条目必须同时登记段落类型")

    def test_labels_balanced(self):
        its = C.items()
        n = {k: sum(1 for i in its if i["human"] == k) for k in ("真", "假", "存疑")}
        self.assertGreaterEqual(n["真"], 8, "真类分母必须撑起来（真实语料只有 1 条）")
        self.assertGreaterEqual(min(n.values()), 5)

    def test_reasons_nonempty_and_valid(self):
        vocab = set(st.AUTHOR_CONTROLLED) | set(st.DISCIPLINARY)
        for it in C.items():
            self.assertTrue(it["reasons"], it["case"])
            self.assertTrue(set(it["reasons"]) <= vocab,
                            f'{it["case"]} 出现未知维度 {it["reasons"]}')

    def test_excerpt_matches_product_window(self):
        for it in C.items():
            self.assertLessEqual(len(it["excerpt"]), C.EXCERPT_MAX + 1)

    def test_no_real_identifiers(self):
        import privacy_gate as pg
        blob = "\n".join(t for *_, t in C.CASES)
        self.assertEqual(pg.scan_text(blob), [], "合成语料不得含真实隐私串或密钥")

    @needs_real28
    def test_separate_from_real_corpus(self):
        """合成 id 不得与真实台账 case 撞名，避免两份结果被误合并。"""
        import style_eval as se
        real = {it["case"] for it in se.load_real28()}
        syn = {it["case"] for it in C.items()}
        self.assertEqual(real & syn, set())


class GateBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.items = {it["case"]: it for it in C.items()}

    def run_case(self, case, complete):
        it = self.items[case]
        return st.triage_passage(it["reasons"], it["excerpt"], complete)

    def test_exempt_cases_need_no_model(self):
        def boom(p, s):
            raise AssertionError("豁免段落不该调模型")
        for case in ("cover-fields", "cover-english", "ref-entries",
                     "method-passive-only"):
            r = self.run_case(case, boom)
            self.assertEqual(r["verdict"], "假", case)
            self.assertFalse(r["model_called"], case)
            self.assertEqual(r["source"], "local_gate", case)

    def test_hard_positives_have_local_evidence(self):
        for it in C.items():
            if it["layer"] != "hard_positive":
                continue
            sig = st.local_signals(it["excerpt"])
            self.assertTrue(sig["present"], f'{it["case"]} 本地无证据，构造无效')

    def test_hard_positives_can_reach_true(self):
        """模型确认时护栏必须能判真——否则护栏只是把所有问题压成存疑。"""
        for it in C.items():
            if it["layer"] != "hard_positive":
                continue
            case = it["case"]
            r = self.run_case(case, stub(case, "是", "是", it["reasons"]))
            self.assertEqual(r["verdict"], "真", f'{case} → {r["source"]}: {r["reason"]}')

    def test_adversarial_model_cannot_force_true_on_negatives(self):
        """核心不退让性质：模型对合法文本坚持「需要修改」，护栏也不得判真。"""
        bad = []
        for it in C.items():
            if it["human"] != "假":
                continue
            case = it["case"]
            r = self.run_case(case, stub(case, "是", "是", it["reasons"]))
            if r["verdict"] == "真":
                bad.append(f'{case} → {r["source"]}')
        self.assertEqual(bad, [], "合法文本被误判为真：\n" + "\n".join(bad))

    def test_insufficient_never_collapses_to_false(self):
        bad = []
        for it in C.items():
            if it["layer"] != "insufficient":
                continue
            case = it["case"]
            r = self.run_case(case, stub(case, "无法判断", "否", []))
            if r["verdict"] != "存疑":
                bad.append(f'{case} → {r["verdict"]} ({r["source"]})')
        self.assertEqual(bad, [], "信息不足被塌缩：\n" + "\n".join(bad))

    def test_no_model_means_doubt_not_false(self):
        for it in C.items():
            if it["human"] == "假":
                continue
            r = st.triage_passage(it["reasons"], it["excerpt"], None)
            self.assertNotEqual(r["verdict"], "假",
                                f'{it["case"]} 关掉模型就判假 = 漏判')

    def test_quantified_hype_has_no_local_hype_evidence(self):
        for case in ("hype-quantified", "hype-stat-reported"):
            sig = st.local_signals(self.items[case]["excerpt"])
            self.assertNotIn("拔高词没有数字支撑", sig["present"], case)


if __name__ == "__main__":
    unittest.main(verbosity=2)