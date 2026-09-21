"""
付费调用熔断测试（v2.29 · LLM_PAID_MAX_CALLS）
==============================================
全部用 mock Agent（不打真实 API），覆盖：

    1. 默认（未设 LLM_PAID_MAX_CALLS）不熔断，付费模型照常走
    2. 设上限后，项目方付费调用到达上限即被熔断（候选跳过、报错标注"付费熔断"）
    3. 熔断只拦付费模型：免费模型（FREE_MODELS 白名单）不受计数影响
    4. BYOK（用户自带 Key）调用不计入熔断——费用用户自担
    5. 计费发生在发请求之前（先记账后调用），失败调用也占额度
    6. paid_calls() 观测口径：used / cap 正确
    7. 非法/负数上限按"不启用"处理
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 测试全程不需要真实 Key（mock Agent 代替）
for _k in ("SILICONFLOW_API_KEY", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY",
           "DASHSCOPE_API_KEY", "KIMI_API_KEY", "MIMO_API_KEY", "MAAS_API_KEY"):
    os.environ.pop(_k, None)
os.environ.pop("LLM_PAID_MAX_CALLS", None)

from agents.base import AgentError, BaseAgent
from agents.router import Router, FREE_MODELS

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name} {detail}')


class MockAgent(BaseAgent):
    """可编程 mock：script 每项 str=成功返回 / Exception=抛出。"""
    def __init__(self, model_name, script):
        self.model_name = model_name
        self.script = list(script)
        self.calls = 0

    def complete(self, prompt, *, temperature=None, max_tokens=None, **kw):
        self.calls += 1
        action = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(action, Exception):
            raise action
        return action


def make_router(agents_spec, tier="pro"):
    """mock Router：替换 _make_agent，按 (provider, model) 返回预设 Agent。"""
    r = Router(tier=tier)

    def fake_make(provider, model, temp):
        key = (provider, model)
        if key in agents_spec:
            return agents_spec[key]
        raise AgentError(f"环境变量 MOCK_{provider.upper()}_API_KEY 未设置。")

    r._make_agent = fake_make
    return r


# paper_check 链（当前）：deepseek/deepseek-flash（付费）→ sf/deepseek-v4-pro（付费）
#                     → zhipu/glm-4.7-flash（免费）→ maas/glm-5（付费）
#                     → kimi/kimi-k2.6（付费）→ mimo/mimo-v2.5-pro（付费）
PAID1 = ("deepseek", "deepseek-flash")
PAID2 = ("sf", "deepseek-v4-pro")
FREE1 = ("zhipu", "glm-4.7-flash")

assert PAID1 not in FREE_MODELS and PAID2 not in FREE_MODELS, "测试前提：这两个不在免费白名单"
assert FREE1 in FREE_MODELS, "测试前提：glm-4.7-flash 在免费白名单"


print('=== 1. 默认不熔断（未设 LLM_PAID_MAX_CALLS）===')
os.environ.pop("LLM_PAID_MAX_CALLS", None)
a = MockAgent("deepseek-flash", ["付费成功"])
r = make_router({PAID1: a})
out = r.complete("paper_check", "测试")
check("未设上限时付费模型照常调用", out == "付费成功")
check("付费调用已记账（used=1）", r.paid_calls()["used"] == 1)
check("未启用时 cap=0", r.paid_calls()["cap"] == 0)

print()
print('=== 2. 上限到达 → 付费候选被熔断跳过 ===')
os.environ["LLM_PAID_MAX_CALLS"] = "2"
p1 = MockAgent("deepseek-flash", ["付费A", "付费A2", "付费A3"])
p2 = MockAgent("deepseek-v4-pro", ["付费B"])
f1 = MockAgent("glm-4.7-flash", ["免费兜底"])
r = make_router({PAID1: p1, PAID2: p2, FREE1: f1})
# maas/kimi/mimo 无 mock（缺 Key 自动跳过），不影响断言
r.complete("paper_check", "x1")   # 付费 1
r.complete("paper_check", "x2")   # 付费 2（resolved 复用 PAID1）
out = r.complete("paper_check", "x3")   # 第 3 次：付费候选全熔断 → 免费兜底
check("第 3 次落到免费兜底", out == "免费兜底")
check("付费 A 只被调 2 次", p1.calls == 2)
check("付费 B 一次未调（熔断在选中前就跳过）", p2.calls == 0)
check("熔断计数停在 2", r.paid_calls()["used"] == 2)

print()
print('=== 3. 熔断报错文案可辨（全链只剩付费时）===')
import agents.router as _R
_orig = _R.STATE_TO_MODEL
_R.STATE_TO_MODEL = {"paper_check": [PAID1 + (0.3,)]}
try:
    os.environ["LLM_PAID_MAX_CALLS"] = "0"   # cap=0 不启用
    r_only = make_router({PAID1: MockAgent("deepseek-flash", ["ok"])})
    r_only.complete("paper_check", "x")
    check("cap=0 视为不启用，付费照常", True)
    os.environ["LLM_PAID_MAX_CALLS"] = "1"
    r_b = make_router({PAID1: MockAgent("deepseek-flash", ["ok", "ok2"])})
    r_b.complete("paper_check", "x")
    try:
        r_b.complete("paper_check", "y")
        check("超限后应抛 AgentError", False, "居然成功了")
    except AgentError as e:
        check("超限报错含『付费熔断』", "付费熔断" in str(e), f"实际={e}")
finally:
    _R.STATE_TO_MODEL = _orig

print()
print('=== 4. BYOK（用户自带 Key）不计入熔断 ===')
os.environ["LLM_PAID_MAX_CALLS"] = "1"
u = MockAgent("deepseek-flash", ["BYOK-1", "BYOK-2", "BYOK-3"])
r = make_router({PAID1: u})
r.set_user_key("deepseek", "sk-user-own-key")
for i in range(3):
    out = r.complete("paper_check", f"byok-{i}")
check("BYOK 调用 3 次全部放行（不受 cap=1 限制）", out == "BYOK-3" and u.calls == 3)
check("BYOK 不计入付费计数（used=0）", r.paid_calls()["used"] == 0)

print()
print('=== 5. 失败的付费调用也占额度（先记账后调用）===')
os.environ["LLM_PAID_MAX_CALLS"] = "1"
bad = MockAgent("deepseek-flash", [AgentError("模拟 500")])
ok_free = MockAgent("glm-4.7-flash", ["免费接住"])
r = make_router({PAID1: bad, FREE1: ok_free})
out = r.complete("paper_check", "x")
check("付费失败 → 落到免费兜底", out == "免费接住")
check("失败的付费调用也记账（used=1）", r.paid_calls()["used"] == 1,
      f"实际={r.paid_calls()}")

print()
print('=== 6. 并发下计数不丢（threading）===')
os.environ["LLM_PAID_MAX_CALLS"] = "1000"
m = MockAgent("deepseek-flash", ["ok"] * 60)
r = make_router({PAID1: m})
threads = [threading.Thread(target=r.complete, args=("paper_check", f"t{i}"))
           for i in range(50)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("50 线程并发后 used=50", r.paid_calls()["used"] == 50,
      f"实际={r.paid_calls()['used']}")

print()
print('=== 7. 非法上限按不启用处理 ===')
for bad_val in ("abc", "", "-5"):
    os.environ["LLM_PAID_MAX_CALLS"] = bad_val
    r = make_router({PAID1: MockAgent("deepseek-flash", ["ok"] * 3)})
    try:
        r.complete("paper_check", "x")
        check(f"上限={bad_val!r} 不熔断", True)
    except AgentError as e:
        check(f"上限={bad_val!r} 不熔断", False, f"居然报错：{e}")

os.environ.pop("LLM_PAID_MAX_CALLS", None)

print()
print(f'=== 结果：{PASS} 通过 / {FAIL} 失败 ===')
sys.exit(1 if FAIL else 0)
