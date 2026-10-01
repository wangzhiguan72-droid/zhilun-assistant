"""
v2.37 · 摘要↔正文统计量核对（rigorously 风格）契约测试
=========================================================
覆盖：
    1. find_abstract_span 的边界判定（目录页/正文页双锚、标题拆字、无锚）
    2. 定位不到时必须**如实返回 (0,0) 并给出人话 note**，绝不猜
    3. compare_abstract_vs_body：对不上的报、对得上的不报、正文缺项只记备查
    4. 一致性判据是「正文同类值里有一个对得上就算过」（宁可漏报不可误报）

语料依据：真实 10 篇硕士学位论文实测（见 extract_paper.py §3.4 注释）——
「摘要」标题几乎必然出现两次（目录页一次、正文页一次）。

运行：.venv/Scripts/python.exe -u abstract_check_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f'  << {detail}' if detail else ''))


import audit
import extract_paper as ep

# ---- 合成样本：目录页 + 正文页的典型形态（取自真实 PDF 抽取文本） ----
ABS_BODY = (
    "近年来，我国居民超重与肥胖率持续上升。本研究以 96 名大学生为对象，"
    "测量 BMI 与身体活动量。结果显示，两组差异显著（t(94) = 2.41，p = 0.018），"
    "相关系数 r = 0.32，Cronbach's α = 0.87。"
)

TOC_PAGE = "目  录\n1 绪论 ········· 1\n摘  要 ········· I\nAbstract ········· II\n"

FULL_PAPER = (
    TOC_PAGE
    + "\n摘  要\n"
    + "摘  要\n"          # 正文页那个锚（真实论文里两个都叫「摘要」）
    + ABS_BODY
    + "\n关  键  词：超重；身体活动；大学生\n"
    + "\n第一章 绪论\n研究方法采用问卷调查。三组比较用单因素方差分析"
      "（F(2, 93) = 3.10，p = 0.049），另 r = 0.32，α = 0.87，"
      "t(94) = 2.41，p = 0.018。\n"
)


print('=== 1. 摘要区间定位（目录页 vs 正文页双锚） ===')
start, end = ep.find_abstract_span(FULL_PAPER)
check("双锚时能定位到正文页那个摘要", start > 0 and end > start, f"{start},{end}")
seg = FULL_PAPER[start:end] if start else ""
check("区间落在正文页（含 96 名，不含目录页码）",
      "96 名" in seg and "·······" not in seg, seg[:60])
# 终点 = 全文第一个「关键词」锚的起点（不能只搜字——摘要正文里「相关系数」就含「关」）
_kw_pos = ep._KW_HEAD_RE.search(FULL_PAPER).start()
check("区间终点停在第一个「关键词」锚的起点", start > 0 and end == _kw_pos, f"{end} vs {_kw_pos}")
check("区间长度在守卫范围内",
      ep._ABS_MIN <= (end - start) <= ep._ABS_MAX if start else False,
      f"{end - start}")

print()
print('=== 2. 定位不到时如实返回，不猜 ===')
check("空文本 → (0,0)", ep.find_abstract_span("") == (0, 0))
check("None → (0,0)", ep.find_abstract_span(None) == (0, 0))
# 目录把「关键词」逐字拆行 —— 真实 10 篇里有 2 篇这样，实测必返回空
_BROKEN = ("关\n键\n词\n" + "摘  要\n" + ABS_BODY)
check("没有「关键词」锚 → (0,0)", ep.find_abstract_span(_BROKEN) == (0, 0))
check("摘要锚过短（目录行尾巴）→ (0,0)",
      ep.find_abstract_span("摘  要\n太短了。\n关 键 词：x\n") == (0, 0))

qs, note = ep.extract_abstract_quantities(_BROKEN)
check("未定位时统计量为空", qs == [], str(qs))
check("未定位时给出人话 note（绝不静默）",
      bool(note) and "未定位到摘要" in note, note)

print()
print('=== 3. 摘要↔正文逐条核对 ===')
abs_qs, note2 = ep.extract_abstract_quantities(FULL_PAPER)
check("能抽出摘要统计量", len(abs_qs) > 0, f"note={note2} n={len(abs_qs)}")
check("摘要里抽到了 p 值", any(q["kind"] == "p" for q in abs_qs),
      str([q["kind"] for q in abs_qs]))

body_qs = ep.extract_quantities(FULL_PAPER)
r = audit.compare_abstract_vs_body(abs_qs, body_qs)
check("核对结果含 checked / mismatches / missing_in_body 三个键",
      {"checked", "mismatches", "missing_in_body"} <= set(r), str(list(r)))

# 本样本：摘要的 p=0.018 / r=0.32 / α=0.87 在正文里都能找到 → 不该报
check("摘要值在正文对得上时**不报**不一致",
      not r["mismatches"],
      str([m["abstract_raw"] for m in r["mismatches"]]))
check("正文缺同类量只记备查，不算错",
      all(m["kind"] not in {"p", "r", "a"} for m in r["missing_in_body"]),
      str(r["missing_in_body"]))

print()
print('=== 4. 植入不一致必须抓到 ===')
# 摘要说 p = 0.001，正文只有 p = 0.62 —— 一个都对不上
abs_bad = [{"kind": "p", "value": 0.001, "raw": "p = 0.001", "context": "…"}]
body_ok = [{"kind": "p", "value": 0.62, "raw": "p = 0.62", "context": "…"}]
r2 = audit.compare_abstract_vs_body(abs_bad, body_ok)
check("对不上的统计量被报为不一致", len(r2["mismatches"]) == 1, str(r2))
if r2["mismatches"]:
    m = r2["mismatches"][0]
    check("不一致条目带中文量名", m.get("kind_cn"), str(m))
    check("不一致条目带摘录原文与正文取值",
          bool(m.get("abstract_raw")) and bool(m.get("body_values")), str(m))

# 同一量在正文出现多次：只要有一次对得上就不报（宁可漏报）
body_multi = [
    {"kind": "p", "value": 0.31, "raw": "p = 0.31", "context": "…"},
    {"kind": "p", "value": 0.041, "raw": "p = 0.041", "context": "…"},
]
r3 = audit.compare_abstract_vs_body(
    [{"kind": "p", "value": 0.041, "raw": "p = 0.041", "context": "…"}], body_multi)
check("正文多条同类值：有一条对得上就不报", not r3["mismatches"], str(r3))

# 正文里完全没有同类量 → 只备查，不报错
r4 = audit.compare_abstract_vs_body(abs_bad, [{"kind": "r", "value": 0.3, "raw": "r = 0.3"}])
check("正文无同类量 → 记 missing 而非 mismatch",
      not r4["mismatches"] and len(r4["missing_in_body"]) == 1, str(r4))
check("正文无同类量时 checked 不增加", r4["checked"] == 0, str(r4["checked"]))

print()
print('=== 5. 接入审计报告（abstract_check 必须出现） ===')
import inspect
src = inspect.getsource(audit.build_audit_report)
check("build_audit_report 里接入了摘要核对", "abstract_check" in src or "extract_abstract_quantities" in src, "")
rep_src = open(audit.__file__, encoding="utf-8").read()
check("报告返回字典含 abstract_check 键", '"abstract_check"' in rep_src, "")

print()
print('=== 汇总 ===')
print(f'结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
