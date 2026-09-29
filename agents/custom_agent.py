"""
自定义供应商 Agent（v2.31）
===================================
任意 OpenAI 兼容端点（官方平台 / one-api、new-api 等中转）的接入壳。
参考 zcode / cc-switch 的供应商管理思路：用户自填三件套——
Base URL、API Key、模型名——即可接入，不需要改代码。

配置来源（优先级从高到低）：
    1. 请求参数：Router.set_custom_provider(base_url, key, models)
       —— 前端设置页「自定义供应商」三项，随请求注入（BYOK，Key 零残留）
    2. 环境变量：CUSTOM_LLM_BASE_URL + CUSTOM_LLM_API_KEY
    3. 都没有 → 实例化抛 AgentError，Router 容灾链自动跳过

模型名直传（不走 COMMON_MODELS 别名表）：自定义端点的模型 ID 千差万别，
别名映射只会帮倒忙；用户填什么就调什么。
"""
from __future__ import annotations

from .base import AgentError
from .siliconflow_agent import SiliconFlowAgent


class CustomAgent(SiliconFlowAgent):
    """OpenAI 兼容自定义端点（用户自填 base_url + Key + 模型名）。"""

    provider = "custom"
    env_var = "CUSTOM_LLM_API_KEY"
    DEFAULT_BASE_URL = ""    # 无默认：必须显式提供

    def _resolve_model(self, model: str) -> str:
        # 模型名直传：自定义端点不做任何别名映射
        return model

    def __init__(self, model: str = "", *,
                 default_temperature: float = 0.5,
                 default_max_tokens: int = 2048,
                 base_url: str | None = None,
                 api_key: str | None = None):
        if not model or not str(model).strip():
            raise AgentError(
                "自定义供应商未填写模型名：请在「模型设置 → 自定义供应商」"
                "的模型名一栏填写目标端点的模型 ID。"
            )
        super().__init__(str(model).strip(),
                         default_temperature=default_temperature,
                         default_max_tokens=default_max_tokens,
                         base_url=base_url, api_key=api_key)
        if not self.base_url:
            raise AgentError(
                "自定义供应商未配置 Base URL：请在设置页填写"
                "（或设置环境变量 CUSTOM_LLM_BASE_URL）。"
            )
