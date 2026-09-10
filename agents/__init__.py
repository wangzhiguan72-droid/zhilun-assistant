"""
Agent 子模块
============
按 ROADMAP §5「多模型路由与缓存」设计，每个模型一个专属子智能体。

当前版本 (v0.5)：
    - BaseAgent: 抽象接口（所有 Agent 必须实现 complete）+ last_usage 观测
    - OpenAICompatAgent: 通用 OpenAI 兼容适配器（多 Key 轮转 /
      usage 采集 / Retry-After 透传，agents/openai_compat.py）
    - SiliconFlowAgent: 硅基流动薄壳（provider="sf"）
    - ZhipuAgent: 智谱 BigModel 薄壳（GLM 代次 thinking 兼容 + 前缀缓存观测）
    - DeepSeekAgent: DeepSeek 官方直连薄壳（provider="deepseek"，v0.5 新增）
    - Router: 多 Provider 状态机（容灾链 + 429 冷却，Retry-After 感知）

加新平台：在 openai_compat.PROVIDER_REGISTRY 写一行配置 + 一个薄壳类，
仿 DeepSeekAgent（FreeLLMAPI 的 provider 模板模式）。

API Key 安全：
    - 所有 Key 从环境变量读，禁止写入代码
    - 支持逗号分隔多 Key 轮转（429 时先轮 Key 再轮模型）
    - SiliconFlowAgent → SILICONFLOW_API_KEY
    - ZhipuAgent → ZHIPU_API_KEY（https://bigmodel.cn/apikey/platform）
    - DeepSeekAgent → DEEPSEEK_API_KEY（https://platform.deepseek.com）
    - 缺失时 Router 自动退回容灾链的下一个免费档
"""

from .base import BaseAgent, AgentError
from .openai_compat import OpenAICompatAgent, PROVIDER_REGISTRY
from .siliconflow_agent import SiliconFlowAgent
from .zhipu_agent import ZhipuAgent
from .deepseek_agent import DeepSeekAgent
from .qwen_agent import QwenAgent
from .router import Router, get_router

__all__ = [
    "BaseAgent", "AgentError", "OpenAICompatAgent", "PROVIDER_REGISTRY",
    "SiliconFlowAgent", "ZhipuAgent", "DeepSeekAgent", "QwenAgent",
    "Router", "get_router",
]
