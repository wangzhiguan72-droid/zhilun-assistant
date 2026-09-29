"""
行业规则知识库（v2.32）
================================
回答"这个值在这个行业/量表里合不合理"——规则放在用户可编辑的本地
《行业规则.json》里(不进 exe、不进仓库),数据体检时逐列匹配。

设计:
    - 规则来源优先级:exe 同目录 → 源码根目录 → 当前目录;都没有则
      首次调用自动生成**模板**(含常用示例),并在体检卡片里提示可编辑。
    - 匹配:列名包含任一 match 关键词(忽略大小写)即命中;一条列名
      可命中多条规则(都报,让用户判断)。
    - 可选 AI 草稿:draft_with_llm() 用已有 LLM 层(BYOK/预置 Key)按
      列名与取值概况生成规则草稿文本,**仅供人工确认后**写回 json。
      联网搜索不做——本地规则 + 人工确认的口径最诚实。

规则文件示例(自动生成的模板即此结构):
    {
      "rules": [
        {"name": "五点量表", "match": ["焦虑", "满意度", "量表"],
         "min": 1, "max": 5, "integer": true, "note": "Likert 1-5"},
        {"name": "百分比列", "match": ["占比", "百分比", "率"],
         "min": 0, "max": 100, "note": "百分数 0-100"}
      ]
    }
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from datacheck import LEVEL_MID, LEVEL_LOW

RULES_FILENAME = "行业规则.json"

_TEMPLATE: dict = {
    "rules": [
        {"name": "五点量表(1-5)", "match": ["焦虑", "抑郁", "满意度", "量表", "态度"],
         "min": 1, "max": 5, "integer": True,
         "note": "Likert 五点计分;改成 0/1 起点请按你的量表手册调整"},
        {"name": "百分比列(0-100)", "match": ["占比", "百分比", "合格率", "利用率", "率"],
         "min": 0, "max": 100, "note": "百分数应在 0-100 之间"},
        {"name": "七点量表(1-7)", "match": ["七点", "ses", "自我效能"],
         "min": 1, "max": 7, "integer": True, "note": "示例:按需删除"},
        {"name": "血压收缩压(示例·行业规则)", "match": ["收缩压", "sbp"],
         "min": 60, "max": 260, "note": "临床常识范围,示例:证明可接行业规则"},
    ],
    "_说明": "match=列名包含任一关键词即命中;integer=true 要求整数;"
             "enum=[...] 可选枚举。改完保存即可,下次体检生效。",
}


def _candidate_dirs() -> list[Path]:
    dirs = []
    if getattr(__import__("sys"), "frozen", False):
        dirs.append(Path(__import__("sys").executable).parent)
    here = Path(__file__).resolve().parent
    dirs += [here, Path.cwd()]
    return dirs


def rules_path() -> Path | None:
    """已存在的规则文件路径;都没有 → 在第一个可写目录生成模板并返回它。"""
    for d in _candidate_dirs():
        p = d / RULES_FILENAME
        if p.is_file():
            return p
    for d in _candidate_dirs():
        try:
            p = d / RULES_FILENAME
            p.write_text(json.dumps(_TEMPLATE, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            return p
        except OSError:
            continue
    return None


def load_rules() -> list[dict]:
    p = rules_path()
    if not p:
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        rules = data.get("rules", [])
        return [r for r in rules if isinstance(r, dict) and r.get("match")]
    except Exception:  # noqa: BLE001 — 规则文件坏了 = 没有规则,绝不炸体检
        return []


def _issue(level, category, title, evidence, explain, suggestion, column=""):
    # v2.33:columns 带上列名——经验库按"类别+列"分键,不同列不混经验
    return {"level": level, "category": category, "title": title,
            "evidence": evidence, "explain": explain,
            "suggestion": suggestion, "rows": [],
            "columns": ([column] if column else [])}


def check_rules(df: pd.DataFrame) -> list[dict]:
    """逐列匹配行业规则,返回体检卡片(与 datacheck 同构)。"""
    issues: list[dict] = []
    rules = load_rules()
    if not rules or df is None or df.empty:
        return issues
    for col in df.columns:
        name = str(col)
        lname = name.lower()
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            continue
        for rule in rules:
            kws = [str(k).lower() for k in (rule.get("match") or [])]
            if not any(k in lname for k in kws):
                continue
            rname = rule.get("name") or "/".join(map(str, rule.get("match", [])))
            lo, hi = rule.get("min"), rule.get("max")
            if lo is not None and hi is not None:
                bad = series[(series < float(lo)) | (series > float(hi))]
                if len(bad):
                    shown = ", ".join(f"{v:g}" for v in bad.head(5))
                    issues.append(_issue(
                        LEVEL_MID, "行业规则·取值越界",
                        f"「{name}」有 {len(bad)} 个值超出规则「{rname}」"
                        f"的 [{lo}, {hi}]",
                        f"越界值示例:{shown}"
                        + (f" 等(共 {len(bad)} 个)" if len(bad) > 5 else ""),
                        f"规则「{rname}」来自《行业规则.json》"
                        f"({'规则备注:' + rule['note'] if rule.get('note') else '可自行编辑'})。"
                        "越界可能是录入错误,也可能是规则不适用你的数据——请人工判断。",
                        f"确认是录入错误的改正;确认规则不适用的,编辑"
                        f"{RULES_FILENAME} 调整 match/min/max。", column=name))
            if rule.get("integer"):
                non_int = series[series != series.round()]
                if len(non_int):
                    issues.append(_issue(
                        LEVEL_LOW, "行业规则·应为整数",
                        f"「{name}」有 {len(non_int)} 个非整数,但规则「{rname}」要求整数",
                        f"示例:{', '.join(f'{v:g}' for v in non_int.head(4))}",
                        "计数/量表原始分一般应为整数;小数可能来自均值列误入或加权计分。",
                        "核对计分方式;确属加权的,在规则里去掉 integer。", column=name))
    return issues


def draft_with_llm(columns_summary: list[dict], complete) -> str:
    """用 LLM 生成《行业规则.json》草稿(纯建议文本,人工确认后才写文件)。"""
    cols_desc = "\n".join(
        f"- {c.get('name')}:类型={c.get('dtype', '?')},"
        f"最小={c.get('min', '?')},最大={c.get('max', '?')},"
        f"唯一值数={c.get('unique', '?')}"
        for c in columns_summary[:40])
    prompt = (
        "你是数据质量顾问。下面是一份数据集的列概况,请为其中的业务/量表列"
        "提出合理的取值范围规则建议,输出 JSON 数组,每条形如 "
        '{"name":"规则名","match":["列名关键词"],"min":0,"max":100,'
        '"integer":false,"note":"依据"}。'
        "只输出 JSON 数组本身,不要解释。\n\n列概况:\n" + cols_desc)
    text = complete(prompt)
    return text
