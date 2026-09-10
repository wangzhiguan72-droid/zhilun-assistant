"""
智谱 BigModel Agent（v0.5 重构）
================================
v0.4：继承 SiliconFlowAgent 换 base_url/env_var
v0.5：接入通用适配器（多 Key 轮转 + usage 采集），并按 GLM 代次
      处理 thinking 参数兼容：

    - GLM-4.x 系列：thinking.type 可传 enabled/disabled，轻量任务关掉
      省时省 token（v0.4 踩坑：开思考时小 max_tokens 会被思考耗尽）
    - GLM-5 系列（官方文档，2026-09 查证）：thinking.type 只接受
      enabled，传 disabled 直接报错 → 低开销场景应改用
      reasoning_effort="low"。适配器对 glm-5* 自动不传 thinking 开关。

**前缀缓存适配（v0.5，Tianshu 式推广）**：
    智谱 GLM 支持隐式上下文缓存——冻结前缀重复发送时自动命中，
    命中 token 按约 1/5 价计费（免费模型的缓存也免费），无需任何
    手动配置。usage 里 `prompt_tokens_details.cached_tokens` 可观测，
    已由通用适配器采集进 self.last_usage（见 zhipu_cache_test.py 实证）。
    agents/prompts.py 的冻结前缀契约对智谱同样必须遵守。

多 Key 轮转（v0.5）：
    ZHIPU_API_KEY 支持 `key1,key2` 逗号分隔。免费档限 1 并发、429
    是常态——先轮 Key 重试，全部 Key 限流才走 Router 容灾链。

配置：
    - 环境变量 ZHIPU_API_KEY（必填，https://bigmodel.cn/apikey/platform）
    - 环境变量 ZHIPU_BASE_URL（可选覆盖）
    - 接口地址 https://open.bigmodel.cn/api/paas/v4（OpenAI 兼容）
"""
from __future__ import annotations

from .base import AgentError
from .openai_compat import OpenAICompatAgent
from .siliconflow_agent import SiliconFlowAgent


# 智谱 BigModel 平台常用模型（官方 ID，跟硅基流动的 THUDM/* 命名不同！）
ZHIPU_MODELS = {
    # 免费档
    "glm-4.7-flash":   "glm-4.7-flash",   # ★★☆ 永久免费，200K，混合思考 30B/A3B
    "glm-4.6v-flash":  "glm-4.6v-flash",  # 免费多模态（图片+视频）
    # 付费档
    "glm-4.7":         "glm-4.7",         # ★★★ GLM-4.7 旗舰（同代最高质量）
    "glm-4.7-flashx":  "glm-4.7-flashx",  # 付费加速版（0.5/3 元每百万）
    "glm-4.5-air":     "glm-4.5-air",     # 轻量付费
    "glm-5":           "glm-5",           # GLM-5 系列（thinking 参数兼容性见上）
    "glm-5.3":         "glm-5.3",         # GLM-5.3 新品（1M 上下文）
}


class ZhipuAgent(SiliconFlowAgent):
    """通过智谱 BigModel 开放平台调用 GLM 系列模型。

    复用 SiliconFlowAgent（底层 OpenAICompatAgent）的全部调用逻辑，
    只换四样东西：
        1. provider    → "zhipu"（注册表：base_url / env_var / 显示名）
        2. thinking 兼容 → GLM-4.x 默认关思考；GLM-5 系不传 thinking
        3. 空 content 保护 → 报错让上层 fallback（混合思考踩坑遗留）
        4. usage 观测   → last_usage.cached_tokens 验证前缀缓存命中
    """

    provider = "zhipu"
    DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
    env_var = "ZHIPU_API_KEY"

    def __init__(self, model: str = "glm-4.7-flash", *,
                 enable_thinking: bool = False, **kwargs):
        # 智谱模型名解析（顺手把别名映射成官方 ID）
        resolved = ZHIPU_MODELS.get(model, model)
        super().__init__(model=resolved, **kwargs)
        self.enable_thinking = enable_thinking
        # GLM-5 系列：thinking.type 只接受 enabled（传 disabled 直接报错）
        # → 这个开关对 glm-5* 无效，交给 _extra_create_kwargs 跳过
        self._thinking_supported = not self.model_name.startswith("glm-5")

    def _extra_create_kwargs(self):
        """智谱专属：思考模式开关（按代次兼容）。"""
        if not self._thinking_supported:
            # glm-5*：不传 thinking；低开销需求将来可换 reasoning_effort="low"
            return {}
        if self.enable_thinking:
            return {"extra_body": {"thinking": {"type": "enabled"}}}
        return {"extra_body": {"thinking": {"type": "disabled"}}}

    def complete(self, prompt: str, **kwargs) -> str:
        """调用智谱模型。额外加一层保护：思考耗尽 token 时给出明确报错。"""
        result = super().complete(prompt, **kwargs)
        if not result.strip():
            # 开思考模式下小 max_tokens 会被思考吃光 → 报错让上层 fallback
            if self.enable_thinking:
                raise AgentError(
                    f"智谱 {self.model_name} 返回空内容：思考过程可能耗尽了 "
                    f"max_tokens（混合思考模型的思考也计 token）。"
                    f"请调大 max_tokens 或关闭 enable_thinking。"
                )
            raise AgentError(
                f"智谱 {self.model_name} 返回空内容，请检查 prompt 或稍后重试。"
            )
        return result

    def __repr__(self) -> str:
        return (f"<ZhipuAgent model={self.model_name} "
                f"thinking={'on' if self.enable_thinking else 'off'} "
                f"base_url={self.base_url}>")
