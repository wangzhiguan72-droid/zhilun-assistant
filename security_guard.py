"""
安全护栏（v1.7 · 规划 §四 P2 上线安全：应用层限流 + 防御）
==========================================================
把「上线前必须做」的应用层安全从 ROADMAP 落到代码。纯标准库、零依赖、可单测。

设计原则（改动前务必读）：
    1. **本地内测优先**：默认阈值宽松，绝不干扰正常单机使用；
       只有真上线（多用户）时才需要调紧 —— 阈值全部可用环境变量覆盖。
    2. **两类限流**：普通接口（便宜）与 LLM 接口（贵、烧钱）分开计数。
       LLM 接口单独更严，避免一个用户把 API 额度刷爆。
    3. **内存有界**：空闲 IP 必须被回收，否则 `dict` 无限增长是慢性泄漏
       （旧实现 `_REQUEST_LOG` 从不清理，是真实缺陷）。
    4. **不信任客户端头**：`X-Forwarded-For` 只在显式声明"我在反代后面"时
       才采信（`TRUST_PROXY=1`），否则任何人加个头就能伪造 IP 绕过限流。
    5. **失败放行**：限流器自身异常不应阻断服务（宁可漏限，不可挂站）。

环境变量（全部可选）：
    RATE_LIMIT_PER_MIN       普通接口每分钟上限（默认 60）
    RATE_LIMIT_LLM_PER_MIN   LLM 接口每分钟上限（默认 8）
    TRUST_PROXY              设为 1 才采信 X-Forwarded-For
"""
from __future__ import annotations

import os
import threading
import time

# ---------------------------------------------------------------------------
# 阈值（环境变量可覆盖；本地内测宽松，上线按需收紧）
# ---------------------------------------------------------------------------
_DEFAULT_PER_MIN = 60
_DEFAULT_LLM_PER_MIN = 8

# LLM 接口前缀：这些会真花钱 / 真调外部 API，单独且更严地限流
LLM_PATH_PREFIXES: tuple[str, ...] = (
    "/api/check_paper",
    "/api/copilot/",
    "/api/audit_chat",
    "/api/audit_image",
)

# 完全不限流的路径（静态资源 / 健康检查 / 首页）
_EXEMPT_EXACT = ("/", "/health", "/favicon.ico")

# 空闲 IP 多久没动静就回收（秒）。窗口是 60s，留足余量。
_IDLE_TTL = 300.0
# 最多跟踪多少个 IP（防分布式刷；超出时按最旧淘汰）
_MAX_TRACKED_IPS = 4096


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


def disabled() -> bool:
    """是否整体关闭限流。

    ⚠️ 仅供**测试 / CI / 本地单机内测**使用：回归测试会在一个进程里连续打
    几十次 LLM 接口（如 multi_paper_test 逐篇扫模板论文），若受限流必然误报 429。
    生产环境**绝不能**设 `RATE_LIMIT_DISABLE=1`。
    """
    return (os.environ.get("RATE_LIMIT_DISABLE") or "").strip() in (
        "1", "true", "True", "yes")


def per_min() -> int:
    return _env_int("RATE_LIMIT_PER_MIN", _DEFAULT_PER_MIN)


def llm_per_min() -> int:
    return _env_int("RATE_LIMIT_LLM_PER_MIN", _DEFAULT_LLM_PER_MIN)


def trust_proxy() -> bool:
    return (os.environ.get("TRUST_PROXY") or "").strip() in ("1", "true", "True", "yes")


class RateLimiter:
    """线程安全的滑动窗口限流器（内存有界）。

    每个 IP 一条时间戳列表；查询时顺带清理过期项并回收空闲 IP。
    """

    def __init__(self, window: float = 60.0, idle_ttl: float = _IDLE_TTL,
                 max_ips: int = _MAX_TRACKED_IPS) -> None:
        self.window = float(window)
        self.idle_ttl = float(idle_ttl)
        self.max_ips = int(max_ips)
        self._lock = threading.Lock()
        self._log: dict[str, list[float]] = {}
        self._last_seen: dict[str, float] = {}
        self._rejects = 0        # 累计拒绝次数（可观测）

    # -- 内部：不加锁版本，调用方必须已持锁 ------------------------------
    def _sweep(self, now: float) -> None:
        """回收空闲 IP，保证「插入新 key 后」仍不超上限。

        ⚠️ 本函数在**插入之前**被调用，所以判定要用 `>= max_ips`：
        `_last_seen` 恰为 max_ips 时若不清理，插入后就变成 max_ips+1。
        """
        if len(self._last_seen) >= self.max_ips:
            # 先按「最后活跃时间」从旧到新淘汰，腾出至少 1 个名额
            order = sorted(self._last_seen.items(), key=lambda kv: kv[1])
            drop = len(order) - self.max_ips + 1
            for ip, _t in order[:max(drop, 0)]:
                self._last_seen.pop(ip, None)
                self._log.pop(ip, None)
            return
        # 未超上限：顺手清掉明显过期的，避免慢泄漏
        stale = [ip for ip, t in self._last_seen.items()
                 if now - t > self.idle_ttl]
        for ip in stale:
            self._last_seen.pop(ip, None)
            self._log.pop(ip, None)

    # -- 公共 -----------------------------------------------------------
    def check(self, key: str, limit: int) -> tuple[bool, int]:
        """记录一次请求。返回 (allowed, retry_after_seconds)。

        retry_after 在 allowed=False 时表示建议等待秒数。
        """
        now = time.time()
        with self._lock:
            hist = self._log.get(key)
            # 新 key 才需要先腾地方，保证「加入后」不超上限
            if hist is None:
                self._sweep(now)
                hist = []
                self._log[key] = hist
            # 滑出窗口
            cutoff = now - self.window
            hist[:] = [t for t in hist if t > cutoff]
            if len(hist) >= limit:
                self._rejects += 1
                self._last_seen[key] = now
                # 最早那次滑出窗口即可再请求
                retry = max(1, int(self.window - (now - hist[0])) + 1)
                return False, retry
            hist.append(now)
            self._last_seen[key] = now
            return True, 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "tracked_ips": len(self._log),
                "rejects_total": self._rejects,
                "window_seconds": self.window,
            }

    def reset(self) -> None:
        """清空（测试用）。"""
        with self._lock:
            self._log.clear()
            self._last_seen.clear()
            self._rejects = 0


# 模块级单例（app.py 复用；测试可 reset）
limiter = RateLimiter()


def client_ip(req, *, trusted: bool | None = None) -> str:
    """从请求对象取客户端 IP。

    ⚠️ 安全要点：只有当 `TRUST_PROXY=1`（显式声明在可信反代后面）时才采信
    `X-Forwarded-For`。否则任何人都能 `curl -H "X-Forwarded-For: 1.2.3.4"`
    伪造 IP，把限流变成摆设。
    """
    if trusted is None:
        trusted = trust_proxy()
    if trusted:
        xff = (req.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff:
            return xff
        real = (req.headers.get("X-Real-IP") or "").strip()
        if real:
            return real
    return req.remote_addr or "unknown"


def is_llm_path(path: str) -> bool:
    return any(path.startswith(p) for p in LLM_PATH_PREFIXES)


def is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    if path.startswith("/static"):
        return True
    return False


def is_preflight(method: str) -> bool:
    """CORS 预检请求（OPTIONS）不计入限流。

    为什么必须豁免：浏览器在**每个**跨域真实请求之前都要先发一次预检。
    预检本身不消耗业务资源（没有计算、没有 LLM 调用），却会挤占配额——
    结果是用户才点几次按钮就被自己的预检耗光额度，然后收到 429，
    表现为「跨域调用时好时坏」这种极难排查的症状。

    安全性：豁免预检不等于放开访问 —— 预检由 cross_platform 决定是否放行
    （来源不在白名单时直接 403），且真实请求（GET/POST）照常计数。
    """
    return (method or "").upper() == "OPTIONS"
