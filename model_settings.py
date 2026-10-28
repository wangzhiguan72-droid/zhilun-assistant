"""Read-only model configuration status and an explicitly requested connection test.

No key, private URL, prompt or provider error body is returned to the browser.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

from agents.router import Router
from agents.base import AgentError
from agents.openai_compat import PROVIDER_REGISTRY
from agents.task_policy import TASKS, selected_for


def _status(payload: dict) -> dict:
    credentials = payload.get("credentials") or {}
    if not isinstance(credentials, dict):
        credentials = {}
    source_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    files = [name for name in (".env", ".env.local") if (source_dir / name).is_file()]
    return {
        "ok": True,
        "server": {
            "source": "current_process_environment",
            "config_files_present": files,
            "providers": {
                name: {"configured": bool(os.environ.get(cfg.env_var)),
                       "status": "filled_untested" if os.environ.get(cfg.env_var) else "unconfigured"}
                for name, cfg in PROVIDER_REGISTRY.items() if name != "custom"
            },
        },
        "client": {
            name: {"status": "filled_untested" if isinstance(value, str) and value.strip()
                   else "unconfigured"}
            for name, value in credentials.items()
            if name in PROVIDER_REGISTRY and name != "custom"
        },
    }


def create_model_blueprint(router_factory=Router) -> Blueprint:
    bp = Blueprint("model_settings", __name__)

    @bp.get("/api/model/status")
    def model_status():
        return jsonify(_status({}))

    @bp.post("/api/model/status")
    def model_status_client():
        payload = request.get_json(silent=True)
        return jsonify(_status(payload if isinstance(payload, dict) else {}))

    @bp.post("/api/model/test")
    def model_connection_test():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirm_network") is not True:
            return jsonify({"ok": False, "error": "请先确认连接测试可能联网并按服务条款计费。"}), 400
        task = payload.get("task", "write_text")
        if task not in TASKS:
            return jsonify({"ok": False, "error": "未知 AI 任务。"}), 400
        credentials = payload.get("credentials") or {}
        if not isinstance(credentials, dict):
            return jsonify({"ok": False, "error": "模型凭据格式无效。"}), 400
        try:
            router = router_factory()
            for name, value in credentials.items():
                if name in PROVIDER_REGISTRY and name != "custom" and isinstance(value, str) and value.strip():
                    router.set_user_key(name, value.strip())
            custom = payload.get("custom_providers") or []
            if custom:
                if not isinstance(custom, list):
                    raise ValueError("自定义服务配置无效。")
                router.set_custom_providers(custom)
            try:
                router.set_task_policy(payload.get("ai_policy"))
            except AgentError:
                return jsonify({"ok": False, "error": "模型选择无效，请核对服务和模型 ID。"}), 400
            policy = router._task_policy
            if policy is None or selected_for(policy, task) is None:
                return jsonify({"ok": False, "error": "请先选择这个任务的模型。"}), 400
            # The user explicitly clicked a network test. Never send paper content.
            # Reasoning tokens may share the completion budget. A tiny limit can
            # falsely mark a working reasoning model as unavailable (CLAUDE.md).
            reply = router.complete(task, "请只回复 OK。", max_tokens=4096)
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("empty provider reply")
            meta = router.last_invocation() or {}
            return jsonify({"ok": True, "status": "connection_test_succeeded",
                            "tested_at": datetime.now(timezone.utc).isoformat(),
                            "selected_model": meta.get("selected_model", ""),
                            "actual_model": meta.get("actual_model", ""),
                            "attempts": meta.get("attempts", [])})
        except Exception as exc:  # noqa: BLE001 - provider body can contain secrets/content
            # Deliberately classify without echoing arbitrary SDK text. It may contain a
            # private URL query string, credentials or user supplied document fragments.
            name = type(exc).__name__
            reason = "鉴权失败" if "Authentication" in name else "连接或模型调用失败"
            meta = router.last_invocation() if "router" in locals() else None
            meta = meta or {}
            return jsonify({"ok": False, "status": "connection_test_failed",
                            "tested_at": datetime.now(timezone.utc).isoformat(),
                            "selected_model": meta.get("selected_model", ""),
                            "actual_model": "",
                            "attempts": meta.get("attempts", []),
                            "error": reason + "；请核对服务地址、模型 ID、凭据和额度。"}), 502

    return bp
