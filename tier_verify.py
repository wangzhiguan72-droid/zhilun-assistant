"""
档位（tier）行为验证（v0.5.3）
==============================
验证免费档 / 会员档把状态路由到不同的模型：

    free（默认）：只走免费模型 —— paper_check / write_text 都落到 glm-4.7-flash
    pro（会员）  ：走付费优先链 —— paper_check 落到 deepseek-flash

只实例化 Agent、看 model_name，**不真调 API**，因此不耗额度、不受 429 影响。
端到端真调见 write_text 真调小节（可选）。
"""
import sys

sys.path.insert(0, r'D:\论文排版辅助agent')

from env_loader import load_dotenv
load_dotenv()

from agents.router import Router  # noqa: E402

STATES = ("recommend", "write_text", "paper_check")


def show(tier: str) -> None:
    r = Router(tier=tier)
    print(f"[{tier} 档] tier={r.tier}")
    for state in STATES:
        try:
            agent = r._get_agent(state)
            print(f"    {state:12s} -> {agent.model_name}")
        except Exception as e:  # noqa: BLE001
            print(f"    {state:12s} -> 不可用：{type(e).__name__}: {str(e)[:80]}")
    print()


if __name__ == "__main__":
    show("free")
    show("pro")
