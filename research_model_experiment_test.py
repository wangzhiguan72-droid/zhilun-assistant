"""Offline contracts for the research experiment runner; never invokes a model."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parent / "scripts" / "research_model_experiment.py"


class ExperimentRunnerTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), "The independent research runner must exist")
        spec = importlib.util.spec_from_file_location("research_model_experiment", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_case_identity_separates_budget_model_arm_and_document(self):
        m = self.module
        kwargs = dict(text_hash="paper", model="provider/model", arm="evidence", output_tokens=4096)
        first = m.case_identity(**kwargs)
        self.assertEqual(first, m.case_identity(**kwargs))
        for field, value in (("text_hash", "other"), ("model", "other/model"),
                             ("arm", "tools"), ("output_tokens", 8192)):
            self.assertNotEqual(first, m.case_identity(**{**kwargs, field: value}))

    def test_duplicate_sources_create_one_case_and_one_request(self):
        m = self.module
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            a, b = base / "a.md", base / "b.md"
            a.write_text("相同内容", encoding="utf-8")
            b.write_text("相同内容", encoding="utf-8")
            def run_case(**kwargs):
                calls.append(kwargs)
                return {"status": "success"}
            with patch.object(m, "gateway_key", return_value="fixture"), \
                 patch.object(m, "run_case", side_effect=run_case):
                rc = m.main(["--run", "--models", "provider/test", "--arms", "evidence",
                             "--papers", str(a), str(a), str(b), "--workers", "2",
                             "--output", str(base / "out")])
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)

    def test_resume_only_reuses_matching_success(self):
        m = self.module
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "case.json"
            self.assertFalse(m.is_complete(path, "case"))
            for payload in ({"case_id": "case", "status": "failed"},
                            {"case_id": "other", "status": "success"}):
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(m.is_complete(path, "case"))
            path.write_text(json.dumps({"case_id": "case", "status": "success"}), encoding="utf-8")
            self.assertTrue(m.is_complete(path, "case"))

    def test_source_manifest_hashes_extracted_text_and_file_separately(self):
        m = self.module
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.md"
            path.write_bytes("研究方法\r\n硕士样本文本".encode("utf-8"))
            text, manifest = m.read_source(path)
            self.assertNotIn("\r", text)
            self.assertEqual(manifest["source_chars"], len(text))
            self.assertEqual(manifest["source_kind"], "master_candidate")
            self.assertNotEqual(manifest["file_sha256"], manifest["text_sha256"])

    def test_simulated_participants_are_reproducible_and_explicit(self):
        m = self.module
        with tempfile.TemporaryDirectory() as folder:
            first = m.write_simulation(Path(folder) / "one", seed=421, n=120)
            second = m.write_simulation(Path(folder) / "two", seed=421, n=120)
            self.assertEqual(Path(first["csv"]).read_bytes(), Path(second["csv"]).read_bytes())
            self.assertEqual(first["data_origin"], "synthetic_not_human_not_thesis_raw_data")
            self.assertGreater(first["missing_values"], 0)
            self.assertEqual(first["rows"], 120)

    def test_unavailable_usage_remains_null_and_router_never_falls_back(self):
        m = self.module
        class Agent:
            model_name = "provider/exact"
            last_usage = None
            def complete(self, prompt, **kwargs):
                return "response"
        router = m.FixedRouter(Agent())
        self.assertEqual(router.complete("paper_check", "prompt", max_tokens=4096), "response")
        self.assertIsNone(router._last_usage)
        self.assertEqual(router.last_invocation()["actual_model"], "provider/exact")
        self.assertEqual(router.last_invocation()["attempts"], ["provider/exact"])

    def test_cli_bounds_workers_and_output_budget(self):
        parser = self.module.build_parser()
        for args in (["--discover", "--workers", "4"], ["--discover", "--output-tokens", "50"]):
            with self.assertRaises(SystemExit):
                parser.parse_args(args)

    def test_evidence_case_saves_full_output_and_resume_makes_no_call(self):
        m = self.module
        class Agent:
            def __init__(self, model, **kwargs):
                self.model_name = model
                self.last_usage = None
            def complete(self, prompt, **kwargs):
                self.last_usage = None
                return "完整输出：依据[P001]，尚需核对。"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "paper.md"
            source.write_text("研究方法：抽样。结果：有待核对。", encoding="utf-8")
            text, manifest = m.read_source(source)
            kwargs = dict(text=text, manifest=manifest, model="provider/exact", arm="evidence",
                          output_tokens=4096, gateway=m.DEFAULT_GATEWAY, key="test-secret", output=base / "run")
            with patch("agents.custom_agent.CustomAgent", Agent):
                first = m.run_case(**kwargs)
            self.assertEqual(first["status"], "success")
            payload = json.loads(Path(first["path"]).read_text(encoding="utf-8"))
            self.assertIsNone(payload["usage"])
            self.assertEqual(payload["actual_router_id"], "provider/exact")
            self.assertFalse(payload["backend_identity_verified"])
            self.assertIn("完整输出", payload["output"])
            self.assertNotIn("test-secret", Path(first["path"]).read_text(encoding="utf-8"))
            with patch("agents.custom_agent.CustomAgent", side_effect=AssertionError("resume must not call")):
                second = m.run_case(**kwargs)
            self.assertEqual(second["status"], "resumed")

    def test_failed_call_keeps_partial_usage_without_previous_call_values(self):
        class Agent:
            model_name = "provider/exact"
            last_usage = {"prompt_tokens": 999}
            def complete(self, prompt, **kwargs):
                raise RuntimeError("gateway unavailable")
        router = self.module.FixedRouter(Agent())
        router._last_usage = {"prompt_tokens": 888}
        with self.assertRaises(RuntimeError):
            router.complete("paper_check", "prompt")
        self.assertIsNone(router._last_usage)
        self.assertEqual(router.records[0]["status"], "failed")
        self.assertIsNone(router.records[0]["usage"])

    def test_custom_gateway_does_not_reuse_local_config_credentials(self):
        with patch.dict("os.environ", {}, clear=True), patch("env_loader.load_dotenv"):
            with self.assertRaisesRegex(ValueError, "explicit"):
                self.module.gateway_key("https://example.com/v1")

    def test_resume_identity_tracks_context_version_and_nonempty_directive(self):
        m = self.module
        kwargs = dict(text_hash="paper", model="provider/model", arm="tools", output_tokens=4096)
        first = m.case_identity(**kwargs)
        self.assertEqual(first, m.case_identity(**kwargs, directive="  "))
        self.assertNotEqual(first, m.case_identity(**kwargs, directive="调用stat_recheck"))
        with patch("paper_context.CONTEXT_VERSION", "changed-context-version"):
            self.assertNotEqual(first, m.case_identity(**kwargs))

    def test_call_records_capture_planning_output_and_sanitize_errors(self):
        class Agent:
            model_name = "provider/exact"
            last_usage = None
            def complete(self, prompt, **kwargs):
                if prompt == "error":
                    raise RuntimeError("gateway rejected test-local-credential")
                return '{"tool":"stat_recheck","start":0,"end":80}'
        router = self.module.FixedRouter(Agent(), api_key="test-local-credential")
        returned = router.complete("paper_check", "plan")
        self.assertEqual(router.records[0]["response_text"], returned)
        with self.assertRaises(RuntimeError):
            router.complete("paper_check", "error")
        record = router.records[1]
        self.assertIsNone(record["response_text"])
        self.assertNotIn("test-local-credential", json.dumps(record))
        self.assertIn("REDACTED", record["error"])

    def test_directive_reaches_actual_prompt_and_recorded_case(self):
        m = self.module
        prompts = []
        class Agent:
            def __init__(self, model, **kwargs):
                self.model_name, self.last_usage = model, None
            def complete(self, prompt, **kwargs):
                prompts.append(prompt)
                return "核对意见"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "paper.md"
            source.write_text("明确标注的模拟控制片段 t(20)=2.0,p=.5", encoding="utf-8")
            text, manifest = m.read_source(source)
            directive = "本轮测试请调用stat_recheck验证论文区间。"
            with patch("agents.custom_agent.CustomAgent", Agent):
                summary = m.run_case(text=text, manifest=manifest, model="provider/exact", arm="evidence",
                                     output_tokens=4096, gateway=m.DEFAULT_GATEWAY, key="fixture-key",
                                     output=base / "run", directive=directive)
            self.assertEqual(summary["status"], "success")
            self.assertIn(directive, prompts[0])
            payload = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["directive"], directive)
            self.assertEqual(payload["calls"][0]["response_text"], "核对意见")


if __name__ == "__main__":
    unittest.main()
