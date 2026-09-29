"""
自定义供应商（custom provider）离线测试（v2.31）
========================================================
全 mock，不打真 API。验证：
    1. CustomAgent：模型名直传 / 缺 Base URL 或缺模型名时实例化失败
    2. Router.set_custom_provider：链首插入、BYOK 通行、请求级隔离
    3. _make_agent 把上下文里的 base_url 传给 Agent
    4. 视觉状态（audit_image）不放行自定义模型
    5. app._apply_byok：三项齐才启用，缺一项不启用
运行：python custom_provider_test.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


# ---------------------------------------------------------------------------
print("[1] CustomAgent 基本行为")
# ---------------------------------------------------------------------------
from agents.base import AgentError
from agents.custom_agent import CustomAgent
from agents.router import Router, STATE_TO_MODEL

# 1.1 模型名直传 + 显式 base_url/key
a = CustomAgent(model="my-own-model-id",
                base_url="https://api.example.com/v1/",
                api_key="sk-test-custom-000000")
check("模型名直传（不做别名映射）", a.model_name == "my-own-model-id",
      f"实际={a.model_name!r}")
check("base_url 末尾斜杠被规整", a.base_url == "https://api.example.com/v1",
      f"实际={a.base_url!r}")
check("BYOK Key 生效", a._api_keys[0] == "sk-test-custom-000000")

# 1.2 缺 base_url → 实例化失败（环境变量也不给）
os.environ.pop("CUSTOM_LLM_BASE_URL", None)
os.environ.pop("CUSTOM_LLM_API_KEY", None)
try:
    CustomAgent(model="m1", api_key="sk-x-123456789012")
    check("缺 Base URL 时实例化抛 AgentError", False, "没有抛错")
except AgentError:
    check("缺 Base URL 时实例化抛 AgentError", True)

# 1.3 缺模型名 → 实例化失败
try:
    CustomAgent(model="", base_url="https://api.example.com/v1",
                api_key="sk-x-123456789012")
    check("缺模型名时实例化抛 AgentError", False, "没有抛错")
except AgentError:
    check("缺模型名时实例化抛 AgentError", True)

# ---------------------------------------------------------------------------
print("[2] Router.set_custom_provider：链首插入 + BYOK 通行 + 隔离")
# ---------------------------------------------------------------------------
r = Router()
r.set_custom_provider("https://api.example.com/v1", "sk-ctx-key-000000000",
                      "model-a, model-b ,")

chain = r._ordered_chain("recommend")
check("custom 两个模型排链首且保序",
      chain[0] == ("custom", "model-a", STATE_TO_MODEL["recommend"][0][2])
      and chain[1] == ("custom", "model-b", STATE_TO_MODEL["recommend"][0][2]),
      f"实际前两位={chain[:2]}")
check("原有候选仍在链上", ("zhipu", "glm-4.7-flash", 0.5) in chain)
check("BYOK 通行（free 档放行 custom）", r._allowed("custom", "model-a"))
check("custom 记入 _user_keys", "custom" in r._user_keys)

# 2.2 实例化拿得到上下文 base_url（懒加载不建客户端，零网络）
agent = r._make_agent("custom", "model-a", 0.5)
check("_make_agent 传入上下文 base_url",
      agent.base_url == "https://api.example.com/v1",
      f"实际={agent.base_url!r}")
check("_make_agent 传入用户 Key", agent._api_keys[0] == "sk-ctx-key-000000000")

# 2.3 请求级隔离：新请求（新 ContextVar 上下文）看不到上一请求的配置
import contextvars  # noqa: F401

ctx = contextvars.copy_context()


def _in_fresh_context():
    r2 = Router()
    return r2._custom_cfg, "custom" in r2._user_keys


cfg2, leaked = ctx.run(_in_fresh_context)
check("新上下文无 custom 配置（零残留）", cfg2 is None and not leaked)

# 2.4 视觉状态不放行自定义模型（无法验证读图能力）
img_chain = r._ordered_chain("audit_image")
check("audit_image 链上无 custom",
      all(p != "custom" for p, _m, _t in img_chain))

# 2.5 配置不完整 = 不启用
r3 = Router()
r3.set_custom_provider("", "sk-k", "m1")            # 缺 base_url
r3.set_custom_provider("https://x/v1", "sk-k", " ")  # 缺模型
check("配置不完整时不启用 custom",
      (not r3._custom_cfg or not r3._custom_cfg.get("providers"))
      and "custom" not in r3._user_keys)

# ---------------------------------------------------------------------------
print("[3] app._apply_byok：三项齐才启用")
# ---------------------------------------------------------------------------
from app import _apply_byok

r4 = Router()
n = _apply_byok(r4, lambda f: {
    "custom_base_url": "https://api.example.com/v1",
    "custom_key": "sk-app-000000000000",
    "custom_model": "m1,m2",
}.get(f))
check("三项齐 → 启用（返回计数含 custom）", n == 1)
check("链首是 custom/m1", r4._ordered_chain("recommend")[0][:2] == ("custom", "m1"))

r5 = Router()
n5 = _apply_byok(r5, lambda f: {
    "custom_base_url": "https://api.example.com/v1",
    "custom_key": "sk-app-000000000000",
    # 缺 custom_model
}.get(f))
check("缺模型名 → 不启用", n5 == 0 and r5._custom_cfg is None)

# ---------------------------------------------------------------------------
print("[4] 多自定义供应商（v2.33：多家按序容灾）")
# ---------------------------------------------------------------------------
r6 = Router()
r6.set_custom_providers([
    {"name": "中转A", "base_url": "https://a.example.com/v1/",
     "key": "sk-aaa-000000000000", "models": "m-a1, m-a2"},
    {"name": "中转B", "base_url": "https://b.example.com/v1",
     "key": "sk-bbb-000000000000", "models": ["m-b1"]},
    {"base_url": "", "key": "sk-ccc", "models": "m-c1"},   # 缺 base_url 跳过
])
chain6 = r6._ordered_chain("recommend")
head6 = [(p, m) for p, m, _t in chain6][:3]
check("多家按序排链首(A 两模型 → B 一模型)",
      head6 == [("custom1", "m-a1"), ("custom1", "m-a2"), ("custom2", "m-b1")],
      f"实际={head6}")
check("不完整条目被跳过", all(p != "custom3" for p, _m, _t in chain6))
check("两家 Key 均注入(BYOK 通行)",
      r6._allowed("custom1", "m-a1") and r6._allowed("custom2", "m-b1"))
a1 = r6._make_agent("custom1", "m-a1", 0.5)
b1 = r6._make_agent("custom2", "m-b1", 0.5)
check("A 家拿到自己的 base_url(去尾斜杠)", a1.base_url == "https://a.example.com/v1",
      f"实际={a1.base_url!r}")
check("B 家拿到自己的 base_url 与 Key",
      b1.base_url == "https://b.example.com/v1" and b1._api_keys[0] == "sk-bbb-000000000000")
check("视觉状态不放行任何自定义", all(
    p == "custom" or not p.startswith("custom")
    for p, _m, _t in r6._ordered_chain("audit_image")))

# ---------------------------------------------------------------------------
print()
print(f"结果:{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
