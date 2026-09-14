"""
小米 MiMo Agent（v1.8，通用适配器第七个 provider）
========================================================
小米 MiMo 开放平台（platform.xiaomimimo.com）OpenAI 兼容直连。
仿 QwenAgent 写薄壳（FreeLLMAPI 的 provider 模板模式）：
加平台只写 ProviderConfig + 薄壳类，调用逻辑全复用通用适配器。

关键事实（2026-09 实测，凭真实 Key 打通）：
    - 兼容接口：https://api.xiaomimimo.com/v1（OpenAI Chat Completions
      与 Anthropic 双协议兼容，这里走 OpenAI 协议）
    - Key 环境变量：MIMO_API_KEY（platform.xiaomimimo.com 控制台申请，
      Key 形如 sk-xxxxx）
    - 平台现役模型（GET /models 实测）：mimo-v2.5、mimo-v2.5-pro，
      外加 asr / tts 语音系列（不在本项目链路里，不登记）。
      模型 ID 全小写；未登记的名字原样透传。
    - **连接偏慢（实测踩坑）**：首连接 6 秒以上，openai SDK 默认 5 秒
      连接超时会误杀 → _build_client 放宽到 connect=15s / 总 120s。
    - 新平台常有免费额度 / 首周免费活动，但政策随时可变 —— 不进
      FREE_MODELS 白名单（按付费档处理），BYOK 用户自带 Key 直用。

什么时候用：
    - recommend / write_text / audit_chat 的轻量尾位备胎
    - 路由表未引用它就不会实例化，没 Key 零影响

配置：
    - 环境变量 MIMO_API_KEY（必填，https://platform.xiaomimimo.com）
    - 环境变量 MIMO_BASE_URL（可选覆盖）
"""
from __future__ import annotations

from .base import AgentError
from .siliconflow_agent import SiliconFlowAgent


# 小米 MiMo 现役模型 ID（GET /models 实测，全小写）
MIMO_MODELS = {
    "mimo-v2.5":     "mimo-v2.5",      # 主力档（默认）
    "mimo-v2.5-pro": "mimo-v2.5-pro",  # 质量档
    # 旧一代别名 → 就近映射（平台已不提供 V2 系列，防前端/旧配置写死）
    "mimo-v2-flash": "mimo-v2.5",
    "mimo-v2-pro":   "mimo-v2.5-pro",
    # 未登记的模型名原样透传（用户直接填官方 ID 也能用）
}


class MimoAgent(SiliconFlowAgent):
    """小米 MiMo OpenAI 兼容直连（v1.8，付费档，BYOK 友好）。

    复用通用适配器全部能力，只声明 provider 归属 + 两点平台适配
    （连接超时放宽、空内容防护，均见类文档）。
    """

    provider = "mimo"
    DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
    env_var = "MIMO_API_KEY"

    def _resolve_model(self, model: str) -> str:
        """MiMo 模型别名解析：平台 ID 全小写，故按小写匹配
        （宣传页常写 MiMo-V2.5，别让大小写挡住用户）；
        未登记的名字原样透传（用户直接填官方 ID 也能用）。"""
        return MIMO_MODELS.get(model.lower(), model)

    def __init__(self, model: str = "mimo-v2.5", *,
                 default_max_tokens: int = 4096, **kwargs):
        # 默认 4096（对齐 QwenAgent）：mimo-v2.5 是思考模型，思考 token
        # 计入 completion，max_tokens 给小了（如 64）会被思考吃光，
        # 正文为空 → 看起来像"没回答"
        super().__init__(model=model, default_max_tokens=default_max_tokens,
                         **kwargs)

    def _build_client(self):
        """MiMo 平台连接偏慢（首连实测 6s+），SDK 默认 5s 连接超时会误杀
        → 放宽到 connect=15s / 总 120s（openai.Timeout 是 httpx.Timeout）。"""
        try:
            from openai import OpenAI, Timeout
        except ImportError as e:
            raise AgentError(
                "缺少 openai SDK，请先 `pip install openai`（在 .venv 里）。"
            ) from e
        return OpenAI(api_key=self._api_key, base_url=self.base_url,
                      timeout=Timeout(120.0, connect=15.0))

    def complete(self, prompt: str, **kwargs) -> str:
        """调用 MiMo 模型。多兜一层：空内容明确报错，
        让 Router 走下一个容灾候选（对齐 QwenAgent 的防护）。"""
        result = super().complete(prompt, **kwargs)
        if not result.strip():
            raise AgentError(
                f"小米 MiMo {self.model_name} 返回空内容，"
                f"请检查 prompt、max_tokens 或稍后重试。")
        return result

    def __repr__(self) -> str:
        return f"<MimoAgent model={self.model_name} base_url={self.base_url}>"
