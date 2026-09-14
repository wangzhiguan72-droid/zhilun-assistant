"""v1.5 · 人话解释卡片测试（③学术诚信 XAI）
================================================================
测 `_explain_suggestions`：把「你哪里用错了方法」讲成人话。

测试重点（与红线测试同样的双边思路）：
  1. 该出的卡片必须出（各方法误用规则命中正确）
  2. 不该出的不出（正常用法不误报）
  3. fix 必须是**注册表里真实存在**的方法（绝不编造方法名 —— 护栏）
  4. 向后兼容：原 suggestions 仍是 list[str]，一字不动

跑法：.venv/Scripts/python.exe explain_test.py
"""
import io
import pathlib
import sys

import app as A
from audit import _explain_suggestions, _generate_suggestions
from methods_registry import method_keys, method_labels

PASS = 0
FAIL = 0
REG_KEYS = set(method_keys())
REG_LABELS = method_labels()


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _fixes(items):
    return {it["fix"] for it in items if it["fix"]}


# ===========================================================================
section("1. 方法误用规则 —— 应触发")
# ===========================================================================
# (名称, methods, real, quantities, columns, 期望出现的 fix)
MUST_TRIGGER = [
    ("R1 分组 3 却用独立 T",
     [{"method_key": "independent_t"}],
     {"n_groups": 3, "group_col": "专业", "ok": True}, [], None, "anova"),
    ("R2 有前后测列却用独立 T",
     [{"method_key": "independent_t"}],
     {"n_groups": 2, "group_col": "组别", "ok": True}, [],
     [{"name": "anxiety_pre", "type": "continuous"},
      {"name": "anxiety_post", "type": "continuous"}], "paired_t"),
    ("R3 只有 2 组却用 ANOVA",
     [{"method_key": "anova"}],
     {"n_groups": 2, "group_col": "性别", "ok": True}, [], None, "independent_t"),
    ("R4 卡方配了连续列",
     [{"method_key": "chi_square"}],
     {"value_col": "分数", "ok": True}, [],
     [{"name": "分数", "type": "continuous"}], "correlation"),
    ("R5 相关分析配了分类列",
     [{"method_key": "correlation"}],
     {"value_col": "性别", "ok": True}, [],
     [{"name": "性别", "type": "categorical"}], "chi_square"),
    ("R6 小样本参数检验",
     [{"method_key": "independent_t"}],
     {"n1": 10, "n2": 12, "n_groups": 2, "group_col": "性别", "ok": True},
     [], None, "mann_whitney"),
]
for name, methods, real, quantities, columns, want_fix in MUST_TRIGGER:
    items = _explain_suggestions(real, methods, quantities, columns)
    check(f"{name} → fix 含 {want_fix}",
          want_fix in _fixes(items),
          f"实际 fixes={_fixes(items)}")

# R1 的 counter_example 应算出一类错误膨胀（具体数字，不是空话）
items_r1 = _explain_suggestions(
    {"n_groups": 3, "group_col": "专业", "ok": True},
    [{"method_key": "independent_t"}], [], None)
r1_card = next((it for it in items_r1 if it["fix"] == "anova"), None)
check("R1 卡片给出具体膨胀概率（~14%）",
      r1_card and "14%" in r1_card["counter_example"],
      f"counter={r1_card['counter_example'] if r1_card else '无卡片'}")

# R7 补报告类：fix 为空但卡片存在
items_r7 = _explain_suggestions(
    {"n1": 50, "n2": 55, "n_groups": 2, "group_col": "性别", "ok": True},
    [{"method_key": "independent_t"}], [], None)
texts = "｜".join(it["text"] for it in items_r7)
check("R7 补效应量卡片（fix 为空）",
      any("效应量" in it["text"] and not it["fix"] for it in items_r7), f"texts={texts}")
check("R8 补前提检验卡片（fix 为空）",
      any("正态性" in it["text"] and not it["fix"] for it in items_r7), f"texts={texts}")


# ===========================================================================
section("2. 正常用法 —— 不误报")
# ===========================================================================
MUST_NOT = [
    ("2 组用独立 T，不该推 ANOVA", "anova",
     [{"method_key": "independent_t"}],
     {"n_groups": 2, "group_col": "性别", "ok": True}, [], None),
    ("无前后测列，不该推配对 T", "paired_t",
     [{"method_key": "independent_t"}],
     {"n_groups": 2, "group_col": "性别", "ok": True}, [],
     [{"name": "性别", "type": "categorical"}, {"name": "分数", "type": "continuous"}]),
    ("非卡方方法，不该推相关", "correlation",
     [{"method_key": "independent_t"}],
     {"n_groups": 2, "group_col": "性别", "ok": True}, [],
     [{"name": "分数", "type": "continuous"}]),
    ("大样本，不该推非参数", "mann_whitney",
     [{"method_key": "independent_t"}],
     {"n1": 80, "n2": 90, "n_groups": 2, "group_col": "性别", "ok": True},
     [], None),
]
for name, bad_fix, methods, real, quantities, columns in MUST_NOT:
    items = _explain_suggestions(real, methods, quantities, columns)
    check(f"不误报：{name}",
          bad_fix not in _fixes(items),
          f"误推出 {bad_fix}，fixes={_fixes(items)}")


# ===========================================================================
section("3. 出参契约")
# ===========================================================================
FIELDS = {"text", "why_wrong", "counter_example", "fix", "fix_label",
          "severity", "from_method"}
all_items = []
for _, methods, real, quantities, columns, _f in MUST_TRIGGER:
    all_items += _explain_suggestions(real, methods, quantities, columns)

check("每项含全部 7 个字段",
      all(FIELDS <= set(it) for it in all_items))
check("severity 只能是 high/mid/low",
      all(it["severity"] in ("high", "mid", "low") for it in all_items))
check("fix 为空串或注册表里的真实方法（不编造）",
      all(it["fix"] == "" or it["fix"] in REG_KEYS for it in all_items))
check("fix_label 与注册表中文名一致",
      all((it["fix"] == "" and it["fix_label"] == "")
          or it["fix_label"] == REG_LABELS.get(it["fix"])
          for it in all_items))
check("why_wrong / counter_example 非空",
      all(it["why_wrong"] and it["counter_example"] for it in all_items))

# severity 排序：high 在前（逐场景检查——每个场景的输出各自有序）
order = {"high": 0, "mid": 1, "low": 2}
sorted_ok, bad_ranks = True, None
for _, methods, real, quantities, columns, _f in MUST_TRIGGER:
    _items = _explain_suggestions(real, methods, quantities, columns)
    _ranks = [order[it["severity"]] for it in _items]
    if _ranks != sorted(_ranks):
        sorted_ok, bad_ranks = False, _ranks
        break
check("每个场景内按 severity 排序（high → mid → low）",
      sorted_ok, f"乱序 ranks={bad_ranks}")

# 边界输入不炸
check("空方法列表 → 空卡片", _explain_suggestions({}, [], [], None) == [])
check("空 real 不炸",
      isinstance(_explain_suggestions({}, [{"method_key": "anova"}], [], None), list))


# ===========================================================================
section("4. 向后兼容：suggestions 一字未动")
# ===========================================================================
s = _generate_suggestions(
    {"n1": 15, "n2": 15, "n_groups": 2, "group_col": "性别", "ok": True},
    [{"method_key": "independent_t"}], [])
check("suggestions 仍是 list[str]",
      isinstance(s, list) and all(isinstance(x, str) for x in s))
check("无统计方法论文的兜底建议仍在（2 条）",
      isinstance(_generate_suggestions({}, [], []), list))


# ===========================================================================
section("5. 端到端：/api/check_paper 返回 explanations")
# ===========================================================================
c = A.app.test_client()
paper = pathlib.Path("examples/sample_paper.md").read_bytes()
data = pathlib.Path("examples/student_scores.csv").read_bytes()
resp = c.post("/api/check_paper", data={
    "paper": (io.BytesIO(paper), "p.md"),
    "data": (io.BytesIO(data), "d.csv"),
}, content_type="multipart/form-data")
jr = resp.get_json()
check("check_paper 正常返回（200）", resp.status_code == 200 and jr.get("ok") is True,
      f"{resp.status_code}")
audit_part = jr.get("audit") or {}
exps = audit_part.get("explanations")
check("响应含 explanations 字段", isinstance(exps, list))
check("示例场景至少有补报告类卡片（效应量 / 前提检验）",
      isinstance(exps, list) and any(not e.get("fix") for e in exps),
      f"explanations={[e.get('text') for e in (exps or [])]}")
check("suggestions 字段仍在（旧契约不破）",
      isinstance(audit_part.get("suggestions"), list))


print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
