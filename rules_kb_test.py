"""行业规则知识库 + 经验库离线测试(v2.32)。全离线,文件指到临时目录。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


import pandas as pd  # noqa: E402
import rules_kb  # noqa: E402
import lessons  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="zl_rules_test_"))
rules_kb._candidate_dirs = lambda: [TMP]
lessons._base_dirs = lambda: [TMP]

print("[1] 首次调用自动生成模板")
p = rules_kb.rules_path()
check("模板已生成", p is not None and p.is_file() and p.parent == TMP)
check("模板可解析且含规则", len(rules_kb.load_rules()) >= 3)

print("[2] 取值越界 + 整数规则")
df = pd.DataFrame({
    "焦虑得分": [1, 2, 3, 7],        # 超出五点量表 [1,5]
    "焦虑总分": [10, 15.5, 20, 22],   # integer=True → 15.5 报
    "姓名": ["甲", "乙", "丙", "丁"],  # 不匹配任何规则
})
issues = rules_kb.check_rules(df)
cats = [i["category"] for i in issues]
check("焦虑得分越界被拦", "行业规则·取值越界" in cats, str(cats))
check("非整数被拦", "行业规则·应为整数" in cats, str(cats))
check("卡片结构同构", all(set(i) >= {"level", "category", "title", "evidence",
                                    "explain", "suggestion", "rows", "columns"}
                          for i in issues))
check("正常列零误报", all("姓名" not in (i.get("columns") or []) for i in issues))

print("[3] 规则文件损坏 = 无规则不炸")
(TMP / rules_kb.RULES_FILENAME).write_text("{broken json", encoding="utf-8")
check("坏 json → load_rules 空列表", rules_kb.load_rules() == [])
check("坏 json → check_rules 空", rules_kb.check_rules(df) == [])

print("[4] 经验库 v2:三轮确认 + 误报关 + 上下文")
issue_a = {"category": "行业规则·取值越界", "title": "「焦虑得分」…",
           "columns": ["焦虑得分"], "evidence": "越界值示例:7",
           "explain": "x", "suggestion": "y"}
cs = {"焦虑得分": {"dtype": "int64", "min": 1, "max": 7, "unique": 7,
                 "samples": [1, 2, 3, 7]}}
def _round(tag_expr):
    """模拟 app 流程:先标注(基于历史)再记录。"""
    f = dict(issue_a)
    lessons.annotate([f])
    lessons.record([issue_a], source="t", col_stats=cs)
    return f

f1 = _round("第1轮")
check("第 1 次不出声(先标注后记录,历史为空)",
      "经验库" not in str(f1.get("explain", "")), str(f1.get("explain")))
check("但已挂 lesson_key(供误报按钮)", bool(f1.get("lesson_key")))
f2 = _round("第2轮")
check("第 2 次预警(累计3次确认)", "将确认为经验" in str(f2["explain"]), str(f2["explain"]))
f3 = _round("第3轮")
check("第 3 次仍候选口径(记录后才转正)",
      "将确认为经验" in str(f3["explain"]), str(f3["explain"]))
e = lessons.get(lessons.issue_key(issue_a))
check("第 3 次记录后自动转正 confirmed", e["status"] == "confirmed", str(e.get("status")))
check("上下文带列统计(防事后看不懂)",
      e["context"].get("min") == 1 and e["context"].get("max") == 7
      and "7" in [str(x) for x in (e["context"].get("samples") or [])],
      str(e.get("context")))
check("同类合并为一行(不堆重复)", len([l for l in
      (TMP / lessons.LESSONS_FILENAME).read_text(encoding="utf-8").splitlines()
      if l.strip()]) == 1)
f4 = _round("第4轮")
check("转正后催固化+把关提示", "固化" in str(f4["suggestion"])
      and "误报" in str(f4["suggestion"]), str(f4["suggestion"]))
check("第 4 次显示已确认为经验", "已确认为经验" in str(f4["explain"]), str(f4["explain"]))
k = lessons.issue_key(issue_a)
lessons.feedback(k, "false_positive")
check("误报 1 次未拒绝", lessons.get(k)["status"] == "confirmed")
lessons.feedback(k, "false_positive")
check("误报 2 次且过半 → rejected", lessons.get(k)["status"] == "rejected")
f5 = dict(issue_a)
lessons.annotate([f5])
check("拒绝后静音(不再标注)", "经验库" not in str(f5.get("explain", "")))
st = lessons.stats()
check("stats 汇总", st["entries"] == 1 and st["rejected"] == 1, str(st))

print("[5] draft_with_llm(mock complete)")
draft = rules_kb.draft_with_llm(
    [{"name": "焦虑得分", "dtype": "int64", "min": 1, "max": 7, "unique": 7}],
    lambda p: "[{\"name\":\"五点\",\"match\":[\"焦虑\"],\"min\":1,\"max\":5}]")
check("AI 草稿透传", "焦虑" in draft)

print()
print(f"结果:{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
