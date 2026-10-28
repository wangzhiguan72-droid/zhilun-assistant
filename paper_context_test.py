"""Context coverage and permission boundaries, independent of model quality."""
import hashlib
import unittest

from paper_context import build_paper_context


class PaperContextTest(unittest.TestCase):
    def test_default_head_keeps_old_send_limit_but_hashes_whole_input(self):
        first = "摘要内容" * 700
        a = build_paper_context(first + "结果A")
        b = build_paper_context(first + "结果B")
        self.assertEqual(a["included_chars"], 2000)
        self.assertEqual(a["passages"][0]["text"], first[:2000])
        self.assertNotEqual(a["text_hash"], b["text_hash"])

    def test_short_evidence_and_full_preserve_exact_input(self):
        text = "\n摘要\n  p < .05\n方法与结果 **n=41**\n"
        for mode in ("evidence", "full"):
            c = build_paper_context(text, mode=mode)
            self.assertTrue(c["complete"])
            self.assertEqual(c["passages"][0]["text"], text)
            self.assertEqual(c["text_hash"], hashlib.sha256(text.encode()).hexdigest())

    def test_late_results_directive_and_rule_evidence_are_reachable(self):
        text = ("摘要\n" + "背景材料。" * 6000 + "\n方法\n随机分配至两组。\n"
                + "过程记录。" * 3000 + "\n结果\n晚期结果 n=37, p=.043。\n"
                + "讨论材料。" * 3000 + "\n结论\n用户关注的异质性没有得到解释。\n")
        c = build_paper_context(text, mode="evidence", directive="异质性",
                                comparisons=[{"paper": "晚期结果 n=37, p=.043。"}])
        sent = "\n".join(p["text"] for p in c["passages"])
        for phrase in ("随机分配至两组", "晚期结果 n=37", "异质性没有得到解释"):
            self.assertIn(phrase, sent)
        self.assertFalse(c["complete"])
        self.assertLessEqual(c["included_chars"], 24000)
        previous_end = 0
        for p in c["passages"]:
            self.assertGreaterEqual(p["start"], previous_end)
            self.assertEqual(p["text"], text[p["start"]:p["end"]])
            previous_end = p["end"]
        self.assertEqual(c["included_chars"], sum(p["end"]-p["start"] for p in c["passages"]))
        self.assertEqual(c, build_paper_context(text, mode="evidence", directive="异质性",
                                              comparisons=[{"paper": "晚期结果 n=37, p=.043。"}]))

    def test_no_headings_long_line_still_includes_tail_and_stays_bounded(self):
        text = "a" * 100000 + "TAIL-MARK"
        c = build_paper_context(text, mode="evidence")
        self.assertIn("TAIL-MARK", c["prompt_text"])
        self.assertLessEqual(c["included_chars"], 24000)
        self.assertLessEqual(len(c["passages"]), 24)

    def test_full_never_silently_truncates_and_modes_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "120000"):
            build_paper_context("a" * 120001, mode="full")
        with self.assertRaises(ValueError):
            build_paper_context("abc", mode="unknown")
        c = build_paper_context("")
        self.assertEqual(c["passages"], [])
        self.assertEqual(c["included_chars"], 0)


if __name__ == "__main__":
    unittest.main()
