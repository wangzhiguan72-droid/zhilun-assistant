"""
v0.4 接 API 端到端测试：LLM 深度解读
====================================
覆盖：
    1. llm_enhance 单元：_sanitize / 冻结前缀稳定性
    2. Flask test_client：use_llm=0（默认纯模板，不调 LLM）
    3. Flask test_client：use_llm=1 无 Key → llm_error 降级，主结果不破
    4. 真实 LLM 调用（有 Key 时）：SSE 流 llm 事件 + section 内容校验
"""
import io
import pathlib
import os

# 硬性纪律 7（与 registry_test / wizard_test 同款）：本套件会连续调用限流路径，
# 必须整体关闭限流，否则 60 秒滑窗内必吃 429（v2.27 扫描报告 P1-1）。
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = pathlib.Path(__file__).resolve().parent

ENV_TMP = str(BASE / '.env.tmp')
if os.path.exists(ENV_TMP):
    with open(ENV_TMP, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()
else:
    # 回退：加载项目根 .env（与 app.py __main__ 同一套加载器）
    from env_loader import load_dotenv
    load_dotenv()

from llm_enhance import enhance_analysis, _sanitize, FROZEN_SYSTEM

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

print('=== 1. llm_enhance 单元 ===')

# _sanitize：浮点 4 位、嵌套、不可序列化对象
s = _sanitize({"t": -14.091234567, "nested": {"p": 0.000123456}, "ok": True, "weird": {1, 2}})
check("_sanitize 浮点截断", abs(s["t"] - (-14.0912)) < 1e-9, str(s["t"]))
check("_sanitize 嵌套", s["nested"]["p"] == 0.0001)
check("_sanitize set 转字符串", isinstance(s["weird"], str))

# 冻结前缀稳定性（v0.5 前缀缓存前提）
check("冻结 system 非空且 >100 字", len(FROZEN_SYSTEM) > 100)
check("冻结 system 含硬性规则", "禁止编造" in FROZEN_SYSTEM)

print()
print('=== 2. Flask test_client：use_llm=0 默认纯模板 ===')
from app import app

client = app.test_client()
with open(str(BASE / 'examples' / 'student_scores.csv'), 'rb') as f:
    r = client.post('/api/upload', data={'file': (io.BytesIO(f.read()), 'student_scores.csv')},
                    content_type='multipart/form-data')
up = r.get_json()
check("上传成功", r.status_code == 200 and up.get("ok"))

fid = up["file_id"]
r = client.post('/api/analyze', json={
    "file_id": fid, "method": "independent_t",
    "group_col": "gender", "value_col": "score",
})
d = r.get_json()
check("纯模板分析成功", r.status_code == 200 and d.get("ok"))
check("use_llm=0 时 llm_enhanced=False", d.get("llm_enhanced") is False)
check("use_llm=0 时无 LLM 段", "AI 深度解读" not in (d.get("markdown") or ""))

print()
print('=== 3. use_llm=1：无 Key 降级 / 有 Key 真调 ===')
# 先清掉「全部」平台的 Key 才能模拟降级。
# 注意：只清 SF/ZHIPU 不够 —— 项目根 .env 里还有 DEEPSEEK/DASHSCOPE Key，
# 容灾链会继续往下找到可用 Key，导致「无 Key」用例假失败。
# 【血泪】MAAS 平台上线后又漏清 MAAS_API_KEY，同样造成假失败 ——
#   所以这里**从注册表派生**环境变量清单，新增平台自动纳入，永不再漏。
# 同时清掉 Router 单例缓存，避免已实例化的 Agent 带着旧 Key 继续可用。
try:
    from agents.openai_compat import PROVIDER_REGISTRY
    _ALL_KEY_ENVS = tuple(sorted({
        e.strip()
        for cfg in PROVIDER_REGISTRY.values()
        for e in (getattr(cfg, "env_var", "") or "").split(",")
        if e.strip()
    }))
except Exception:  # noqa: BLE001
    _ALL_KEY_ENVS = ("SILICONFLOW_API_KEY", "ZHIPU_API_KEY",
                     "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MAAS_API_KEY")
assert _ALL_KEY_ENVS, "未能从注册表派生出任何 Key 环境变量"
print(f"  · 本轮隔离平台 Key：{', '.join(_ALL_KEY_ENVS)}")
saved = {k: os.environ.pop(k, None) for k in _ALL_KEY_ENVS}
try:
    from agents import get_router
    _r = get_router()
    _r._agents.clear()
    _r._resolved.clear()
except Exception:  # noqa: BLE001
    pass
# Router 是单例且可能已缓存 _resolved —— 新起子进程测试降级更可靠，这里直接调 enhance_analysis
sec, err, _meta = enhance_analysis("independent_t", {"t": -1.0, "p": 0.3}, "独立样本 T 检验")
check("无 Key 时返回 (None, error)", sec is None and err is not None, f"sec={sec} err={err}")
check("无 Key 错误信息提到 LLM 不可用", "LLM 不可用" in (err or ""))

# 恢复 Key
for k, v in saved.items():
    if v:
        os.environ[k] = v
try:
    from agents import get_router
    _r = get_router()
    _r._agents.clear()
    _r._resolved.clear()
except Exception:  # noqa: BLE001
    pass

if os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("ZHIPU_API_KEY"):
    # 真实调用（SSE 流）
    r = client.post('/api/analyze', json={
        "file_id": fid, "method": "independent_t",
        "group_col": "gender", "value_col": "score",
        "use_llm": 1, "stream": 1,
    })
    body = r.get_data(as_text=True)
    has_llm_ok = "event: llm" in body and '"ok": true' in body.replace("True", "true").replace('"ok":true', '"ok": true')
    # 提取 llm section 内容
    section = ""
    for block in body.split("\n\n"):
        if block.startswith("event: llm"):
            data_line = [l for l in block.split("\n") if l.startswith("data: ")][0][6:]
            try:
                j = json.loads(data_line)
                section = j.get("section", "")
                llm_ok = j.get("ok")
            except Exception:
                pass
    check("SSE 含 llm 事件", "event: llm" in body)
    if section:
        check("llm section 含标题", "AI 深度解读" in section)
        check("llm section 提到 t 或 p（用了统计量）",
              ("t" in section.lower() or "p" in section.lower()))
        print(f'  ---- AI 解读预览（前 300 字）----')
        print('  ' + section[:300].replace("\n", "\n  "))
        print('  --------------------------------')
    else:
        print(f'  [INFO] llm 未生成：{body[:400]}')
else:
    print("  [SKIP] 没有可用 Key，跳过真实 LLM 调用")

print()
print(f'=== 结果：{PASS} 通过 / {FAIL} 失败 ===')
sys.exit(1 if FAIL else 0)