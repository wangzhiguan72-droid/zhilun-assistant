"""Explicit per-request AI policy must never reuse the legacy shared response cache."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import agents
import audit_chat
import llm_audit
import llm_enhance
import multimodal_agent
import paper_polisher
from agents import AgentError
from llm_cache import llm_cache


class FakeRouter:
    def __init__(self):
        self.task_policy = None
        self.calls = 0
        self._last_usage = {}
        self._resolved = {}

    def cache_model_name(self, _state):
        return "zhipu/glm-4.7-flash"

    def _get_agent(self, _state):
        return SimpleNamespace(model_name="zhipu/glm-4.7-flash")

    def last_invocation(self):
        return {"actual_model": "other/actual-model", "status": "success"}

    def complete(self, _state, *args, **kwargs):
        self.calls += 1
        if self.task_policy and self.task_policy["mode"] == "off":
            raise AgentError("任务 AI 已关闭")
        self._resolved[_state] = ("other", "actual-model")
        return "已核对的模拟模型解释"


SUMMARY = {"method_cn": "独立样本 T 检验", "paper": 2.5,
           "real": 2.4, "status_cn": "需核对", "verdict": "请核对"}


class ExplicitPolicyCacheTest(unittest.TestCase):
    def setUp(self):
        llm_cache.clear()
        self.router = FakeRouter()
        self.patcher = patch.object(agents, "get_router", return_value=self.router)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def tearDown(self):
        llm_cache.clear()

    def _call(self, kind):
        if kind == "analysis":
            return llm_enhance.enhance_analysis("test_method", {"n": 37}, "实验")
        if kind == "chat":
            return audit_chat.explain_comparison(SUMMARY, "解释这条结果")
        return llm_audit.audit_paper_with_llm("自造论文段落" * 25,
                                              {"comparisons": [], "suggestions": []}, "")

    def test_disabled_policy_cannot_read_legacy_cache(self):
        for kind in ("analysis", "chat", "paper"):
            with self.subTest(kind=kind):
                llm_cache.clear()
                self.router.task_policy = None
                legacy_text, legacy_error, legacy_meta = self._call(kind)
                self.assertIsNone(legacy_error)
                self.assertTrue(legacy_text)
                self.assertFalse(legacy_meta["cached"])
                self.assertGreater(llm_cache.stats()["entries"], 0)

                self.router.task_policy = {"mode": "off"}
                before = self.router.calls
                with patch.object(llm_cache, "get", wraps=llm_cache.get) as get, \
                     patch.object(llm_cache, "peek", wraps=llm_cache.peek) as peek, \
                     patch.object(llm_cache, "put", wraps=llm_cache.put) as put:
                    text, error, meta = self._call(kind)
                self.assertIsNone(text)
                self.assertIn("关闭", error or "")
                self.assertFalse(meta["cached"])
                self.assertEqual(self.router.calls, before + 1)
                get.assert_not_called()
                peek.assert_not_called()
                put.assert_not_called()

    def test_explicit_success_never_populates_legacy_cache(self):
        for kind in ("analysis", "chat", "paper"):
            with self.subTest(kind=kind):
                llm_cache.clear()
                self.router.task_policy = {"mode": "unified"}
                with patch.object(llm_cache, "get", wraps=llm_cache.get) as get, \
                     patch.object(llm_cache, "peek", wraps=llm_cache.peek) as peek, \
                     patch.object(llm_cache, "put", wraps=llm_cache.put) as put:
                    text, error, meta = self._call(kind)
                self.assertTrue(text)
                self.assertIsNone(error)
                self.assertFalse(meta["cached"])
                self.assertEqual(meta["model"], "actual-model" if kind == "paper" else "other/actual-model")
                self.assertEqual(llm_cache.stats()["entries"], 0)
                get.assert_not_called()
                peek.assert_not_called()
                put.assert_not_called()
                if kind == "paper":
                    self.assertIsNone(meta["cost_estimate"])

    def test_image_disabled_policy_never_reads_or_writes_shared_cache(self):
        from agents.router import Router
        router = Router()
        router.set_task_policy({'schema_version':1, 'mode':'off'})
        with patch.object(agents, 'get_router', return_value=router), \
             patch.object(llm_cache, 'get') as get, patch.object(llm_cache, 'peek') as peek, \
             patch.object(llm_cache, 'put') as put:
            result, error, meta = multimodal_agent.audit_image(b'png-fixture', 'image/png', '演示结论')
        self.assertIsNone(result)
        self.assertIn('关闭', error)
        self.assertEqual(router.last_invocation()['status'], 'disabled')
        get.assert_not_called(); peek.assert_not_called(); put.assert_not_called()

    def test_copilot_polish_disabled_does_not_read_cached_text(self):
        self.router.task_policy = {'mode':'off'}
        with patch.object(llm_cache, 'get') as get, patch.object(llm_cache, 'put') as put:
            result, meta = paper_polisher.polish_paper('开发自造文本，保留原始稿。', [])
        self.assertEqual(result, '开发自造文本，保留原始稿。')
        self.assertFalse(meta['llm_used'])
        self.assertIn('关闭', meta['reject_reason'])
        get.assert_not_called(); put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
