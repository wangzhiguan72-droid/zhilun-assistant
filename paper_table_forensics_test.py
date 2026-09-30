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


from paper_table_forensics import (  # noqa: E402
    audit_paper_tables, check_digits, check_grim, check_mixed_precision,
    check_progression, check_relations, check_totals)

print("[1] 尾串重复 + 末位偏好(一列全是 .33)")
rows = [["组别", "得分A", "得分B"]]
for i in range(25):
    rows.append([f"G{i+1}", f"{i%9+1}.33", f"{(i*3)%8+2}.33"])
rep = audit_paper_tables([rows])
cats = [i["category"] for i in rep["issues"]]
check("命中「尾数重复」", "表格取证·尾数重复" in cats, str(cats))
check("命中「末位偏好」", "表格取证·末位偏好" in cats, str(cats))
check("摘要字段齐全", rep["tables"] == 1 and rep["numeric_cells"] >= 50)
check("跨类印证时保留中档", any(i["level"] == "mid" for i in rep["issues"]),
      str([(i["category"], i["level"]) for i in rep["issues"]]))

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

print("[7] 混合小数位(整列 2 位小数里混进 1 位)")
t7 = [["编号", "得分"]]
for i in range(24):
    t7.append([f"S{i+1}", f"{(i % 5) + 1}.{(i * 17) % 90 + 10}"])
t7.append(["S25", "3.4"])                     # 唯一一个 1 位小数
mp = check_mixed_precision([t7])
check("命中「小数位不齐」且恒为最低档",
      len(mp) == 1 and mp[0]["level"] == "low", str(mp))
mp_clean = check_mixed_precision([t7[:-1]])   # 去掉那一个异常值
check("整列同精度不报", mp_clean == [], str(mp_clean))

print("[8] 列间固定差(同表两列恒差一个常数)")
t8 = [["编号", "前测", "后测"]]
for i in range(12):
    t8.append([f"P{i+1}", f"{(i * 13) % 60 + 10}.{i % 7}", f"{(i * 13) % 60 + 15}.{i % 7}"])
rel = check_relations([t8])
check("命中「列间固定关系」", len(rel) == 1 and rel[0]["level"] == "low", str(rel))
t8b = [["编号", "前测", "后测"]]
for i in range(12):
    t8b.append([f"P{i+1}", f"{(i * 13) % 60 + 10}.{i % 7}",
                f"{(i * 11) % 60 + 20}.{(i * 3) % 9}"])
check("无关两列不报", check_relations([t8b]) == [], str(check_relations([t8b])))
t8c = [["序号", "剂量"], *[[f"{i+1}", f"{i+1}.5"] for i in range(12)]]
check("设计轴列(序号)不参与列间判定",
      check_relations([t8c]) == [], str(check_relations([t8c])))

print("[9] 等差数列列")
t9 = [["编号", "孔径读数"]]
for i in range(10):
    t9.append([f"Q{i+1}", f"{1.2 + i * 0.35:.2f}"])
ap = check_progression([t9])
check("命中「等差数列列」且为最低档",
      len(ap) == 1 and ap[0]["level"] == "low", str(ap))
t9b = [["编号", "剂量"], *[[f"R{i+1}", f"{i * 5:.1f}"] for i in range(10)]]
check("列名带「剂量」的设计轴列不报",
      check_progression([t9b]) == [], str(check_progression([t9b])))
t9c = [["编号", "孔径"], *[[f"T{i+1}", f"{(i * 37) % 91 + 10}.{(i * 29) % 90 + 10:02d}"] for i in range(10)]]
check("无规律的列不报", check_progression([t9c]) == [], str(check_progression([t9c])))

print("[9b] 等比数列列(T05 的另一半)")
# 1.5 → 3 → 6 → 12 …:逐项 ×2,人不会这么量
t9d = [["编号", "渗透率"], *[[f"V{i+1}", f"{1.5 * 2 ** i:.2f}"] for i in range(9)]]
gp = check_progression([t9d])
check("命中「等比数列列」且为最低档",
      len(gp) == 1 and gp[0]["category"] == "表格取证·等比数列列"
      and gp[0]["level"] == "low", str(gp))
check("等比说明里点明「都是上一项的 N 倍」",
      gp and "倍" in gp[0]["evidence"], str(gp[:1]))
# 反向:公比恒为 1(常数段)不算等比,也不是等差
t9e = [["编号", "读数"], *[[f"W{i+1}", "3.14"] for i in range(9)]]
check("常数段不报等比", check_progression([t9e]) == [], str(check_progression([t9e])))
# 反向:带 0 的段不能当等比(后项/前项无意义),且不得抛异常
t9f = [["编号", "读数"], *[[f"X{i+1}", f"{i % 3 * 1.0:.2f}"] for i in range(9)]]
check("含 0 的列不误报等比也不会崩",
      all(i["category"] != "表格取证·等比数列列" for i in check_progression([t9f])),
      str(check_progression([t9f])))

print("[10] 独立组降噪(单一类信号 → 全降最低档 + 注明未印证)")
# 第一张只有「列间」一类统计信号;第二张给一条合计硬矛盾(合计行与分项不符)
t10 = [["编号", "前测", "后测"],
       *[[f"U{i+1}", f"{(i * 37) % 90 + 10}.{(i * 29) % 90 + 10:02d}",
          f"{(i * 37) % 90 + 15}.{(i * 29) % 90 + 10:02d}"] for i in range(12)]]
t10b = [["组", "分项1", "分项2", "合计"],
        ["A", "10", "20", "合计 31"],
        ["B", "12", "23", "合计 36"],
        ["C", "17", "19", "合计 37"]]
rep10 = audit_paper_tables([t10, t10b])
rel10 = [i for i in rep10["issues"] if i["category"] == "表格取证·列间固定关系"]
tot10 = [i for i in rep10["issues"] if i["category"] == "表格取证·合计矛盾"]
check("单一类信号被降为最低档并注明未印证",
      rel10 and all(i["level"] == "low" and "未经" in i["explain"] for i in rel10),
      str([(i["category"], i["level"]) for i in rel10]))
check("算术硬矛盾(合计)不受降噪影响",
      tot10 and all(i["level"] == "mid" and "未经" not in i["explain"] for i in tot10),
      str([(i["category"], i["level"]) for i in tot10]))
check("groups_hit 只记统计类命中", rep10["groups_hit"] == ["列间"], str(rep10["groups_hit"]))

print()
print(f"结果:{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
