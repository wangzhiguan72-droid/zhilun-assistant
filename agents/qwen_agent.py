"""
阿里云百炼 Agent（v0.5.1，通用适配器第四个 provider）
========================================================
阿里云百炼（dashscope.aliyuncs.com）OpenAI 兼容模式直连。
仿 DeepSeekAgent 写薄壳（FreeLLMAPI 的 provider 模板模式）：
加平台只写 ProviderConfig + 薄壳类，调用逻辑全复用通用适配器。

关键事实（2026-09 查证）：
    - 兼容接口：https://dashscope.aliyuncs.com/compatible-mode/v1
    - Key 环境变量：DASHSCOPE_API_KEY
    - 覆盖 Qwen 全系列（含 Qwen3.5 / Qwen3.7 / Qwen3-Omni 多模态）
    - Qwen 的缓存机制分两套：显式 cache_control（需手动加，5 分钟有效期，
      创建按 125% 计费、命中按 10%）+ 隐式缓存（自动生效、命中按 20%）。
      本项目用冻结前缀契约 → 隐式缓存自动生效，无需额外配置。

什么时候用：
    - 智谱 / 硅基流动 / DeepSeek 官方都不可用时，百炼是兜底
    - Qwen3.5-4B / 9B 免费额度足够轻量任务（方法推荐等）
    - 路由表未引用它就不会实例化，没 Key 零影响

配置：
    - 环境变量 DASHSCOPE_API_KEY（必填，https://help.aliyun.com/model-studio/get-api-key）
    - 环境变量 DASHSCOPE_BASE_URL（可选覆盖）
"""
from __future__ import annotations

from .base import AgentError
from .siliconflow_agent import SiliconFlowAgent


# 阿里云百炼常用模型 ID（官方命名）
QWEN_MODELS = {
    # Qwen3.5 系列（v0.5.1 实测：免费额度覆盖 4B/9B）
    "qwen3.5-4b":  "qwen3.5-4b",
    "qwen3.5-9b":  "qwen3.5-9b",
    "qwen3.5-14b": "qwen3.5-14b",
    "qwen3.5-32b": "qwen3.5-32b",
    # Qwen3.7 系列（最新旗舰）
    "qwen3.7-plus":  "qwen3.7-plus",
    "qwen3.7-flash": "qwen3.7-flash",
    # Qwen3-Omni 多模态
    "qwen3-omni-flash": "qwen3-omni-flash",
    # 经典系列
    "qwen-plus":  "qwen-plus",
    "qwen-max":   "qwen-max",
    "qwen-turbo": "qwen-turbo",
}


class QwenAgent(SiliconFlowAgent):
    """阿里云百炼 OpenAI 兼容直连（v0.5.1，暂不在路由表默认链中）。

    复用通用适配器全部能力，只声明 provider 归属。
    """

    provider = "dashscope"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    env_var = "DASHSCOPE_API_KEY"

    def _resolve_model(self, model: str) -> str:
        """百炼模型别名解析。必须覆盖：qwen3.5-9b 等别名与硅基流动
        COMMON_MODELS 同名但指向不同平台的模型 ID（Qwen/Qwen3.5-9B），
        不覆盖会被父类错误改写。"""
        return QWEN_MODELS.get(model, model)

    def __init__(self, model: str = "qwen3.5-9b", *,
                 default_max_tokens: int = 4096, **kwargs):
        # 默认 4096：qwen3.7 系列是思考模型，思考 token 计入 completion，
        # 给小了（如 1024）长输出场景会被思考吃光导致 content 为空
        super().__init__(model=model, default_max_tokens=default_max_tokens,
                         **kwargs)

    def complete(self, prompt: str, **kwargs) -> str:
        """调用百炼模型。多兜一层：思考模型的思考 token 耗尽 max_tokens 时
        content 会是空串 → 明确报错让 Router 走下一个容灾候选。"""
        result = super().complete(prompt, **kwargs)
        if not result.strip():
            raise AgentError(
                f"百炼 {self.model_name} 返回空内容：思考 token 可能耗尽了 "
                f"max_tokens（思考也计入 completion）。请调大 max_tokens。")
        return result

    def __repr__(self) -> str:
        return (f"<QwenAgent model={self.model_name} "
                f"base_url={self.base_url}>")
