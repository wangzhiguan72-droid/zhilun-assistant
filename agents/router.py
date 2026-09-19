"""
多 Provider Router（v0.4.1）
============================
按 ROADMAP §5.4 的状态机实现，支持**两个独立平台**：

    硅基流动（SiliconFlow）：
        - deepseek-ai/DeepSeek-V4-Pro     ★★★ 顶级质量（付费，缓存命中 ¥1/M）
        - deepseek-ai/DeepSeek-V4-Flash   ★★☆ 轻量快档
            ⚠️ 官方公告 2026-09-01 起分时段收费（闲时 2-8 点输入 ¥1.5/M，
               忙时 ¥3/M，输出 ¥4.5-9/M），"完全免费"已成历史
        - THUDM/GLM-Z1-9B-0414            ★☆☆ 免费小模型

    智谱 BigModel（bigmodel.cn，GLM 官方入口）：
        - glm-4.7-flash                   ★★☆ **永久免费**，200K 上下文，混合思考 30B/A3B
        - glm-4.7                         ★★★ GLM-4.7 旗舰（付费）

路由表（状态 → 容灾链，顺序 = 优先级）：

    recommend     → zhipu/glm-4.7-flash → sf/glm-z1-9b → dashscope/qwen3.7-flash
                    → maas/qwen-plus-2025-07-28 → mimo/mimo-v2.5
    analyze       → 不调 LLM（Python 算就够了）
    paper_check   → deepseek/deepseek-flash → sf/deepseek-v4-pro
                    → zhipu/glm-4.7-flash → maas/glm-5
                    → kimi/kimi-k2.6（v1.8 长上下文备胎）→ mimo/mimo-v2.5-pro
    write_text    → zhipu/glm-4.7-flash → sf/deepseek-v4-flash
                    → dashscope/qwen3.7-flash → maas/qwen-plus-2025-07-28
                    → mimo/mimo-v2.5（v1.8）

    （maas = 百炼 MaaS 私有工作空间端点，v0.5.4 新增，恒为尾位兜底；
      kimi / mimo = v1.8 新增的两家付费档平台，BYOK 用户自带 Key 即插即用）

档位（v0.5.3，区分免费用户 / 会员）：
    free（默认）：只走 FREE_MODELS 白名单里的零成本模型，付费候选自动跳过
                 （报错串里标注"免费档跳过"）。产品面向免费用户，故为出厂默认。
    pro（会员）  ：不限制，链上所有模型都可用（含 deepseek-* / v4-pro）。
    切换方式：Router(tier="pro") / router.set_tier("pro") / 环境变量 LLM_TIER=pro

懒加载：只有真正调到时才实例化 Agent。

v0.4.1 新增：**运行时容灾 + 429 冷却**
    - 旧版只在"缺 Key"（实例化失败）时走容灾链；运行时 429/5xx 直接抛错退模板
    - 现在：运行时调用失败 → 该 Agent 冷却（默认 60s，429 带 Retry-After
      则按平台说的冷却，封顶 600s）→ 自动试容灾链下一个
      （智谱免费档限 1 并发，429 是常态，这层必须有）
    - 冷却按 (provider, model) 粒度，过期自动恢复

v0.5 新增（FreeLLMAPI 式推广）：
    - deepseek 官方 provider 注册（不在默认链，有 Key 可手动加入容灾链）
    - 多 Key 轮转下沉到 Agent 层：429 先轮 Key，全 Key 限流才轮模型

前缀缓存（v0.4.1 实测验证，见 agents/prompts.py 文档字符串）：
    - 硅基流动透传 DeepSeek 缓存计费，冻结前缀重复调用全量命中（价差 10 倍）
    - 所有 paper_check 调用必须走 agents/prompts.py 的冻结前缀契约

省钱原则（用户核心诉求）：
    - 免费档（默认）只走零成本模型：智谱 glm-4.7-flash / 百炼 qwen3.7-flash / GLM-Z1-9B
    - 付费模型（deepseek-* / V4-Pro）只在会员档（tier=pro）启用
    - 容灾链：首选缺 Key 或运行时失败，自动试下一个，不中断服务
"""
from __future__ import annotations

import contextvars
import os
import time
from typing import Any

from .base import AgentError, BaseAgent
from .deepseek_agent import DeepSeekAgent
from .kimi_agent import KimiAgent
from .maas_agent import MaasAgent
from .mimo_agent import MimoAgent
from .qwen_agent import QwenAgent
from .siliconflow_agent import SiliconFlowAgent
from .zhipu_agent import ZhipuAgent


# provider 前缀 → Agent 类（七个平台全部登记）
# 不在 STATE_TO_MODEL 里的平台就是备用：懒加载，不进链就不会实例化，
# 没 Key 零影响
_PROVIDERS = {
    "sf":        SiliconFlowAgent,   # 硅基流动
    "zhipu":     ZhipuAgent,         # 智谱 BigModel（bigmodel.cn）
    "deepseek":  DeepSeekAgent,      # DeepSeek 官方（api.deepseek.com）
    "dashscope": QwenAgent,          # 阿里云百炼（dashscope.aliyuncs.com）
    "maas":      MaasAgent,          # 百炼 MaaS 专属工作空间（私有端点）
    "kimi":      KimiAgent,          # Kimi 开放平台（月之暗面，v1.8）
    "mimo":      MimoAgent,          # 小米 MiMo 开放平台（v1.8）
}

# 状态 → 容灾链 [(provider, 模型友好名, 默认温度), ...]
# 列表顺序 = 优先级；第一个失败（缺 Key / 运行时 429 等）自动试下一个
STATE_TO_MODEL: dict[str, list[tuple[str, str, float]]] = {
    "recommend": [
        ("zhipu",     "glm-4.7-flash", 0.5),   # ★★☆ 免费：智谱永久免费，200K，混合思考
        ("sf",        "glm-z1-9b",     0.5),   # ★☆☆ 免费：硅基流动免费档（智谱没 Key 时退回这）
        ("dashscope", "qwen3.7-flash", 0.5),   # ★★☆ 免费：百炼免费额度，缓存实测 92.5%
        ("maas",      "qwen-plus-2025-07-28", 0.5),  # v0.5.4 私有端点尾位兜底
        ("mimo",      "mimo-v2.5",     0.5),   # ★★☆ v1.8 BYOK 尾位：轻量快档
    ],
    "paper_check": [
        # 付费档（会员）优先：deepseek-flash 官方直连，缓存命中价差最大；
        # v4-pro 是付费档备胎。免费档（tier=free）会把这两个跳过，
        # 直接落到下面免费档做基础审计（质量降级但不花钱）。
        ("deepseek", "deepseek-flash", 0.3),
        ("sf",       "deepseek-v4-pro", 0.3),
        ("zhipu",    "glm-4.7-flash",  0.3),   # ★★☆ 免费兜底：审计质量够用的最低成本档
        ("maas",     "glm-5",          0.3),   # ★★★ v0.5.4 私有端点：GLM 旗舰审计兜底
        ("kimi",     "kimi-k2.6",      0.3),   # ★★★ v1.8 长上下文备胎：整篇论文放得下（BYOK）
        ("mimo",     "mimo-v2.5-pro", 0.3),   # ★★☆ v1.8 质量档备胎（BYOK）
    ],
    "write_text": [
        ("zhipu",     "glm-4.7-flash",    0.5),  # ★★☆ 免费：长文本生成主力
        ("sf",        "deepseek-v4-flash", 0.5), # ★★☆ 付费档备胎
        ("dashscope", "qwen3.7-flash",    0.5),  # ★★☆ 免费：v0.5.3 尾位兜底（缓存实测 92.5%）
        ("maas",      "qwen-plus-2025-07-28", 0.5),  # ★★★ v0.5.4 私有端点尾位
        ("mimo",      "mimo-v2.5",      0.5),  # ★★☆ v1.8 BYOK 尾位：轻量快档
    ],
    # v1.6 ②审计对话：用户对「一条比对」追问。
    # LLM 只负责**解释**（把已算好的统计量讲成人话），绝不参与计算。
    # 温度略高（0.6）让措辞自然，但输入只有单条 summary，幻觉空间极小。
    "audit_chat": [
        ("zhipu",     "glm-4.7-flash", 0.6),   # ★★☆ 免费：问答足够，200K 上下文
        ("sf",        "glm-z1-9b",     0.6),   # ★☆☆ 免费：备胎
        ("dashscope", "qwen3.7-flash", 0.6),   # ★★☆ 免费：尾位
        ("maas",      "qwen-plus-2025-07-28", 0.6),  # 私有端点兜底
        ("mimo",      "mimo-v2.5",     0.6),   # ★★☆ v1.8 BYOK 尾位：轻量问答
    ],
    # v1.8 Kimi / MiMo 接入原则：
    #   - 两家都是付费档（不进 FREE_MODELS），默认链里排在免费候选之后
    #     作为**尾部备胎**——免费用户零感知，BYOK 用户填了 Key 即插即用；
    #   - set_user_key() 过的平台自动排到链首（用户自己的 Key 优先），
    #     见 _ordered_chain()。
    #
    # v2.9 ④多模态图表核查：读图 + 比对论文结论句。
    # 与其它状态的关键差异：**必须用支持视觉输入的模型**，
    # 所以链上候选都带 V 标识；glm-4.6v-flash 是智谱免费多模态档（首选）。
    "audit_image": [
        ("zhipu",     "glm-4.6v-flash", 0.2),   # ★★☆ 免费：智谱多模态（图片+视频）
        ("maas",      "qwen3-vl-235b-a22b-thinking", 0.2),  # ★★★ 私有端点视觉旗舰兜底
        ("maas",      "qwen3-vl-32b-thinking", 0.2),  # ★★☆ 私有点端视觉中档
        ("mimo",      "mimo-v2.5",      0.2),   # ★★☆ v1.8 BYOK 尾位
    ],
}

# v2.9：多模态状态要求模型**必须支持视觉输入**。容灾链上若混入纯文本模型，
# 图片会被静默忽略、模型凭空描述（比报错更危险——用户以为图被看过了）。
# 这里显式声明"哪些模型能看图"，_get_agent 对本状态做一次能力校验。
VISION_MODELS: frozenset[str] = frozenset({
    "glm-4.6v-flash",
    "qwen3-vl-235b-a22b-thinking",
    "qwen3-vl-32b-thinking",
    "qwen3-vl-30b-a3b-thinking",
    "qwen-vl-plus",
    "qwen-vl-max",
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "claude-3-5-sonnet", "claude-3-opus",
    "mimo-v2.5", "mimo-vl",
})

# 需要视觉能力的路由状态（目前只有图表核查）
VISION_REQUIRED_STATES: frozenset[str] = frozenset({"audit_image"})

# ── 模型档位（v0.5.3）────────────────────────────────────────────────────────
# 产品定位：免费用户只享受**零成本模型**，付费模型留给会员。
# 这里用「免费白名单」表达：tier=free 时只允许白名单内的 (provider, model)；
# tier=pro（会员）不限制。用独立集合而不是给链路加第 4 个字段，
# 是为了不破坏 STATE_TO_MODEL 的 3 元组契约（多处测试依赖）。
FREE_MODELS: frozenset[tuple[str, str]] = frozenset({
    ("zhipu",     "glm-4.7-flash"),   # 智谱永久免费
    ("zhipu",     "glm-4.6v-flash"),  # v2.9 智谱免费多模态（④图表核查首选）
    ("sf",        "glm-z1-9b"),       # 硅基流动免费档
    ("dashscope", "qwen3.7-flash"),   # 百炼免费额度
    # v0.5.4：自有 MaaS 端点（成本由项目方承担，对用户是零成本）→ 免费档放行
    ("maas",      "qwen-plus-2025-07-28"),
    ("maas",      "glm-5"),
    ("maas",      "qwen3-vl-235b-a22b-thinking"),  # v2.9 私有端点视觉兜底
    ("maas",      "qwen3-vl-32b-thinking"),
})

TIER_FREE = "free"
TIER_PRO = "pro"
# 出厂默认：面向免费用户，只走零成本模型。会员请求显式设 tier=pro
# （或环境变量 LLM_TIER=pro）。
DEFAULT_TIER = TIER_FREE

# 运行时失败后的默认冷却时长（秒）：429 限流通常几十秒内恢复。
# v0.5：若平台 429 响应头带 Retry-After，优先用它（上限 COOLDOWN_MAX 秒）
COOLDOWN_SECONDS = 60.0
COOLDOWN_MAX = 600.0


class Router:
    """多 Provider 状态机 Router（含运行时容灾 + 429 冷却）。

    v0.5.1 新增：BYOK（用户自带 Key）模式
        - 调用 set_user_key(provider, key) 后,该 provider 的所有调用都用用户 Key
        - 用户 Key 只活在当前请求上下文（contextvars），请求结束即销毁
        - 带用户 Key 的 Agent 不进共享缓存（用完即弃，Key 零残留）
    """

    @property
    def _user_keys(self) -> dict[str, str]:
        """当前请求上下文里的用户 Key（只读视图）。

        对外保持 dict 语义（byok_test / kimi_mimo_test 直接断言它），
        但底层是 ContextVar —— 跨请求、跨线程互不可见。
        """
        d = self._user_keys_var.get()
        return d if d is not None else {}

    def __init__(self, tier: str | None = None):
        # v0.5.3 档位：free（默认，免费用户，只走零成本模型）| pro（会员，不限）
        # 可通过构造参数、环境变量 LLM_TIER 或 set_tier() 指定
        self.tier: str = self._normalize_tier(
            tier if tier is not None else os.environ.get("LLM_TIER", DEFAULT_TIER)
        )
        # 已实例化的 Agent，key = (provider, model)
        # 注意: BYOK 模式下不缓存,避免用户 Key 泄露
        self._agents: dict[tuple[str, str], BaseAgent] = {}
        # 每个状态实际用上的 Agent（首次成功后记住，避免每次都重试）
        self._resolved: dict[str, tuple[str, str]] = {}
        # 冷却表：key = (provider, model)，value = 冷却截止时间戳
        self._cooldown_until: dict[tuple[str, str], float] = {}
        # 用户提供的 Key（provider → key），BYOK 模式。
        # ⚠️ 安全关键（v2.25 修复）：必须用 contextvars 按「请求上下文」隔离，
        # 不能用普通实例 dict —— 本 Router 是 get_router() 的进程级单例，
        # 普通 dict 会让用户 A 的 Key 在请求结束后残留，被用户 B 的请求
        # 继续使用（串号 + 盗用 A 的额度；若 A 的是付费 Key 就是真金白银）。
        # ContextVar 让每个请求线程各拿一份，请求结束线程销毁，Key 零残留。
        # 实例属性而非模块级：测试里 new Router() 能得到干净隔离的上下文。
        self._user_keys_var: contextvars.ContextVar[dict[str, str] | None] = \
            contextvars.ContextVar(f"zhilun_user_keys_{id(self)}", default=None)
        # 上一次成功调用的 Agent（v0.5.2：供 llm_enhance 拿 usage 做前缀缓存观测）
        self._last_agent: BaseAgent | None = None
        # 上一次调用的 usage（同上；None = 尚未调用或调用失败）
        self._last_usage: dict[str, Any] | None = None
        # 上一次成功调用的**状态名**（v0.5.2：/api/llm_stats 按状态展示前缀缓存命中率）
        self._last_state: str | None = None
        # 每个状态**上一次真正成功**用过的模型名（v1.1.1：缓存 key 稳定性）
        # 容灾切换会让"当前候选"漂移，用这个记住"实际服务过该状态的是谁"，
        # 让缓存 key 在模型切换前后保持稳定，避免命中率被容灾悄悄打掉。
        self._last_ok_model: dict[str, str] = {}

    # ── 档位（v0.5.3）─────────────────────────────────────────────
    @staticmethod
    def _normalize_tier(tier: str | None) -> str:
        """把任意输入规范成 TIER_FREE / TIER_PRO，未识别一律按 free。"""
        t = (tier or DEFAULT_TIER).strip().lower()
        return TIER_PRO if t == TIER_PRO else TIER_FREE

    def set_tier(self, tier: str) -> None:
        """切换档位（free / pro）。切换后清空已解析缓存，
        避免 free 档继续复用 pro 档实例化过的付费 Agent。"""
        self.tier = self._normalize_tier(tier)
        self._resolved.clear()

    def _allowed(self, provider: str, model: str) -> bool:
        """当前档位是否放行该模型。

        - pro 档不限；
        - free 档只放行免费白名单；
        - v1.8 BYOK 例外：用户给某平台填了自己的 Key（set_user_key），
          该平台的模型即插即用（费用由用户自付，与"会员"无关）。
        """
        if self.tier == TIER_PRO:
            return True
        return (provider, model) in FREE_MODELS or provider in self._user_keys

    @staticmethod
    def _vision_ok(state: str, model: str) -> bool:
        """该状态用这个模型是否安全（v2.9）。

        只有视觉类状态（audit_image）需要校验：链上不是视觉模型就跳过，
        避免"图片被忽略、模型凭空描述图表"——这比直接报错更危险，
        因为用户会以为模型真的看过图了。
        """
        if state not in VISION_REQUIRED_STATES:
            return True
        return model in VISION_MODELS

    def set_user_key(self, provider: str, key: str) -> None:
        """设置用户自带的 API Key（BYOK 模式）。

        只在**当前请求上下文**生效：该 provider 的后续调用都用这个 Key，
        忽略环境变量；请求结束（线程销毁）后 Key 自动消失，不会残留到
        下一个请求、更不会串到其他用户。key 为空字符串时清除（回退环境变量）。

        不再清空 _agents/_resolved：共享缓存里只会有「环境变量 Key」的
        Agent（带用户 Key 的 Agent 本就不进缓存，见 _get_agent/complete），
        清它只会白白牺牲全站命中率。
        """
        d = dict(self._user_keys_var.get() or {})
        if not key.strip():
            d.pop(provider, None)
        else:
            d[provider] = key.strip()
        self._user_keys_var.set(d)

    def _make_agent(self, provider: str, model: str, temp: float) -> BaseAgent:
        """按 provider 实例化对应 Agent。缺 Key 时抛 AgentError。

        v0.5.1 BYOK：用户自带 Key 直接传给 Agent 构造（不走环境变量，
        避免并发竞态和环境污染）；没设用户 Key 时回退环境变量。
        """
        cls = _PROVIDERS.get(provider)
        if cls is None:
            raise AgentError(f"未知 provider：{provider}。已知：{list(_PROVIDERS.keys())}")

        user_key = self._user_keys.get(provider)
        return cls(model=model, default_temperature=temp, api_key=user_key)

    # ── 容灾链排序（v1.8 BYOK）────────────────────────────────────
    def _ordered_chain(self, state: str) -> list[tuple[str, str, float]]:
        """该状态的候选链：用户自带 Key 的平台排最前（其余保持原序）。

        规则很简单：用户填了 Key = "我就要用这一家"，优先级高于
        服务端内置 Key（平台额度）。没填 Key 的用户完全无感知。
        """
        chain = STATE_TO_MODEL[state]
        return sorted(chain, key=lambda c: c[0] not in self._user_keys)

    def _cooling(self, key: tuple[str, str]) -> bool:
        """该 Agent 是否还在冷却期（近期运行时失败过）。"""
        return self._cooldown_until.get(key, 0.0) > time.time()

    def _get_agent(self, state: str) -> BaseAgent:
        """懒加载 + 容灾链：首选失败（缺 Key）自动试下一个。

        仅处理"实例化阶段"的失败（缺 Key 等）；运行时调用失败由
        complete() 捕获并冷却后重走容灾链。
        """
        # 已解决过且不在冷却期的状态直接复用。
        # ⚠️ BYOK 防护：捷径只在「缓存里真有这个 Agent 且当前请求没有给它
        # 换用户 Key」时才可走 —— 缓存里只会是环境变量 Key 的 Agent，
        # 当前请求若带了该 provider 的用户 Key，必须绕开捷径走下面的
        # 全新实例化，否则用户 Key 会被静默忽略（旧行为靠清缓存实现）。
        if state in self._resolved:
            key = self._resolved[state]
            cached = self._agents.get(key)
            if (cached is not None and not self._cooling(key)
                    and key[0] not in self._user_keys):
                return cached

        if state == "analyze":
            raise AgentError(
                "analyze 状态不调 LLM，应该走 Python 计算层，"
                "而不是 router.complete。请检查调用逻辑。"
            )
        if state not in STATE_TO_MODEL:
            raise AgentError(f"未知状态：{state}。已知：{list(STATE_TO_MODEL.keys())}")

        errors: list[str] = []
        for provider, model, temp in self._ordered_chain(state):
            key = (provider, model)
            if not self._vision_ok(state, model):
                errors.append(f"{provider}/{model}: 无视觉能力（{state} 需要读图），跳过")
                continue
            if not self._allowed(provider, model):
                errors.append(f"{provider}/{model}: 免费档跳过（付费模型，需会员）")
                continue
            if self._cooling(key):
                errors.append(f"{provider}/{model}: 冷却中（近期调用失败，"
                              f"{int(self._cooldown_until[key] - time.time())}s 后恢复）")
                continue
            if key in self._agents and provider not in self._user_keys:
                self._resolved[state] = key
                return self._agents[key]
            try:
                agent = self._make_agent(provider, model, temp)
            except AgentError as e:
                errors.append(f"{provider}/{model}: {e}")
                continue
            if provider in self._user_keys:
                # BYOK：带用户 Key 的 Agent **不进共享缓存、不污染 _resolved**
                # —— 缓存它等于把用户 Key 留在进程里给后续所有请求用。
                return agent
            self._agents[key] = agent
            self._resolved[state] = key
            return agent

        raise AgentError(
            f"状态 {state} 的所有候选模型都不可用（缺 Key？）。\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    def complete(self, state: str, prompt: str, *,
                 system: str | None = None,
                 temperature: float | None = None,
                 max_tokens: int | None = None) -> str:
        """按状态路由到对应 Agent 完成 LLM 调用。

        运行时失败（429 / 5xx / 超时）→ 冷却该 Agent 60 秒 → 自动重试
        容灾链下一个。全链失败才抛 AgentError（让上层 fallback 到纯模板）。
        """
        if state == "analyze" or state not in STATE_TO_MODEL:
            # 复用 _get_agent 的状态校验与报错文案
            agent = self._get_agent(state)
        else:
            agent = None
            errors: list[str] = []
            for provider, model, temp in self._ordered_chain(state):
                key = (provider, model)
                if not self._vision_ok(state, model):
                    errors.append(f"{provider}/{model}: 无视觉能力（{state} 需要读图），跳过")
                    continue
                if not self._allowed(provider, model):
                    errors.append(f"{provider}/{model}: 免费档跳过（付费模型，需会员）")
                    continue
                if self._cooling(key):
                    errors.append(f"{provider}/{model}: 冷却中（近期调用失败）")
                    continue
                # 实例化（缺 Key 等失败 → 记录，试下一个）
                # BYOK：带用户 Key 的 Agent 用完即弃，不进共享缓存
                # （缓存它 = 把用户 Key 留在进程里给别人的请求用）
                try:
                    if provider in self._user_keys:
                        agent = self._make_agent(provider, model, temp)
                    else:
                        if key not in self._agents:
                            self._agents[key] = self._make_agent(provider, model, temp)
                        agent = self._agents[key]
                except AgentError as e:
                    errors.append(f"{provider}/{model}: {e}")
                    continue
                # 运行时调用（429 等 → 冷却，试下一个）
                try:
                    result = agent.complete(prompt, system=system,
                                            temperature=temperature,
                                            max_tokens=max_tokens)
                    self._resolved[state] = key
                    # v0.5.2：记下这次调用的 Agent + usage（前缀缓存观测）
                    self._last_agent = agent
                    self._last_usage = getattr(agent, "last_usage", None)
                    self._last_state = state
                    self._remember_model(state, agent)  # v1.1.1 缓存 key 稳定性
                    return result
                except AgentError as e:
                    # v0.5：429 带 Retry-After 时按平台说的冷却，封顶 COOLDOWN_MAX
                    retry_after = getattr(e, "retry_after", None)
                    cooldown = min(retry_after, COOLDOWN_MAX) if retry_after else COOLDOWN_SECONDS
                    self._cooldown_until[key] = time.time() + cooldown
                    if self._resolved.get(state) == key:
                        del self._resolved[state]  # 别让下次直接复用刚失败的
                    wait_txt = (f"{cooldown:.0f}s（平台 Retry-After）" if retry_after
                                else f"{int(COOLDOWN_SECONDS)}s")
                    errors.append(f"{provider}/{model}: 运行时失败（已冷却 "
                                  f"{wait_txt}）：{e}")
                    continue

            raise AgentError(
                f"状态 {state} 的容灾链全部失败。\n" +
                "\n".join(f"  - {e}" for e in errors)
            )
        result = agent.complete(prompt, system=system,
                                temperature=temperature, max_tokens=max_tokens)
        # v0.5.2：记下这次调用的 Agent + usage（前缀缓存观测）
        self._last_agent = agent
        self._last_usage = getattr(agent, "last_usage", None)
        self._last_state = state
        self._remember_model(state, agent)  # v1.1.1 缓存 key 稳定性
        return result

    # ── 缓存 key 稳定性（v1.1.1）──────────────────────────────────
    def _remember_model(self, state: str, agent: BaseAgent) -> None:
        """记下该状态实际成功服务过的模型名。"""
        name = getattr(agent, "model_name", None)
        if name:
            self._last_ok_model[state] = name

    def cache_model_name(self, state: str) -> str:
        """给缓存 key 用的**稳定**模型名。

        优先返回该状态上次真正成功用过的模型（跨容灾切换保持一致）；
        没有历史时回退到"当前首选候选"（首次调用前的探测）。
        拿不到就返回 ""（key 仍可生成，只是不区分模型）。
        """
        name = self._last_ok_model.get(state)
        if name:
            return name
        try:
            agent = self._get_agent(state)
            return getattr(agent, "model_name", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def health(self) -> dict[str, Any]:
        """列出每个状态实际路由到的 Agent + 冷却状态（调试用）。"""
        result: dict[str, Any] = {}
        for state in STATE_TO_MODEL:
            key = self._resolved.get(state)
            info = repr(self._agents[key]) if key else "（尚未实例化）"
            # 冷却中的候选一并展示（排障时一眼看到"为什么没用它"）
            cooling = [
                f"{p}/{m}" for p, m, _t in STATE_TO_MODEL[state]
                if self._cooling((p, m))
            ]
            if cooling:
                info += f" ｜冷却中: {', '.join(cooling)}" + \
                    f"（{int(min(self._cooldown_until[(p, m)] for p, m, _t in STATE_TO_MODEL[state] if self._cooling((p, m))) - time.time())}s 后恢复）"
            result[state] = info
        return result


# 单例（项目内复用，避免重复加载客户端）
_router: Router | None = None


def get_router() -> Router:
    global _router
    if _router is None:
        _router = Router()
    return _router