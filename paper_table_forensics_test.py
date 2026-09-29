"""论文表格取证离线测试(v2.32)。全离线,零网络。"""
from __future__ import annotations

import os
import sys

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


from paper_table_forensics import audit_paper_tables, check_digits, check_grim, check_totals  # noqa: E402

print("[1] 尾串重复 + 末位偏好(一列全是 .33)")
rows = [["组别", "得分A", "得分B"]]
for i in range(25):
    rows.append([f"G{i+1}", f"{i%9+1}.33", f"{(i*3)%8+2}.33"])
rep = audit_paper_tables([rows])
cats = [i["category"] for i in rep["issues"]]
check("命中「尾数重复」", "表格取证·尾数重复" in cats, str(cats))
check("命中「末位偏好」", "表格取证·末位偏好" in cats, str(cats))
check("摘要字段齐全", rep["tables"] == 1 and rep["numeric_cells"] >= 50)

print("[2] GRIM 可达性(n=30 与均值 3.47 不可达)")
t2 = [["维度", "n", "均值±标准差"], ["得分", "30", "3.47±0.82"]]
g = check_grim([t2])
check("命中 GRIM 卡片", len(g) == 1 and "GRIM" in g[0]["category"], str(g))
t2b = [["维度", "n", "均值±标准差"], ["得分", "30", "3.50±0.82"]]
g2 = check_grim([t2b])
check("可达组合(3.50)不报", len(g2) == 0, str(g2))

print("[3] 合计矛盾(行/列)")
t3 = [["组", "人数", "占比%", "合计"],
      ["A", "10", "40.0", ""],
      ["B", "20", "60.0", ""],
      ["合计", "35", "100.0", ""]]
tot = check_totals([t3])
check("命中合计矛盾 ≥1 条", len(tot) >= 1,
      str([i["title"] for i in tot]))

print("[4] 干净表格不报(≥40 个分散小数)")
clean = [["组", "A", "B"]]
for i in range(30):
    clean.append([f"r{i}", f"{(i*13)%97}.{(i*29)%89+10:02d}", f"{(i*7)%83}.{(i*31)%79+11:02d}"])
rep4 = audit_paper_tables([clean])
digits_issues = [i for i in rep4["issues"]
                 if i["category"].startswith("表格取证·末位")
                 or i["category"].startswith("表格取证·尾数")]
check("分散小数不触发末位/尾串", len(digits_issues) == 0,
      str([i["title"] for i in digits_issues]))

print("[5] 数字太少不判(闸门)")
small = [["x", "y"], ["1.5", "2.5"], ["3.5", "4.5"]]
rep5 = audit_paper_tables([small])
check("小表零卡片", rep5["issues"] == [], str(rep5["issues"]))

print("[6] 空输入")
rep6 = audit_paper_tables([])
check("空表格零卡片且 ok", rep6["ok"] and rep6["issues"] == [])

print()
print(f"结果:{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
