"""Request-scoped AI choices and evidence handoff validation.

Policy is supplied by the current browser request. It contains model identifiers,
never credentials or paper text. Absence of a policy preserves the legacy router.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

SCHEMA_VERSION = 1
TASKS = frozenset({"recommend", "write_text", "paper_check", "audit_chat", "audit_image"})
_NAME = re.compile(r"^[\w./:\-]{1,160}$", re.ASCII)
_HASH = re.compile(r"^[a-fA-F0-9]{64}$")


def _choice(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("请选择模型配置。")
    provider = raw.get("provider")
    model = raw.get("model")
    if not isinstance(provider, str) or not _NAME.fullmatch(provider):
        raise ValueError("模型服务标识无效。")
    if not isinstance(model, str) or not _NAME.fullmatch(model):
        raise ValueError("模型 ID 无效。")
    return {"provider": provider, "model": model}


def parse_task_policy(raw: object) -> dict | None:
    """Normalize a request's explicit model policy; fail closed on malformed input."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        if len(raw) > 8192:
            raise ValueError("模型配置过长。")
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("模型配置不是有效 JSON。") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("模型配置版本不受支持。")
    mode = raw.get("mode")
    if mode not in {"off", "unified", "advanced"}:
        raise ValueError("模型配置模式无效。")
    policy = {"schema_version": SCHEMA_VERSION, "mode": mode,
              "selected": None, "tasks": {}, "allow_fallback": False,
              "candidates": [], "allow_external_fallback": False,
              "allow_paid_fallback": False}
    if mode != "off":
        policy["selected"] = _choice(raw.get("selected"))
    tasks = raw.get("tasks") or {}
    if not isinstance(tasks, dict) or set(tasks) - TASKS:
        raise ValueError("模型任务类型无效。")
    for task, entry in tasks.items():
        if not isinstance(entry, dict) or entry.get("mode") not in {"inherit", "off", "select"}:
            raise ValueError("任务模型模式无效。")
        normalized = {"mode": entry["mode"]}
        if entry["mode"] == "select":
            normalized.update(_choice(entry))
        policy["tasks"][task] = normalized
    candidates = raw.get("candidates") or []
    if not isinstance(candidates, list) or len(candidates) > 8:
        raise ValueError("模型候选数量无效。")
    policy["candidates"] = [_choice(item) for item in candidates]
    for flag in ("allow_fallback", "allow_external_fallback", "allow_paid_fallback"):
        if type(raw.get(flag, False)) is not bool:
            raise ValueError("模型回退授权值无效。")
        policy[flag] = raw.get(flag, False)
    return policy


def selected_for(policy: dict, task: str) -> dict[str, str] | None:
    if policy["mode"] == "off":
        return None
    entry = policy["tasks"].get(task) if policy["mode"] == "advanced" else None
    if entry and entry["mode"] == "off":
        return None
    if entry and entry["mode"] == "select":
        return {"provider": entry["provider"], "model": entry["model"]}
    return deepcopy(policy["selected"])


def validate_handoff(snapshot: object, *, input_hash: str, rules_version: str) -> dict:
    """Refuse stale or ungrounded model context before any downstream task runs.

    This checks the boundary only; callers still source statistical facts locally.
    """
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("交接快照版本无效。")
    if not isinstance(input_hash, str) or not _HASH.fullmatch(input_hash):
        raise ValueError("输入哈希无效。")
    if snapshot.get("input_hash") != input_hash or snapshot.get("rules_version") != rules_version:
        raise ValueError("输入或规则版本已变化，请重新运行。")
    for field in ("task_id", "task_type", "rules_version", "status"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field]:
            raise ValueError("交接快照缺少必要字段。")
    if snapshot["task_type"] not in TASKS or snapshot["status"] not in {"ready", "paused"}:
        raise ValueError("任务状态无效。")
    evidence = snapshot.get("evidence")
    facts = snapshot.get("facts")
    allowed = snapshot.get("allowed_send")
    if not isinstance(evidence, list) or not isinstance(facts, dict) or not isinstance(allowed, list):
        raise ValueError("交接证据结构无效。")
    indexed = {}
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item.get("location"):
            raise ValueError("证据缺少 ID 或原文位置。")
        if item["id"] in indexed:
            raise ValueError("证据 ID 重复。")
        indexed[item["id"]] = item
    for fact in facts.values():
        if not isinstance(fact, dict) or fact.get("evidence_id") not in indexed:
            raise ValueError("事实缺少可定位证据。")
        source = indexed[fact["evidence_id"]]
        if fact.get("value") != source.get("value") or fact.get("unit") != source.get("unit"):
            raise ValueError("事实与本地证据不一致。")
    if any(not isinstance(item, str) or item not in {"summary", "excerpt", "image", "question"}
           for item in allowed):
        raise ValueError("发送范围无效。")
    return deepcopy(snapshot)
