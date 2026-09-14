"""
API Key 防泄露测试（v0.5.4）
============================
场景：百炼 MaaS 专属端点的 Key 是项目方私有资源，用户绝不能拿到。

本测试把一条**可识别的假 Key** 注入所有可能对外输出的路径，
逐一断言它不会出现在任何返回给前端的字符串里：

    1. secrets_guard.redact  —— 精确 Key + 形态兜底
    2. AgentError.__str__    —— 报错文案
    3. Agent.__repr__        —— 调试展示（含私有端点地址）
    4. Router.health()       —— 路由看板
    5. /api/llm_stats        —— 前端唯一能看到 provider 信息的接口
    6. llm_audit._short()    —— 真正进 jsonify(error=...) 的那一段

全部离线（test_client，不启服务、不调 API）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 刻意不加载 .env：本测试要完全受控的假 Key
os.environ.pop("MAAS_API_KEY", None)
os.environ.pop("ZHIPU_API_KEY", None)
os.environ.pop("DASHSCOPE_API_KEY", None)
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("SILICONFLOW_API_KEY", None)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


FAKE_MAAS = "sk-ws-H.LEAKTEST01.LEAKTEST02"
FAKE_PRIVATE_HOST = "ws-ubquyin5epzugojr.cn-beijing.maas.aliyuncs.com"

print("=== 1. secrets_guard.redact ===")
from agents import redact_secrets  # noqa: E402
from agents.secrets_guard import register_secret, clear_registry  # noqa: E402

register_secret(FAKE_MAAS)
check("已登记 Key 精确擦除", FAKE_MAAS not in redact_secrets(f"key={FAKE_MAAS}"))
check("擦除结果带掩码", "***" in redact_secrets(f"key={FAKE_MAAS}"))
check("形态兜底：sk- 前缀 Key",
      "sk-abcdefghijklmnop" not in redact_secrets("Authorization: sk-abcdefghijklmnop"))
check("形态兜底：智谱 hex32.串",
      "0123456789abcdef0123456789abcdef.AAAA1111BBBB" not in
      redact_secrets("k=0123456789abcdef0123456789abcdef.AAAA1111BBBB"))
check("None / 空串安全", redact_secrets(None) == "" and redact_secrets("") == "")
check("普通文本不被误伤",
      redact_secrets("模型 glm-4.7-flash 调用成功") == "模型 glm-4.7-flash 调用成功")

print()
print("=== 2-3. AgentError / Agent.__repr__ ===")
os.environ["MAAS_API_KEY"] = FAKE_MAAS
from agents import AgentError, MaasAgent, get_router  # noqa: E402

err = AgentError(f"鉴权失败：Bearer {FAKE_MAAS} 无效")
check("AgentError.__str__ 擦除 Key", FAKE_MAAS not in str(err))

ma = MaasAgent(model="glm-5")
check("MaasAgent.repr 无 Key", FAKE_MAAS not in repr(ma))
check("MaasAgent.repr 无私有端点地址", FAKE_PRIVATE_HOST not in repr(ma))
check("MaasAgent 内部仍持有 Key（功能不受影响）",
      ma._api_keys == [FAKE_MAAS])

print()
print("=== 4. Router.health() ===")
router = get_router()
router._agents[("maas", "glm-5")] = ma
router._resolved["paper_check"] = ("maas", "glm-5")
health_dump = str(router.health())
check("health 无 Key", FAKE_MAAS not in health_dump)
check("health 无私有端点地址", FAKE_PRIVATE_HOST not in health_dump)

print()
print("=== 5. /api/llm_stats（前端可见的唯一 provider 接口）===")
from app import app  # noqa: E402

client = app.test_client()
resp = client.get("/api/llm_stats")
body = resp.get_data(as_text=True)
check("llm_stats 返回 200", resp.status_code == 200, f"实际={resp.status_code}")
check("llm_stats 响应无 Key", FAKE_MAAS not in body)
check("llm_stats 响应无私有端点地址", FAKE_PRIVATE_HOST not in body)

print()
print("=== 6. llm_audit._short（真正进 jsonify(error=...) 的出口）===")
from llm_audit import _short  # noqa: E402

safe = _short(f"LLM 不可用：Error 401 - key={FAKE_MAAS} rejected")
check("_short 擦除 Key", FAKE_MAAS not in safe)
check("_short 仍保留可读报错", "LLM 不可用" in safe and len(safe) > 0)

print()
print("=== 7. 全量字符串扫描（模拟前端拿到的完整 payload）===")
payload = (repr(ma) + str(router.health()) + body + safe +
           str(AgentError(f"x {FAKE_MAAS} y")))
check("汇总 payload 无 Key", FAKE_MAAS not in payload)
check("汇总 payload 无私有端点", FAKE_PRIVATE_HOST not in payload)

clear_registry()

print()
print(f"====== 结果：{PASS} PASS / {FAIL} FAIL ======")
sys.exit(1 if FAIL else 0)
