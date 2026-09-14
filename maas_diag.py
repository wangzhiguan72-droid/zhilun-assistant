"""
百炼 MaaS 端点「模型可访问性」诊断（v0.5.4）
=============================================
用途：工作空间 /models 能列出上百个模型，但**能列 ≠ 能调**。
本脚本逐个真调一批代表模型，打印完整报错，判定这把 Key 实际放行哪些。

两类典型拒绝：
    AccessDenied.Unpurchased (403) —— 工作空间未开通/未购买该模型
    Arrearage (400)                —— 阿里云账号欠费，第三方模型不可用

账号侧修好后重跑本脚本即可确认放行清单。

用法：
    .venv/Scripts/python.exe maas_diag.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env_loader import load_dotenv  # noqa: E402

load_dotenv()

from agents import MaasAgent, AgentError  # noqa: E402
from agents.secrets_guard import redact  # noqa: E402

# 覆盖几个可能的命名空间变体
PROBE = [
    "glm-5",
    "ZHIPU/GLM-5",
    "GLM-5",
    "glm-4.7",
    "qwen-plus",
    "qwen-plus-2025-07-28",
    "qwen3.7-flash",
    "qwen3.7-plus",
    "qwen-flash",
    "qwen-turbo",
    "deepseek-v4-pro",
    "deepseek-v3.2",
    "vanchin/deepseek-v4-pro",
    "siliconflow/deepseek-v3.2",
    "kimi/kimi-k2.6",
    "MiniMax/MiniMax-M2.5",
    "qwen3-vl-plus",
    "qwen-mt-flash",
]

print("模型访问诊断（完整报错）")
print("=" * 70)
ok, denied = [], []
for name in PROBE:
    try:
        ag = MaasAgent(model=name)
        out = ag.complete("只回答两个字：收到", max_tokens=4096)
        print(f"[OK]   {name:34s} → {out.strip()[:40]}")
        ok.append(name)
    except AgentError as e:
        msg = redact(e)
        # 压缩空白便于阅读
        flat = " ".join(str(msg).split())
        print(f"[DENY] {name:34s} → {flat[:230]}")
        denied.append(name)
    except Exception as e:  # noqa: BLE001
        print(f"[ERR ] {name:34s} → {type(e).__name__}: {redact(e)[:200]}")

print("=" * 70)
print(f"放行 {len(ok)}：{ok}")
print(f"拒绝 {len(denied)}")
