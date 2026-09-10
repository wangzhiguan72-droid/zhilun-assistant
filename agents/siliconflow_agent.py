"""
硅基流动 Agent（v0.5 重构为通用适配器薄壳）
=============================================
v0.4：独立实现全部调用逻辑
v0.5：调用核心（多 Key 轮转 / usage 采集 / Retry-After）下沉到
      agents/openai_compat.py 的 OpenAICompatAgent，本类只剩：
      - COMMON_MODELS 模型别名表
      - provider = "sf" 的注册声明

加新平台（DeepSeek 官方、ModelScope……）：看 openai_compat.PROVIDER_REGISTRY，
写一行配置即可，不用再复制整个类（FreeLLMAPI 的 provider 模板模式）。

配置：
    - 环境变量 SILICONFLOW_API_KEY（必填；v0.5 起支持逗号分隔多 Key 轮转）
    - 环境变量 SILICONFLOW_BASE_URL（可选，默认 https://api.siliconflow.cn/v1）
"""
from __future__ import annotations

from .base import BaseAgent
from .openai_compat import OpenAICompatAgent


# 一些常用模型的官方命名（硅基流动）
# 注意：硅基流动上的 GLM 名称和智谱自己平台不同，没有 glm-4-flash。
# 实际可用 + 大概率免费的：THUDM/GLM-Z1-9B-0414、Qwen/Qwen2.5-7B-Instruct、deepseek-ai/DeepSeek-V4-Flash
#
# 命名约定的解读（v0.4 实测）：
#   - Pro/ 前缀：硅基流动"付费加速档"，独立推理资源、不被免费用户挤占
#   - V4 系列：DeepSeek 新一代旗舰架构（V4-Pro 同代顶级 / V4-Flash 同代轻量快档）
#   - V3 系列：DeepSeek 上一代，现已沦为中端
#   - Flash 后缀：同代轻量快速版，质量略低但速度快、价格低
#   - 裸名（无前缀）：标准档，可能共享资源、可能限速
COMMON_MODELS = {
    # === DeepSeek V4 新一代旗舰 ===
    "deepseek-v4-pro":   "deepseek-ai/DeepSeek-V4-Pro",   # 顶级质量档，写作/审计/复杂推理首选
    "deepseek-v4-flash": "deepseek-ai/DeepSeek-V4-Flash", # V4 轻量快档，**免费**，日常主力
    # === DeepSeek V3 上一代 ===
    "deepseek-v3":       "deepseek-ai/DeepSeek-V3",
    "deepseek-v3.1":     "deepseek-ai/DeepSeek-V3.1-Terminus",
    "deepseek-v3.2":     "deepseek-ai/DeepSeek-V3.2",
    "deepseek-r1":       "deepseek-ai/DeepSeek-R1",
    # === Qwen 通义千问 ===
    "qwen2.5-7b":        "Qwen/Qwen2.5-7B-Instruct",
    "qwen2.5-14b":       "Qwen/Qwen2.5-14B-Instruct",
    "qwen2.5-32b":       "Qwen/Qwen2.5-32B-Instruct",
    "qwen2.5-72b":       "Qwen/Qwen2.5-72B-Instruct",
    "qwen3-8b":          "Qwen/Qwen3-8B",
    "qwen3.5-4b":        "Qwen/Qwen3.5-4B",               # 手机端可跑
    "qwen3.5-9b":        "Qwen/Qwen3.5-9B",
    # === GLM 智谱（硅基流动版）：实际可用 + 大概率免费 ===
    "glm-z1-9b":         "THUDM/GLM-Z1-9B-0414",          # GLM 最新推理 9B，方法推荐首选（轻量）
    "glm-4-9b":          "THUDM/GLM-4-9B-0414",
    "glm-4-32b":         "THUDM/GLM-4-32B-0414",
    # === Kimi 月之暗面 ===
    "kimi-k2":           "Pro/moonshotai/Kimi-K2.6",      # Pro/ 付费加速档
}


class SiliconFlowAgent(OpenAICompatAgent, BaseAgent):
    """通过硅基流动 OpenAI 兼容 API 调用任意国产大模型。

    v0.5 起调用逻辑全部来自 OpenAICompatAgent（多 Key 轮转 +
    usage 采集 + Retry-After 透传），本类只声明 provider 归属。
    """

    DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
    provider = "sf"
    # 子类（如 ZhipuAgent）可覆盖此属性来换 provider
    env_var = "SILICONFLOW_API_KEY"

    def _resolve_model(self, model: str) -> str:
        """友好名 → 官方模型 ID。子类（QwenAgent 等）可覆盖以用自己的别名表。

        注意：QwenAgent 的 qwen3.5-9b 等别名与本表同名但指向不同平台
        的模型 ID，必须由子类先解析，否则会被本表错误覆盖。
        """
        return COMMON_MODELS.get(model, model)

    def __init__(self, model: str = "qwen2.5-7b", *,
                 default_temperature: float = 0.5,
                 default_max_tokens: int = 2048,
                 base_url: str | None = None,
                 api_key: str | None = None):
        # 解析友好名 → 官方模型 ID（子类可覆盖 _resolve_model）
        self.model_name = self._resolve_model(model)
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        # Key 优先级：显式传入（BYOK）> 环境变量（多 Key 逗号分隔）
        # base_url 优先级：显式传入 > 环境变量 > 注册表默认（见 _init_provider）
        self._init_provider(base_url=base_url, api_key=api_key)

    def _extra_create_kwargs(self) -> dict:
        """子类钩子：往 chat.completions.create 传额外参数（如智谱的 thinking 开关）。"""
        return {}

    def complete(self, prompt: str, *,
                 temperature: float | None = None,
                 max_tokens: int | None = None,
                 system: str | None = None) -> str:
        """调用模型，返回纯文本。失败抛 AgentError，便于 router fallback。"""
        return self._complete_openai_compat(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            default_temperature=self.default_temperature,
            default_max_tokens=self.default_max_tokens,
            system=system,
            extra_kwargs=self._extra_create_kwargs(),
            empty_guard=False,  # 保持 v0.4 行为：空内容不报错（Zhipu 子类自己兜）
        )

    def __repr__(self) -> str:
        return (f"<SiliconFlowAgent model={self.model_name} "
                f"base_url={self.base_url}>")
