"""
Kimi + 小米 MiMo 接入离线测试（v1.8，全 mock，零 API 调用）
================================================================
覆盖 2026-09-14 用真实 Key 实测（kimi_mimo_live_test.py 12/12）沉淀下来的
平台事实，离线锁定、防止回归：

    1. PROVIDER_REGISTRY 登记完整（base_url / env_var / 显示名）
    2. 模型别名解析：kimi-k2.6 原样 / mimo 小写别名 → 官方 ID /
       未登记名透传 / 不被硅基流动 COMMON_MODELS 改写
    3. Kimi 温度约束自动适配：非 1 温度被平台 400 拒 → 自动锁定
       temperature=1 重试一次成功，且记忆约束（后续不再撞）
    4. Kimi / MiMo 空内容防护（思考模型吃光 max_tokens → AgentError）
    5. MiMo 客户端超时放宽（connect=15s；实测首连 6s+）
    6. 路由容灾链：两家都在链上；BYOK 后排链首
    7. free 档放行规则：默认不放行付费平台；set_user_key 后放行
    8. BYOK 字段对齐：app.py 的 kimi_key / mimo_key → provider 注入

用法：
    .venv/Scripts/python.exe kimi_mimo_test.py
（真实 API 冒烟见 kimi_mimo_live_test.py，有 Key 才跑）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for v in ("KIMI_API_KEY", "MIMO_API_KEY"):
    os.environ.pop(v, None)

from agents import AgentError, KimiAgent, MimoAgent, Router
from agents.openai_compat import PROVIDER_REGISTRY
from agents.router import STATE_TO_MODEL

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------------------
print("[1] Provider 注册表登记")
# ---------------------------------------------------------------------------
k_cfg = PROVIDER_REGISTRY["kimi"]
check("kimi base_url", k_cfg.base_url == "https://api.moonshot.cn/v1",
      f"实际={k_cfg.base_url}")
check("kimi env_var", k_cfg.env_var == "KIMI_API_KEY")
m_cfg = PROVIDER_REGISTRY["mimo"]
check("mimo base_url", m_cfg.base_url == "https://api.xiaomimimo.com/v1",
      f"实际={m_cfg.base_url}")
check("mimo env_var", m_cfg.env_var == "MIMO_API_KEY")

# ---------------------------------------------------------------------------
print("\n[2] 模型别名解析（不被硅基流动 COMMON_MODELS 改写）")
# ---------------------------------------------------------------------------
os.environ["KIMI_API_KEY"] = "k-test"
os.environ["MIMO_API_KEY"] = "m-test"

k = KimiAgent(model="kimi-k2.6")
check("kimi-k2.6 原样解析", k.model_name == "kimi-k2.6", f"实际={k.model_name}")
k2 = KimiAgent(model="kimi-k2.7-code")
check("kimi-k2.7-code 原样解析", k2.model_name == "kimi-k2.7-code")
k3 = KimiAgent(model="some-future-model")
check("未登记名透传", k3.model_name == "some-future-model")

m = MimoAgent(model="mimo-v2.5")
check("mimo-v2.5 原样解析", m.model_name == "mimo-v2.5", f"实际={m.model_name}")
m2 = MimoAgent(model="mimo-v2.5-pro")
check("mimo-v2.5-pro 原样解析", m2.model_name == "mimo-v2.5-pro")
m3 = MimoAgent(model="MiMo-V2-Flash")  # 旧写法 → 就近映射
check("旧写法 MiMo-V2-Flash 映射到 mimo-v2.5", m3.model_name == "mimo-v2.5",
      f"实际={m3.model_name}")

# ---------------------------------------------------------------------------
print("\n[3] Kimi 温度约束自动适配（mock 父类，模拟平台 400）")
# ---------------------------------------------------------------------------
import agents.siliconflow_agent as sf_mod

_orig_complete = sf_mod.SiliconFlowAgent.complete
_calls: list = []


def _fake_complete(self, prompt, *, temperature=None, max_tokens=None, system=None):
    """模拟 Kimi 平台：temperature != 1 一律 400。"""
    _calls.append(temperature)
    if temperature != 1.0:
        raise AgentError(
            "Kimi（月之暗面） 调用失败：Error code: 400 - "
            "{'error': {'message': 'invalid temperature: only 1 is allowed "
            "for this model'}}")
    return "测试回答"


sf_mod.SiliconFlowAgent.complete = _fake_complete
try:
    ka = KimiAgent(model="kimi-k2.6")
    out = ka.complete("你好", max_tokens=64)
    check("非 1 温度自动重试成功", out == "测试回答")
    check("撞过一次后锁定 temperature=1", ka._temp_forced is True)
    check("重试只发生一次（第一次 None → 第二次 1.0）",
          _calls == [None, 1.0], f"实际={_calls}")
    _calls.clear()
    ka.complete("再问一次")
    check("后续调用直接用 1.0（不再撞 400）", _calls == [1.0], f"实际={_calls}")
finally:
    sf_mod.SiliconFlowAgent.complete = _orig_complete

# 空 content 防护
sf_mod.SiliconFlowAgent.complete = \
    lambda self, prompt, **kw: "   "  # 空白内容
try:
    ka2 = KimiAgent(model="kimi-k2.6")
    try:
        ka2.complete("x")
        check("Kimi 空内容抛 AgentError", False, "居然没抛")
    except AgentError as e:
        check("Kimi 空内容抛 AgentError", "空内容" in str(e))
finally:
    sf_mod.SiliconFlowAgent.complete = _orig_complete

# ---------------------------------------------------------------------------
print("\n[4] MiMo 默认 max_tokens + 空内容防护 + 超时放宽")
# ---------------------------------------------------------------------------
check("MiMo 默认 max_tokens=4096（思考模型防吃光）",
      MimoAgent(model="mimo-v2.5").default_max_tokens == 4096)
sf_mod.SiliconFlowAgent.complete = lambda self, prompt, **kw: ""
try:
    ma = MimoAgent(model="mimo-v2.5")
    try:
        ma.complete("x")
        check("MiMo 空内容抛 AgentError", False, "居然没抛")
    except AgentError as e:
        check("MiMo 空内容抛 AgentError", "空内容" in str(e))
finally:
    sf_mod.SiliconFlowAgent.complete = _orig_complete

client = MimoAgent(model="mimo-v2.5")._build_client()  # 不发请求，只构建
check("MiMo 客户端构建成功（openai.Timeout 放宽连接超时）", client is not None)

# ---------------------------------------------------------------------------
print("\n[5] 路由链 + BYOK 规则")
# ---------------------------------------------------------------------------
for state, expect_kimi, expect_mimo in (
    ("paper_check", "kimi-k2.6", "mimo-v2.5-pro"),
    ("write_text", None, "mimo-v2.5"),
    ("recommend", None, "mimo-v2.5"),
    ("audit_chat", None, "mimo-v2.5"),
):
    chain = STATE_TO_MODEL[state]
    kimi_hit = next((c for c in chain if c[0] == "kimi"), None)
    mimo_hit = next((c for c in chain if c[0] == "mimo"), None)
    if expect_kimi:
        check(f"{state} 链上有 kimi 且模型={expect_kimi}",
              kimi_hit is not None and kimi_hit[1] == expect_kimi,
              f"实际={kimi_hit}")
    check(f"{state} 链上有 mimo 且模型={expect_mimo}",
          mimo_hit is not None and mimo_hit[1] == expect_mimo,
          f"实际={mimo_hit}")

r = Router()
check("free 档默认不放行 kimi", not r._allowed("kimi", "kimi-k2.6"))
check("free 档默认不放行 mimo", not r._allowed("mimo", "mimo-v2.5"))
r.set_user_key("kimi", "user-key")
check("BYOK 后 free 档放行 kimi", r._allowed("kimi", "kimi-k2.6"))
check("BYOK 后 kimi 排链首",
      r._ordered_chain("paper_check")[0][0] == "kimi",
      f"实际={r._ordered_chain('paper_check')[0][0]}")
r.set_user_key("mimo", "user-key-2")
check("双 BYOK 后链首二选一（kimi/mimo）",
      r._ordered_chain("write_text")[0][0] in ("kimi", "mimo"),
      f"实际={r._ordered_chain('write_text')[0][0]}")
check("没填 Key 的付费候选仍被跳过（deepseek）",
      not r._allowed("deepseek", "deepseek-flash"))

# BYOK 注入的 Agent 真用用户的 Key
agent = r._make_agent("kimi", "kimi-k2.6", 0.3)
check("kimi Agent 用用户 Key", agent._api_keys[0] == "user-key",
      f"实际={agent._api_keys[0]!r}")

# ---------------------------------------------------------------------------
print("\n[6] app.py BYOK 字段对齐（静态源码检查）")
# ---------------------------------------------------------------------------
app_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "app.py"), encoding="utf-8").read()
check("app.py 六家 BYOK 字段齐备",
      all(f'"{f}"' in app_src for f in
          ("siliconflow_key", "zhipu_key", "deepseek_key",
           "dashscope_key", "kimi_key", "mimo_key")))

print(f"\n{'=' * 50}")
print(f"Kimi + MiMo 离线测试：{PASS} 通过 / {FAIL} 失败")
print(f"{'=' * 50}")
sys.exit(1 if FAIL else 0)
