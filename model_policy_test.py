"""V1.8 模型选择与交接边界的离线回归。"""
from __future__ import annotations

import contextvars
import unittest
from unittest.mock import patch

from agents.base import AgentError
from agents.router import Router
from agents.task_policy import validate_handoff
from model_settings import create_model_blueprint
from flask import Flask


class _Agent:
    def __init__(self, provider, model, fail=False):
        self.provider = provider
        self.model_name = model
        self.fail = fail
        self.last_usage = None

    def complete(self, prompt, **kwargs):
        if self.fail:
            raise AgentError("offline failure")
        return f"{self.provider}/{self.model_name}"


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.router = Router(tier="pro")
        self.made = []

        def make(provider, model, temp):
            self.made.append((provider, model))
            return _Agent(provider, model, (provider, model) in self.failing_models)

        self.failing_models = set()
        self.patch = patch.object(self.router, "_make_agent", side_effect=make)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_disabled_state_does_not_call_any_provider(self):
        self.router.set_task_policy({"schema_version": 1, "mode": "off"})
        with self.assertRaisesRegex(AgentError, "已关闭"):
            self.router.complete("write_text", "statistics")
        self.assertEqual(self.made, [])

    def test_unified_pins_selected_model_without_implicit_fallback(self):
        self.router.set_task_policy({"schema_version": 1, "mode": "unified",
                                     "selected": {"provider": "zhipu", "model": "my-model"}})
        self.failing_models.add(("zhipu", "my-model"))
        with self.assertRaises(AgentError):
            self.router.complete("write_text", "statistics")
        self.assertEqual(self.made, [("zhipu", "my-model")])

    def test_advanced_off_and_override(self):
        self.router.set_task_policy({"schema_version": 1, "mode": "advanced",
                                     "selected": {"provider": "zhipu", "model": "base"},
                                     "tasks": {"audit_chat": {"mode": "off"},
                                               "paper_check": {"mode": "select", "provider": "sf", "model": "picked"}}})
        with self.assertRaises(AgentError):
            self.router.complete("audit_chat", "question")
        self.assertEqual(self.router.complete("paper_check", "paper"), "sf/picked")
        self.assertEqual(self.router.last_invocation()["actual_model"], "sf/picked")
        self.assertEqual(self.made, [("sf", "picked")])

    def test_fallback_only_on_approved_candidates(self):
        self.router.set_task_policy({"schema_version": 1, "mode": "unified",
                                     "selected": {"provider": "zhipu", "model": "glm-4.7-flash"},
                                     "allow_fallback": True,
                                     "candidates": [{"provider": "sf", "model": "glm-z1-9b"}]})
        self.failing_models.add(("zhipu", "glm-4.7-flash"))
        self.assertEqual(self.router.complete("write_text", "prompt"), "sf/glm-z1-9b")
        self.assertEqual(self.made, [("zhipu", "glm-4.7-flash"), ("sf", "glm-z1-9b")])

    def test_cross_provider_requires_explicit_external_and_paid_flags(self):
        payload = {"schema_version": 1, "mode": "unified",
                   "selected": {"provider": "custom1", "model": "local"},
                   "allow_fallback": True,
                   "candidates": [{"provider": "zhipu", "model": "glm-4.7-flash"}]}
        self.router.set_custom_providers([{"id": "custom1", "base_url": "http://127.0.0.1:3000/v1",
                                           "key": "local-secret", "models": "local"}])
        self.router.set_task_policy(payload)
        self.failing_models.add(("custom1", "local"))
        with self.assertRaises(AgentError):
            self.router.complete("write_text", "prompt")
        self.assertEqual(self.made, [("custom1", "local")])
        payload["allow_external_fallback"] = True
        self.router.set_task_policy(payload)
        self.assertEqual(self.router.complete("write_text", "prompt"), "zhipu/glm-4.7-flash")

    def test_unknown_cost_fallback_needs_paid_authorization(self):
        payload = {"schema_version": 1, "mode": "unified",
                   "selected": {"provider": "zhipu", "model": "glm-4.7-flash"},
                   "allow_fallback": True,
                   "candidates": [{"provider": "mimo", "model": "unknown-cost"}]}
        self.router.set_task_policy(payload)
        self.failing_models.add(("zhipu", "glm-4.7-flash"))
        with self.assertRaises(AgentError):
            self.router.complete("write_text", "prompt")
        self.assertEqual(self.made, [("zhipu", "glm-4.7-flash")])
        payload["allow_paid_fallback"] = True
        self.router.set_task_policy(payload)
        self.assertEqual(self.router.complete("write_text", "prompt"), "mimo/unknown-cost")

    def test_unknown_cost_to_unknown_cost_also_needs_paid_authorization(self):
        self.router.set_custom_providers([{"id": "custom1", "base_url": "http://localhost:3000/v1",
                                           "key": "local-secret", "models": "local,local-backup"}])
        payload = {"schema_version": 1, "mode": "unified",
                   "selected": {"provider": "custom1", "model": "local"},
                   "allow_fallback": True,
                   "candidates": [{"provider": "custom1", "model": "local-backup"}]}
        self.router.set_task_policy(payload)
        self.failing_models.add(("custom1", "local"))
        with self.assertRaises(AgentError):
            self.router.complete("write_text", "prompt")
        self.assertEqual(self.made, [("custom1", "local")])

    def test_policy_is_request_scoped(self):
        self.router.set_task_policy({"schema_version": 1, "mode": "off"})
        self.assertEqual(contextvars.Context().run(lambda: self.router._task_policy), None)

    def test_reset_clears_same_worker_credentials_and_selection(self):
        self.router.set_user_key('zhipu', 'request-A')
        self.router.set_custom_providers([{'base_url':'http://localhost:1234/v1', 'key':'request-A', 'models':'local'}])
        self.router.set_task_policy({'schema_version':1, 'mode':'off'})
        self.router.reset_request_state()
        self.assertEqual(self.router._user_keys, {})
        self.assertIsNone(self.router._custom_cfg)
        self.assertIsNone(self.router.task_policy)
        self.assertIsNone(self.router.last_invocation())

    def test_invalid_explicit_policy_fails_closed(self):
        self.router.set_task_policy({"schema_version": 1, "mode": "unified",
                                     "selected": {"provider": "zhipu", "model": "first"}})
        for bad in ('{"schema_version":1,', {"schema_version": 0, "mode": "off"},
                    {"schema_version": 1, "mode": "unified",
                     "selected": {"provider": "custom1", "model": "unknown"}}):
            with self.assertRaises(AgentError):
                self.router.set_task_policy(bad)
            self.assertIsNone(self.router.task_policy)
        self.assertEqual(self.made, [])

    def test_selected_custom_model_must_match_current_request_config(self):
        self.router.set_custom_providers([{"id": "custom1", "base_url": "http://localhost:3000/v1",
                                           "key": "local-secret", "models": "only-this"}])
        with self.assertRaises(AgentError):
            self.router.set_task_policy({"schema_version": 1, "mode": "unified",
                                         "selected": {"provider": "custom1", "model": "other"}})

    def test_handoff_rejects_stale_hash_version_and_missing_evidence(self):
        snapshot = {"schema_version": 1, "task_id": "t1", "task_type": "paper_check",
                    "input_hash": "a" * 64, "rules_version": "r1", "evidence": [
                        {"id": "e1", "location": "paragraph:1", "value": 2, "unit": "n"}],
                    "facts": {"n": {"evidence_id": "e1", "value": 2, "unit": "n"}},
                    "allowed_send": ["summary"], "status": "ready"}
        self.assertEqual(validate_handoff(snapshot, input_hash="a" * 64,
                                          rules_version="r1")["task_id"], "t1")
        for altered in ({**snapshot, "schema_version": 0},
                        {**snapshot, "evidence": []}):
            with self.assertRaises(ValueError):
                validate_handoff(altered, input_hash="a" * 64, rules_version="r1")
        with self.assertRaises(ValueError):
            validate_handoff(snapshot, input_hash="b" * 64, rules_version="r1")
        with self.assertRaises(ValueError):
            validate_handoff(snapshot, input_hash="a" * 64, rules_version="r2")


class ModelEndpointTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(create_model_blueprint())
        self.client = app.test_client()

    def test_connection_test_leaves_room_for_reasoning_before_visible_reply(self):
        calls = []

        class _ReasoningRouter(Router):
            def complete(self, state, prompt, **kwargs):
                calls.append((state, prompt, kwargs))
                if kwargs.get("max_tokens", 0) < 4096:
                    return ""
                return "OK"

        app = Flask("reasoning_budget")
        app.register_blueprint(create_model_blueprint(router_factory=_ReasoningRouter))
        response = app.test_client().post("/api/model/test", json={
            "confirm_network": True,
            "ai_policy": {"schema_version": 1, "mode": "unified",
                          "selected": {"provider": "zhipu", "model": "reasoning-model"}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[0][1], "请只回复 OK。")
        self.assertEqual(response.get_json()["status"], "connection_test_succeeded")

    def test_status_does_not_return_credentials_or_fake_connection_success(self):
        response = self.client.post("/api/model/status", json={
            "credentials": {"zhipu": "sk-secret-never-return"}})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["client"]["zhipu"]["status"], "filled_untested")
        self.assertNotIn("sk-secret-never-return", response.get_data(as_text=True))
        self.assertNotIn("connected", response.get_data(as_text=True))

    def test_connection_test_requires_explicit_network_confirmation(self):
        response = self.client.post("/api/model/test", json={
            "ai_policy": {"schema_version": 1, "mode": "unified",
                          "selected": {"provider": "zhipu", "model": "glm-4.7-flash"}},
            "credentials": {"zhipu": "sk-secret-never-return"}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("确认", response.get_json()["error"])

    def test_connection_failure_scrubs_provider_error_text(self):
        class _FailingRouter(Router):
            def complete(self, state, prompt, **kwargs):
                raise AgentError("401 sk-secret-never-return https://private.test/?token=bad paper text")

        app = Flask("test_failure")
        app.register_blueprint(create_model_blueprint(router_factory=_FailingRouter))
        response = app.test_client().post("/api/model/test", json={
            "confirm_network": True,
            "ai_policy": {"schema_version": 1, "mode": "unified",
                          "selected": {"provider": "zhipu", "model": "glm-4.7-flash"}},
            "credentials": {"zhipu": "sk-secret-never-return"}})
        self.assertEqual(response.status_code, 502)
        body = response.get_data(as_text=True)
        self.assertNotIn("sk-secret-never-return", body)
        self.assertNotIn("private.test", body)
        self.assertNotIn("paper text", body)
        self.assertEqual(response.get_json()["status"], "connection_test_failed")
        self.assertIn("tested_at", response.get_json())

    def test_invalid_policy_is_client_error_and_never_calls_provider(self):
        response = self.client.post("/api/model/test", json={
            "confirm_network": True,
            "ai_policy": {"schema_version": 0, "mode": "unified"}})
        self.assertEqual(response.status_code, 400)

    def test_empty_provider_reply_is_failed_connection_test(self):
        class _EmptyRouter(Router):
            def complete(self, state, prompt, **kwargs):
                return "  "

        app = Flask("test_empty")
        app.register_blueprint(create_model_blueprint(router_factory=_EmptyRouter))
        response = app.test_client().post("/api/model/test", json={
            "confirm_network": True,
            "ai_policy": {"schema_version": 1, "mode": "unified",
                          "selected": {"provider": "zhipu", "model": "glm-4.7-flash"}}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["status"], "connection_test_failed")


if __name__ == "__main__":
    unittest.main()
