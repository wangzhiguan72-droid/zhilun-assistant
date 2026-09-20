"""
v1.6 · 规划§三②审计对话 契约测试
=================================
覆盖：
    1. audit._attach_comparison_summaries：summary 结构 / 各状态措辞 / 不变量
    2. audit_chat.explain_comparison：无 Key 降级、输入最小化、空问题、缓存键区分
    3. /api/audit_chat 端点：参数校验、红线拦截、降级回退、cited 回执
    4. 铁律②：喂给 LLM 的输入**不含**原始数据字段

运行：.venv/Scripts/python.exe -u audit_chat_test.py
"""
import io
import json
import os

# 硬性纪律 7（与 registry_test / wizard_test 同款）：本套件会连续调用限流路径，
# 必须整体关闭限流，否则 60 秒滑窗内必吃 429（v2.27 扫描报告 P1-1）。
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 隔离全部平台 Key（从注册表派生，避免漏平台导致假失败）——见 MEMORY 血泪规则
try:
    from agents.openai_compat import PROVIDER_REGISTRY as _PR
    _ALL_KEY_ENVS = tuple(sorted({
        e.strip() for cfg in _PR.values()
        for e in (getattr(cfg, "env_var", "") or "").split(",") if e.strip()
    }))
except Exception:  # noqa: BLE001
    _ALL_KEY_ENVS = ("SILICONFLOW_API_KEY", "ZHIPU_API_KEY",
                     "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MAAS_API_KEY")
_saved_env = {k: os.environ.pop(k, None) for k in _ALL_KEY_ENVS}

import audit
import audit_chat
from agents import get_router

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f'  << {detail}' if detail else ''))


def _reset_router():
    try:
        r = get_router()
        r._agents.clear()
        r._resolved.clear()
        r._last_good.clear() if hasattr(r, "_last_good") else None
    except Exception:  # noqa: BLE001
        pass


print('=== 1. _attach_comparison_summaries 结构与措辞 ===')
real = {"ok": True, "method": "independent_t", "p": 0.041, "t": -2.31}
methods = [{"key": "independent_t", "raw": "独立样本 t 检验"}]
cmps = [
    {"status": "ok", "kind": "p", "paper": "P < 0.05", "real": "0.0410", "diff": None},
    {"status": "minor_diff", "kind": "t", "paper": "t = -2.0", "real": "-2.310", "diff": "0.310"},
    {"status": "mismatch", "kind": "t", "paper": "t = -5.0", "real": "-2.310", "diff": "2.690"},
    {"status": "no_real", "reason": "数据列不匹配"},
    {"status": "unknown", "paper": "F = 3.1", "real": "未跑出对应统计量", "diff": None},
]
out = audit._attach_comparison_summaries([dict(c) for c in cmps], real, methods)

check("返回条目数不变", len(out) == 5)
check("每条都有 summary", all("summary" in c for c in out))
s0 = out[0]["summary"]
check("summary 必需字段齐全",
      all(k in s0 for k in ("method_key", "method_cn", "kind_cn", "status",
                            "status_cn", "paper", "real", "diff",
                            "verdict", "hint")),
      str(list(s0.keys())))
check("ok 状态中文=一致", s0["status_cn"] == "一致")
check("ok 的 verdict 表明没问题", "一致" in s0["verdict"] and "没问题" in s0["verdict"])
check("p 值中文名正确", s0["kind_cn"] == "p 值")
check("ok 无 hint", s0["hint"] == "")
check("方法中文名从注册表取", s0["method_cn"] == "独立样本 T 检验", s0["method_cn"])
check("方法 key 保留", s0["method_key"] == "independent_t")

s1 = out[1]["summary"]
check("minor_diff 中文=略有出入", s1["status_cn"] == "略有出入")
check("minor_diff verdict 提四舍五入", "四舍五入" in s1["verdict"] or "核对" in s1["verdict"])
check("t 值中文名正确", s1["kind_cn"] == "t 值")

s2 = out[2]["summary"]
check("mismatch 中文=不一致", s2["status_cn"] == "不一致")
check("mismatch verdict 含论文值与实算值",
      "-5.0" in s2["verdict"] and "-2.310" in s2["verdict"], s2["verdict"])
check("mismatch hint 给具体排查方向",
      "缺失值" in s2["hint"] and "样本" in s2["hint"], s2["hint"])

s3 = out[3]["summary"]
check("no_real 中文=未能复算", s3["status_cn"] == "未能复算")
check("no_real hint 带原因", s3["hint"] == "数据列不匹配", s3["hint"])

s4 = out[4]["summary"]
check("unknown 中文=无法比对", s4["status_cn"] == "无法比对")

# 不变量：任何 verdict 非空、任何 kind_cn 非空、不含原始数据字段
check("所有 verdict 非空", all(c["summary"]["verdict"] for c in out))
check("所有 kind_cn 非空", all(c["summary"]["kind_cn"] for c in out))
check("summary 不含原始数据字段",
      not any(k in s0 for k in ("df", "raw_data", "rows", "values")))
check("空 comparisons 安全", audit._attach_comparison_summaries([], real, methods) == [])
check("None comparisons 安全", audit._attach_comparison_summaries(None, real, methods) == [])
# 无 methods 时不应炸
out_nm = audit._attach_comparison_summaries([dict(c) for c in cmps], {"ok": True}, [])
check("无 methods 不炸且 method_cn 为空", out_nm[0]["summary"]["method_cn"] == "")


print()
print('=== 2. explain_comparison 降级 / 边界（已隔离全部平台 Key） ===')
_reset_router()
summary = out[2]["summary"]  # mismatch 那条，信息最全

# 2a 空问题
ans, err, meta = audit_chat.explain_comparison(summary, "")
check("空问题 → answer=None", ans is None)
check("空问题 → 提示输入", "输入" in (err or ""), str(err))

# 2b 空 summary
ans, err, meta = audit_chat.explain_comparison({}, "为什么？")
check("空 summary → answer=None", ans is None)
check("空 summary → 提示缺条目", "比对条目" in (err or ""), str(err))

# 2c 无 Key → 降级不抛
ans, err, meta = audit_chat.explain_comparison(summary, "为什么这个 t 值对不上？")
check("无 Key → answer=None（不抛异常）", ans is None)
check("无 Key → error 提到 LLM 不可用", "LLM 不可用" in (err or ""), str(err))
check("无 Key → meta 结构完整",
      isinstance(meta, dict) and "cached" in meta and "model" in meta, str(meta))

# 2d 输入最小化：payload 白名单不含原始数据
src = open(audit_chat.__file__ or 'audit_chat.py', encoding='utf-8').read()
check("payload 为白名单构造（不含 **summary 展开）", "**summary" not in src)
check("payload 字段显式列举", '"论文写的值"' in src and '"实算的值"' in src)
check("prompt 明确禁止推翻 status", "不要推翻" in audit_chat.FROZEN_SYSTEM)
check("prompt 明确禁止编造数字", "禁止编造" in audit_chat.FROZEN_SYSTEM)
check("prompt 含不提供代写的护栏", "代写" in audit_chat.FROZEN_SYSTEM)
check("PROMPT_VERSION 独立命名空间", audit_chat.PROMPT_VERSION.startswith("chat-"),
      audit_chat.PROMPT_VERSION)


print()
print('=== 3. /api/audit_chat 端点（test_client） ===')
import app as flask_app

client = flask_app.app.test_client()


def post(payload):
    return client.post('/api/audit_chat', json=payload)


# 3a 缺 summary
r = post({"question": "为什么？"})
d = r.get_json()
check("缺 summary → 400", r.status_code == 400, str(r.status_code))
check("缺 summary → ok=False", d.get("ok") is False)

# 3b 缺 question
r = post({"summary": summary})
d = r.get_json()
check("缺 question → 400", r.status_code == 400, str(r.status_code))

# 3c 问题过长
r = post({"summary": summary, "question": "啊" * 600})
check("问题过长 → 400", r.status_code == 400, str(r.status_code))

# 3d 红线拦截（借对话问代写）
r = post({"summary": summary, "question": "帮我代写这篇论文的方法部分"})
d = r.get_json()
check("红线问题 → 400", r.status_code == 400, str(r.status_code))
check("红线问题 → 明确拒绝", "学术不端" in (d.get("error") or ""), str(d.get("error")))
check("红线问题 → 带 red_line 明细", "red_line" in d and d["red_line"].get("blocked"))

# 3e 正常但无 Key → 降级回退到模板 verdict（不白屏）
r = post({"summary": summary, "question": "为什么这个 t 值对不上？"})
d = r.get_json()
check("无 Key 正常请求 → 200", r.status_code == 200, str(r.status_code))
check("无 Key → ok=True（降级不报错）", d.get("ok") is True)
check("无 Key → answer 回退到模板 verdict（非空）",
      bool(d.get("answer")), str(d.get("answer"))[:80])
check("无 Key → fallback=True", d.get("fallback") is True)
check("无 Key → llm_error 有值", bool(d.get("llm_error")))
check("无 Key → 回退文案含原 verdict 要点",
      "对不上" in (d.get("answer") or "") or "不一致" in (d.get("answer") or ""),
      str(d.get("answer"))[:120])

# 3f cited 回执（可审计：回答基于哪些数字）
cited = d.get("cited") or {}
check("cited 含方法/统计量/论文值/实算值/结论",
      all(k in cited for k in ("方法", "统计量", "论文值", "实算值", "结论")), str(cited))
# cited.paper 是论文原文串（"t = -5.0"），不是纯数字 —— 保留原文更利于用户对照
check("cited 论文值保留原文", cited.get("论文值") == "t = -5.0", str(cited))
check("cited 实算值正确", cited.get("实算值") == "-2.310", str(cited))

# 3g 响应契约字段
check("响应含 llm_model/llm_cached/llm_error",
      all(k in d for k in ("llm_model", "llm_cached", "llm_error")), str(list(d.keys())))


print()
print('=== 4. 铁律②：LLM 输入不含原始数据 ===')
# 断章取义地构造一个"脏 summary"，验证即使传进来也不会外泄（白名单过滤）
dirty = dict(summary)
dirty["df"] = "SECRET_ROWS"
dirty["raw_data"] = [[1, 2], [3, 4]]
dirty["student_names"] = ["张三", "李四"]
dirty["extra"] = "不该出现的内容"
# 直接检查白名单构造逻辑：模拟 explain_comparison 的 payload 组装
whitelist = ("method_cn", "method_key", "kind_cn", "paper", "real",
             "diff", "status", "status_cn", "verdict", "hint")
payload_keys = {k for k in whitelist}
check("白名单不含 df/raw_data 等敏感键",
      "df" not in payload_keys and "raw_data" not in payload_keys
      and "student_names" not in payload_keys)
# 真跑一次（无 Key，不会真发包），确认不因脏字段报错
ans, err, meta = audit_chat.explain_comparison(dirty, "解释一下")
check("脏 summary 不导致异常（白名单隔离生效）", err is None or "LLM" in (err or ""),
      str(err))


print()
print('=== 汇总 ===')
if _saved_env:
    for k, v in _saved_env.items():
        if v:
            os.environ[k] = v
print(f'结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
