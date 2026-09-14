"""
Kimi（月之暗面 Moonshot）Agent（v1.8，通用适配器第六个 provider）
========================================================================
Kimi 开放平台（platform.kimi.com）OpenAI 兼容直连。
仿 QwenAgent 写薄壳（FreeLLMAPI 的 provider 模板模式）：
加平台只写 ProviderConfig + 薄壳类，调用逻辑全复用通用适配器。

关键事实（2026-09 实测，凭真实 Key 打通）：
    - 兼容接口：https://api.moonshot.cn/v1（OpenAI Chat Completions 兼容）
    - Key 环境变量：KIMI_API_KEY（platform.kimi.com 控制台申请）
    - 平台现役模型（GET /models 实测只有两个）：kimi-k2.6、kimi-k2.7-code。
      moonshot-v1 系列已于 2026-08-31 下线；k3 旗舰未在本平台开放。
    - **温度约束（实测踩坑）**：kimi-k2.6 只接受 temperature=1，
      传其他值报 400 "invalid temperature: only 1 is allowed for this
      model"。→ complete() 捕获该错误后自动锁定 temperature=1 重试，
      并记住约束（本实例后续调用直接用 1，不再多打一次）。
      路由表里按状态传的 0.3 / 0.5 对 Kimi 会被此机制覆盖。
    - 长上下文是看家本领（整篇论文放得下），适合 paper_check 深度审计；
      付费档（无免费模型），免费档用户填自己的 Key（BYOK）即可直用。

什么时候用：
    - paper_check 的长上下文备胎：整篇论文 + 数据摘要一次喂进去
    - 路由表未引用它就不会实例化，没 Key 零影响

配置：
    - 环境变量 KIMI_API_KEY（必填，https://platform.kimi.com）
    - 环境变量 KIMI_BASE_URL（可选覆盖；国际版把默认地址换成
      https://api.moonshot.ai/v1 即可）
"""
from __future__ import annotations

from .base import AgentError
from .siliconflow_agent import SiliconFlowAgent


# Kimi 开放平台现役模型 ID（GET /models 实测，2026-09）
KIMI_MODELS = {
    "kimi-k2.6":      "kimi-k2.6",       # 现役主力（温度恒为 1，见类文档）
    "kimi-k2.7-code": "kimi-k2.7-code",  # 代码/结构化任务档
    # 未登记的模型名原样透传（平台以后上新不用改代码）
}


class KimiAgent(SiliconFlowAgent):
    """Kimi 开放平台 OpenAI 兼容直连（v1.8，付费档，BYOK 友好）。

    复用通用适配器全部能力（多 Key 轮转 / usage 采集 / Retry-After）。
    额外处理平台的 temperature=1 约束（自动适配 + 记忆，见类文档）。
    """

    provider = "kimi"
    DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
    env_var = "KIMI_API_KEY"

    def __init__(self, model: str = "kimi-k2.6", **kwargs):
        super().__init__(model=model, **kwargs)
        # 平台温度约束记忆：None = 未知（允许任意温度）；True = 该模型
        # 只接受 temperature=1（首次撞到 400 后锁定，后续不再重试）
        self._temp_forced: bool | None = None

    def _resolve_model(self, model: str) -> str:
        """Kimi 模型别名解析。必须覆盖：防止父类 SiliconFlowAgent 的
        COMMON_MODELS 把同名别名错误改写成硅基流动的模型 ID。"""
        return KIMI_MODELS.get(model, model)

    def complete(self, prompt: str, *, temperature: float | None = None,
                 max_tokens: int | None = None, system: str | None = None) -> str:
        """调用 Kimi 模型；对平台的 temperature=1 约束自动适配，
        空内容抛 AgentError 让 Router 走下一个容灾候选
        （kimi-k2.6 是思考模型：max_tokens 太小时思考吃光额度、正文为空）。"""
        if self._temp_forced:
            temperature = 1.0
        try:
            result = super().complete(prompt, temperature=temperature,
                                      max_tokens=max_tokens, system=system)
        except AgentError as e:
            if "invalid temperature" in str(e).lower() and not self._temp_forced:
                # 锁定约束并按平台要求用 temperature=1 重试一次
                self._temp_forced = True
                result = super().complete(prompt, temperature=1.0,
                                          max_tokens=max_tokens, system=system)
            else:
                raise
        if not result.strip():
            raise AgentError(
                f"Kimi {self.model_name} 返回空内容：思考 token 可能耗尽了 "
                f"max_tokens（思考也计入 completion）。请调大 max_tokens。")
        return result

    def __repr__(self) -> str:
        return (f"<KimiAgent model={self.model_name} "
                f"temp_forced={self._temp_forced} base_url={self.base_url}>")
