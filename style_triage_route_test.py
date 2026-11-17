"""v2.61 风格护栏复核的路由契约（离线，隔离全部平台 Key，不真调 API）。

覆盖：
    1. /api/ai_audit 出参带本地豁免门结果（passages / passages_exempted）
    2. /api/style_triage llm=false → 零模型调用，逐条仍有判词
    3. 无 Key 时逐条降级，绝不丢条、绝不 500，错误文案擦除凭据
    4. 空文本 400；红线口径（只评风格、不判作者身份）随响应下发
"""
import os
import sys
import unittest

os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from agents.openai_compat import PROVIDER_REGISTRY as _PR
    _ALL_KEY_ENVS = tuple(sorted({
        e.strip() for cfg in _PR.values()
        for e in (getattr(cfg, "env_var", "") or "").split(",") if e.strip()
    }))
except Exception:  # noqa: BLE001
    _ALL_KEY_ENVS = ("SILICONFLOW_API_KEY", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY",
                     "DASHSCOPE_API_KEY", "MAAS_API_KEY")
for _k in _ALL_KEY_ENVS:
    os.environ.pop(_k, None)

BODY = ("综上所述，本研究具有重要的理论意义与实践价值。首先，研究结论有利于改善平台的服务环境，"
        "进而提升平台竞争力。其次，示例信息的主动披露也会显著提升该平台对用户群体的吸引力。"
        "最后，本文的研究成果得到了验证，能够为相关团队提供决策参考，"
        "因而也是改善用户黏性的关键环节。")


class StyleTriageRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()

    def post(self, route, payload):
        return self.client.post(route, json=payload)

    def test_ai_audit_exposes_gate_keys(self):
        r = self.post("/api/ai_audit", {"text": BODY})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertIn("passages", d)
        self.assertIn("passages_exempted", d)
        self.assertIsInstance(d["passages_exempted"], list)
        self.assertTrue(d["passages"], "夹具应至少定位到一处风格集中段落")

    def test_triage_local_only_makes_zero_model_calls(self):
        r = self.post("/api/style_triage", {"text": BODY, "llm": False})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["model_calls"], 0)
        self.assertEqual(len(d["items"]), len(self.post("/api/ai_audit", {"text": BODY})
                                          .get_json()["passages"]))
        for it in d["items"]:
            self.assertIn(it["verdict"], ("真", "假", "存疑"))
            self.assertFalse(it["model_called"])
            self.assertIn("excerpt", it)
            self.assertIn("reasons", it)
            self.assertIn("local_evidence", it)
            self.assertIn("source", it)

    def test_triage_without_key_degrades_per_item_never_drops(self):
        audit = self.post("/api/ai_audit", {"text": BODY}).get_json()
        r = self.post("/api/style_triage", {"text": BODY})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["items"]), len(audit["passages"]))
        for it in d["items"]:
            self.assertIn(it["verdict"], ("真", "假", "存疑"))
            self.assertNotIn("sk-", str(it.get("error", "")))
        self.assertIn("不判定作者身份", d["disclaimer"])

    def test_true_verdict_always_carries_local_evidence(self):
        d = self.post("/api/style_triage", {"text": BODY, "llm": False}).get_json()
        for it in d["items"]:
            if it["verdict"] == "真":
                self.assertTrue(it["local_evidence"], "判真必须有本地作者可控证据")
                self.assertEqual(it["source"], "local_plus_model")

    def test_empty_text_is_400(self):
        for route in ("/api/ai_audit", "/api/style_triage"):
            r = self.post(route, {"text": "   "})
            self.assertEqual(r.status_code, 400)
            self.assertFalse(r.get_json()["ok"])
            self.assertTrue(r.get_json()["error"])

    def test_router_knows_style_triage_state(self):
        from agents.router import STATE_TO_MODEL
        self.assertIn("style_triage", STATE_TO_MODEL)
        self.assertTrue(all(len(t) == 3 for t in STATE_TO_MODEL["style_triage"]))
        self.assertTrue(all(t[2] == 0.2 for t in STATE_TO_MODEL["style_triage"]))


if __name__ == "__main__":
    unittest.main()