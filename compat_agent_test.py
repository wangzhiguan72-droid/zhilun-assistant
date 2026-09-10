"""
通用适配器离线测试（v0.5，全 mock，零 API 调用）
==================================================
覆盖 FreeLLMAPI 式推广的核心能力：

    1. PROVIDER_REGISTRY 三平台注册完整（sf / zhipu / deepseek）
    2. parse_keys 多 Key 解析
    3. usage 采集：智谱 cached_tokens（OpenAI 标准字段）
                 + 硅基流动 prompt_cache_hit_tokens（model_extra）
    4. 多 Key 轮转：key1 429 → 自动切 key2 成功
    5. 全 Key 429 → AgentError 带 retry_after（从响应头解析）
    6. 非 429 错误不轮转，直接抛 AgentError
    7. Router 冷却感知 retry_after（120s 而非默认 60s）
    8. GLM-5 thinking 兼容：glm-5.3 不传 thinking 开关，glm-4.7 传 disabled
    9. 行为兼容：SiliconFlowAgent 空内容返回 ""，ZhipuAgent 空内容抛错
"""
import os
import sys
import time

sys.path.insert(0, r'D:\论文排版辅助agent')

# 假 Key（逗号分隔两个，测轮转）；测试全程不碰真实 API
os.environ["SILICONFLOW_API_KEY"] = "sf-key-1,sf-key-2"
os.environ["ZHIPU_API_KEY"] = "zhipu-key-1,zhipu-key-2"
os.environ.pop("DEEPSEEK_API_KEY", None)

from agents import (PROVIDER_REGISTRY, AgentError, SiliconFlowAgent,
                    ZhipuAgent, DeepSeekAgent, QwenAgent, Router)
from agents.openai_compat import parse_keys
from agents.router import STATE_TO_MODEL, FREE_MODELS

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


# ---------------------------------------------------------------------------
# Mock 基础设施
# ---------------------------------------------------------------------------

class FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


class Fake429(Exception):
    """模拟 openai SDK 的 RateLimitError（带响应头）。"""
    def __init__(self, msg="rate limit", retry_after=None):
        super().__init__(f"Error code: 429 - {msg}")
        self.response = type("R", (), {})()
        headers = FakeHeaders()
        if retry_after is not None:
            headers["retry-after"] = str(retry_after)
        self.response.headers = headers


class FakeUsage:
    """模拟 usage：支持 OpenAI 标准字段 / model_extra 两种形态。"""
    def __init__(self, prompt=100, completion=20, cached=None, sf_hit=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion
        if cached is not None:
            self.prompt_tokens_details = type("D", (), {"cached_tokens": cached})()
        if sf_hit is not None:
            self.model_extra = {"prompt_cache_hit_tokens": sf_hit}


class FakeResp:
    def __init__(self, content="ok", usage=None):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = usage


class ScriptedClient:
    """按脚本返回/抛错的 mock 客户端工厂：按当前 api_key 记录调用。"""
    def __init__(self, agent, script):
        # script: list of (api_key_pattern, action)；action = FakeResp | Exception
        self.agent = agent
        self.script = list(script)

    def __call__(self):
        outer = self

        def create(**kw):
            key = outer.agent._api_key
            for pattern, action in outer.script:
                if key == pattern:
                    if isinstance(action, Exception):
                        raise action
                    return action
            raise AssertionError(f"没有为 key={key} 编排脚本")

        comps = type("Comps", (), {"create": staticmethod(create)})()
        chat = type("Chat", (), {"completions": comps})()
        return type("Client", (), {"chat": chat})()


print('=== 1. PROVIDER_REGISTRY 注册完整性 ===')
check('四平台已注册', set(PROVIDER_REGISTRY) == {"sf", "zhipu", "deepseek", "dashscope"},
      f'实际: {set(PROVIDER_REGISTRY)}')
check('deepseek 官方 base_url',
      PROVIDER_REGISTRY["deepseek"].base_url == "https://api.deepseek.com/v1")
check('dashscope 百炼 base_url',
      PROVIDER_REGISTRY["dashscope"].base_url ==
      "https://dashscope.aliyuncs.com/compatible-mode/v1")
check('router 也注册了 deepseek provider',
      "deepseek" in sys.modules["agents.router"]._PROVIDERS)
check('router 注册了 dashscope provider（v0.5.2 四平台齐备）',
      "dashscope" in sys.modules["agents.router"]._PROVIDERS)
check('recommend 链尾位是百炼（第三道保险）',
      STATE_TO_MODEL["recommend"][-1] == ("dashscope", "qwen3.7-flash", 0.5),
      f'实际: {STATE_TO_MODEL["recommend"]}')
check('paper_check 链：付费档优先 + 免费兜底（v0.5.3）',
      STATE_TO_MODEL["paper_check"] == [("deepseek", "deepseek-flash", 0.3),
                                        ("sf", "deepseek-v4-pro", 0.3),
                                        ("zhipu", "glm-4.7-flash", 0.3)],
      f'实际: {STATE_TO_MODEL["paper_check"]}')
check('write_text 链尾位是百炼（v0.5.3）',
      STATE_TO_MODEL["write_text"][-1] == ("dashscope", "qwen3.7-flash", 0.5),
      f'实际: {STATE_TO_MODEL["write_text"]}')
check('FREE_MODELS 白名单覆盖三家免费模型',
      {("zhipu", "glm-4.7-flash"), ("sf", "glm-z1-9b"),
       ("dashscope", "qwen3.7-flash")} <= set(FREE_MODELS),
      f'实际: {FREE_MODELS}')

print()
print('=== 2. parse_keys 多 Key 解析 ===')
check('单 Key', parse_keys("k1") == ["k1"])
check('双 Key 去空白', parse_keys(" k1 , k2 ") == ["k1", "k2"])
check('空项过滤', parse_keys("k1,,k2,") == ["k1", "k2"])

print()
print('=== 3. usage 采集（前缀缓存观测） ===')
a = SiliconFlowAgent(model="deepseek-v4-flash")
a._build_client = ScriptedClient(a, [("sf-key-1", FakeResp("ok", usage=FakeUsage(cached=2048)))])
out = a.complete("hi")
check('智谱式 cached_tokens 采集', a.last_usage and a.last_usage["cached_tokens"] == 2048,
      f'last_usage={a.last_usage}')
check('prompt_tokens 采集', a.last_usage["prompt_tokens"] == 100)

b = SiliconFlowAgent(model="deepseek-v4-flash")
b._build_client = ScriptedClient(b, [("sf-key-1", FakeResp("ok", usage=FakeUsage(sf_hit=512)))])
b.complete("hi")
check('硅基流动 prompt_cache_hit_tokens 采集', b.last_usage["cached_tokens"] == 512,
      f'last_usage={b.last_usage}')

c = SiliconFlowAgent(model="deepseek-v4-flash")
c._build_client = ScriptedClient(c, [("sf-key-1", FakeResp("ok", usage=FakeUsage()))])
c.complete("hi")
check('无缓存字段时 cached=0 不报错', c.last_usage["cached_tokens"] == 0)

print()
print('=== 4. 多 Key 轮转：key1 429 → key2 成功 ===')
d = SiliconFlowAgent(model="deepseek-v4-flash")
d._build_client = ScriptedClient(d, [
    ("sf-key-1", Fake429()),
    ("sf-key-2", FakeResp("from-key-2")),
])
out = d.complete("hi")
check('自动切 key2 拿到结果', out == "from-key-2", f'实际: {out!r}')
check('轮转后停留在 key2（粘性）', d._api_key == "sf-key-2")

print()
print('=== 5. 全 Key 429 → AgentError.retry_after ===')
e = SiliconFlowAgent(model="deepseek-v4-flash")
e._build_client = ScriptedClient(e, [
    ("sf-key-1", Fake429()),
    ("sf-key-2", Fake429(retry_after=120)),
])
try:
    e.complete("hi")
    check('应抛 AgentError', False)
except AgentError as err:
    check('抛 AgentError 且带 retry_after=120', err.retry_after == 120,
          f'retry_after={err.retry_after}')
    check('报错文案含轮转信息', "2 个 Key" in str(err))

print()
print('=== 6. 非 429 错误不轮转 ===')
f = SiliconFlowAgent(model="deepseek-v4-flash")
calls = []
f._build_client = ScriptedClient(f, [
    ("sf-key-1", RuntimeError("500 Internal Server Error")),
])
try:
    f.complete("hi")
    check('应抛 AgentError', False)
except AgentError as err:
    check('500 直接抛 AgentError', "调用失败" in str(err))
    check('Key 未轮转（仍 key1）', f._api_key == "sf-key-1")

print()
print('=== 7. Router 冷却感知 retry_after ===')


class MockAgent:
    """抛带 retry_after 的 AgentError 的 mock。"""
    model_name = "mock"

    def complete(self, prompt, **kw):
        err = AgentError("mock 429")
        err.retry_after = 120
        raise err


router = Router(tier="pro")   # v0.5.3：本用例测付费模型 v4-pro 的 Retry-After 冷却
router._agents[("sf", "deepseek-v4-pro")] = MockAgent()
before = time.time()
try:
    router.complete("paper_check", "hi")
except AgentError:
    pass
cd = router._cooldown_until[("sf", "deepseek-v4-pro")] - before
check('冷却 ≈120s（Retry-After）而非默认 60s', 115 <= cd <= 125, f'实际 {cd:.0f}s')

print()
print('=== 8. GLM-5 thinking 兼容 ===')
z5 = ZhipuAgent(model="glm-5.3")
check('glm-5.3 不传 thinking 开关', z5._extra_create_kwargs() == {},
      f'实际: {z5._extra_create_kwargs()}')
z4 = ZhipuAgent(model="glm-4.7-flash")
check('glm-4.7-flash 默认关思考',
      z4._extra_create_kwargs()["extra_body"]["thinking"]["type"] == "disabled")
check('glm-5 标记 thinking 不受支持', z5._thinking_supported is False)

print()
print('=== 9. 行为兼容（v0.4 契约不破） ===')
g = SiliconFlowAgent(model="deepseek-v4-flash")
g._build_client = ScriptedClient(g, [("sf-key-1", FakeResp(""))])
check('SiliconFlowAgent 空内容返回 ""（v0.4 行为）', g.complete("hi") == "")

h = ZhipuAgent(model="glm-4.7-flash")
h._build_client = ScriptedClient(h, [("zhipu-key-1", FakeResp(""))])
try:
    h.complete("hi")
    check('ZhipuAgent 空内容抛 AgentError', False)
except AgentError:
    check('ZhipuAgent 空内容抛 AgentError', True)

os.environ["DEEPSEEK_API_KEY"] = "fake"   # 构造需要 Key，用假 Key
check('DeepSeekAgent 类可用 + base_url 正确',
      DeepSeekAgent(model="deepseek-flash").base_url == "https://api.deepseek.com/v1")
check('DeepSeekAgent 有 Key 时可实例化',
      DeepSeekAgent(model="deepseek-flash").model_name == "deepseek-flash")
# v0.5.2 真调抓到的坑：必须覆盖父类别名表，否则 deepseek-v4-flash 会被
# 改写成硅基流动的 deepseek-ai/DeepSeek-V4-Flash，官方直连直接 400
check('DeepSeekAgent 别名不被硅基流动表改写',
      DeepSeekAgent(model="deepseek-v4-flash").model_name == "deepseek-flash",
      f"实际={DeepSeekAgent(model='deepseek-v4-flash').model_name}")
check('DeepSeekAgent 旧名 deepseek-reasoner 兼容映射到 v4-pro',
      DeepSeekAgent(model="deepseek-reasoner").model_name == "deepseek-v4-pro")
del os.environ["DEEPSEEK_API_KEY"]
try:
    DeepSeekAgent(model="deepseek-flash")
    check('DeepSeekAgent 缺 Key 报 AgentError', False)
except AgentError:
    check('DeepSeekAgent 缺 Key 报 AgentError', True)

print()
print('=== 10. QwenAgent（阿里云百炼，v0.5.1 新增） ===')
os.environ["DASHSCOPE_API_KEY"] = "fake-dashscope"
qw = QwenAgent(model="qwen3.5-9b")
check('百炼 base_url 正确',
      qw.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1")
check('模型别名解析', qw.model_name == "qwen3.5-9b")
check('多 Key 解析（假 Key 单个）', qw._api_keys == ["fake-dashscope"])
qw._build_client = ScriptedClient(qw, [("fake-dashscope", FakeResp("qwen ok", usage=FakeUsage(cached=256)))])
out = qw.complete("hi")
check('百炼调用 + usage 采集', out == "qwen ok" and qw.last_usage["cached_tokens"] == 256)
del os.environ["DASHSCOPE_API_KEY"]
try:
    QwenAgent(model="qwen3.5-9b")
    check('QwenAgent 缺 Key 报 AgentError', False)
except AgentError:
    check('QwenAgent 缺 Key 报 AgentError', True)

print()
print(f'====== 结果：{PASS} PASS / {FAIL} FAIL ======')
sys.exit(1 if FAIL else 0)
