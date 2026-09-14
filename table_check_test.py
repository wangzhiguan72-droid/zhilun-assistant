"""表格数字交叉核查测试（P3 · 纯本地，零 API）
================================================
被测模块：`table_check.py`（论文描述统计表 vs 原始数据实算值）

本模块要防的事故（按严重程度排）：
  1. **解析器静默返回 0 张表** —— 踩过一次：分隔行判定用 `set(body) <= set("-:")`，
     内部 `|` 没剥掉，字符集永远含 `|`，于是所有 Markdown 表都被当普通行跳过。
     这属于"整个功能悄悄不工作"的一级事故，必须有护栏。
  2. **误报** —— 论文老老实实四舍五入到 1 位（60.9 vs 60.93）却被报不一致。
     误报会直接毁掉工具可信度，容差必须按论文报告位数动态定。
  3. **漏报** —— 真不一致（换过数据忘更新表）必须报出来。
  4. **文案红线** —— 任何输出不得出现"造假"类肯定式指控。
  5. **崩溃** —— 脏输入 / 空输入 / 非数值单元格不得抛异常。

跑法：.venv/Scripts/python.exe table_check_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

from table_check import (  # noqa: E402
    _decimals_of, _explain_mismatch, _is_mean_header, _is_sd_header, _to_num,
    parse_markdown_tables, render_table_section, table_cross_check,
)

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


# ===========================================================================
section("1. 表格解析：Markdown 表 / 管道行 / 多表 / 脏输入")
# ===========================================================================

# 1.1 最基本的 Markdown 表 —— 历史静默失败 bug 的一级护栏
_MD = """### 表 4-1 各时间点描述统计

| 时间点 | M | SD |
| --- | ---: | ---: |
| 前测 | 60.90 | 3.42 |
| 1 个月 | 65.97 | 4.10 |
"""
_t = parse_markdown_tables(_MD)
check("1.1 能解析出 1 张 Markdown 表（分隔行判定护栏）", len(_t) == 1,
      f"实际 {len(_t)} 张 —— 若为 0，说明 _is_sep 又坏了")
check("1.1b 表头正确", _t and _t[0]["header"] == ["时间点", "M", "SD"],
      f"实际 {_t[0]['header'] if _t else None}")
check("1.1c 行数正确", _t and len(_t[0]["rows"]) == 2,
      f"实际 {len(_t[0]['rows']) if _t else None}")
check("1.1d 首行单元格正确", _t and _t[0]["rows"][0] == ["前测", "60.90", "3.42"],
      f"实际 {_t[0]['rows'][0] if _t else None}")
check("1.1e 抽到标题", _t and "表 4-1" in _t[0]["title"],
      f"实际 title={_t[0]['title'] if _t else None}")

# 1.2 无标题的表也能解析（title 允许为空）
_MD_NT = "| 组别 | 均值 | 标准差 |\n| --- | --- | --- |\n| A | 3.5 | 0.8 |\n"
_t2 = parse_markdown_tables(_MD_NT)
check("1.2 无标题表也能解析", len(_t2) == 1 and _t2[0]["title"] == "",
      f"实际 {len(_t2)} 张 / title={_t2[0]['title'] if _t2 else None}")

# 1.3 对齐符形态（:---: / ---: / :---）都要认
for _align in ("| :--- | :---: | ---: |", "|---|---|---|", "| :--- | --- | :---: |"):
    _m = f"| a | b | c |\n{_align}\n| 1 | 2 | 3 |\n"
    check(f"1.3 分隔行形态 {_align} 可识别",
          len(parse_markdown_tables(_m)) == 1)

# 1.4 一篇文章里多张表
_MULTI = (
    "| 组别 | M | SD |\n| --- | --- | --- |\n| A | 1.0 | 0.1 |\n"
    "\n正文段落，隔开两张表。\n\n"
    "| 组别 | 均值 | 标准差 |\n| --- | --- | --- |\n| B | 2.0 | 0.2 |\n"
)
check("1.4 能抽出一篇里的多张表", len(parse_markdown_tables(_MULTI)) == 2,
      f"实际 {len(parse_markdown_tables(_MULTI))}")

# 1.5 docx 转来的"管道行"形态（无分隔行）也要认
_PIPE = "组别 | n | M\nA组 | 30 | 3.47\nB组 | 28 | 4.12\n"
_t5 = parse_markdown_tables(_PIPE)
check("1.5 无分隔行的连续管道行能识别为表", len(_t5) == 1,
      f"实际 {len(_t5)}")

# 1.6 脏输入不崩
for _bad in (None, "", "   ", "没有表格的一段纯文字", "|", "|||", "| --- |"):
    try:
        parse_markdown_tables(_bad)
        _ok = True
    except Exception as e:  # noqa: BLE001
        _ok = False
        print(f"      -> {_bad!r} 抛了 {e!r}")
    check(f"1.6 脏输入不崩：{_bad!r}", _ok)

# 1.7 单个 `|` 的散句不该被当成表
check("1.7 散落的单管道行不成表",
      len(parse_markdown_tables("这行有 | 一个管道符\n另一行没有")) == 0)


# ===========================================================================
section("2. 表头识别：M / 均值 / SD / 标准差，且不误认")
# ===========================================================================

for _h in ("M", "m", "Mean", "mean", "均值", "平均值", "平均", "  M  ", "M*"):
    check(f"2.1 均值表头认得出：{_h!r}", _is_mean_header(_h))

for _h in ("SD", "sd", "S.D.", "Std", "标准差", "标准偏差"):
    check(f"2.2 标准差表头认得出：{_h!r}", _is_sd_header(_h))

# 关键的"别乱认"：n / p / F / t 不是均值也不是标准差
for _h in ("n", "N", "p", "F", "t", "df", "序号", "组别", "样本量", "95%CI"):
    check(f"2.3 {_h!r} 既不算均值也不算标准差",
          not _is_mean_header(_h) and not _is_sd_header(_h),
          f"mean={_is_mean_header(_h)} sd={_is_sd_header(_h)}")

# "MeanDiff" / "SD_diff" 这类衍生列头暂不纳入（避免和差值列混淆）
check("2.4 'Cohen d' 不被认作均值/标准差",
      not _is_mean_header("Cohen d") and not _is_sd_header("Cohen d"))


# ===========================================================================
section("3. 单元格数字解析 与 报告位数")
# ===========================================================================

for _cell, _want in [("3.47", 3.47), ("60.90", 60.9), ("-0.52", -0.52),
                     ("1,234.5", 1234.5), ("0", 0.0), ("100", 100.0)]:
    check(f"3.1 数值解析 {_cell!r} -> {_want}",
          _to_num(_cell) == _want, f"实际 {_to_num(_cell)}")

check("3.2 百分号折算：50% -> 0.5", _to_num("50%") == 0.5)
check("3.2b 百分号折算：12.5% -> 0.125", _to_num("12.5%") == 0.125)

for _cell in ("—", "-", "n/a", "", "  ", "3.4.5", "abc", "±0.5", "3.47±0.52"):
    check(f"3.3 非纯数值不解析：{_cell!r}", _to_num(_cell) is None,
          f"实际 {_to_num(_cell)}")

# 报告位数（决定容差）—— 这条直接决定"会不会冤枉老实人"
for _raw, _want in [("60.9", 1), ("60.90", 2), ("60.901", 3), ("61", 0),
                    ("3.42%", 2), ("1,234.50", 2), (" 5.0 ", 1)]:
    check(f"3.4 报告位数 {_raw!r} -> {_want}",
          _decimals_of(_raw) == _want, f"实际 {_decimals_of(_raw)}")


# ===========================================================================
section("4. 不该报的绝不报（防误报 —— 最高优先级）")
# ===========================================================================

df_exact = pd.DataFrame({
    "前测": [60.0, 61.0, 62.0, 63.0],
    "后测": [70.0, 71.0, 72.0, 73.0],
})
# 这四个数的 mean=61.5 / sd=1.2909944487...
_md_exact = (
    "| 时间点 | M | SD |\n| --- | --- | --- |\n"
    "| 前测 | 61.5 | 1.2910 |\n"
)
_r = table_cross_check(_md_exact, df_exact)
check("4.1 精确值不误报（M=61.5 / SD=1.2910）", len(_r["mismatches"]) == 0,
      f"误报了：{_r['mismatches']}")

# 四舍五入宽容：论文写 1 位小数，实算 61.47 -> 不该报
df_round = pd.DataFrame({"A": [61.0, 61.5, 61.9]})
_mean = df_round["A"].mean()   # 61.4666...
_md_round = f"| 组别 | M |\n| --- | --- |\n| A | {_mean:.1f} |\n"
_r2 = table_cross_check(_md_round, df_round)
check(f"4.2 四舍五入到 1 位不误报（实算 {_mean:.4f}）",
      len(_r2["mismatches"]) == 0, f"误报了：{_r2['mismatches']}")

# 2 位小数同理
_md_round2 = f"| 组别 | M |\n| --- | --- |\n| A | {_mean:.2f} |\n"
_r2b = table_cross_check(_md_round2, df_round)
check(f"4.3 四舍五入到 2 位不误报（实算 {_mean:.4f}）",
      len(_r2b["mismatches"]) == 0, f"误报了：{_r2b['mismatches']}")

# 边界：刚好卡在容差边缘上（0.5 * 10^-1 = 0.05），小于等于不报
# 实算 61.4666，写 61.5 → diff=0.0333 < 0.05 → 放行（上面已验）
# 写 61.52 → diff=0.0533 > 0.05 → 该报
_md_edge = "| 组别 | M |\n| --- | --- |\n| A | 61.52 |\n"
_r_edge = table_cross_check(_md_edge, df_round)
check("4.4 超出容差该报（61.52 vs 61.4666，tol=0.005）",
      len(_r_edge["mismatches"]) == 1, f"实际 {len(_r_edge['mismatches'])}")

# 行标签对不上的行 -> 跳过，不猜、不报
_md_nomatch = "| 组别 | M |\n| --- | --- |\n| 这个组不存在 | 99.9 |\n"
_r3 = table_cross_check(_md_nomatch, df_round)
check("4.5 行标签对不上 -> 跳过而非误报",
      len(_r3["mismatches"]) == 0 and len(_r3["skipped"]) == 1,
      f"mm={len(_r3['mismatches'])} skipped={len(_r3['skipped'])}")

# 非统计表（文献综述表）不该被核查
_md_lit = (
    "| 序号 | 作者 | 年份 | 来源 |\n| --- | --- | --- | --- |\n"
    "| 1 | 张三 | 2020 | 心理学报 |\n"
)
_r4 = table_cross_check(_md_lit, df_round)
check("4.6 文献表不算统计表（0 张）", _r4["tables"] == 0,
      f"实际 {_r4['tables']}")

# 论文里压根没有表 -> 优雅说明，不报错
_r5 = table_cross_check("这是一篇没有任何表格的论文。", df_round)
check("4.7 无数值表 -> tables=0 且不崩溃",
      _r5["tables"] == 0 and not _r5["mismatches"])

# 空数据 -> 明确说明
_r6 = table_cross_check(_md_round, pd.DataFrame())
check("4.8 空 df -> 明确说明无法核查",
      "没有原始数据" in _r6["note"], f"note={_r6['note']!r}")


# ===========================================================================
section("5. 该报的必须报（漏报 —— 真事故）")
# ===========================================================================

df_real = pd.DataFrame({"前测": [62, 58, 65, 60], "后测": [80, 76, 84, 78]})
_mean_real = df_real["前测"].mean()   # 61.25
_sd_real = df_real["前测"].std()      # 2.9861...

# 5.1 均值明显不符（换过数据没更新表）
_md_wrong = "| 时间点 | M |\n| --- | --- |\n| 前测 | 55.00 |\n"
_rw = table_cross_check(_md_wrong, df_real)
check("5.1 均值明显不符必须报出", len(_rw["mismatches"]) == 1,
      f"实际 {len(_rw['mismatches'])}")
if _rw["mismatches"]:
    _m = _rw["mismatches"][0]
    check("5.1b mismatch 字段完整（real/diff/tol/n）",
          abs(_m["real"] - round(_mean_real, 6)) < 1e-6
          and _m["kind"] == "mean" and _m["n"] == 4,
          f"实际 {_m}")
    check("5.1c diff 计算正确",
          abs(_m["diff"] - (abs(_mean_real - 55.0))) < 1e-6,
          f"实际 diff={_m['diff']}")

# 5.2 标准差明显不符
_md_sd_wrong = f"| 时间点 | SD |\n| --- | --- |\n| 前测 | 9.99 |\n"
_rs = table_cross_check(_md_sd_wrong, df_real)
check("5.2 标准差明显不符必须报出", len(_rs["mismatches"]) == 1,
      f"实际 {len(_rs['mismatches'])}")
if _rs["mismatches"]:
    check("5.2b kind 标为 sd",
          _rs["mismatches"][0]["kind"] == "sd",
          f"实际 {_rs['mismatches'][0]['kind']}")

# 5.3 均值 + 标准差 同时写、同时错 → 报 2 条
_md_both = "| 时间点 | M | SD |\n| --- | --- | --- |\n| 前测 | 55.00 | 9.99 |\n"
_rb = table_cross_check(_md_both, df_real)
check("5.3 均值与标准差同时错 -> 报 2 条", len(_rb["mismatches"]) == 2,
      f"实际 {len(_rb['mismatches'])}")
check("5.3b checked 计数正确（2 格）", _rb["checked"] == 2,
      f"实际 {_rb['checked']}")

# 5.4 多行多表全部核查（不漏）
_md_many = (
    "| 时间点 | M | SD |\n| --- | --- | --- |\n"
    "| 前测 | 55.00 | 9.99 |\n| 后测 | 11.11 | 8.88 |\n"
)
_rm = table_cross_check(_md_many, df_real)
check("5.4 多行全部核查（4 格）", _rm["checked"] == 4,
      f"实际 {_rm['checked']}")
check("5.4b 多行全部报出（4 处不一致）", len(_rm["mismatches"]) == 4,
      f"实际 {len(_rm['mismatches'])}")

# 5.5 只列有效数值不少于 2 个才比（<2 跳过，避免拿单值比）
df_one = pd.DataFrame({"A": [5.0]})
_r7 = table_cross_check("| 组别 | M |\n| --- | --- |\n| A | 1.0 |\n", df_one)
check("5.5 有效值 <2 个 -> 跳过",
      not _r7["mismatches"] and len(_r7["skipped"]) == 1,
      f"mm={len(_r7['mismatches'])} skipped={len(_r7['skipped'])}")


# ===========================================================================
section("6. 合并单元格 3.47±0.52 的支持")
# ===========================================================================

df_merge = pd.DataFrame({
    "A组": [3.0, 3.5, 4.0, 3.5],
})
_m_mean = df_merge["A组"].mean()   # 3.5
_m_sd = df_merge["A组"].std()      # 0.4082...
_md_merge = f"| 组别 | 得分 |\n| --- | --- |\n| A组 | {_m_mean:.2f}±{_m_sd:.2f} |\n"
# 表头没有 M/SD 关键词 -> 本该不算统计表；这里验证的是"若被当均值列则能拆"
check("6.1 无 M/SD 表头的合并单元格表不算统计表",
      table_cross_check(_md_merge, df_merge)["tables"] == 0)

# 带 M 表头时，合并格应能取到第 1 个数作均值
_md_merge2 = (
    "| 组别 | M |\n| --- | --- |\n"
    f"| A组 | {_m_mean:.2f}±{_m_sd:.2f} |\n"
)
_r8 = table_cross_check(_md_merge2, df_merge)
check("6.2 表头含 M 时，3.50±0.41 能取出均值 3.50 并判一致",
      _r8["checked"] >= 1 and not _r8["mismatches"],
      f"checked={_r8['checked']} mm={_r8['mismatches']}")


# ===========================================================================
section("7. 文案红线：只报可疑，永不判造假")
# ===========================================================================

_ex = _explain_mismatch("mean", 55.0, 61.25, 4, 6.25, 0.005)
check("7.1 解释含「请核对」", "请核对" in _ex, f"实际 {_ex!r}")
check("7.2 解释明确声明这不代表造假",
      ("不代表造假" in _ex) or ("不等于造假" in _ex), f"实际 {_ex!r}")

_sd_ex = _explain_mismatch("sd", 9.99, 2.986, 4, 7.004, 0.005)
check("7.3 sd 版解释也含请核对", "请核对" in _sd_ex)

# 渲染出的报告段落同样不得含否定式指控
_md_x = "| 时间点 | M | SD |\n| --- | --- | --- |\n| 前测 | 55.00 | 9.99 |\n"
_rc = table_cross_check(_md_x, df_real)
_rendered = "\n".join(render_table_section(_rc))
_banned = ["造假", "篡改", "伪造", "学术不端", "作弊"]
_hits = [b for b in _banned if b in _rendered.replace("不等于造假", "").replace("不代表造假", "")]
check("7.4 渲染段落不含造假指控词",
      not _hits, f"命中 {_hits}")
check("7.5 渲染段落含「这不等于造假」声明",
      "不等于造假" in _rendered or "不代表造假" in _rendered)

# 全部 explain 文案统一扫描
_all_reasons = [m["explain"] for m in _rc["mismatches"]]
check("7.6 所有 explain 均含「请核对」",
      all("请核对" in r for r in _all_reasons),
      f"实际 {_all_reasons}")
check("7.7 所有 explain 均声明不代表造假",
      not any("造假" in r for r in _all_reasons
              if "不代表造假" not in r and "不等于造假" not in r),
      f"实际 {_all_reasons}")


# ===========================================================================
section("8. 渲染：有表/无表/全一致/有错 四种形态")
# ===========================================================================

check("8.1 没解析到表 -> 渲染为空（不进报告）",
      render_table_section({"tables": 0, "checked": 0, "mismatches": []}) == [])

check("8.2 空 dict -> 渲染为空", render_table_section({}) == [])
check("8.3 None -> 渲染为空", render_table_section(None) == [])

_ok_sec = render_table_section(
    {"tables": 1, "checked": 4, "mismatches": [], "skipped": []})
_ok_txt = "\n".join(_ok_sec)
check("8.4 全部一致 -> 出现 ✅ 且无表格",
      "✅" in _ok_txt and "|" not in _ok_txt, f"实际 {_ok_txt!r}")

_bad_sec = render_table_section(_rc)
_bad_txt = "\n".join(_bad_sec)
check("8.5 有不一致 -> 渲染出 Markdown 表格",
      "| 表格 | 行 | 项目 |" in _bad_txt, f"实际 {_bad_txt[:200]!r}")
check("8.5b 表头列数 = 分隔行列数（Markdown 表格合法性）",
      _bad_txt.count("| --- |") == 1
      or all(len(l.split("|")) == len(_bad_sec[2].split("|"))
             for l in _bad_sec[2:12] if l.startswith("|")),
      "列数不齐会渲染错位")

check("8.6 有跳过项时能说明原因（未能比对 / 跳过 二者其一）",
      ("未能比对" in "\n".join(render_table_section(_r3))
       or "跳过" in "\n".join(render_table_section(_r3))),
      f"实际 {render_table_section(_r3)!r}")

check("8.6b note 字段同样不得假装一致",
      "一致" not in _r3["note"] or "未能" in _r3["note"],
      f"note={_r3['note']!r}")

# 8.7 关键：全一致但一格都没比成（行标签全对不上）—— 不能说"核查通过"
_r_allskip = table_cross_check(
    "| 时间点 | M | SD |\n| --- | --- | --- |\n| 甲组 | 1.00 | 0.10 |\n",
    df_real)
_alls_txt = "\n".join(render_table_section(_r_allskip))
check("8.7 全跳过时不得假装核查通过（出现「未能比对」）",
      "未能比对" in _alls_txt and "实算值一致" not in _alls_txt,
      f"实际 {_alls_txt!r}")

# 8.8 有错 + 有跳过的混合形态，两个提示都要在
_mix = table_cross_check(
    "| 时间点 | M |\n| --- | --- |\n| 前测 | 55.00 |\n| 不存在组 | 1.00 |\n",
    df_real)
_mix_txt = "\n".join(render_table_section(_mix))
check("8.8 有错+有跳过：两条提示并存",
      "不一致" in _mix_txt and "跳过" in _mix_txt, f"实际 {_mix_txt!r}")


# ===========================================================================
section("9. 端到端：真实示例文件（rm_anova）")
# ===========================================================================

_base = os.path.dirname(os.path.abspath(__file__))
_paper = os.path.join(_base, "examples", "rm_anova_paper.md")
_data = os.path.join(_base, "examples", "rm_anova_data.csv")
if os.path.exists(_paper) and os.path.exists(_data):
    with open(_paper, encoding="utf-8") as f:
        _text = f.read()
    _df = pd.read_csv(_data)
    _e2e = table_cross_check(_text, _df)
    check("9.1 示例论文解析出 1 张统计表", _e2e["tables"] == 1,
          f"实际 {_e2e['tables']}")
    check("9.2 端到端不崩溃且有结论", isinstance(_e2e["note"], str)
          and _e2e["note"], f"note={_e2e['note']!r}")
    check("9.3 示例表里 4 个时间点都核查了（≥8 格：4×M + 4×SD）",
          _e2e["checked"] >= 8, f"实际 {_e2e['checked']}")
    check("9.4 每个 mismatch 都带 explain 与 n",
          all(m.get("explain") and m.get("n") for m in _e2e["mismatches"]),
          f"实际 {_e2e['mismatches'][:1]}")
    print(f"      note: {_e2e['note']}")
    for _m in _e2e["mismatches"][:3]:
        print(f"      - {_m['label']} {_m['kind']}: 表 {_m['raw']} vs 算 "
              f"{_m['real']:.4f} (diff {_m['diff']:.4f})")

    _sec = render_table_section(_e2e)
    check("9.5 端到端能渲染成报告段落", bool(_sec))
else:
    check("9.0 示例文件存在", False, f"缺 {_paper} 或 {_data}")


# ===========================================================================
print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
