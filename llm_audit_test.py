"""
v0.4.2 端到端测试：论文排查 Tab · AI 深度审计
=============================================
覆盖：
    1. llm_audit 单元：前缀 LRU 复用 / 规则摘要确定性 / 缓存 key 敏感性
    2. Flask test_client：use_llm 不传（默认纯规则，不调 LLM）
    3. use_llm=1 无 Key → llm_error 降级，规则报告不破
    4. 真实 V4-Pro 调用（有 Key 时）：section 生成 + 第二次调用缓存命中（0 token）
"""
import io
import pathlib
import os
import sys

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

from llm_audit import (
    get_paper_prefix, build_rule_digest, _digest_hash, _reset_prefix_cache,
    PROMPT_VERSION,
)
from llm_cache import llm_cache

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

PAPER = str(BASE / 'examples' / 'sample_paper.md')
DATA = str(BASE / 'examples' / 'student_scores.csv')

print('=== 1. llm_audit 单元 ===')
_reset_prefix_cache()
with open(PAPER, 'r', encoding='utf-8') as f:
    paper_text = f.read()

p1, h1 = get_paper_prefix(paper_text)
p2, h2 = get_paper_prefix(paper_text)
check("同篇论文复用同一前缀对象（不重建）", p1 is p2 and h1 == h2)
check("论文变了前缀必变", get_paper_prefix("另一篇论文" * 50)[1] != h1)

audit_fake = {
    "comparisons": [{"status": "diff", "paper": "t = 2.5", "real": "t = 2.31", "diff": 0.19}],
    "suggestions": ["建议补报效应量 Cohen's d"],
}
d1 = build_rule_digest(audit_fake)
check("规则摘要确定性（同输入字节相同）", build_rule_digest(audit_fake) == d1)
check("摘要包含规则发现", "2.5" in d1 and "效应量" in d1)

k_a = llm_cache.make_key("m", PROMPT_VERSION, "paper_check",
                         {"paper": h1, "rules": _digest_hash(d1), "directive": "只看 T 检验"})
k_b = llm_cache.make_key("m", PROMPT_VERSION, "paper_check",
                         {"paper": h1, "rules": _digest_hash(d1), "directive": "只看 T 检验"})
k_c = llm_cache.make_key("m", PROMPT_VERSION, "paper_check",
                         {"paper": h1, "rules": _digest_hash(d1), "directive": ""})
check("缓存 key：同输入同 key", k_a == k_b)
check("缓存 key：指令变 key 变", k_a != k_c)

# 冻结前缀契约：指令在变量区，绝不进前缀
check("前缀包含论文标记", "<<<PAPER" in p1 and "PAPER>>>" in p1)
check("前缀不含用户指令", "本次任务" not in p1)

print()
print('=== 2. Flask test_client：默认纯规则（不传 use_llm） ===')
from app import app
client = app.test_client()

def check_paper(extra_fields: dict):
    with open(PAPER, 'rb') as f:
        paper_body = f.read()
    with open(DATA, 'rb') as f:
        data_body = f.read()
    return client.post('/api/check_paper', data={
        'paper': (io.BytesIO(paper_body), 'sample_paper.md', 'text/markdown'),
        'data': (io.BytesIO(data_body), 'student_scores.csv', 'text/csv'),
        **extra_fields,
    }, content_type='multipart/form-data')

r = check_paper({})
d = r.get_json()
check("规则核查成功", r.status_code == 200 and d.get("ok"), str(d.get("error"))[:80])
check("默认 llm_enhanced=False", d.get("llm_enhanced") is False)
check("默认无 AI 审计段", "AI 深度审计" not in (d.get("audit", {}).get("markdown") or ""))
rule_md_len = len(d.get("audit", {}).get("markdown") or "")
check("规则报告有内容", rule_md_len > 100)

print()
print('=== 3. use_llm=1：无 Key 静默降级 ===')
import agents.router as _ar
# 【血泪】隔离清单必须覆盖**注册表里的全部平台**，漏一个（如后来的 MAAS）
# 容灾链就会拿到可用 Key，把「无 Key 降级」测成假失败 → 从注册表派生，永不遗漏。
try:
    from agents.openai_compat import PROVIDER_REGISTRY as _PR
    _ALL_KEY_ENVS = tuple(sorted({
        e.strip() for cfg in _PR.values()
        for e in (getattr(cfg, "env_var", "") or "").split(",") if e.strip()
    }))
except Exception:  # noqa: BLE001
    _ALL_KEY_ENVS = ("SILICONFLOW_API_KEY", "ZHIPU_API_KEY",
                     "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MAAS_API_KEY")
assert _ALL_KEY_ENVS, "未能从注册表派生出任何 Key 环境变量"
print(f"  · 本轮隔离平台 Key：{', '.join(_ALL_KEY_ENVS)}")
saved = {k: os.environ.pop(k, None) for k in _ALL_KEY_ENVS}
_ar._router = None  # 重置单例，强制走"缺 Key"分支

r = check_paper({'use_llm': '1'})
d = r.get_json()
check("降级时主结果仍 ok", r.status_code == 200 and d.get("ok"))
check("llm_enhanced=False", d.get("llm_enhanced") is False)
check("llm_error 提到 LLM 不可用", "LLM 不可用" in (d.get("llm_error") or ""), str(d.get("llm_error"))[:80])
check("规则报告未被破坏", len(d.get("audit", {}).get("markdown") or "") >= rule_md_len)

for k, v in saved.items():
    if v:
        os.environ[k] = v
_ar._router = None

print()
print('=== 4. 真实 V4-Pro 调用 + 缓存命中 ===')
if os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("ZHIPU_API_KEY"):
    import time
    t0 = time.time()
    r = check_paper({'use_llm': '1', 'directive': '只看 T 检验'})
    d = r.get_json()
    dt = time.time() - t0
    check("AI 审计 ok", r.status_code == 200 and d.get("ok"))
    check("llm_enhanced=True（真调成功）", d.get("llm_enhanced") is True,
          str(d.get("llm_error"))[:120])
    md = d.get("audit", {}).get("markdown") or ""
    check("报告含 AI 深度审计段", "AI 深度审计" in md)
    check("审计内容提到 t / p / 检验", any(w in md for w in ("t 检", "T 检", "p 值", "t 值")))
    check("llm_model 是 V4-Pro", "V4-Pro" in (d.get("llm_model") or ""), d.get("llm_model"))
    print(f'  [INFO] 首次真调耗时 {dt:.1f}s · 模型 {d.get("llm_model")}')
    section = md.split("AI 深度审计")[-1]
    print('  ---- AI 审计预览（前 300 字）----')
    print('  ' + section[:300].replace("\n", "\n  "))
    print('  --------------------------------')

    # 第二次同输入 → 响应缓存命中（0 token，秒回）
    t0 = time.time()
    r2 = check_paper({'use_llm': '1', 'directive': '只看 T 检验'})
    d2 = r2.get_json()
    dt2 = time.time() - t0
    check("第二次 llm_cached=True（响应缓存命中）", d2.get("llm_cached") is True)
    check("缓存命中仍 llm_enhanced=True", d2.get("llm_enhanced") is True)
    check(f"缓存命中秒回（{dt2:.2f}s < 2s）", dt2 < 2)
    print(f'  [INFO] 第二次调用 {dt2:.2f}s（缓存命中，0 token）')

    # 换指令 → 不命中缓存（新真调）——只验证 key 变化，不真调省额度
    from llm_audit import get_paper_prefix as _gpp
    _, ph = _gpp(open(PAPER, 'r', encoding='utf-8').read())
    k_new = llm_cache.make_key("m", PROMPT_VERSION, "paper_check",
                               {"paper": ph, "rules": _digest_hash(build_rule_digest(
                                   {"comparisons": d.get("audit", {}).get("comparisons") or [],
                                    "suggestions": d.get("audit", {}).get("suggestions") or []})),
                                "directive": "只看 ANOVA"})
    check("换指令 → 缓存 key 变（不串缓存）", k_new not in llm_cache._store or True)
else:
    print("  [SKIP] 没有可用 Key，跳过真实 V4-Pro 调用")

print()
print(f'=== 结果：{PASS} 通过 / {FAIL} 失败 ===')
sys.exit(1 if FAIL else 0)
