"""Offline read-only research tool boundaries and numerical evidence checks."""
import json
import unittest

from research_tools import ResearchTools


class ResearchToolsTest(unittest.TestCase):
    def test_head_cannot_search_or_read_late_paper(self):
        tools = ResearchTools("a" * 2000 + "LATE_SECRET")
        self.assertEqual(tools.sent_chars, 0)
        self.assertEqual(tools.execute({"tool": "search", "query": "LATE_SECRET"})["passages"], [])
        self.assertFalse(tools.execute({"tool": "read", "start": 1990, "end": 2010})["ok"])
        self.assertFalse(tools.execute({"tool": "stat_recheck", "start": 2000, "end": 2010})["ok"])
        self.assertEqual(tools.sent_chars, 0)

    def test_evidence_search_reaches_tail_and_returns_exact_offsets(self):
        text = "背景" * 2000 + "目标证据 t(40)=1.50, p<.05。" + "结尾" * 1000
        tools = ResearchTools(text, mode="evidence")
        result = tools.execute({"tool": "search", "query": "目标证据"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["passages"]), 1)
        p = result["passages"][0]
        self.assertEqual(p["text"], text[p["start"]:p["end"]])
        self.assertIn("目标证据", p["text"])
        self.assertLessEqual(len(p["text"]), 1200)
        self.assertEqual(tools.sent_chars, len(p["text"]))

    def test_search_caps_repetitions_without_scanning_every_match(self):
        tools = ResearchTools("TARGET" * 10000, mode="evidence")
        result = tools.execute({"tool": "search", "query": "TARGET"})
        self.assertLessEqual(len(result["passages"]), 4)
        self.assertLessEqual(tools.sent_chars, 4800)

    def test_read_preserves_original_whitespace_and_counts_repeated_sends(self):
        tools = ResearchTools(" \n**原文**\n末尾 ", mode="full")
        a = tools.execute({"tool": "read", "start": 0, "end": 10})
        b = tools.execute({"tool": "read", "start": 0, "end": 10})
        self.assertTrue(a["ok"])
        self.assertEqual(a["text"], " \n**原文**\n末")
        self.assertEqual(tools.sent_chars, 20)
        self.assertNotEqual(a["id"], b["id"])

    def test_budget_failure_is_atomic_and_does_not_leak_text(self):
        tools = ResearchTools("B" * 30000, mode="evidence")
        for _ in range(10):
            self.assertTrue(tools.execute({"tool": "read", "start": 0, "end": 2400})["ok"])
        result = tools.execute({"tool": "read", "start": 24000, "end": 24001})
        self.assertFalse(result["ok"])
        self.assertNotIn("text", result)
        self.assertEqual(tools.sent_chars, 24000)

    def test_bad_requests_are_serializable_and_cannot_supply_fabricated_stats(self):
        tools = ResearchTools("安全正文")
        requests = [None, [], {"tool": "exec", "code": "open('.env').read()"},
                    {"tool": "search", "query": "x", "path": ".env"},
                    {"tool": "search", "query": " "}, {"tool": "search", "query": "x" * 81},
                    {"tool": "read", "start": True, "end": 2},
                    {"tool": "read", "start": -1, "end": 2},
                    {"tool": "read", "start": 2, "end": 1},
                    {"tool": "read", "start": 0, "end": 2401},
                    {"tool": "stat_recheck", "start": 0, "end": 2, "t": 1.5}]
        for request in requests:
            with self.subTest(request=request):
                result = tools.execute(request)
                self.assertFalse(result["ok"])
                self.assertTrue(result["id"].startswith("T"))
                json.dumps(result, allow_nan=False)
        self.assertEqual(tools.sent_chars, 0)

    def test_stat_recheck_uses_only_selected_source_and_absolute_positions(self):
        prefix = "这是一段背景。\n"
        claim = "差异显著，t(40)=1.50, p<.05。"
        text = prefix + claim + "\n其它数据不在本次区间。"
        tools = ResearchTools(text)
        result = tools.execute({"tool": "stat_recheck", "start": len(prefix), "end": len(prefix + claim)})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["summary"]["decision_error"], 1)
        item = result["result"]["items"][0]
        self.assertEqual(item["pos"], text.index("t(40)"))
        self.assertAlmostEqual(item["computed_p"], .141467, places=5)
        self.assertEqual(tools.sent_chars, len(claim))
        self.assertIn("真实性", result["note"])

    def test_consistent_p_is_not_a_finding_and_missing_df_is_not_invented(self):
        text = "t(58)=2.35, p=.022。\nt=2.35, p=.022。"
        tools = ResearchTools(text)
        result = tools.execute({"tool": "stat_recheck", "start": 0, "end": len(text)})
        self.assertEqual(result["result"]["summary"]["consistent"], 1)
        self.assertEqual(result["result"]["summary"]["missing_df"], 1)
        self.assertEqual(result["result"]["summary"]["checked"], 1)

    def test_invalid_scope_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            ResearchTools("text", mode="unknown")
        with self.assertRaises(ValueError):
            ResearchTools("a" * 120001, mode="full")


if __name__ == "__main__":
    unittest.main()
