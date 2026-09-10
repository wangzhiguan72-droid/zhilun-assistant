"""
通用 OpenAI 兼容适配器（v0.5）
================================
参考 FreeLLMAPI 的 provider 模板模式：**平台差异全部数据化**，
加一个新平台 = 在 PROVIDER_REGISTRY 里写一行配置，不写新代码。

    PROVIDER_REGISTRY = {
        "sf":       ProviderConfig(base_url=..., env_var="SILICONFLOW_API_KEY", ...),
        "zhipu":    ProviderConfig(...),
        "deepseek": ProviderConfig(...),   # 官方直连，有 Key 即可用
    }

三层能力（全部内置，子类不用重复实现）：

    1. **多 Key 轮转**（FreeLLMAPI key rotation）：
       环境变量支持逗号分隔多个 Key（`ZHIPU_API_KEY=k1,k2`）。
       429 限流时先轮转下一个 Key 重试（key 级容灾），全部 Key 都 429
       才把异常抛给 Router 走模型级容灾链。
       智谱免费档限 1 并发，多 Key 轮转是对症药。

    2. **usage 采集**（Tianshu 前缀缓存工程）：
       每次调用后把 usage 记到 self.last_usage，兼容三种字段命名：
       - OpenAI 标准：usage.prompt_tokens_details.cached_tokens（智谱用这个）
       - 硅基流动：usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
         （SDK 放在 model_extra 里，两处都取）
       供 /api/llm_stats 观测 + 缓存命中率实证。

    3. **Retry-After 透传**：
       429 时解析响应头的 Retry-After（秒），挂到 AgentError.retry_after
       上抛出，Router 用它替代一刀切的 60s 冷却。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from .base import AgentError


# ---------------------------------------------------------------------------
# Provider 注册表：加平台只写这里（FreeLLMAPI 式模板）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderConfig:
    """一个 OpenAI 兼容平台的全部差异点。"""
    base_url: str                     # API 入口
    env_var: str                      # Key 环境变量（支持逗号分隔多 Key）
    display_name: str                 # 报错/日志里的名字
    extra_env: dict = field(default_factory=dict)  # 额外配置项（如 base_url 覆盖）


PROVIDER_REGISTRY: dict[str, ProviderConfig] = {
    "sf": ProviderConfig(
        base_url="https://api.siliconflow.cn/v1",
        env_var="SILICONFLOW_API_KEY",
        display_name="硅基流动",
        extra_env={"base_url": "SILICONFLOW_BASE_URL"},
    ),
    "zhipu": ProviderConfig(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        env_var="ZHIPU_API_KEY",
        display_name="智谱 BigModel",
        extra_env={"base_url": "ZHIPU_BASE_URL"},
    ),
    "deepseek": ProviderConfig(
        # DeepSeek 官方直连（OpenAI 兼容；上下文缓存自动生效、按命中计费）
        base_url="https://api.deepseek.com/v1",
        env_var="DEEPSEEK_API_KEY",
        display_name="DeepSeek 官方",
        extra_env={"base_url": "DEEPSEEK_BASE_URL"},
    ),
    "dashscope": ProviderConfig(
        # 阿里云百炼（OpenAI 兼容模式；Qwen 全系列，隐式缓存自动生效）
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        env_var="DASHSCOPE_API_KEY",
        display_name="阿里云百炼",
        extra_env={"base_url": "DASHSCOPE_BASE_URL"},
    ),
}


def parse_keys(raw: str) -> list[str]:
    """`k1,k2,k3` → ['k1', 'k2', 'k3']（去空白、去空项）。"""
    return [k.strip() for k in raw.split(",") if k.strip()]


class OpenAICompatAgent:
    """所有 OpenAI 兼容平台的通用调用逻辑（混入 BaseAgent 子类使用）。

    不直接继承 BaseAgent：SiliconFlowAgent 等具体类保持原有继承链
    （BaseAgent → SiliconFlowAgent → ZhipuAgent），避免破坏现有代码。
    """

    # --- 子类覆盖这两个属性即可接入 ---
    provider: str = ""            # PROVIDER_REGISTRY 的 key
    # 子类构造时设置：self._api_keys / self.base_url / self._require_env 兜底

    def _init_provider(self, base_url: str | None = None,
                       api_key: str | None = None) -> None:
        """按注册表初始化 provider 配置。在子类 __init__ 末尾调用。

        需要 self.env_var（Key 环境变量名）已设置。

        v0.5.1 BYOK：api_key 显式传入时优先用它（用户自带 Key），
        否则回退到环境变量。逗号分隔多 Key 轮转照常生效。
        """
        cfg = PROVIDER_REGISTRY.get(self.provider)
        env_url = ""
        if cfg:
            env_url = (os.environ.get(cfg.extra_env.get("base_url", ""), "")
                       or "").strip()
            if base_url:
                env_url = base_url  # 显式传入优先级最高
            elif not env_url:
                env_url = cfg.base_url
            self._display_name = cfg.display_name
        else:
            # 未注册的自定义平台（向后兼容：直接用传入的 base_url）
            env_url = (base_url or os.environ.get("CUSTOM_BASE_URL", "")
                       or "").rstrip("/")
            self._display_name = "自定义平台"

        # v0.5.1 BYOK：显式传入的 Key 优先，否则读环境变量
        if api_key and api_key.strip():
            raw_key = api_key.strip()
        else:
            raw_key = self._require_env(self.env_var)
        self._api_keys = parse_keys(raw_key)
        self._key_index = 0          # 当前使用的 Key 序号（429 时轮转）
        self._client: Any = None     # 懒加载，Key 轮转时重建
        self._api_key = self._api_keys[0]
        self.base_url = env_url.rstrip("/")
        # 上次调用的 usage 观测（None = 尚未调用/失败）
        self.last_usage: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Key 轮转 + 调用核心
    # ------------------------------------------------------------------

    def _build_client(self):
        """构建 openai 客户端（子类/测试可覆盖）。"""
        try:
            from openai import OpenAI
        except ImportError as e:
            raise AgentError(
                "缺少 openai SDK，请先 `pip install openai`（在 .venv 里）。"
            ) from e
        return OpenAI(api_key=self._api_key, base_url=self.base_url)

    @property
    def client(self):
        """懒加载 openai 客户端（Key 轮转后自动重建）。"""
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _rotate_key(self) -> bool:
        """轮转到下一个 Key。成功返回 True（还有没试过的 Key）。"""
        if len(self._api_keys) <= 1:
            return False
        self._key_index = (self._key_index + 1) % len(self._api_keys)
        # 不到一圈说明还有新 Key 可试
        return True

    def _rebuild_client(self) -> None:
        self._api_key = self._api_keys[self._key_index]
        self._client = self._build_client()

    def _extract_retry_after(self, exc: Exception) -> float | None:
        """从 429 异常里解析 Retry-After（秒）。解析不了返回 None。"""
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if not headers:
            return None
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(float(raw), 1.0)
        except (TypeError, ValueError):
            return None  # HTTP 日期格式等，暂不支持（MVP 用默认冷却）

    def _is_rate_limit(self, exc: Exception) -> bool:
        """是否 429 限流（openai SDK 的 RateLimitError 判定，含字符串兜底）。"""
        cls = type(exc)
        if any(c.__name__ == "RateLimitError" for c in cls.__mro__):
            return True
        return "429" in str(exc)[:500] and "rate" in str(exc).lower()[:800]

    def _call_with_rotation(self, make_request):
        """带 Key 轮转的请求执行器。

        make_request: 无参函数，返回 openai 响应对象。
        429 → 轮 Key 重试（每个 Key 一次机会）；全部 Key 都 429 → 抛带
        retry_after 的 AgentError。其他异常直接抛 AgentError。
        """
        last_exc: Exception | None = None
        tried = set()
        while len(tried) < len(self._api_keys):
            tried.add(self._key_index)
            try:
                return make_request()
            except Exception as e:
                if not self._is_rate_limit(e):
                    raise AgentError(
                        f"{self._display_name} 调用失败：{e}") from e
                last_exc = e
                if len(tried) >= len(self._api_keys):
                    break  # 所有 Key 都 429 了
                self._rotate_key()
                self._rebuild_client()
        # 全部 Key 429：抛给 Router 走模型级容灾链，带上 Retry-After
        err = AgentError(
            f"{self._display_name} {self.model_name} 429 限流"
            f"（已轮转 {len(self._api_keys)} 个 Key 仍失败）：{last_exc}")
        err.retry_after = self._extract_retry_after(last_exc)  # type: ignore[attr-defined]
        raise err

    # ------------------------------------------------------------------
    # usage 采集（前缀缓存观测）
    # ------------------------------------------------------------------

    def _extract_usage(self, resp) -> dict[str, Any] | None:
        """从响应里提取 usage，兼容智谱/硅基流动/OpenAI 三种字段命名。

        返回 dict：{prompt_tokens, completion_tokens, total_tokens,
                    cached_tokens}  （cached_tokens 取不到 = 0）
        """
        usage = getattr(resp, "usage", None)
        if usage is None:
            return None
        d: dict[str, Any] = {}
        d["prompt_tokens"] = getattr(usage, "prompt_tokens", 0) or 0
        d["completion_tokens"] = getattr(usage, "completion_tokens", 0) or 0
        d["total_tokens"] = getattr(usage, "total_tokens", 0) or 0

        cached = 0
        # ① OpenAI 标准字段：智谱 GLM 走这个（docs.bigmodel.cn 上下文缓存）
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        # ② 硅基流动：prompt_cache_hit_tokens（SDK 落在 model_extra）
        if not cached:
            extra = getattr(usage, "model_extra", None) or {}
            cached = extra.get("prompt_cache_hit_tokens", 0) or 0
        d["cached_tokens"] = cached or 0
        return d

    # ------------------------------------------------------------------
    # 子类用的完整调用入口
    # ------------------------------------------------------------------

    def _complete_openai_compat(self, prompt: str, *,
                                temperature: float | None,
                                max_tokens: int | None,
                                default_temperature: float,
                                default_max_tokens: int,
                                system: str | None,
                                extra_kwargs: dict[str, Any] | None = None,
                                empty_guard: bool = True) -> str:
        """通用 complete 实现。子类的 complete() 直接委托到这里。

        empty_guard：响应为空字符串时抛 AgentError（智谱混合思考模型的
        思考 token 耗尽场景），子类可以关掉自己定制报错文案。
        """
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def make_request():
            return self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=temperature if temperature is not None
                else default_temperature,
                max_tokens=max_tokens if max_tokens is not None
                else default_max_tokens,
                **(extra_kwargs or {}),
            )

        resp = self._call_with_rotation(make_request)
        self.last_usage = self._extract_usage(resp)

        try:
            content = resp.choices[0].message.content or ""
        except (AttributeError, IndexError, KeyError) as e:
            raise AgentError(
                f"{self._display_name} 返回结构异常：{e}") from e
        if empty_guard and not content.strip():
            raise AgentError(
                f"{self._display_name} {self.model_name} 返回空内容，"
                f"请检查 prompt、max_tokens 或稍后重试。")
        return content
