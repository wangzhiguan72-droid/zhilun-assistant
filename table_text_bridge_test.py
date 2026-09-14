"""文本表格不一致 → 可比对条目（v2.16 · 纯本地，零 API）
========================================================
v2.12 把**文本形态表格**（Markdown 表 / docx 转出的管道表）的不一致只写进了
`suggestions`，没进 `comparisons`。后果有两条，都是用户能直接感知的：

  1. **审计对话里点不到** —— 只有带 `summary` 的 comparison 才能追问，
     而 summary 是 `_attach_comparison_summaries` 挂的；
  2. **协作审阅的逐条比对里看不到** —— 分享页按 `comparisons` 渲染。

可 v2.11 的 **docx 表格**两者都进。同一类问题两种待遇，用户只会以为
"文本表那条不能追问是设计如此"。

本套测试守护的契约：**报告里看到的条目 ＝ 能追问的条目 ＝ 分享出去的条目**。

同时守住两个附带修复：
  * `_KIND_CN` 补 `table_n/table_mean/table_sd` —— 否则标签显示成英文 key；
  * 第五节**不再重复**渲染表格条目（5.5d/5.5e 已有信息更全的专门表），
    但也不能因此误报"没有可对比的统计量"。

跑法：.venv/Scripts/python.exe table_text_bridge_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import audit  # noqa: E402
import review_share as rs  # noqa: E402
from extract_paper import (extract_methods, extract_quantities,  # noqa: E402
                           extract_variables)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def df_fixture():
    rng = np.random.default_rng(7)
    return pd.DataFrame({
        "成绩": rng.normal(61.5, 3.4, 60),
        "年龄": rng.normal(21.0, 2.1, 60),
    })


def run(paper, df=None):
    df = df if df is not None else df_fixture()
    claims = {
        "methods": extract_methods(paper),
        "quantities": extract_quantities(paper),
        "variables": extract_variables(paper),
        "raw_text": paper,
    }
    return audit.build_audit_report(claims, df, [])


def table_rows(rep):
    return [c for c in rep["comparisons"]
            if str(c.get("kind") or "").startswith("table_")]


PAPER_MD = """# 研究结果

本研究对被试进行了测量，描述统计如下。

| 变量 | M | SD |
| --- | --- | ---: | ---: |
| 成绩 | 60.90 | 3.42 |
| 年龄 | 20.10 | 2.10 |

采用独立样本 t 检验，t(58) = 2.31, p = 0.024。
"""

PAPER_CAP = """## 表1 描述统计

| 变量 | M | SD |
| --- | --- | ---: | ---: |
| 成绩 | 60.90 | 3.42 |
"""


# ===========================================================================
section("一、文本表不一致必须进 comparisons（核心契约）")
# ===========================================================================
rep = run(PAPER_MD)
rows = table_rows(rep)
check("文本表产生了比对条目", len(rows) >= 2, f"只有 {len(rows)} 条")
check("kind 用 table_*（与 docx 表同口径）",
      all(str(c.get("kind") or "").startswith("table_") for c in rows),
      str([c.get("kind") for c in rows]))
check("status 全是 mismatch",
      all(c.get("status") == "mismatch" for c in rows))
check("每条都有 summary（可追问）",
      all(isinstance(c.get("summary"), dict) for c in rows))
check("mean → table_mean / sd → table_sd",
      {"table_mean", "table_sd"} <= {c.get("kind") for c in rows},
      str({c.get("kind") for c in rows}))
check("带 paper / real / diff 三件套",
      all(c.get("paper") and c.get("real") and c.get("diff") for c in rows))
check("real 里带上了列名与样本量",
      all("列「" in str(c.get("real")) and "n=" in str(c.get("real"))
          for c in rows),
      str(rows[0].get("real")) if rows else "")
check("标了来源 source=table_text",
      all(c.get("source") == "table_text" for c in rows))


# ===========================================================================
section("二、kind_cn 必须是中文（别把英文 key 露给用户）")
# ===========================================================================
cns = {(c.get("summary") or {}).get("kind_cn") for c in rows}
check("没有英文 key 漏出", "table_mean" not in cns and "table_sd" not in cns,
      str(cns))
check("显示「表格均值」", "表格均值" in cns, str(cns))
check("显示「表格标准差」", "表格标准差" in cns, str(cns))
check("_KIND_CN 三种表格统计量齐全",
      audit._KIND_CN.get("table_n") and audit._KIND_CN.get("table_mean")
      and audit._KIND_CN.get("table_sd"))
check("非表格统计量未受影响",
      audit._KIND_CN.get("p") == "p 值" and audit._KIND_CN.get("t") == "t 值")


# ===========================================================================
section("三、报告正文：不重复渲染，也不误报「没有可比对」")
# ===========================================================================
md = rep["markdown"]
check("第五节没有重复的 table_* 行",
      "| table_mean |" not in md and "| table_sd |" not in md)
check("专门的表格小节仍在", "表格数字一致性核查" in md)
check("没有误报「没有可对比的统计量」", "没有可对比的统计量" not in md)

only_table = run(PAPER_CAP)
md2 = only_table["markdown"]
check("只有表格条目时也不误报", "没有可对比的统计量" not in md2, md2[-400:])
check("只有表格条目时会指路", "表格数字一致性核查" in md2)

# 无表格的论文：行为必须与改动前一致
no_table = run("采用独立样本 t 检验，t(58) = 2.31, p = 0.024。")
check("无表格时不产生 table_* 条目", table_rows(no_table) == [])
check("无表格时第五节照旧", "声称值 vs 实际值 对比" in no_table["markdown"])


# ===========================================================================
section("四、表题判定（前一行不一定是表题）")
# ===========================================================================
cases = {
    "前一行是长句正文": ("# 结果\n\n本研究对被试进行了测量，描述统计如下。\n\n"
                         "| 变量 | M | SD |\n| --- | ---: | ---: |\n"
                         "| 成绩 | 60.90 | 3.42 |\n", ""),
    "表1 描述统计": (PAPER_CAP, "「表1 描述统计」"),
    "表一 描述统计": ("## 表一 描述统计\n\n| 变量 | M | SD |\n"
                      "| --- | ---: | ---: |\n| 成绩 | 60.90 | 3.42 |\n",
                      "「表一 描述统计」"),
    "Table 1": ("## Table 1\n\n| 变量 | M | SD |\n| --- | ---: | ---: |\n"
                "| 成绩 | 60.90 | 3.42 |\n", "「Table 1」"),
    "表格前没有其它行": ("| 变量 | M | SD |\n| --- | ---: | ---: |\n"
                         "| 成绩 | 60.90 | 3.42 |\n", ""),
}
for name, (paper, want) in cases.items():
    r = run(paper)
    rr = table_rows(r)
    got = rr[0].get("paper", "") if rr else ""
    ok = (want in got) if want else ("「" not in got.split("第")[0])
    check(f"表题处理 · {name}", ok, got)
    if want:
        check(f"  └ 不出现套娃 表「{want[1:-1]}」", f"表「{want[1:-1]}」" not in got, got)


# ===========================================================================
section("五、脏输入不崩")
# ===========================================================================
bad = [
    {"mismatches": [{"kind": "mean", "label": "x", "column": "x",
                     "raw": "1", "real": None, "diff": 1, "tol": 0.1,
                     "n": 3, "table": ""}]},
    {"mismatches": [{"kind": "sd"}]},
    {"mismatches": [{"kind": "mean", "real": "不是数字", "diff": "?"}]},
    {"mismatches": ["不是字典"]},
    {"mismatches": None},
    {},
    None,
]
ok = 0
for i, tct in enumerate(bad):
    try:
        out = audit._table_text_comparisons(tct)
        ok += 1
        if tct and isinstance(tct.get("mismatches"), list) and \
                tct["mismatches"] and isinstance(tct["mismatches"][0], dict) \
                and tct["mismatches"][0].get("real") is None:
            check(f"  脏输入 {i}：real=None 的条目被跳过", out == [], str(out))
    except Exception as e:  # noqa: BLE001
        check(f"脏输入 {i} 不崩", False, f"{type(e).__name__}: {e}")
check(f"全部 {len(bad)} 组脏输入不崩", ok == len(bad), f"{ok}/{len(bad)}")

# 浮点格式化：不能出现超长小数
r = run(PAPER_MD)
check("real 的小数被限到 4 位",
      all(len(str(c.get("real")).split("（")[0].split(".")[-1]) <= 4
          for c in table_rows(r)),
      str(table_rows(r)[0].get("real")))


# ===========================================================================
section("六、下游消费：协作审阅能吃到这些条目")
# ===========================================================================
import tempfile  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="zhilun_ttb_")
_old = os.environ.get(rs.SHARE_DIR_ENV)
os.environ[rs.SHARE_DIR_ENV] = _tmp
try:
    info = rs.create_share("桥接测试", rep["markdown"],
                           comparisons=rep["comparisons"],
                           suggestions=rep.get("suggestions") or [])
    doc = rs.load_share(info["token"])
    check("分享里带着表格条目", len(doc["comparisons"]) >= len(rows))
    html = rs.render_share_html(doc)
    check("分享页渲染出逐条比对", "逐条比对" in html)
    check("分享页出现中文统计量名", "表格均值" in html, "")
    check("分享页不含英文 key 行", "| table_mean |" not in html)
    check("分享页红线文案仍在", "不代表造假" in html)
finally:
    if _old is None:
        os.environ.pop(rs.SHARE_DIR_ENV, None)
    else:
        os.environ[rs.SHARE_DIR_ENV] = _old
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)


# ===========================================================================
section("七、文案红线")
# ===========================================================================
blob = rep["markdown"] + " ".join(
    str(c.get("summary", {}).get("verdict", "")) for c in rep["comparisons"])
check("全链路不出现「造假」断言", "造假" not in blob.replace("不代表造假", "")
      .replace("不等于造假", ""))
check("表格小节有「这不等于造假」兜底", "不等于造假" in rep["markdown"])
# 注意：mismatch 的通用 hint 讲的是"常见原因"，不含"请核对"字样；
# "请核对"出现在 suggestions 里。断言要贴着**实际的**文案位置写，别想当然。
check("建议里用了「请核对」而非下结论",
      any("请核对" in s for s in (rep.get("suggestions") or [])),
      str((rep.get("suggestions") or [])[:2]))
check("比对结论不出现「错了」这类定性词",
      not any("错了" in str(c.get("summary", {}).get("verdict", ""))
              for c in rep["comparisons"]))
check("每条 hint 非空（用户能拿到排查方向）",
      all(str(c.get("summary", {}).get("hint", "")).strip()
          for c in rep["comparisons"]))


# ===========================================================================
print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
