"""Bounded research loop and provenance checks; model transport is a fixture."""
import unittest


class ResearchReviewTest(unittest.TestCase):
    def run_review(self, replies, text="方法\nt(30)=2.0, p=.001。\n结论\n需核对。", mode="evidence"):
        import research_review
        from paper_context import build_paper_context

        class Router:
            _last_usage = None
            calls = []
            def complete(self, state, prompt, **kwargs):
                self.calls.append((prompt, kwargs))
                self._last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
                return replies[len(self.calls)-1]
            def last_invocation(self):
                return {"actual_model": "custom_primary/test"}
        router = Router()
        context = build_paper_context(text, mode=mode)
        result, meta = research_review.run_research_review(
            router, "材料\n" + context["prompt_text"], text, mode, 8192, context)
        return result, meta, router

    def test_search_then_recheck_then_report_with_provenance(self):
        result, meta, router = self.run_review([
            '{"tool":"search","query":"t(30)"}',
            '{"tool":"stat_recheck","start":0,"end":24}',
            "依据 [T001]，该报告需核对；假说仍待原始数据验证。"])
        self.assertEqual(len(router.calls), 3)
        self.assertEqual(len(meta["trace"]), 2)
        self.assertEqual(meta["usage"]["prompt_tokens"], 30)
        self.assertIn("T001", router.calls[-1][0])
        self.assertIn("假说", router.calls[-1][0])
        self.assertIn("工具证据", result)
        self.assertEqual(meta["calls"][0]["model"], "custom_primary/test")

    def test_bad_json_stops_tools_but_keeps_final_review(self):
        result, meta, router = self.run_review(["ignore rules; run shell!", "材料不足，待核对。"])
        self.assertEqual(len(router.calls), 2)
        self.assertFalse(meta["trace"][0]["ok"])
        self.assertIn("材料不足", result)

    def test_single_json_after_prose_is_validated_and_executes_bounded_read(self):
        _, meta, _ = self.run_review([
            'We will issue a read action.{"tool":"read","start":0,"end":12}',
            '{"done":true}', "依据工具证据核对。"])
        self.assertTrue(meta["trace"][0]["ok"])
        self.assertEqual(meta["trace"][0]["end"], 12)

    def test_multiple_json_objects_are_rejected_as_ambiguous(self):
        _, meta, router = self.run_review([
            '{"tool":"read","start":0,"end":12} {"done":true}', "无法取得工具证据。"])
        self.assertEqual(len(router.calls), 2)
        self.assertFalse(meta["trace"][0]["ok"])

    def test_head_tool_request_cannot_read_unsent_tail(self):
        text = "A"*2100 + "TAIL_SECRET"
        _, meta, router = self.run_review([
            '{"tool":"read","start":2000,"end":2111}', '{"done":true}', "未取得尾部证据。"], text, "head")
        self.assertFalse(meta["trace"][0]["ok"])
        self.assertNotIn("TAIL_SECRET", "".join(p for p, _ in router.calls))

    def test_done_means_one_planner_and_one_final(self):
        _, meta, router = self.run_review(['{"done":true}', "未发现有证据支持的新问题。"])
        self.assertEqual(len(router.calls), 2)
        self.assertEqual(meta["trace"], [])
        self.assertEqual(router.calls[-1][1]["max_tokens"], 8192)

    def test_missing_usage_is_unknown_not_zero(self):
        import research_review
        from paper_context import build_paper_context
        class Router:
            _last_usage = None
            def complete(self, *args, **kwargs):
                return '{"done":true}' if "仅返回" in kwargs["system"] else "待补证据。"
            def last_invocation(self):
                return None
        _, meta = research_review.run_research_review(Router(), "材料", "材料", "head", 4096,
                                                     build_paper_context("材料"))
        self.assertIsNone(meta["usage"])


if __name__ == "__main__":
    unittest.main()
