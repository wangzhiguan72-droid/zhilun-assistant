"""
v1.7 · 规划§四 P2 上线安全：应用层限流与防御 契约测试
======================================================
覆盖：
    1. RateLimiter 纯逻辑：窗口滑动、超限拒绝、retry_after、reset
    2. 内存有界：空闲 IP 回收 + 超上限淘汰（旧实现的真实泄漏缺陷）
    3. client_ip：默认**不采信 X-Forwarded-For**（防伪造绕过）；TRUST_PROXY=1 才采信
    4. 路径分类：LLM 接口更严、静态/健康检查豁免
    5. /health 返回限流可观测统计
    6. 端点集成：LLM 接口超限 → 429 + Retry-After 头；普通接口独立计数
    7. 阈值环境变量可覆盖

运行：.venv/Scripts/python.exe -u security_guard_test.py

⚠️ 本脚本**测的就是限流器本身**，因此绝不能在外部全局 `export RATE_LIMIT_DISABLE=1`
   （那等于把被测对象关掉，会出现 4 个假失败）。下面显式清掉该变量以保证测试有效。
"""
import json
import os
import sys
import time

# 本套件必须在「限流开启」的状态下运行；主动清除外部可能设置的关闭开关，
# 避免批处理里被全局 export 毒害（本项目已踩过这个坑）。
# 注意：第 9 节会临时自设该变量来验证开关本身，用完即删。
os.environ.pop("RATE_LIMIT_DISABLE", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


import security_guard as sg
from security_guard import RateLimiter

print('=== 1. RateLimiter 滑窗逻辑 ===')
rl = RateLimiter(window=60.0)
for i in range(5):
    ok, retry = rl.check("a", 5)
check("前 5 次放行", ok is True and retry == 0)
ok, retry = rl.check("a", 5)
check("第 6 次拒绝", ok is False)
check("拒绝时给 retry_after（>=1 秒）", retry >= 1, str(retry))
# 不同 key 独立计数
ok, _ = rl.check("b", 5)
check("不同 key 独立计数", ok is True)
# 窗口滑动：把所有记录推到窗口外应恢复放行
rl2 = RateLimiter(window=0.15)
for i in range(3):
    rl2.check("k", 3)
check("窗口内已用满", rl2.check("k", 3)[0] is False)
time.sleep(0.2)
check("窗口滑过后恢复放行", rl2.check("k", 3)[0] is True)


print()
print('=== 2. 内存有界（旧实现的真实泄漏缺陷） ===')
# 2a 空闲 IP 回收
rl3 = RateLimiter(window=60.0, idle_ttl=0.1, max_ips=100)
for i in range(20):
    rl3.check(f"ip{i}", 10)
check("20 个 IP 已跟踪", rl3.stats()["tracked_ips"] == 20, str(rl3.stats()))
time.sleep(0.15)
rl3.check("trigger_sweep", 10)   # 触发一次 sweep
tracked = rl3.stats()["tracked_ips"]
check("空闲 IP 被回收（tracked 降到极少）", tracked <= 2, str(tracked))

# 2b 超上限淘汰最旧
rl4 = RateLimiter(window=60.0, idle_ttl=9999.0, max_ips=10)
for i in range(30):
    rl4.check(f"ip{i}", 10)
t4 = rl4.stats()["tracked_ips"]
check("超过 max_ips 的 IP 被淘汰（有界）", t4 <= 10, str(t4))

# 2c 反复请求同一 IP 不会无限增长内存
rl5 = RateLimiter(window=0.05, idle_ttl=0.05)
for i in range(200):
    rl5.check("same", 1000)
    if i % 50 == 0:
        time.sleep(0.06)
check("单 IP 反复请求内存恒定", rl5.stats()["tracked_ips"] <= 2, str(rl5.stats()))

# 2d rejects 可观测
rl6 = RateLimiter(window=60.0)
for i in range(3):
    rl6.check("x", 2)
check("拒绝次数被统计", rl6.stats()["rejects_total"] == 1, str(rl6.stats()))


print()
print('=== 3. client_ip：防 X-Forwarded-For 伪造 ===')
class _Req:
    def __init__(self, remote, headers=None):
        self.remote_addr = remote
        self.headers = headers or {}

# 3a 默认（TRUST_PROXY 未设）→ 忽略 XFF，用 remote_addr
os.environ.pop("TRUST_PROXY", None)
r = _Req("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})
ip = sg.client_ip(r, trusted=False)
check("默认不采信 XFF（防伪造）", ip == "203.0.113.9", ip)
check("trust_proxy() 默认 False", sg.trust_proxy() is False)

# 3b TRUST_PROXY=1 → 采信 XFF 首个
os.environ["TRUST_PROXY"] = "1"
r2 = _Req("10.0.0.1", {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
ip2 = sg.client_ip(r2, trusted=True)
check("TRUST_PROXY=1 时采信 XFF 首段", ip2 == "1.2.3.4", ip2)
check("trust_proxy() 识别 =1", sg.trust_proxy() is True)

# 3c TRUST_PROXY=1 但无 XFF → 回退 X-Real-IP → 再回退 remote_addr
r3 = _Req("10.0.0.1", {"X-Real-IP": "9.9.9.9"})
check("无 XFF 时用 X-Real-IP", sg.client_ip(r3, trusted=True) == "9.9.9.9")
r4 = _Req("10.0.0.1", {})
check("都无时回退 remote_addr", sg.client_ip(r4, trusted=True) == "10.0.0.1")
r5 = _Req(None, {})
check("remote_addr 为 None 时兜底 unknown", sg.client_ip(r5, trusted=True) == "unknown")
os.environ.pop("TRUST_PROXY", None)


print()
print('=== 4. 路径分类 ===')
check("/api/check_paper 属 LLM", sg.is_llm_path("/api/check_paper") is True)
check("/api/copilot/paper 属 LLM", sg.is_llm_path("/api/copilot/paper") is True)
check("/api/audit_chat 属 LLM", sg.is_llm_path("/api/audit_chat") is True)
check("/api/analyze 属 LLM（use_llm=1 真调 LLM，v2.26 补登）",
      sg.is_llm_path("/api/analyze") is True)
check("/api/upload 非 LLM", sg.is_llm_path("/api/upload") is False)
check("首页豁免", sg.is_exempt("/") is True)
check("/health 豁免", sg.is_exempt("/health") is True)
check("/static 豁免", sg.is_exempt("/static/app.js") is True)
check("/api/analyze 不豁免", sg.is_exempt("/api/analyze") is False)
check("LLM 上限 < 普通上限（更严）", sg.llm_per_min() < sg.per_min(),
      f"llm={sg.llm_per_min()} normal={sg.per_min()}")


print()
print('=== 5. 阈值环境变量可覆盖 ===')
os.environ["RATE_LIMIT_PER_MIN"] = "123"
os.environ["RATE_LIMIT_LLM_PER_MIN"] = "7"
check("RATE_LIMIT_PER_MIN 生效", sg.per_min() == 123, str(sg.per_min()))
check("RATE_LIMIT_LLM_PER_MIN 生效", sg.llm_per_min() == 7, str(sg.llm_per_min()))
os.environ["RATE_LIMIT_PER_MIN"] = "abc"
check("非法值回退默认（不炸）", sg.per_min() == 60, str(sg.per_min()))
os.environ["RATE_LIMIT_PER_MIN"] = "-5"
check("负数回退默认", sg.per_min() == 60, str(sg.per_min()))
os.environ["RATE_LIMIT_PER_MIN"] = "0"
check("0 回退默认（0 会让服务不可用）", sg.per_min() == 60, str(sg.per_min()))
os.environ.pop("RATE_LIMIT_PER_MIN", None)
os.environ.pop("RATE_LIMIT_LLM_PER_MIN", None)


print()
print('=== 6. 端点集成（test_client） ===')
import app as flask_app

client = flask_app.app.test_client()

# /health 返回限流统计
r = client.get("/health")
d = r.get_json()
check("/health 200", r.status_code == 200, str(r.status_code))
check("/health ok=True", d.get("ok") is True)
check("/health 含 rate_limit 统计", isinstance(d.get("rate_limit"), dict), str(d.get("rate_limit")))
check("/health 统计含 tracked_ips / rejects_total",
      "tracked_ips" in d["rate_limit"] and "rejects_total" in d["rate_limit"])

# LLM 接口超限 → 429 + Retry-After
sg.limiter.reset()
os.environ["RATE_LIMIT_LLM_PER_MIN"] = "2"
# 直接打 LLM 路径（用 audit_chat，参数错误也会先过限流）
code_3 = None
for i in range(3):
    rr = client.post("/api/audit_chat", json={"summary": {}, "question": ""})
    if i == 2:
        code_3 = rr.status_code
        hdr = rr.headers.get("Retry-After")
        body = rr.get_json()
check("LLM 接口第 3 次 → 429", code_3 == 429, str(code_3))
check("429 带 Retry-After 头", bool(hdr), str(hdr))
check("429 文案含频率提示", "频繁" in (body.get("error") or ""), str(body))
check("429 body 带 retry_after 字段", "retry_after" in (body or {}), str(body))
os.environ.pop("RATE_LIMIT_LLM_PER_MIN", None)

# 普通接口独立计数：LLM 用满不影响普通接口
sg.limiter.reset()
os.environ["RATE_LIMIT_LLM_PER_MIN"] = "1"
client.post("/api/audit_chat", json={"summary": {}, "question": ""})   # 用满 LLM
client.post("/api/audit_chat", json={"summary": {}, "question": ""})   # 应 429
r_norm = client.get("/api/llm_stats")
check("普通接口不受 LLM 限流影响", r_norm.status_code != 429, str(r_norm.status_code))
os.environ.pop("RATE_LIMIT_LLM_PER_MIN", None)

# 豁免路径不受限流
sg.limiter.reset()
os.environ["RATE_LIMIT_PER_MIN"] = "1"
for i in range(5):
    r_static = client.get("/health")
check("豁免路径不计数（5 次仍 200）", r_static.status_code == 200, str(r_static.status_code))
os.environ.pop("RATE_LIMIT_PER_MIN", None)

sg.limiter.reset()


print()
print('=== 8. 错误出口统一 JSON（堆栈不外漏） ===')
sg.limiter.reset()
# 404：API 路径回 JSON
r404 = client.get("/api/definitely_not_exist")
check("API 404 → JSON", r404.is_json, r404.content_type)
check("API 404 → ok=False", r404.get_json().get("ok") is False)
check("API 404 不回显内部路径细节",
      "definitely_not_exist" not in json.dumps(r404.get_json(), ensure_ascii=False))
# 405：方法不允许
r405 = client.delete("/api/methods_graph")
check("405 → JSON", r405.is_json, r405.content_type)
check("405 → ok=False", r405.get_json().get("ok") is False)
# 所有错误响应都不得含 Flask/Werkzeug 指纹
for rr in (r404, r405):
    blob = json.dumps(rr.get_json(), ensure_ascii=False) + (rr.get_data(as_text=True) or "")
    check(f"{rr.status_code} 响应不含框架指纹",
          "Werkzeug" not in blob and "Traceback" not in blob, blob[:80])


print()
print('=== 7. 失败放行（限流器自身异常不挂站） ===')
sg.limiter.reset()
_orig = sg.limiter.check


def _boom(*a, **kw):
    raise RuntimeError("simulated limiter failure")


sg.limiter.check = _boom
try:
    r = client.get("/api/llm_stats")
    check("限流器抛异常时请求仍放行（不 500）", r.status_code == 200, str(r.status_code))
finally:
    sg.limiter.check = _orig
sg.limiter.reset()


print()
print('=== 9. 测试/CI 关闭开关（RATE_LIMIT_DISABLE） ===')
os.environ.pop("RATE_LIMIT_DISABLE", None)
check("默认不关闭（生产安全默认）", sg.disabled() is False)
os.environ["RATE_LIMIT_DISABLE"] = "1"
check("=1 时关闭", sg.disabled() is True)
os.environ["RATE_LIMIT_DISABLE"] = "0"
check("=0 时不关闭", sg.disabled() is False)
os.environ["RATE_LIMIT_DISABLE"] = "yes"
check("=yes 时关闭", sg.disabled() is True)
os.environ.pop("RATE_LIMIT_DISABLE", None)
# 关闭后端点不受限
sg.limiter.reset()
os.environ["RATE_LIMIT_DISABLE"] = "1"
os.environ["RATE_LIMIT_LLM_PER_MIN"] = "1"
codes = []
for i in range(5):
    codes.append(client.post("/api/audit_chat",
                             json={"summary": {}, "question": ""}).status_code)
check("关闭后连打 5 次无 429", 429 not in codes, str(codes))
os.environ.pop("RATE_LIMIT_DISABLE", None)
os.environ.pop("RATE_LIMIT_LLM_PER_MIN", None)
sg.limiter.reset()


print()
print('=== 汇总 ===')
print(f'结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
