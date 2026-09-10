"""
BYOK（用户自带 Key）离线测试（v0.5.1）
=====================================
全 mock，不打真 API。验证：
    1. Router.set_user_key 能注入用户 Key
    2. 用户 Key 真正传到 Agent（不走环境变量，无 SF_API_KEY 这种拼错）
    3. 清 Key 后回退环境变量
    4. BYOK 模式不破坏原容灾链契约
    5. app.py 的表单/JSON 参数名与 router 对齐

覆盖之前踩的坑：
    - router 曾拼成 f"{provider.upper()}_API_KEY" → sf 变成 SF_API_KEY（错！
      实际是 SILICONFLOW_API_KEY），且环境变量注入有并发竞态
    - 现在改为直接传 api_key 参数给 Agent 构造
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
print("[1] Agent 构造接受 api_key 参数（BYOK 通道打通）")
# ---------------------------------------------------------------------------
from agents.router import Router, STATE_TO_MODEL
from agents.siliconflow_agent import SiliconFlowAgent
from agents.zhipu_agent import ZhipuAgent
from agents.deepseek_agent import DeepSeekAgent

# 清掉所有真实 Key，确保走的是传入的 api_key
for v in ("SILICONFLOW_API_KEY", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY"):
    os.environ.pop(v, None)

USER_KEY = "sk-user-supplied-key-12345"

for cls, label in ((SiliconFlowAgent, "硅基流动"),
                   (ZhipuAgent, "智谱"),
                   (DeepSeekAgent, "DeepSeek")):
    try:
        a = cls(model="some-model", api_key=USER_KEY)
        check(f"{label} 用传入 Key 实例化成功", True)
        check(f"{label} Key 生效（_api_keys[0]）",
              a._api_keys[0] == USER_KEY, f"实际={a._api_keys[0]!r}")
    except Exception as e:
        check(f"{label} 用传入 Key 实例化", False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n[2] 没传 Key 时回退环境变量（原契约不变）")
# ---------------------------------------------------------------------------
os.environ["SILICONFLOW_API_KEY"] = "sk-env-key"
try:
    a = SiliconFlowAgent(model="m")
    check("环境变量 Key 生效", a._api_keys[0] == "sk-env-key",
          f"实际={a._api_keys[0]!r}")
except Exception as e:
    check("环境变量 Key 生效", False, str(e))

# 用户 Key 优先于环境变量
try:
    a = SiliconFlowAgent(model="m", api_key=USER_KEY)
    check("用户 Key 优先级 > 环境变量", a._api_keys[0] == USER_KEY,
          f"实际={a._api_keys[0]!r}")
except Exception as e:
    check("用户 Key 优先级 > 环境变量", False, str(e))

# 多 Key 逗号分隔在 BYOK 下也生效
try:
    a = SiliconFlowAgent(model="m", api_key="k1,k2,k3")
    check("BYOK 多 Key 轮转解析", a._api_keys == ["k1", "k2", "k3"],
          f"实际={a._api_keys}")
except Exception as e:
    check("BYOK 多 Key 轮转解析", False, str(e))

os.environ.pop("SILICONFLOW_API_KEY", None)

# ---------------------------------------------------------------------------
print("\n[3] Router.set_user_key 注入链路")
# ---------------------------------------------------------------------------
r = Router()
r.set_user_key("sf", USER_KEY)
check("set_user_key 记录到 _user_keys", r._user_keys.get("sf") == USER_KEY)

try:
    agent = r._make_agent("sf", "deepseek-v4-pro", 0.3)
    check("_make_agent 用用户 Key 实例化", agent._api_keys[0] == USER_KEY,
          f"实际={agent._api_keys[0]!r}")
except Exception as e:
    check("_make_agent 用用户 Key 实例化", False, f"{type(e).__name__}: {e}")

# 清 Key → 回退环境变量（此时无 env key，应抛缺 Key 错误）
r.set_user_key("sf", "")
check("清空后 _user_keys 移除", "sf" not in r._user_keys)
try:
    r._make_agent("sf", "deepseek-v4-pro", 0.3)
    check("清空后无 env Key → 抛 AgentError", False, "居然没抛错")
except Exception as e:
    check("清空后无 env Key → 抛 AgentError", "环境变量" in str(e) or "Key" in str(e),
          f"错误信息不含 Key 提示：{e}")

# ---------------------------------------------------------------------------
print("\n[4] 智谱 BYOK（provider 名 → env 映射正确性回归）")
# ---------------------------------------------------------------------------
# 这是之前 bug 的核心：sf 曾被拼成 SF_API_KEY（应为 SILICONFLOW_API_KEY）
r2 = Router()
r2.set_user_key("zhipu", "zhipu-user-key")
try:
    a = r2._make_agent("zhipu", "glm-4.7-flash", 0.5)
    check("智谱用户 Key 生效", a._api_keys[0] == "zhipu-user-key",
          f"实际={a._api_keys[0]!r}")
except Exception as e:
    check("智谱用户 Key 生效", False, f"{type(e).__name__}: {e}")

# 环境变量名硬断言：防止再次拼错
from agents.openai_compat import PROVIDER_REGISTRY
check("sf → SILICONFLOW_API_KEY", PROVIDER_REGISTRY["sf"].env_var == "SILICONFLOW_API_KEY",
      f"实际={PROVIDER_REGISTRY['sf'].env_var}")
check("zhipu → ZHIPU_API_KEY", PROVIDER_REGISTRY["zhipu"].env_var == "ZHIPU_API_KEY",
      f"实际={PROVIDER_REGISTRY['zhipu'].env_var}")

# ---------------------------------------------------------------------------
print("\n[5] 容灾链/路由表契约未被破坏")
# ---------------------------------------------------------------------------
check("recommend 链是列表且非空", isinstance(STATE_TO_MODEL["recommend"], list)
      and len(STATE_TO_MODEL["recommend"]) >= 1)
check("paper_check 首选官方直连（v0.5.2 平台级容灾）",
      STATE_TO_MODEL["paper_check"][0] == ("deepseek", "deepseek-flash", 0.3),
      f"实际={STATE_TO_MODEL['paper_check'][0]}")
check("路由项三元组 (provider, model, temp)",
      all(len(t) == 3 for t in STATE_TO_MODEL["paper_check"]))

# ---------------------------------------------------------------------------
print("\n[6] app.py 参数名对齐（静态源码检查）")
# ---------------------------------------------------------------------------
app_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "app.py"), encoding="utf-8").read()
check("/api/check_paper 读 siliconflow_key",
      "siliconflow_key" in app_src)
check("/api/check_paper 读 zhipu_key", "zhipu_key" in app_src)
check("调用 router.set_user_key", "set_user_key" in app_src)

html_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "templates", "index.html"), encoding="utf-8").read()
check("前端有 siliconflowKey 输入框", 'id="siliconflowKey"' in html_src)
check("前端有 zhipuKey 输入框", 'id="zhipuKey"' in html_src)

# ---------------------------------------------------------------------------
print("\n[7] audit.py 免责声明已注入")
# ---------------------------------------------------------------------------
audit_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "audit.py"), encoding="utf-8").read()
check("含免责声明段", "免责声明" in audit_src)
check("含学术诚信承诺段", "学术诚信承诺" in audit_src)

# ---------------------------------------------------------------------------
print("\n[8] 论文截断 + 审计范围提示（v0.5.1 防 prompt 超长）")
# ---------------------------------------------------------------------------
import llm_audit as la

check("PAPER_PREFIX_LIMIT 常量存在", hasattr(la, "PAPER_PREFIX_LIMIT"))
LIMIT = la.PAPER_PREFIX_LIMIT

short_paper = "本研究采用 t 检验。"
check("短文不显示范围提示", la._scope_note(short_paper) == "")

long_paper = "论文正文内容" * 1000        # 6000 字 > LIMIT
note = la._scope_note(long_paper)
check("长文显示范围提示", f"前 {LIMIT} 字" in note, f"实际={note!r}")
check("提示含全文实际字数", "6000" in note, f"实际={note!r}")

prefix, _ = la.get_paper_prefix(long_paper)
check("冻结前缀确实被截断", len(prefix) < len(long_paper),
      f"前缀={len(prefix)} 原文={len(long_paper)}")

wrapped = la._wrap("审计结论", long_paper)
check("_wrap 长文带提示", "⚠️" in wrapped and "审计结论" in wrapped)
check("_wrap 短文不带提示", "⚠️" not in la._wrap("审计结论", short_paper))

print(f"\n{'='*46}")
print(f"BYOK 离线测试：{PASS} 通过 / {FAIL} 失败")
print(f"{'='*46}")
sys.exit(1 if FAIL else 0)
