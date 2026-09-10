"""
Agent 抽象基类
==============
所有子智能体必须实现 `complete(prompt) -> str`。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AgentError(Exception):
    """Agent 调用失败的统一异常。

    可选属性 retry_after（秒）：429 限流时若平台响应头带 Retry-After，
    Agent 会解析后挂在这里，Router 用它替代默认冷却时长。
    """
    retry_after: float | None = None


class BaseAgent(ABC):
    """所有 LLM Agent 的抽象基类。

    设计要点：
        - 只有一个入口 `complete(prompt)`，屏蔽不同模型的 SDK 差异
        - 内部参数（模型名、温度、max_tokens）在子类构造时固化
        - 失败时抛 AgentError，便于 router 捕获并 fallback
    """

    name: str = "base"
    model_name: str = ""
    # 上次调用的 usage 观测（v0.5 前缀缓存工程）：
    # {prompt_tokens, completion_tokens, total_tokens, cached_tokens}
    # None = 尚未成功调用。Router / 测试用它验证缓存命中。
    last_usage: dict | None = None

    @abstractmethod
    def complete(self, prompt: str, *, temperature: float | None = None,
                max_tokens: int | None = None) -> str:
        """调用底层模型，返回纯文本。失败抛 AgentError。"""

    # 给子类一个友好提示：缺失 Key 时的统一报错文案
    @staticmethod
    def _require_env(var_name: str) -> str:
        import os
        val = os.environ.get(var_name, "").strip()
        if not val:
            raise AgentError(
                f"环境变量 {var_name} 未设置。"
                f"请在终端执行 `export {var_name}=你的Key`（或写在 .env 文件里），"
                f"然后再启动服务。绝对不要把 Key 直接写进代码。"
            )
        return val

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model={self.model_name}>"