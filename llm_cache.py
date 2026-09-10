"""
LLM 结果缓存层（v0.5）
======================
按 ROADMAP §5.3 的策略实现：

    缓存 key = hash(model + prompt_version + method + summary_canonical_json)

设计要点：
    1. **summary 而非 file_id 做 key**：同一数据换列重跑，统计量不同 → 不该命中；
       summary 的 canonical JSON 已涵盖 (数据内容, 方法, 列) 的全部影响，
       且比 file_id 更精确（重新上传相同数据也能命中，这是额外收益）
    2. **prompt_version 失效**：改 FROZEN_SYSTEM / prompt 措辞时手动 bump
       `llm_enhance.PROMPT_VERSION`，旧缓存自动全部失效（§5.3 表第 3 行）
    3. **model 进 key**：双平台容灾切换模型后不串缓存
    4. **force 绕过**：用户"重新生成"时强制真调（§5.3 表第 4 行）
    5. **内存 LRU**：上限 200 条（每条约 1KB，最多 ~200KB），超出淘汰最旧；
       进程重启即消失——和 _CHART_CACHE / _SESSION 一致，符合"不落盘"隐私原则
    6. **Key 挂了也能出结果**：缓存命中不需要 API Key——
       LLM 供应商故障时，旧结果仍然可用（容灾第三层）

内测用内存即可；上线后接 Redis 时只需替换本模块的 get/set（接口不变）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any

MAX_ENTRIES = 200          # LRU 上限（内存安全阀）
PROMPT_VERSION_LLM = "v1"  # 跟随 llm_enhance.PROMPT_VERSION（见 make_key 调用处）


class LLMCache:
    """线程安全的内存 LRU 缓存（OrderedDict 实现）。"""

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._store: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_entries
        # 命中统计（答辩素材："实测 N% 命中率"）
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    # key 构造
    # ------------------------------------------------------------------
    @staticmethod
    def make_key(model: str, prompt_version: str, method: str,
                 summary: dict[str, Any]) -> str:
        """canonical JSON：键排序 + 紧凑分隔，保证相同 summary 一定同 key。"""
        canonical = json.dumps(summary, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        payload = f"{model}|{prompt_version}|{method}|{canonical}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def peek(self, key: str) -> dict[str, Any] | None:
        """只查看不计数（LLM 失败兜底用，不污染 miss 统计）。"""
        with self._lock:
            return self._store.get(key)

    def get(self, key: str) -> dict[str, Any] | None:
        """命中返回 {"text": str, "cached_at": ts}；未命中返回 None。"""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            # LRU touch：移到末尾（最热）
            self._store.move_to_end(key)
            self.hits += 1
            return entry

    def put(self, key: str, text: str) -> None:
        with self._lock:
            self._store[key] = {"text": text, "cached_at": time.time()}
            self._store.move_to_end(key)
            # 超上限淘汰最旧（头部）
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """命中率统计（调试 / 答辩用）。"""
        total = self.hits + self.misses
        return {
            "entries": len(self._store),
            "max_entries": self._max,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else None,
        }


# 全局单例（进程内共享；和 app._CHART_CACHE 同生命周期）
llm_cache = LLMCache()
