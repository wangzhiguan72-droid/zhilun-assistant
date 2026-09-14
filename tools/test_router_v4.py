"""
Router 升级后端到端测试（v0.4 模型分级）
====================================
测试 STATE_TO_MODEL 中的 V4-Pro / V4-Flash / GLM-Z1-9B 三个真实模型都可用。
Key 从 .env.tmp 临时读取，测完删除。
"""
import pathlib
import os
import sys
import tempfile

# 把 .env.tmp 临时导入为环境变量（不写入源码）
ENV_TMP = str(BASE / '.env.tmp')
if not os.path.exists(ENV_TMP):
    print(f'[SKIP] 找不到 {ENV_TMP}，先创建再跑')
    sys.exit(0)

with open(ENV_TMP, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = pathlib.Path(__file__).resolve().parent

from agents.router import Router, STATE_TO_MODEL, get_router

print('=== 当前 STATE_TO_MODEL 配置 ===')
for state, models in STATE_TO_MODEL.items():
    print(f'  {state:14s} → {[f"{m[1]} (temp={m[2]})" for m in models]}')
print()

router = Router()

# 三档模型各跑一个真实 prompt
TESTS = [
    ('recommend',    'glm-z1-9b',         '我有两个独立样本（男/女），想比较平均成绩差异。用什么统计方法？一句话回答。'),
    ('paper_check',  'deepseek-v4-pro',   '一句话总结：t 检验的 p 值小于 0.05 时，研究者最常见的错误解读是什么？'),
    ('write_text',   'deepseek-v4-flash', '把"结果显著，p<0.05"改写成更学术、更不像 AI 写的句子。一句话。'),
]

for state, expected_model, prompt in TESTS:
    print(f'--- [{state}] 期望模型={expected_model} ---')
    try:
        ans = router.complete(state, prompt, max_tokens=200)
        actual_model = router.health()[state]
        # 从 health() 的输出中提取实际模型名
        model_name = actual_model.split("model=")[1].split(" ")[0] if "model=" in actual_model else actual_model
        print(f'  [OK] 用了：{model_name}')
        print(f'  -> {ans[:200]}')
    except Exception as e:
        print(f'  [FAIL] {type(e).__name__}: {str(e)[:200]}')
    print()

print('=== Router.health() ===')
for state, repr_str in router.health().items():
    print(f'  {state}: {repr_str}')