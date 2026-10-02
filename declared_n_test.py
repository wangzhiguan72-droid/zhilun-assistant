"""v2.46 · 正文样本量 n ↔ 数据行数 / 表格 n 契约测试（A 档②）
==============================================================
覆盖：
    1. extract_paper 的 n 类提取：**只认带样本量标记的**，
       年份 / 百分比 / 章节号 / 题数一律不认（n 是最易误报的数字类）
    2. table_check.claimed_n：表格里的 n 列照采，**先采再做列匹配**
       （行标签对不上数据列时也要采到）
    3. compare_declared_n 的三条判据：
       - 数据行数 == 正文任一 n        → 一致
       - 正文主口径 n 出现在表格里      → 一致（佐证）
       - 都对不上                      → 报 mismatch，且给出 diff
    4. 正文根本没写 n 时如实说"不核对"，绝不默认报一致

语料依据：examples/sample_regression_paper.md 第 5 行
「在某城市抽取 120 名劳动力」+ sample_regression_data.csv（恰好 120 行）。

运行：.venv/Scripts/python.exe -u declared_n_test.py
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
import table_check as tc


def _ns(text):
    """只取 n 类，返回 [(value, raw)]。"""
    return [(q["value"], q["raw"]) for q in ep.extract_quantities(text)
            if q["kind"] == "n"]


print("=== 1. n 类提取：该认的认 ===")
for _text, _want in [
    ("本研究在某城市抽取 120 名劳动力。", 120),
    ("有效样本 n = 118。", 118),
    ("样本量=250，回收有效 240 份。", None),        # 两个 n，下面单独查
    ("共 96 名大学生参与实验。", 96),
    ("纳入 320 例患者。", 320),
]:
    _got = [v for v, _ in _ns(_text)]
    if _want is None:
        check(f"多 n 并存都能提出：{_text[:16]}…", len(_got) >= 2, str(_got))
    else:
        check(f"识别出 n={_want}：{_text[:16]}…", _want in _got, str(_got))

print()
print("=== 2. n 类提取：不该认的一个都不认（防误报是这条链的命门）===")
for _text in [
    "本研究于 2024 年开展问卷调查。",
    "有效回收率 92.5%，占全部样本的 60%。",
    "第 3 章共 20 道题目，采用 5 点计分。",
    "年龄均值 23.4 岁，标准差 3.2。",
    "样本量为 1 人时无法计算。",          # 1 位数 → 不是样本量
]:
    _got = _ns(_text)
    check(f"不误报：{_text[:18]}…", not _got, str(_got))

print()
print("=== 3. claimed_n：表格 n 列照采，且不要求行标签能对上数据列 ===")
_PAPER = """### 表 4-1 各年级样本分布

| 年级 | n | M | SD |
| --- | ---: | ---: | ---: |
| 大一 | 30 | 3.47 | 0.52 |
| 大二 | 32 | 3.61 | 0.48 |
"""
import pandas as pd
_df = pd.DataFrame({"成绩": [1.0, 2.0, 3.0]})   # 行标签「大一/大二」对不上「成绩」
_tc = tc.table_cross_check(_PAPER, _df)
check("采到 2 个表格 n", len(_tc["claimed_n"]) == 2, str(_tc["claimed_n"]))
check("n 值正确（30 / 32）",
      sorted(x["n"] for x in _tc["claimed_n"]) == [30, 32], str(_tc["claimed_n"]))
check("行标签对不上数据列时**仍**采到 n（先采后 match）",
      not _tc["checked"] and len(_tc["claimed_n"]) == 2,
      f"checked={_tc['checked']} claimed={len(_tc['claimed_n'])}")

print()
print("=== 4. compare_declared_n 判据 ===")
# 4a 正文 n == 数据行数 → 一致
_r = audit.compare_declared_n(_qs := ep.extract_quantities("抽取 120 名劳动力"),
                              120, None)
check("正文 n=120 / 数据 120 行 → 一致", _r["mismatch"] is None and _r["checked"] == 1, str(_r))

# 4b 行数对不上，但表格里出现过同样的 n → 佐证一致
_r = audit.compare_declared_n(ep.extract_quantities("抽取 120 名劳动力"),
                              118, [{"label": "合计", "n": 120, "table": "表1"}])
check("行数 118 但表格 n=120 → 一致（表格作佐证）",
      _r["mismatch"] is None, str(_r))

# 4c 都对不上 → 报，且 diff 正确
_r = audit.compare_declared_n(ep.extract_quantities("抽取 120 名劳动力"),
                              110, [{"label": "合计", "n": 118, "table": "表1"}])
check("正文 120 / 数据 110 → 报不一致", bool(_r["mismatch"]), str(_r))
check("mismatch 的 diff == 10",
      (_r["mismatch"] or {}).get("diff") == 10, str(_r.get("mismatch")))
check("mismatch 里带上表格 n 的取值 [118]",
      (_r["mismatch"] or {}).get("table_n") == [118], str(_r.get("mismatch")))

# 4d 正文里 n 出现多次但**其中任意一个**等于行数 → 一致（口径不同不算错）
_r = audit.compare_declared_n(ep.extract_quantities("发放 150 份，回收有效 130 份"),
                              130, None)
check("正文有 150/130、数据 130 行 → 一致", _r["mismatch"] is None, str(_r))

# 4e 正文没写 n → 如实说不核对，不能默认报一致
_r = audit.compare_declared_n(ep.extract_quantities("本研究采用问卷调查法。"),
                              120, None)
check("正文没写 n → checked=0 且有 note",
      _r["checked"] == 0 and bool(_r["note"]), str(_r))
check("没写 n 时 mismatch 为 None（不冤枉）", _r["mismatch"] is None, str(_r))

print()
print("=== 5. n 不污染既有刀：GRIM / GRIMMER 只吃 mean/sd ===")
import inspect
_src = inspect.getsource(audit)
check("grim/grimmer 的取数仍按 kind 过滤 mean/sd（未因新增 n 类而松动）",
      'kind"} == "mean"' in _src or "== \"mean\"" in _src, "")

print()
print("=== 6. 真跑一遍 build_audit_report，确认 declared_n 落到返回字典里 ===")
_df2 = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "examples", "sample_regression_data.csv"))
_claims = {
    "methods": [], "variables": [],
    "quantities": ep.extract_quantities("抽取 120 名劳动力"),
    "raw_text": "抽取 120 名劳动力",
}
_rep = audit.build_audit_report(_claims, _df2, [])
check("返回字典含 declared_n 键", "declared_n" in _rep, str(list(_rep.keys())))
check("declared_n 判为一致（数据正好 120 行）",
      (_rep.get("declared_n") or {}).get("mismatch") is None
      and (_rep.get("declared_n") or {}).get("checked") == 1,
      str(_rep.get("declared_n")))
check("报告的 Markdown 里出现「样本量核对」小节",
      "样本量核对" in (_rep.get("markdown") or ""),
      "" if _rep.get("markdown") else "（无 markdown 字段）")

print()
print('=== 汇总 ===')
print(f'结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
