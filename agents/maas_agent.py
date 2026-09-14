"""
阿里云百炼 MaaS 专属端点 Agent（v0.5.4，通用适配器第 5 个 provider）
=====================================================================
接入一个**私有**的百炼 MaaS 工作空间端点（ws-*.maas.aliyuncs.com），
上面挂了 glm-5 / qwen-plus / Qwen3-VL 系列 / 专用小模型。

跟公开 dashscope 的区别：
    - base_url 是工作空间专属域名，不是公共 dashscope 入口
    - 模型 ID 直接用官方名（不像硅基流动那样带 Qwen/ 前缀）
    - Key 属私有资源，**只在服务端 .env 里**，前端 / 日志 / 报错一律不可见

Key 安全（本 provider 的核心约束）：
    - Key 从 MAAS_API_KEY 读，绝不写进代码
    - 构造时登记进 agents/secrets_guard，任何对外文案都会擦掉它
    - `__repr__` 不输出 base_url（私有端点地址也不进前端看板）

可用模型（2026-09 工作空间清单）：
    glm-5                        通用旗舰推理（GLM 最新）
    qwen-plus-2025-07-28         通义千问 Plus 长文本主力
    qwen3-vl-235b-a22b-thinking  视觉语言旗舰（MoE 235B）
    qwen3-vl-32b-thinking        视觉语言中档
    qwen3-vl-30b-a3b-thinking    视觉语言轻量（A3B 激活）
    qwen-math-turbo              数学专项
    qwen-mt-flash                翻译专项（快）
    deepseek-r1-distill-qwen-7b  R1 蒸馏小模型（7B，轻量推理）

什么时候用：
    - 作为各状态容灾链的**尾位兜底**：前面免费档都挂了才轮到它
    - 需要 glm-5 / qwen-plus 级质量时，也可手动指定

配置：
    - 环境变量 MAAS_API_KEY（必填，私有 Key）
    - 环境变量 MAAS_BASE_URL（可选，覆盖端点，便于换工作空间）
"""
from __future__ import annotations

from .base import AgentError
from .siliconflow_agent import SiliconFlowAgent


# 工作空间模型清单（友好名 → 官方 ID；官方 ID 原样透传）
MAAS_MODELS: dict[str, str] = {
    # === 通用对话 / 写作 ===
    "glm-5":                        "glm-5",
    "qwen-plus":                    "qwen-plus-2025-07-28",
    "qwen-plus-2025-07-28":         "qwen-plus-2025-07-28",
    # === 视觉语言（多模态，thinking 系）===
    "qwen3-vl-235b-a22b-thinking":  "qwen3-vl-235b-a22b-thinking",
    "qwen3-vl-32b-thinking":        "qwen3-vl-32b-thinking",
    "qwen3-vl-30b-a3b-thinking":    "qwen3-vl-30b-a3b-thinking",
    # === 专项模型 ===
    "qwen-math-turbo":              "qwen-math-turbo",
    "qwen-mt-flash":                "qwen-mt-flash",
    # === DeepSeek R1 蒸馏 ===
    "deepseek-r1-distill-qwen-7b":  "deepseek-r1-distill-qwen-7b",
}

# 默认模型：qwen-plus 是这台上最均衡的通用档
DEFAULT_MAAS_MODEL = "qwen-plus-2025-07-28"


class MaasAgent(SiliconFlowAgent):
    """百炼 MaaS 专属端点直连（复用通用适配器全部能力）。

    与 QwenAgent 同理，必须覆盖 `_resolve_model`：别名表与硅基流动
    COMMON_MODELS 存在同名不同义风险。
    """

    provider = "maas"
    DEFAULT_BASE_URL = ("https://ws-ubquyin5epzugojr.cn-beijing.maas.aliyuncs.com"
                        "/compatible-mode/v1")
    env_var = "MAAS_API_KEY"

    def _resolve_model(self, model: str) -> str:
        """本端点模型别名解析（官方 ID 直接透传）。"""
        return MAAS_MODELS.get(model, model)

    def __init__(self, model: str = DEFAULT_MAAS_MODEL, *,
                 default_max_tokens: int = 4096, **kwargs):
        # 默认 4096：本端点含多个思考模型（qwen3-vl-*-thinking / R1 蒸馏），
        # 思考 token 计入 completion，给小了 content 会是空的
        super().__init__(model=model, default_max_tokens=default_max_tokens,
                         **kwargs)

    def complete(self, prompt: str, **kwargs) -> str:
        """调用 MaaS 模型。思考模型耗尽 max_tokens 时 content 为空 → 明确
        报错让 Router 走容灾链下一个。"""
        result = super().complete(prompt, **kwargs)
        if not result.strip():
            raise AgentError(
                f"百炼 MaaS {self.model_name} 返回空内容：思考 token 可能"
                f"耗尽了 max_tokens（思考也计入 completion）。请调大 max_tokens。")
        return result

    def __repr__(self) -> str:
        # 私有工作空间地址不进前端看板 / 日志（Key 另由 secrets_guard 兜底）
        return f"<MaasAgent model={self.model_name} base_url=（私有端点已隐藏）>"
