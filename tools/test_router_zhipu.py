"""
Router 多 Provider 升级后端到端测试（v0.4 智谱接入）
====================================================
覆盖三种情况：
    1. 只设 SILICONFLOW_API_KEY（无智谱 Key）
       → recommend 走容灾链退回 sf/glm-z1-9b ✅
    2. paper_check / write_text 仍正常走硅基流动 V4 系列
    3. （用户填了 ZHIPU_API_KEY 后）recommend 走智谱 glm-4.7-flash

Key 从 .env.tmp 临时读取（或已有的环境变量），跑完删除，不写进源码。
"""
import pathlib
import os
import sys

# 把 .env.tmp 临时导入为环境变量（不写入源码）
ENV_TMP = str(BASE / '.env.tmp')
if os.path.exists(ENV_TMP):
    with open(ENV_TMP, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = pathlib.Path(__file__).resolve().parent

from agents.router import Router, STATE_TO_MODEL, _PROVIDERS
from agents import ZhipuAgent

print('=== Provider 注册表 ===')
for p, cls in _PROVIDERS.items():
    print(f'  {p:6s} → {cls.__name__}')
print()

print('=== 路由表（容灾链顺序）===')
for state, chain in STATE_TO_MODEL.items():
    print(f'  {state:12s} → ' + ' → '.join(f'{p}/{m}' for p, m, t in chain))
print()

has_zhipu = bool(os.environ.get('ZHIPU_API_KEY', '').strip())
has_sf = bool(os.environ.get('SILICONFLOW_API_KEY', '').strip())
print(f'环境变量：SILICONFLOW_API_KEY={"有" if has_sf else "无"}，ZHIPU_API_KEY={"有" if has_zhipu else "无"}')
print()

router = Router()

# --- 测试 1：recommend（智谱优先，无 Key 时退回硅基流动）---
print('--- [recommend] 预期：' + ('智谱 glm-4.7-flash' if has_zhipu else '容灾退回 sf/glm-z1-9b') + ' ---')
try:
    ans = router.complete('recommend',
        '我有两个独立样本（男/女），想比较平均成绩差异。用什么统计方法？一句话回答。',
        max_tokens=200)
    print(f'  [OK] 实际路由：{router.health()["recommend"]}')
    print(f'  -> {ans[:200]}')
except Exception as e:
    print(f'  [FAIL] {type(e).__name__}: {str(e)[:300]}')
print()

# --- 测试 2：paper_check（硅基流动 V4-Pro）---
print('--- [paper_check] 预期：sf/deepseek-v4-pro ---')
try:
    ans = router.complete('paper_check',
        '一句话总结：t 检验的 p 值小于 0.05 时，研究者最常见的错误解读是什么？',
        max_tokens=200)
    print(f'  [OK] 实际路由：{router.health()["paper_check"]}')
    print(f'  -> {ans[:200]}')
except Exception as e:
    print(f'  [FAIL] {type(e).__name__}: {str(e)[:300]}')
print()

# --- 测试 3：write_text（硅基流动 V4-Flash 免费）---
print('--- [write_text] 预期：sf/deepseek-v4-flash ---')
try:
    ans = router.complete('write_text',
        '把"结果显著，p<0.05"改写成更学术、更不像 AI 写的句子。一句话。',
        max_tokens=200)
    print(f'  [OK] 实际路由：{router.health()["write_text"]}')
    print(f'  -> {ans[:200]}')
except Exception as e:
    print(f'  [FAIL] {type(e).__name__}: {str(e)[:300]}')
print()

# --- 测试 4：analyze 状态应该拒绝调 LLM ---
print('--- [analyze] 预期：拒绝（走 Python 计算层）---')
try:
    router.complete('analyze', '测试')
    print('  [FAIL] 不应该走到这里')
except Exception as e:
    print(f'  [OK] 正确拒绝：{str(e)[:100]}')
print()

# --- 测试 5：health 总览 ---
print('=== Router.health() 总览 ===')
for state, info in router.health().items():
    print(f'  {state:12s} → {info}')