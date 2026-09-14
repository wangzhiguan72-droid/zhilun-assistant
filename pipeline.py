"""论文副驾驶 · 流水线编排层（Paper Copilot Pipeline）

本模块把「智论助手」从「两个孤立 Tab 的工具」升级为「一条可追踪的论文流水线」。
设计参照 Scientify（端到端科研系统）的编排器模式，但领域做了替换：

    Scientify（科研）            论文副驾驶（论文 / 统计）
    ------------------          ------------------------
    （v2.0 新增 · 第 0 关） →     数据体检（门控：有硬矛盾就止步）
    research-collect     →      资料调研（论文 + 数据）
    research-survey      →      文献与方法综述
    research-plan        →      研究设计（变量 / 方法 / 假设）
    research-implement   →      数据分析（统计计算）
    research-review      →      论文核查（声称 vs 实际）
    research-experiment  →      论文撰写（证据约束初稿）

## 核心设计原则（与 Scientify 一致）

1. **编排器不做研究**：本模块只负责「检查产出 → 派发阶段 → 验证产出 → 推进」，
   绝不自己算统计量、绝不自己写论文正文。真正的计算在 `app.py` 的 `run_*`，
   文本生成在 `paper_writer.py`。
2. **严格顺序执行**：每次只推进一个阶段；前一阶段的产出文件未通过验证，
   绝不进入下一阶段。
3. **产出文件即契约**：每个阶段有明确的产出文件与验证规则（见 `_PHASES`）。
4. **上下文桥接**：派发下一阶段时，只摘要传递上一阶段的 2-5 行关键信息，
   避免把整篇论文塞进下游（省 token + 防污染）。
5. **可恢复**：中断后重新运行，编排器自动跳过已完成阶段，从第一个缺失产出继续。

## 用法

    from pipeline import Pipeline, PHASES

    p = Pipeline(workdir="D:/project")
    state = p.status()          # 查看各阶段状态
    nxt = p.next_phase()        # 下一个待执行阶段
    tpl = p.dispatch_context(nxt, artifacts)   # 生成上下文桥接串
    p.mark_done(nxt, outputs)   # 标记完成（会校验产出文件）

本模块是**纯逻辑，不依赖 Flask / 网络**，方便独立测试与接力。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


# ---------------------------------------------------------------------------
# 阶段定义（流水线契约）
# ---------------------------------------------------------------------------

@dataclass
class Phase:
    """一个流水线阶段的完整契约。"""

    key: str                     # 阶段标识（英文，稳定）
    name: str                    # 中文名（展示用）
    emoji: str
    goal: str                    # 这一阶段要达成什么
    outputs: list[str]           # 必须产出的文件（相对 workdir）
    validator: str               # 验证规则的文字说明
    requires: list[str] = field(default_factory=list)   # 前置阶段 key
    optional: bool = False       # 是否可跳过
    gate: bool = False           # 是否门控阶段（未通过 → 后续阶段一律 blocked）
    context_hint: str = ""       # 上下文桥接时，从哪里摘要给下游

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ⛔ 顺序即契约：列表顺序 = 执行顺序。禁止并行、禁止跳阶（optional 除外）。
PHASES: list[Phase] = [
    Phase(
        key="datacheck",
        name="数据体检",
        emoji="🩺",
        goal="先确认数据本身没有硬伤（前后矛盾 / 不可能取值），再往下做研究。",
        outputs=["datacheck_res.md"],
        validator="datacheck_res.md 存在，且无「高」级问题；有硬矛盾时流水线止步。",
        gate=True,
        context_hint="高优先级问题清单（若有）、数据规模。",
    ),
    Phase(
        key="collect",
        name="资料调研",
        emoji="🔍",
        goal="收集与选题直接相关的论文与数据，建立资料底盘。",
        outputs=["papers/_index.md"],
        validator="papers/_index.md 存在，且登记了 ≥3 条资料（论文或数据）。",
        requires=["datacheck"],
        context_hint="资料数量、覆盖方向、最相关的 3 条。",
    ),
    Phase(
        key="survey",
        name="文献与方法综述",
        emoji="📖",
        goal="深度阅读资料，提炼可借鉴的方法、变量与结论，形成方法对比。",
        outputs=["survey_res.md"],
        validator="survey_res.md 存在，且含「核心方法对比」表格。",
        requires=["collect"],
        context_hint="核心方法、可用变量、尚存空白。",
    ),
    Phase(
        key="plan",
        name="研究设计",
        emoji="📋",
        goal="确定研究假设、变量映射与拟用统计方法，形成可执行的设计。",
        outputs=["plan_res.md"],
        validator="plan_res.md 存在，且含四个 section：假设 / 变量 / 方法 / 预期产出。",
        requires=["survey"],
        context_hint="核心假设、因变量与自变量映射、拟用方法。",
    ),
    Phase(
        key="analyze",
        name="数据分析",
        emoji="📊",
        goal="用真实数据执行统计计算，产出可引用的结果与图表。",
        outputs=["analysis_res.md"],
        validator="analysis_res.md 存在，且含至少一行 [RESULT] 与统计量数值。",
        requires=["plan"],
        context_hint="执行了哪些方法、关键统计量、显著性结论。",
    ),
    Phase(
        key="review",
        name="论文核查",
        emoji="🔎",
        goal="把初稿的统计声称与真实数据对账，给出改进建议。",
        outputs=["review_res.md"],
        validator="review_res.md 存在，且含逐条核对结论（match / mismatch）。",
        requires=["analyze"],
        context_hint="不一致条目、改进建议、需补的方法。",
    ),
    Phase(
        key="write",
        name="论文撰写",
        emoji="📄",
        goal="基于证据台账，产出不越界的论文初稿（LaTeX 或中文 docx）。",
        outputs=["paper/claim_inventory.md", "paper/draft.md"],
        validator="claim_inventory.md 与 draft.md 均存在，且每条结果性 claim 都有来源文件。",
        requires=["review"],
        context_hint="最强结论、可用图表、证据边界。",
    ),
]


PHASE_BY_KEY: dict[str, Phase] = {p.key: p for p in PHASES}


# ---------------------------------------------------------------------------
# 流水线状态
# ---------------------------------------------------------------------------

@dataclass
class PhaseState:
    key: str
    status: str = "pending"      # pending / in_progress / done / blocked
    outputs_found: list[str] = field(default_factory=list)
    outputs_missing: list[str] = field(default_factory=list)
    updated_at: float = 0.0
    note: str = ""


class Pipeline:
    """论文副驾驶流水线编排器。

    不持有任何统计逻辑或写作逻辑 —— 只做「检查 / 派发 / 验证 / 推进」。
    """

    STATE_FILE = ".pipeline_state.json"

    #: 门控阶段（第 0 关）：数据体检。产出文件齐了**不算过**，必须 gate.passed
    GATE_PHASE_KEY = "datacheck"

    def __init__(self, workdir: str):
        self.workdir = os.path.abspath(workdir)

    # -- 状态持久化 ---------------------------------------------------------

    def _state_path(self) -> str:
        return os.path.join(self.workdir, self.STATE_FILE)

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            os.makedirs(self.workdir, exist_ok=True)
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # -- 产出文件检查 -------------------------------------------------------

    def check_outputs(self, phase: Phase) -> tuple[list[str], list[str]]:
        """返回 (存在的产出, 缺失的产出)。"""
        found, missing = [], []
        for rel in phase.outputs:
            full = os.path.join(self.workdir, rel.replace("/", os.sep))
            (found if os.path.exists(full) else missing).append(rel)
        return found, missing

    def is_complete(self, phase: Phase) -> bool:
        """所有必需产出文件都在 → 视为完成。"""
        _, missing = self.check_outputs(phase)
        return not missing

    def _gate_failed(self, phase: Phase, prev: dict[str, Any]) -> bool:
        """门控阶段是否未通过。

        语义：**只有「跑过体检且没通过」才挡人**。
        - 跑过、有硬矛盾 → `passed=False` → 挡（这才是门控的意义）。
        - 从没跑过（无 gate 记录）→ 不额外挡：产出文件缺失本身已经会挡，
          不该再叠一层"用户无法自行脱困"的锁。
        """
        if not phase.gate:
            return False
        gate = prev.get("gate")
        if not isinstance(gate, dict):
            return False
        return gate.get("passed") is not True

    # -- 状态视图 -----------------------------------------------------------

    def status(self) -> list[PhaseState]:
        """扫描全流水线，返回每个阶段的实时状态。

        这是幂等的只读操作：只看文件系统，不改状态文件。
        """
        saved = self._load_state()
        out: list[PhaseState] = []
        blocked_seen = False

        for phase in PHASES:
            found, missing = self.check_outputs(phase)
            prev = saved.get(phase.key, {})

            if self.is_complete(phase):
                st = "done"
                # 门控阶段：报告在，但体检没过 → 仍视为未通关
                if self._gate_failed(phase, prev):
                    st = "blocked"
            elif prev.get("status") == "in_progress":
                st = "in_progress"
            elif blocked_seen:
                st = "blocked"
            else:
                st = "pending"

            # 前置未完成 → 本阶段视为 blocked（顺序即契约）
            if st == "pending" and phase.requires:
                for req in phase.requires:
                    if req in PHASE_BY_KEY and not self.is_complete(PHASE_BY_KEY[req]):
                        st = "blocked"
                        break

            if st != "done":
                blocked_seen = True

            out.append(PhaseState(
                key=phase.key,
                status=st,
                outputs_found=found,
                outputs_missing=missing,
                updated_at=prev.get("updated_at", 0.0),
                note=prev.get("note", ""),
            ))
        return out

    def status_dict(self) -> dict[str, Any]:
        """给 API / 前端用的可序列化状态。"""
        states = self.status()
        done = sum(1 for s in states if s.status == "done")
        return {
            "workdir": self.workdir,
            "total": len(PHASES),
            "done": done,
            "progress": round(done / len(PHASES), 3) if PHASES else 0.0,
            # 第 0 关门控状态：前端用它解释「为什么后面都被挡住了」
            "gate": self.gate_state(),
            "phases": [
                {
                    **PHASE_BY_KEY[s.key].to_dict(),
                    "status": s.status,
                    "outputs_found": s.outputs_found,
                    "outputs_missing": s.outputs_missing,
                    "updated_at": s.updated_at,
                    "note": s.note,
                }
                for s in states
            ],
        }

    # -- 推进 ---------------------------------------------------------------

    def next_phase(self) -> Phase | None:
        """返回第一个未完成的阶段（按 PHASES 顺序）。全部完成则返回 None。

        门控阶段即使产出文件齐备，只要**没通过就要重跑** —— 所以这里也得看 gate。
        """
        saved = self._load_state()
        for phase in PHASES:
            if not self.is_complete(phase):
                return phase
            if self._gate_failed(phase, saved.get(phase.key, {})):
                return phase
        return None

    def dispatch_context(self, phase: Phase, bridge: dict[str, str] | None = None) -> str:
        """生成派发给该阶段的上下文桥接串（只传摘要，不传全文）。

        bridge: {前置阶段 key: 2-5 行摘要}，由调用方（通常是从产出文件里摘要）
        提供。这里只做拼装与裁剪，保证下游拿到「刚好够启动」的信息。
        """
        lines = [f"/{phase.key}", f"目标: {phase.goal}"]
        if phase.requires:
            lines.append("前置产出:")
            for req in phase.requires:
                req_phase = PHASE_BY_KEY.get(req)
                if not req_phase:
                    continue
                summary = (bridge or {}).get(req, "").strip()
                if not summary:
                    summary = f"（见 {', '.join(req_phase.outputs)}）"
                # 裁剪到 5 行以内，防污染
                summary = "\n".join(summary.splitlines()[:5])
                lines.append(f"- {req_phase.name}: {summary}")
        lines.append(f"预期产出: {', '.join(phase.outputs)}")
        lines.append(f"验收标准: {phase.validator}")
        return "\n".join(lines)

    def mark(self, key: str, status: str, note: str = "") -> dict[str, Any]:
        """记录阶段状态（不改文件，只记元数据）。"""
        if key not in PHASE_BY_KEY:
            raise KeyError(f"未知阶段: {key}")
        state = self._load_state()
        state[key] = {
            "status": status,
            "note": note,
            "updated_at": time.time(),
        }
        self._save_state(state)
        return state[key]

    def validate(self, key: str) -> dict[str, Any]:
        """校验某阶段的产出文件是否齐备。"""
        phase = PHASE_BY_KEY.get(key)
        if not phase:
            raise KeyError(f"未知阶段: {key}")
        found, missing = self.check_outputs(phase)
        ok = not missing
        return {
            "phase": key,
            "ok": ok,
            "outputs_found": found,
            "outputs_missing": missing,
            "expected": phase.outputs,
            "rule": phase.validator,
        }

    # -- 第 0 关：数据体检门控 ------------------------------------------------

    def run_datacheck_gate(self, df: Any, *, filename: str = "") -> dict[str, Any]:
        """第 0 阶段门控：跑数据体检 → 落盘报告 → 把「通过与否」写进流水线状态。

        **只卡 high**：硬矛盾（合计对不上 / 不可能取值）必须先核对修正；
        mid / low 只提醒不阻断 —— 否则流水线会寸步难行。这是**门控**，不是刁难。

        报告**始终落盘**（即使没通过，用户也要知道问题在哪一行哪一列）。

        返回：
            {"ok": True, "passed": bool, "high": int, "mid": int, "low": int,
             "report_file": "datacheck_res.md", "issues": [...], "summary": {...}}
        """
        import datacheck as dc  # 延迟导入：不必为体检买单启动成本

        report = dc.run_datacheck(df)
        s = report.get("summary", {}) or {}
        high = int(s.get("high", 0))
        passed = high == 0

        os.makedirs(self.workdir, exist_ok=True)
        rel = "datacheck_res.md"
        try:
            with open(os.path.join(self.workdir, rel), "w", encoding="utf-8") as f:
                f.write(dc.render_markdown(report, filename=filename))
        except OSError:
            rel = ""

        note = ("体检通过：无高优先级问题。" if passed
                else f"体检未通过：{high} 处高优先级问题，请先核对修正。")
        state = self._load_state()
        state[self.GATE_PHASE_KEY] = {
            "status": "done" if passed else "blocked",
            "note": note,
            "updated_at": time.time(),
            "gate": {
                "passed": passed,
                "high": high,
                "mid": int(s.get("mid", 0)),
                "low": int(s.get("low", 0)),
                "rows": s.get("rows", 0),
                "cols": s.get("cols", 0),
                "verdict": s.get("verdict", ""),
            },
        }
        self._save_state(state)

        return {
            "ok": True,
            "passed": passed,
            "high": high,
            "mid": int(s.get("mid", 0)),
            "low": int(s.get("low", 0)),
            "report_file": rel,
            "issues": report.get("issues", []),
            "summary": s,
        }

    def gate_state(self) -> dict[str, Any]:
        """返回第 0 关门控的当前状态（供前端展示「为什么被挡住」）。"""
        prev = self._load_state().get(self.GATE_PHASE_KEY, {}) or {}
        gate = prev.get("gate")
        if not isinstance(gate, dict):
            return {"passed": False, "ran": False}
        return {"passed": gate.get("passed") is True, "ran": True, **gate}


def phase_catalog() -> list[dict[str, Any]]:
    """返回阶段目录（供前端渲染流水线看板）。"""
    return [p.to_dict() for p in PHASES]
