"""
百炼 MaaS 专属端点真调探针（v0.5.4）
=====================================
验证私有工作空间端点：
    1. 连通性 + /models 列表（看清单里哪些模型真的上线了）
    2. 逐个模型真调一次（思考模型给足 max_tokens）
    3. 顺带验证 Key 不会从任何对外接口漏出（repr / 报错文案）

Key 从项目根 .env 的 MAAS_API_KEY 读，不落盘、不回显。

用法：
    .venv/Scripts/python.exe maas_probe.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from env_loader import load_dotenv  # noqa: E402

load_dotenv()

from agents import MaasAgent, AgentError  # noqa: E402
from agents.maas_agent import MAAS_MODELS  # noqa: E402
from agents.secrets_guard import redact  # noqa: E402

# 逐个真调的模型（友好名）：VL 系只发纯文本，验证端点路由是否正常
CANDIDATES = [
    "glm-5",
    "qwen-plus-2025-07-28",
    "qwen-math-turbo",
    "qwen-mt-flash",
    "deepseek-r1-distill-qwen-7b",
    "qwen3-vl-30b-a3b-thinking",
]


def fmt_usage(u) -> str:
    if not u:
        return "（无 usage）"
    total = u.get("prompt_tokens", 0)
    cached = u.get("cached_tokens", 0)
    pct = (cached / total * 100) if total else 0.0
    return (f"prompt={total}, cached={cached} ({pct:.1f}%), "
            f"completion={u.get('completion_tokens', 0)}")


def main() -> int:
    if not os.environ.get("MAAS_API_KEY", "").strip():
        print("[SKIP] 未设置 MAAS_API_KEY，跳过")
        return 0

    print("=" * 66)
    print("百炼 MaaS 专属端点探针")
    print("=" * 66)

    # --- 1. 连通性 + 模型清单 ---
    print("\n[1] /models 列表")
    try:
        a = MaasAgent(model="qwen-plus-2025-07-28")
        print(f"    base_url: {a.base_url}")
        listed = a.client.models.list()
        ids = sorted(m.id for m in listed.data)
        print(f"    端点返回 {len(ids)} 个模型：")
        for i in ids:
            print(f"      - {i}")
    except AgentError as e:
        print(f"    [FAIL] {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"    [FAIL] {type(e).__name__}: {redact(e)}")
        return 1

    # --- 2. 逐模型真调 ---
    print("\n[2] 逐模型真调（探索器用短文，思考模型给 4096）")
    ok, fail = 0, 0
    for name in CANDIDATES:
        try:
            ag = MaasAgent(model=name)
            out = ag.complete("用一句话回答：什么是 p 值？",
                              max_tokens=4096)
            print(f"    [OK] {name:32s} → {out.strip()[:60]}")
            print(f"         usage: {fmt_usage(ag.last_usage)}")
            ok += 1
        except AgentError as e:
            print(f"    [× ] {name:32s} → {redact(e)[:120]}")
            fail += 1

    # --- 3. Key 泄露自检 ---
    print("\n[3] Key 泄露自检")
    key = os.environ["MAAS_API_KEY"].strip()
    ag = MaasAgent(model="glm-5")
    r = repr(ag)
    print(f"    repr: {r}")
    print(f"    repr 含 Key? {'是 ❌' if key in r else '否 ✅'}")
    print(f"    repr 含私有端点? {'是' if 'maas.aliyuncs.com' in r else '否'}")
    leaked = redact(f"调用失败：Authorization: Bearer {key} 无效")
    print(f"    报错文案擦除: {leaked[:60]}...")
    print(f"    擦除后仍含 Key? {'是 ❌' if key in leaked else '否 ✅'}")

    print(f"\n结果：{ok} 通 / {fail} 失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
