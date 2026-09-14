"""
Router 运行时容灾 + 429 冷却测试（v0.5）
=========================================
全部用 mock Agent（不打真实 API），覆盖：

    1. 运行时失败 → 冷却设置 + 自动切容灾链下一个
    2. 冷却中的 Agent 被跳过（不实例化、不调用）
    3. 全链失败 → AgentError 汇总（含每环失败原因）
    4. 冷却到期 → 自动回到首选（不再用备胎）
    5. 缺 Key（实例化失败）不进冷却表
    6. 成功调用后 _resolved 更新；失败后 _resolved 重置
    7. paper_check 新降级链（V4-Pro → V4-Flash）
    8. health() 冷却展示
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 测试全程不需要真实 Key（mock Agent 代替）
os.environ.pop("SILICONFLOW_API_KEY", None)
os.environ.pop("ZHIPU_API_KEY", None)

from agents.base import AgentError, BaseAgent
from agents.router import Router, STATE_TO_MODEL, COOLDOWN_SECONDS

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
    """可编程 mock：按脚本依次返回/抛错。"""
    def __init__(self, model_name, script):
        self.model_name = model_name
        self.script = list(script)   # 每项: str=成功返回 | Exception=抛出
        self.calls = 0

    def complete(self, prompt, *, temperature=None, max_tokens=None, **kw):
        self.calls += 1
        action = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(action, Exception):
            raise action
        return action


def make_router_with(state, agents_spec, tier="pro"):
    """构造 mock Router：替换 _make_agent，按 (provider, model) 返回预设 Agent。

    agents_spec = {(provider, model): MockAgent}

    v0.5.3：默认显式用 pro 档 —— 这些用例测的是完整容灾链（含付费档候选），
    不能受免费档白名单过滤影响。档位专项测试传 tier="free"。
    """
    r = Router(tier=tier)

    def fake_make(provider, model, temp):
        key = (provider, model)
        if key in agents_spec:
            return agents_spec[key]
        raise AgentError(f"环境变量 MOCK_{provider.upper()}_API_KEY 未设置。")

    r._make_agent = fake_make
    return r


E429 = AgentError("SiliconFlow 调用失败：Error code: 429 - Rate limit reached")
E500 = AgentError("SiliconFlow 调用失败：Error code: 500 - Internal Server Error")

print('=== 1. 运行时失败 → 冷却 + 切换容灾链 ===')
# write_text 链：zhipu/glm-4.7-flash（抛 429）→ sf/deepseek-v4-flash（成功）
a1 = MockAgent("glm-4.7-flash", [E429])
a2 = MockAgent("deepseek-ai/DeepSeek-V4-Flash", ["备胎成功"])
r = make_router_with("write_text", {
    ("zhipu", "glm-4.7-flash"): a1,
    ("sf", "deepseek-v4-flash"): a2,
})
result = r.complete("write_text", "测试")
check("429 后自动切到备胎", result == "备胎成功")
check("首选被调用过 1 次（429）", a1.calls == 1)
check("备胎被调用 1 次", a2.calls == 1)
key1 = ("zhipu", "glm-4.7-flash")
check("429 的 Agent 进入冷却表", r._cooldown_until.get(key1, 0) > time.time())
check("冷却时长 = COOLDOWN_SECONDS", abs(r._cooldown_until[key1] - time.time() - COOLDOWN_SECONDS) < 5)
check("resolved 指向备胎", r._resolved.get("write_text") == ("sf", "deepseek-v4-flash"))

print()
print('=== 2. 冷却中的 Agent 被跳过 ===')
# 再调一次：首选还在冷却 → 直接用备胎（不再碰首选）
before = a1.calls
result = r.complete("write_text", "测试2")
check("冷却中首选不被调用", a1.calls == before)
check("仍走备胎", result == "备胎成功")

print()
print('=== 3. 冷却到期 → 回到首选 ===')
# 手动把冷却时间改到过去（模拟到期）
r._cooldown_until[key1] = time.time() - 1
a1.script = ["首选恢复"]
result = r.complete("write_text", "测试3")
check("到期后回到首选", result == "首选恢复")
check("resolved 回到首选", r._resolved.get("write_text") == key1)

print()
print('=== 4. 全链失败 → AgentError 汇总 ===')
b1 = MockAgent("glm-4.7-flash", [E429, E429])
b2 = MockAgent("deepseek-ai/DeepSeek-V4-Flash", [E500, E500])
r2 = make_router_with("write_text", {
    ("zhipu", "glm-4.7-flash"): b1,
    ("sf", "deepseek-v4-flash"): b2,
})
try:
    r2.complete("write_text", "测试")
    check("全链失败应抛 AgentError", False)
except AgentError as e:
    msg = str(e)
    check("全链失败抛 AgentError", "容灾链全部失败" in msg)
    check("汇总含 429 环节", "429" in msg and "glm-4.7-flash" in msg)
    check("汇总含 500 环节", "500" in msg and "deepseek-v4-flash" in msg)
# 全部进冷却
check("两环都进冷却", r2._cooling(("zhipu", "glm-4.7-flash")) and r2._cooling(("sf", "deepseek-v4-flash")))
# 全冷却后再调 → 直接报"冷却中"（不再打 API）
try:
    r2.complete("write_text", "测试")
    check("全冷却时快速失败", False)
except AgentError as e:
    check("全冷却时快速失败（不再调 API）",
          "冷却中" in str(e) and b1.calls == 1 and b2.calls == 1)

print()
print('=== 5. 缺 Key（实例化失败）不进冷却表 ===')
# recommend 链：zhipu 缺 Key（mock 没给）→ sf 有
c2 = MockAgent("THUDM/GLM-Z1-9B-0414", ["免费档兜底"])
r3 = make_router_with("recommend", {
    ("sf", "glm-z1-9b"): c2,
})
result = r3.complete("recommend", "测试")
check("缺 Key 走容灾链下一个", result == "免费档兜底")
check("缺 Key 不进冷却表", ("zhipu", "glm-4.7-flash") not in r3._cooldown_until)

print()
print('=== 6. paper_check 降级链（官方直连 → 硅基流动 V4-Pro）===')
check("paper_check 链含付费 2 个 + 免费兜底 + MaaS 免费尾位（v1.8 允许 BYOK 备胎）",
      len(STATE_TO_MODEL["paper_check"]) >= 4
      and ("deepseek", "deepseek-flash", 0.3) in STATE_TO_MODEL["paper_check"]
      and ("sf", "deepseek-v4-pro", 0.3) in STATE_TO_MODEL["paper_check"]
      and ("maas", "glm-5", 0.3) in STATE_TO_MODEL["paper_check"],
      f'实际({len(STATE_TO_MODEL["paper_check"])}): {STATE_TO_MODEL["paper_check"]}')
p1 = MockAgent("deepseek-flash", [E500, E500])
p2 = MockAgent("deepseek-ai/DeepSeek-V4-Pro", ["降级成功"])
r4 = make_router_with("paper_check", {
    ("deepseek", "deepseek-flash"): p1,
    ("sf", "deepseek-v4-pro"): p2,
})
result = r4.complete("paper_check", "测试")
check("官方挂了降级 V4-Pro", result == "降级成功")
check("deepseek-flash 进冷却", r4._cooling(("deepseek", "deepseek-flash")))

print()
print('=== 7. health() 冷却展示 ===')
h = r4.health()
check("health 含冷却提示", "冷却中" in h["paper_check"] and "deepseek-flash" in h["paper_check"])
check("health 显示实际路由", "V4-Pro" in h["paper_check"])

print()
print('=== 8. analyze 拒绝 / 未知状态 ===')
try:
    r4.complete("analyze", "x")
    check("analyze 拒绝", False)
except AgentError as e:
    check("analyze 拒绝", "Python 计算层" in str(e))
try:
    r4.complete("nope", "x")
    check("未知状态报错", False)
except AgentError as e:
    check("未知状态报错", "未知状态" in str(e))

print()
print('=== 9. 档位过滤（free 默认 / pro 会员，v0.5.3）===')
check("默认档位 = free（面向免费用户）", Router().tier == "free")
check("构造参数可指定 pro", Router(tier="pro").tier == "pro")
check("未识别档位回落 free", Router(tier="weird").tier == "free")

# free 档：paper_check 付费候选被跳过 → 落到免费兜底 zhipu/glm-4.7-flash
paid1 = MockAgent("deepseek-flash", ["付费档不该被调用"])
paid2 = MockAgent("deepseek-v4-pro", ["付费档不该被调用2"])
free_fb = MockAgent("glm-4.7-flash", ["免费兜底成功"])
rf = make_router_with("paper_check", {
    ("deepseek", "deepseek-flash"): paid1,
    ("sf", "deepseek-v4-pro"): paid2,
    ("zhipu", "glm-4.7-flash"): free_fb,
}, tier="free")
res = rf.complete("paper_check", "测试")
check("free 档落到免费兜底", res == "免费兜底成功", f"实际={res}")
check("free 档不调用付费模型", paid1.calls == 0 and paid2.calls == 0)
check("free 档实际路由 = glm-4.7-flash",
      rf._resolved.get("paper_check") == ("zhipu", "glm-4.7-flash"))

# pro 档：付费首选正常命中
paid3 = MockAgent("deepseek-flash", ["付费首选命中"])
rp = make_router_with("paper_check", {
    ("deepseek", "deepseek-flash"): paid3,
    ("sf", "deepseek-v4-pro"): MockAgent("v4-pro", ["备胎"]),
    ("zhipu", "glm-4.7-flash"): MockAgent("glm-4.7-flash", ["免费兜底"]),
}, tier="pro")
res2 = rp.complete("paper_check", "测试")
check("pro 档走付费首选", res2 == "付费首选命中", f"实际={res2}")

# _get_agent 路径同样受档位约束（llm_enhance 用它取模型名进缓存 key）
rf2 = make_router_with("write_text", {
    ("zhipu", "glm-4.7-flash"): MockAgent("glm-4.7-flash", ["x"]),
    ("sf", "deepseek-v4-flash"): MockAgent("deepseek-v4-flash", ["x"]),
    ("dashscope", "qwen3.7-flash"): MockAgent("qwen3.7-flash", ["x"]),
}, tier="free")
check("_get_agent 在 free 档选免费模型",
      rf2._get_agent("write_text").model_name == "glm-4.7-flash")

# v0.5.4：MaaS 专属端点属免费档（成本由项目方承担），free 档可达
rf3 = make_router_with("paper_check", {
    ("deepseek", "deepseek-flash"): MockAgent("deepseek-flash", ["付费"]),
    ("sf", "deepseek-v4-pro"): MockAgent("deepseek-v4-pro", ["付费"]),
    ("zhipu", "glm-4.7-flash"): MockAgent("glm-4.7-flash", [E500]),
    ("maas", "glm-5"): MockAgent("glm-5", ["MaaS 兜底成功"]),
}, tier="free")
check("free 档可达 MaaS 尾位（v0.5.4）",
      rf3.complete("paper_check", "x") == "MaaS 兜底成功")

# set_tier 切换 + 清缓存
rf.set_tier("pro")
check("set_tier 切到 pro", rf.tier == "pro")
check("set_tier 清空 resolved", "paper_check" not in rf._resolved)

# 边界：全链只有付费候选时，free 档应显式报"免费档跳过"，不静默失败
import agents.router as _R
_orig_chain = _R.STATE_TO_MODEL
_R.STATE_TO_MODEL = {"paper_check": [("deepseek", "deepseek-flash", 0.3)]}
try:
    r_only = Router(tier="free")
    r_only._make_agent = lambda p, m, t: paid1  # noqa: E731
    try:
        r_only.complete("paper_check", "x")
        check("free 档全付费链应报错", False, "居然成功了")
    except AgentError as e:
        check("free 档全付费链报『免费档跳过』", "免费档跳过" in str(e), f"实际={e}")
finally:
    _R.STATE_TO_MODEL = _orig_chain

print()
print(f'=== 结果：{PASS} 通过 / {FAIL} 失败 ===')
sys.exit(1 if FAIL else 0)