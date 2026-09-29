"""
经验库 v2（v2.33）——「越用越聪明 × 把好关」
=====================================================
借鉴 xihe agent(羲和)的轻量记忆设计 + 业界共识(可验证/可复现/可追溯):

    存储:exe 旁《经验库.jsonl》,**一个 key 一行**(同类发现去重合并,
          seen++/last 更新,而不是堆重复行)——聊天/会话删了也没事,
          摘要与上下文专门存在这个文件里。
    上下文(防误解的关键):每条经验带列统计(类型/范围/唯一值/样本值)
          + 触发时的证据文本,事后单看这一行也能明白"当时发生了什么"。
    三轮确认(质量把关):第 1 次只记录不出声;第 2 次提示"再犯 1 次将确认";
          第 3 次起 status=confirmed,才开始催"固化进《行业规则.json》"。
    误报关(错误经验不沉淀):用户点「这条是误报」→ false_pos++;
          false_pos≥2 且过半 → status=rejected,之后静音(不再标注、
          不再催固化),但记录保留可追溯。
    轻量红线:零向量库/零嵌入/零 LLM——key 精确匹配 + 计数,JSONL 原子重写。
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

LESSONS_FILENAME = "经验库.jsonl"
CONFIRM_SEEN = 3          # 三轮:第 3 次出现确认为经验
REJECT_FALSE_POS = 2      # 误报 ≥2 次…


def _base_dirs() -> list[Path]:
    dirs = []
    import sys
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent)
    here = Path(__file__).resolve().parent
    dirs += [here, Path.cwd()]
    return dirs


def _lessons_file() -> Path | None:
    for d in _base_dirs():
        p = d / LESSONS_FILENAME
        if p.is_file():
            return p
    for d in _base_dirs():
        try:
            p = d / LESSONS_FILENAME
            p.touch(exist_ok=True)
            return p
        except OSError:
            continue
    return None


def issue_key(issue: dict) -> str:
    """同类问题判定键:类别 + 首列名(去编号前缀)。"""
    cat = str(issue.get("category") or "")
    col = ""
    cols = issue.get("columns") or []
    if cols:
        s = str(cols[0]).strip()
        m = re.match(r"^(.*?)[_\-\s]*\d+$", s)
        col = (m.group(1).strip("_- ") if m else s) or s
    return f"{cat}|{col}"


def _load() -> list[dict]:
    f = _lessons_file()
    if not f:
        return []
    out = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if isinstance(e, dict) and e.get("key"):
                    out.append(e)
            except (ValueError, TypeError):
                continue
    except OSError:
        pass
    return out


def _save(entries: list[dict]) -> None:
    f = _lessons_file()
    if not f:
        return
    try:
        fd, tmp = tempfile.mkstemp(dir=str(f.parent), suffix=".tmp")
        with open(fd, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        Path(tmp).replace(f)
    except OSError:
        pass


def _ctx_of(issue: dict, col_stats: dict | None) -> dict:
    """给经验补上下文:列统计 + 证据片段(防事后看不懂)。"""
    ctx: dict = {"title": str(issue.get("title") or "")[:120],
                 "evidence": str(issue.get("evidence") or "")[:160]}
    cols = issue.get("columns") or []
    if cols:
        name = str(cols[0])
        ctx["column"] = name
        if col_stats and name in col_stats:
            st = col_stats[name]
            ctx.update({k: st.get(k) for k in ("dtype", "min", "max", "unique")})
            ctx["samples"] = st.get("samples") or []
    return ctx


def _samples(ctx: dict) -> list:
    v = ctx.get("samples")
    return list(v) if isinstance(v, list) else []


def _merge_ctx(old: dict, new: dict) -> dict:
    """上下文合并:保留更晚的(通常更全),samples 取并集前 5。"""
    merged = dict(new)
    merged["samples"] = list(dict.fromkeys(
        [*(str(x) for x in _samples(old)), *(str(x) for x in _samples(new))]))[:5]
    return merged


def get(key: str) -> dict | None:
    for e in _load():
        if e.get("key") == key:
            return e
    return None


def record(issues: list[dict], source: str = "datacheck",
           col_stats: dict | None = None) -> None:
    """把本次发现并入经验库(同类 key 合并:seen++/last/上下文,不堆行)。"""
    if not issues:
        return
    entries = _load()
    by_key = {e["key"]: e for e in entries}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    changed = False
    for it in issues:
        k = issue_key(it)
        ctx = _ctx_of(it, col_stats)
        e = by_key.get(k)
        if e is None:
            e = {"id": f"l{len(entries) + 1}", "key": k, "kind": "finding",
                 "text": str(it.get("title") or "")[:160],
                 "context": ctx, "seen": 0, "false_pos": 0, "useful": 0,
                 "status": "candidate", "first": now, "last": now,
                 "sources": []}
            entries.append(e)
            by_key[k] = e
        e["seen"] = int(e.get("seen", 0)) + 1
        e["last"] = now
        e["context"] = _merge_ctx(e.get("context") or {}, ctx)
        if source not in (e.get("sources") or []):
            e["sources"] = (e.get("sources") or []) + [source]
        # 三轮确认(第 CONFIRM_SEEN 次起转正;已拒绝的不自动复活)
        if (e.get("status") == "candidate"
                and e["seen"] >= CONFIRM_SEEN and not e.get("false_pos")):
            e["status"] = "confirmed"
        changed = True
    if changed:
        _save(entries)


def annotate(issues: list[dict]) -> None:
    """在体检卡片上就地标注经验库状态(并挂 lesson_key 供误报按钮用)。"""
    if not issues:
        return
    entries = {e["key"]: e for e in _load()}
    for it in issues:
        k = issue_key(it)
        it["lesson_key"] = k
        e = entries.get(k)
        if not e or e.get("status") == "rejected":
            continue  # 拒绝的经验:静音,不再打扰
        seen = int(e.get("seen", 0))
        ctx = e.get("context") or {}
        col = ctx.get("column") or ""
        rng = ""
        if ctx.get("min") is not None and ctx.get("max") is not None:
            rng = f",历史范围 {ctx['min']}~{ctx['max']}"
        # seen = 此前出现次数;当前是第 seen+1 次
        if e.get("status") == "confirmed":
            tag = (f"〔经验库〕「{col}」同类第 {seen + 1} 次出现{rng}"
                   f"——已确认为经验")
        else:
            tag = (f"〔经验库〕「{col}」同类第 {seen + 1} 次出现{rng}"
                   f"——累计 {CONFIRM_SEEN} 次将确认为经验")
        it["explain"] = (str(it.get("explain") or "") + f";{tag}")
        if e.get("status") == "confirmed":
            it["suggestion"] = (
                str(it.get("suggestion") or "")
                + ";〔经验库〕该经验已多次验证,建议把该列范围固化进"
                  "《行业规则.json》,下次体检第一时间拦住;若这条其实是误报,"
                  "点下方按钮帮经验库把关")


def feedback(key: str, verdict: str = "false_positive") -> dict:
    """用户反馈:误报降权(达到阈值即拒绝静音)/ 有用加速确认。返回该条目。"""
    entries = _load()
    for e in entries:
        if e.get("key") != key:
            continue
        if verdict == "false_positive":
            e["false_pos"] = int(e.get("false_pos", 0)) + 1
            # 两次人工误报 = 直接拒绝:显式的人的判断优先于任何出现次数
            # (此前"过半才拒"在反复出现的问题上永远不触发,已移除)
            if e["false_pos"] >= REJECT_FALSE_POS:
                e["status"] = "rejected"
        elif verdict == "useful":
            e["useful"] = int(e.get("useful", 0)) + 1
            if e.get("status") == "candidate" and e.get("seen", 0) >= 2:
                e["status"] = "confirmed"
        _save(entries)
        return e
    return {}


def stats() -> dict:
    entries = _load()
    return {
        "entries": len(entries),
        "confirmed": sum(1 for e in entries if e.get("status") == "confirmed"),
        "rejected": sum(1 for e in entries if e.get("status") == "rejected"),
        "candidate": sum(1 for e in entries if e.get("status") == "candidate"),
        "top": sorted(((e["key"], e.get("seen", 0)) for e in entries),
                      key=lambda x: -x[1])[:5],
    }
