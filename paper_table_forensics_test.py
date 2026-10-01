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
    check_pct_consistency, check_progression, check_relations, check_totals)

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

print("[2] GRIM 可达性(n=30 与均值 3.46 不可达；3.47/3.50 可达)")
t2 = [["维度", "n", "均值±标准差"], ["得分", "30", "3.46±0.82"]]
g = check_grim([t2])
check("命中 GRIM 卡片", len(g) == 1 and "GRIM" in g[0]["category"], str(g))
t2b = [["维度", "n", "均值±标准差"], ["得分", "30", "3.50±0.82"]]
g2 = check_grim([t2b])
check("可达组合(3.50)不报", len(g2) == 0, str(g2))
# v2.38 口径：报告的均值带舍入区间。n=30、2 位小数的容差是 0.15，
# 3.47 × 30 = 104.1 偏离 104 只有 0.10 → 落在区间内，**合法**。
# 旧实现把它当精确值比整数，是本轮修掉的核心误报（20 万次实测 85.0% → 1.517%）。
t2c = [["维度", "n", "均值±标准差"], ["得分", "30", "3.47±0.82"]]
check("容差内的 3.47 不再报（旧版误报）", len(check_grim([t2c])) == 0,
      str(check_grim([t2c])))
# 整数均值（"3" 这类 0 位小数）曾因 split(".")[-1] 的写法被算成 1 位小数，
# 容差跟着放大 10 倍，整列判定变松。
t2d = [["维度", "n", "均值"], ["得分", "30", "3"]]
check("整数均值按 0 位小数判（不再放大容差）", len(check_grim([t2d])) == 0,
      str(check_grim([t2d])))
# 大 N 归零：tol = 0.5×10⁻²×200 = 1.0 → 检验恒真，应弃权而非报「通过」
t2e = [["维度", "n", "均值±标准差"], ["得分", "200", "3.47±0.82"]]
check("大 N 无信息量 → 不报（弃权口径）", len(check_grim([t2e])) == 0,
      str(check_grim([t2e])))

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

print("[11] 频数 ↔ 百分比自洽(P1-1;纯算术,随 N 变强)")
# 判据: abs(cnt / total * 100 - pct) > 0.6。诚实表误报 0.00%,
# 一格百分比改 ±3.0 后检出 100%——本批唯一「纯赚」的刀。
_pctc = "表格取证·频数与百分比不符"
_honest = [["性别", "频数", "百分比(%)"],
           ["男", "120", "33.3"], ["女", "200", "55.6"],
           ["其他", "40", "11.1"], ["合计", "360", "100.0"]]
check("诚实表不报", pct_issues := [i for i in check_pct_consistency([_honest])
                                if i["category"] == _pctc] == [], str(pct_issues))
# 只把「男」那格的 33.3 改成 30.0,其余行不动
_bad = [["性别", "频数", "百分比(%)"],
        ["男", "120", "30.0"], ["女", "200", "55.6"],
        ["其他", "40", "11.1"], ["合计", "360", "100.0"]]
_pb = [i for i in check_pct_consistency([_bad]) if i["category"] == _pctc]
check("改一格百分比即命中,且为中档(算术硬矛盾不降噪)",
      len(_pb) == 1 and _pb[0]["level"] == "mid", str(_pb))
check("证据里写出期望值与实写值", _pb and "33.3" in _pb[0]["evidence"]
      and "30" in _pb[0]["evidence"], str(_pb[:1]))
check("措辞不判造假", _pb and "不代表造假" in _pb[0]["suggestion"], str(_pb[:1]))

# 防误报 1:多选题(百分比合计 >100)每一行都对不上,但一格没改过 → 整表弃权
_multi = [["选项", "频数", "百分比(%)"],
          ["A", "120", "60.0"], ["B", "100", "50.0"], ["C", "80", "40.0"],
          ["合计", "300", "150.0"]]
check("多选同底(合计>100%)不报",
      [i for i in check_pct_consistency([_multi]) if i["category"] == _pctc] == [],
      str(check_pct_consistency([_multi])))
# 防误报 2:以「有效样本」为分母(合计 <100%)同理
_valid = [["维度", "频数", "百分比(%)"],
          ["甲", "60", "30.0"], ["乙", "60", "30.0"], ["丙", "60", "30.0"],
          ["合计", "180", "90.0"]]
check("分母=有效样本(合计<100%)不报",
      [i for i in check_pct_consistency([_valid]) if i["category"] == _pctc] == [],
      str(check_pct_consistency([_valid])))
# 防误报 3:频数列里出现负数/小数 → 这列不是频数,本刀不开
_notcnt = [["性别", "频数", "百分比(%)"],
           ["男", "-3", "33.3"], ["女", "200", "55.6"], ["其他", "40", "11.1"]]
check("频数列非整数/含负不报",
      [i for i in check_pct_consistency([_notcnt]) if i["category"] == _pctc] == [],
      str(check_pct_consistency([_notcnt])))
# 无合计行时,分母退回分项之和;此时「改了一格」仍能被多数闸门分开
_noT = [["性别", "频数", "百分比(%)"],
        ["男", "120", "30.0"], ["女", "200", "55.6"], ["其他", "40", "11.1"]]
check("无合计行也能抓改格(分母取分项之和)",
      len([i for i in check_pct_consistency([_noT]) if i["category"] == _pctc]) == 1,
      str(check_pct_consistency([_noT])))

# P1-2 口径:参评的族不得同时挂在 abstained 上(弃权 ≠ 通过,但也不能既参评又弃权)
_rep11 = audit_paper_tables([_bad])
check("F6 记入 applicable", "F6" in _rep11["applicable"], str(_rep11["applicable"]))
check("F6 参评后不再记 abstained",
      "F6" not in _rep11["abstained"], str(_rep11["abstained"]))
check("台账 reasons 写人话且含「百分比」",
      "百分比" in _rep11["applicable"].get("F6", ""),
      str(_rep11["applicable"].get("F6")))

print()
print(f"结果:{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
