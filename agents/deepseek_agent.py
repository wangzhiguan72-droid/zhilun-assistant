"""
DeepSeek 官方 Agent（v0.5，通用适配器第三个 provider）
======================================================
DeepSeek 官方平台（api.deepseek.com）OpenAI 兼容直连。v0.4 时只有
硅基流动一条路，v0.5 用 openai_compat 注册表把官方入口也接进来：

    - 上下文缓存**自动生效**（官方文档：服务自动运行，按实际命中计费，
      命中价约为未命中 3%，四家国产里最低价差）
    - usage 采 deepseek 官方字段 prompt_cache_hit_tokens（兼容层已处理）

什么时候用：
    - 平时不用（硅基流动有余额 + 智谱免费档足够）
    - 硅基流动平台级故障 / 余额耗尽时，把容灾链切到这里
    - 路由表未引用它就不会实例化，没 Key 零影响

模型 ID 注意（官方命名跟硅基流动的 deepseek-ai/* 完全不同）：
    - deepseek-flash     V4 轻量快档（**推理模型**：思考 token 计入 completion，
                         max_tokens 给小会导致 content 为空）
    - deepseek-v4-pro    V4 旗舰
    ⚠️ deepseek-chat / deepseek-reasoner 是 V3 时代旧名，2026-09-11 实测已从
       官方模型列表退役（/models 只返回上面两个）；这里保留别名映射仅作兼容，
       新代码请直接用 deepseek-flash / deepseek-v4-pro。

配置：
    - 环境变量 DEEPSEEK_API_KEY（必填，https://platform.deepseek.com）
    - 环境变量 DEEPSEEK_BASE_URL（可选覆盖）
"""
from __future__ import annotations

from .base import AgentError
from .siliconflow_agent import SiliconFlowAgent


# DeepSeek 官方模型 ID（跟硅基流动的 deepseek-ai/DeepSeek-V4-* 命名不同）
#
# 2026-09-11 实测（官方 /models + 真调）：
#   模型列表只有 2 个 —— deepseek-flash（V4 轻量快档）、deepseek-v4-pro（V4 旗舰）。
#   deepseek-chat / deepseek-reasoner 是 V3 时代旧名，已从列表退役；保留映射
#   只是为了避免上层硬编码直接崩。
DEEPSEEK_MODELS = {
    "deepseek-flash":    "deepseek-flash",     # V4 轻量快档 ★
    "deepseek-v4-pro":   "deepseek-v4-pro",    # V4 旗舰
    "deepseek-v4-flash": "deepseek-flash",     # 别名（官方也直接接受此名）
    # ↓ 旧名兼容（V3 时代，实测已退役；新代码勿用）
    "deepseek-chat":     "deepseek-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
}


class DeepSeekAgent(SiliconFlowAgent):
    """DeepSeek 官方直连（v0.5，暂不在路由表默认链中）。

    复用通用适配器全部能力，只声明 provider 归属。
    """

    provider = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
    env_var = "DEEPSEEK_API_KEY"

    def _resolve_model(self, model: str) -> str:
        """DeepSeek 官方模型 ID 解析。**必须覆盖父类**：deepseek-v4-flash 等
        别名与硅基流动 COMMON_MODELS 同名但指向不同命名
        （deepseek-ai/DeepSeek-V4-Flash），不覆盖会被父类错误改写成硅基流动
        的模型 ID，官方直连直接 400。"""
        return DEEPSEEK_MODELS.get(model, model)

    def __init__(self, model: str = "deepseek-flash", *,
                 default_max_tokens: int = 4096, **kwargs):
        # 默认 max_tokens 放大到 4096：deepseek-flash 是推理模型，思考链
        # 也计 completion token，2048 在长前缀下可能被思考吃光导致 content 空
        super().__init__(model=model, default_max_tokens=default_max_tokens,
                         **kwargs)

    def complete(self, prompt: str, **kwargs) -> str:
        """调用官方模型。额外兜一层：推理模型的思考 token 耗尽 max_tokens
        时 content 会是空串，明确报错让 Router 走容灾链（而不是把空文本
        当成有效结果往上传）。"""
        result = super().complete(prompt, **kwargs)
        if not result.strip():
            raise AgentError(
                f"DeepSeek {self.model_name} 返回空内容：推理模型的思考 token "
                f"耗尽了 max_tokens（思考也计入 completion）。请调大 max_tokens。"
            )
        return result

    def __repr__(self) -> str:
        return (f"<DeepSeekAgent model={self.model_name} "
                f"base_url={self.base_url}>")
