"""v2.0 · 数据体检测试（产品入口 · datacheck）
================================================================
测 `datacheck` 的 9 个检测器 + GRIM 工具 + 聚合入口 + HTTP 接口。

测试重点（双边思路，与 explain_test / registry_test 一致）：
  1. **该报的必须报**：每类问题造一个正例，断言命中且证据里含正确行号
  2. **不该报的绝不报**：每类问题配一个"干净数据"反例，断言 0 issue
     —— 这是体检功能的生命线：乱扣帽子比漏报更伤用户信任
  3. **永不崩**：空表 / 单行 / 全空表 / 非数值列混入，都不能抛异常
  4. **排序与裁决**：high → mid → low；verdict 文案随严重度变化
  5. **闭环**：/api/datacheck 端到端可用

跑法：.venv/Scripts/python.exe datacheck_test.py
"""
import numpy as np
import pandas as pd

import datacheck as DC

PASS = 0
FAIL = 0


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


def _titles(issues):
    return " ｜ ".join(i["title"] for i in issues)


def _hit(issues, *keywords):
    """是否存在某条 issue，其 title+evidence 同时包含全部关键词。"""
    for it in issues:
        blob = it["title"] + it["evidence"]
        if all(k in blob for k in keywords):
            return it
    return None


def _rng(n, seed=7):
    return np.random.default_rng(seed).normal(60, 8, n).round(1)


# ===========================================================================
section("1. 合计 ≠ 分项之和（一致性）")
# ===========================================================================
df_bad = pd.DataFrame({
    "分项1": [10.0, 20, 30, 40, 50, 60],
    "分项2": [10.0, 20, 30, 40, 50, 60],
    "总分": [20.0, 40, 60, 80, 100, 125],   # 最后一行少 5
})
iss = DC.check_column_sum(df_bad)
check("对不上 → 报 1 条", len(iss) == 1, f"got={_titles(iss)}")
it = _hit(iss, "总分", "第 7 行")
check("证据定位到 Excel 第 7 行（第 6 条数据）", it is not None)
check("严重度 = high", bool(it) and it["level"] == DC.LEVEL_HIGH)
check("恢复一致 → 不报", len(DC.check_column_sum(df_bad.assign(总分=[20, 40, 60, 80, 100, 120]))) == 0)

df_unrel = pd.DataFrame({"身高": [170, 172, 165, 180, 168], "体重": [60, 70, 55, 80, 66]})
check("无可加总关系 → 不报", len(DC.check_column_sum(df_unrel)) == 0)

# ===========================================================================
section("2. 取值越界（一致性）")
# ===========================================================================
df_score = pd.DataFrame({"性别": ["男", "女", "男", "女", "男", "女"],
                         "成绩": [88.0, 92, 105, 76, 81, 90]})
iss = DC.check_value_range(df_score)
it = _hit(iss, "成绩", "第 4 行")
check("成绩 105 → 报且定位第 4 行", it is not None, f"got={_titles(iss)}")
check("严重度 = high", bool(it) and it["level"] == DC.LEVEL_HIGH)
check("成绩全在 0-100 → 不报",
      len(DC.check_value_range(df_score.assign(成绩=[88, 92, 95, 76, 81, 90]))) == 0)

df_age = pd.DataFrame({"年龄": [19.0, 20, 21, 22, 23, -3]})
check("年龄 -3 → 报", _hit(DC.check_value_range(df_age), "年龄") is not None)
check("年龄正常 → 不报",
      len(DC.check_value_range(pd.DataFrame({"年龄": [19, 20, 21, 22, 23, 24]}))) == 0)

df_likert = pd.DataFrame({"q1": [1, 2, 3, 4, 5, 4, 3, 2, 5, 1],
                          "q2": [2, 3, 4, 5, 1, 2, 3, 4, 5, 2],
                          "q3": [3, 4, 5, 1, 2, 3, 4, 5, 1, 3],
                          "q4": [4, 5, 1, 2, 3, 4, 5, 1, 2, 99]})
check("Likert 出现 99 → 报",
      _hit(DC.check_value_range(df_likert), "q4") is not None)
check("Likert 全在 1-7 → 不报",
      len(DC.check_value_range(df_likert.assign(q4=[4, 5, 1, 2, 3, 4, 5, 1, 2, 4]))) == 0)

# ===========================================================================
section("3. 计数列混入小数（一致性）")
# ===========================================================================
df_cnt = pd.DataFrame({"班级": ["A", "B", "C", "D", "E", "F"],
                       "人数": [30, 32, 12.5, 28, 25, 31]})
check("人数 12.5 → 报", _hit(DC.check_integer_decimals(df_cnt), "人数") is not None)
check("人数全整数 → 不报",
      len(DC.check_integer_decimals(df_cnt.assign(人数=[30, 32, 12, 28, 25, 31]))) == 0)
df_score_dec = pd.DataFrame({"成绩": [88.5, 92.0, 95.5, 76.0, 81.5, 90.0]})
check("成绩带小数 → 不报（非计数列）", len(DC.check_integer_decimals(df_score_dec)) == 0)

# ===========================================================================
section("4. 重复行（重复）")
# ===========================================================================
df_dup = pd.DataFrame({"学号": [1, 2, 3, 4, 5, 1],
                       "成绩": [80.5, 90.2, 85.7, 70.1, 66.3, 80.5],
                       "性别": ["男", "女", "男", "女", "男", "男"]})
iss_dup = DC.check_duplicate_rows(df_dup)
check("完全重复行 → 报 mid", any(i["level"] == DC.LEVEL_MID for i in iss_dup), f"got={_titles(iss_dup)}")
check("定位到第 2、7 行", _hit(iss_dup, "第 2 行") is not None, f"got={_titles(iss_dup)}")

# 30 行、4 个较细粒度字段，只让第 2 条与第 30 条 body 撞车 → 必是真重复
_r = np.random.default_rng(5)
_n = 30
df_same_body = pd.DataFrame({
    "学号": list(range(1, _n + 1)),
    "成绩": _r.normal(80, 7, _n).round(2),
    "学习时长": _r.normal(12, 3, _n).round(2),
    "年龄": _r.integers(18, 26, _n),
    "性别": _r.choice(["男", "女"], _n),
})
df_same_body.loc[29, ["成绩", "学习时长", "年龄", "性别"]] = \
    df_same_body.loc[1, ["成绩", "学习时长", "年龄", "性别"]].values
check("仅编号不同（4 字段全同） → 报",
      any("去掉编号列" in i["title"] for i in DC.check_duplicate_rows(df_same_body)),
      f"got={_titles(DC.check_duplicate_rows(df_same_body))}")

check("全唯一 → 不报",
      len(DC.check_duplicate_rows(pd.DataFrame({"学号": [1, 2, 3, 4], "成绩": [80, 90, 85, 70]}))) == 0)

# 误报护栏：粗粒度窄表撞车是必然的，不许报（真实踩过的坑）
df_coarse = pd.DataFrame({
    "student_id": list(range(1, 61)),
    "gender": (["男", "女"] * 30),
    "teaching_method": (["传统讲授", "互动教学", "翻转课堂"] * 20),
    "score": ([72, 75, 70, 74, 71] * 12),
})
check("粗粒度窄表（60 行×3 列）撞车 → 不报（防误报护栏）",
      len(DC.check_duplicate_rows(df_coarse)) == 0,
      f"got={_titles(DC.check_duplicate_rows(df_coarse))}")

# ===========================================================================
section("5. 前后测差值过于规律（规律）")
# ===========================================================================
pre = [55.0, 62, 48, 70, 58, 66, 51, 73]
df_diff = pd.DataFrame({"成绩_前测": pre, "成绩_后测": [v + 5 for v in pre]})
iss = DC.check_diff_regularity(df_diff)
check("差值恒为 +5 → 报 mid", len(iss) == 1 and iss[0]["level"] == DC.LEVEL_MID, f"got={_titles(iss)}")
check("证据含「+5」", bool(iss) and "+5" in iss[0]["title"])
df_diff_ok = pd.DataFrame({"成绩_前测": pre, "成绩_后测": [v + d for v, d in zip(pre, [3, -2, 7, 0, 5, -1, 4, 2])]})
check("差值各异 → 不报", len(DC.check_diff_regularity(df_diff_ok)) == 0)
df_likert_diff = pd.DataFrame({"焦虑_前": [1, 2, 3, 4, 5, 2, 3, 4],
                               "焦虑_后": [2, 3, 4, 5, 5, 3, 4, 5]})
check("Likert 前后测（均匀 +1）→ 不报（防误报护栏）",
      len(DC.check_diff_regularity(df_likert_diff)) == 0)

# ===========================================================================
section("6. 常数列（规律）")
# ===========================================================================
df_const = pd.DataFrame({"成绩": _rng(6).tolist(), "年级": [3, 3, 3, 3, 3, 3]})
check("整列常数 → 报 mid", _hit(DC.check_low_variance(df_const), "年级") is not None)
check("非常数 → 不报", len(DC.check_low_variance(df_const.drop(columns=["年级"]))) == 0)

# ===========================================================================
section("7. 反向题疑似漏反向（量表）")
# ===========================================================================
q = [1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 2, 3]
df_rev = pd.DataFrame({
    "q1": q, "q2": [(v % 5) + 1 for v in q], "q3": [((v + 1) % 5) + 1 for v in q],
    "q4": [6 - ((v % 5) + 1) for v in q],   # 反向题：与其它题方向相反
})
check("反向题未反向 → 报 mid",
      _hit(DC.check_reverse_scoring(df_rev), "q4") is not None,
      f"got={_titles(DC.check_reverse_scoring(df_rev))}")
df_ok = pd.DataFrame({
    "q1": [3, 2, 4, 5, 1, 3, 2, 4, 5, 1, 2, 4],
    "q2": [3, 2, 4, 5, 1, 3, 2, 4, 5, 1, 2, 4],
    "q3": [4, 3, 5, 5, 2, 4, 3, 5, 5, 2, 3, 5],
    "q4": [2, 1, 3, 4, 1, 2, 1, 3, 4, 1, 1, 3],
})
check("方向一致 → 不报", len(DC.check_reverse_scoring(df_ok)) == 0)

# ===========================================================================
section("8. 直线作答（量表）")
# ===========================================================================
rows = [[3, 3, 3, 3]] * 4 + [[1, 2, 3, 4], [4, 3, 2, 1], [2, 3, 4, 5], [5, 4, 3, 2],
                             [1, 1, 2, 2], [3, 4, 4, 5]]
df_str = pd.DataFrame(rows, columns=["q1", "q2", "q3", "q4"])
check("4/10 直线作答 → 报 mid",
      _hit(DC.check_straightlining(df_str), "直线作答") is not None,
      f"got={_titles(DC.check_straightlining(df_str))}")
df_nostr = pd.DataFrame([[1, 2, 3, 4], [4, 3, 2, 1], [2, 3, 4, 5], [5, 4, 3, 2],
                         [1, 1, 2, 2], [3, 4, 4, 5], [5, 5, 1, 2], [2, 1, 5, 3],
                         [4, 2, 3, 1], [3, 1, 4, 2]], columns=["q1", "q2", "q3", "q4"])
check("无人直线作答 → 不报", len(DC.check_straightlining(df_nostr)) == 0)

# ===========================================================================
section("9. 缺失模式（缺失）")
# ===========================================================================
df_miss = pd.DataFrame({
    "学号": [1, 2, 3, 4, 5, 6, 7, 8],
    "成绩": [80, 90, np.nan, 70, np.nan, 88, 92, 77],
    "备注": [np.nan] * 8,
})
iss = DC.check_missing_pattern(df_miss)
check("整列全空 → 报 high", any(i["level"] == DC.LEVEL_HIGH and "备注" in "".join(i["columns"]) for i in iss),
      f"got={_titles(iss)}")
check("高缺失列（成绩 25% 不到阈值）不误报 30% 档",
      not _hit(iss, "缺失率超过 30%", "成绩"))
df_clean = pd.DataFrame({"学号": list(range(8)), "成绩": [80, 90, 85, 70, 75, 88, 92, 77]})
check("完整数据 → 不报", len(DC.check_missing_pattern(df_clean)) == 0)

# ===========================================================================
section("10. GRIM 工具（供论文交叉核查）")
# ===========================================================================
check("grim_check(3.47, 30) → 不可能（False）", DC.grim_check(3.47, 30) is False)
check("grim_check(3.5, 30) → 可能（True）", DC.grim_check(3.5, 30) is True)
check("grim_check(2.0, 25) → 可能（True）", DC.grim_check(2.0, 25) is True)
check("grim_check(n=0) → 放行（True）", DC.grim_check(3.47, 0) is True)

# ===========================================================================
section("10a. 本福特定律（学术级取证 · 纯 numpy）")
# ===========================================================================
# 生命线：Benford 前提严格（正数 / n≥100 / 跨 2 个数量级 / 取值分散 / 非编号时间量表价格），
# 不满足就**整列跳过**。误报比漏报伤十倍 —— 这节一半的断言在测"不许报"。

_bf_rng = np.random.default_rng(11)
# 正例：人造"过于均匀"的首位分布（真实自然数据必然偏 1、2）
_bf_fake = []
for d in range(1, 10):
    _bf_fake.extend([d * 10 ** _bf_rng.integers(1, 4) + _bf_rng.integers(0, 10)
                     for _ in range(25)])
df_bf_fake = pd.DataFrame({"观测值": _bf_fake})
_bf_iss = DC.check_benford(df_bf_fake)
check("首位均匀分布（人造）→ 报 mid",
      any(i["level"] == DC.LEVEL_MID for i in _bf_iss), f"got={_titles(_bf_iss)}")
check("证据含 MAD 值与符合度结论",
      bool(_bf_iss) and "MAD=" in _bf_iss[0]["title"], 
      f"got={_bf_iss[0]['title'] if _bf_iss else None}")

# 反例 1：真实自然数据（对数正态）必然接近 Benford → 必须不报
_bf_nat = np.random.default_rng(12).lognormal(6, 2, 600)
check("对数正态自然数据 → 不报（不许误伤正常数据）",
      len(DC.check_benford(pd.DataFrame({"人口数": _bf_nat}))) == 0,
      f"got={_titles(DC.check_benford(pd.DataFrame({'人口数': _bf_nat})))}")

# 反例 2-6：各类"不适用 Benford"的列必须整列跳过
check("样本量 < 100 → 跳过",
      len(DC.check_benford(pd.DataFrame({"观测值": _bf_fake[:40]}))) == 0)
check("含非正数 → 跳过",
      len(DC.check_benford(pd.DataFrame({"观测值": list(_bf_nat[:200]) + [-5] * 5}))) == 0)
check("取值太窄（不跨数量级）→ 跳过",
      len(DC.check_benford(pd.DataFrame({"观测值": list(np.random.default_rng(13).integers(100, 200, 300))}))) == 0)
check("编号类列 → 跳过（首位由编码规则决定）",
      len(DC.check_benford(pd.DataFrame({"学号": list(np.random.default_rng(14).integers(1e6, 1e7, 300))}))) == 0)
check("量表/评分类列 → 跳过", len(DC.check_benford(
    pd.DataFrame({"满意度评分": list(np.random.default_rng(15).integers(1, 6, 300))}))) == 0)
check("年份/日期类列 → 跳过", len(DC.check_benford(
    pd.DataFrame({"年份": list(np.random.default_rng(16).integers(2000, 2025, 300))}))) == 0)

# 反例 7：取值重复度过高（nunique < 50）→ 跳过
check("取值分散度不足 → 跳过",
      len(DC.check_benford(pd.DataFrame({"观测值": (list(range(1, 60)) * 5)}))) == 0)

# _benford_eligible 闸门本身
check("_benford_eligible 对自然数据判 True",
      DC._benford_eligible(pd.Series(_bf_nat), "人口数") is not None)

# ===========================================================================
section("10b. 末位数字偏好（学术级取证 · heaping）")
# ===========================================================================
# 双重门槛：卡方 p<0.001 **且** 某末位占比 ≥25%。只显著不报（大样本太易触发）。

# 正例：人工读数向 0/5 取整（血压/体重场景）
_td_vals = []
_td_rng = np.random.default_rng(17)
for _ in range(200):
    _v = int(_td_rng.integers(100, 200))
    _td_vals.append(float(round(_v / 5) * 5) if _td_rng.random() < 0.6 else _v + float(_td_rng.random()))
df_td_fake = pd.DataFrame({"体重": _td_vals})
_td_iss = DC.check_terminal_digits(df_td_fake)
check("末位强烈偏好 0（占 62%）→ 报 mid",
      any(i["level"] == DC.LEVEL_MID for i in _td_iss), f"got={_titles(_td_iss)}")
check("证据含占比与卡方", bool(_td_iss) and "占" in _td_iss[0]["evidence"]
      and "卡方=" in _td_iss[0]["evidence"], f"got={_td_iss[0]['evidence'] if _td_iss else None}")

# 反例 1：真实连续测量（末位近似均匀）→ 不报
check("末位近似均匀的连续量 → 不报",
      len(DC.check_terminal_digits(pd.DataFrame(
          {"反应时": np.random.default_rng(18).uniform(200, 900, 400).round(3).tolist()}))) == 0)

# 反例 2-5：不适用列必须跳过
check("样本量 < 50 → 跳过",
      len(DC.check_terminal_digits(pd.DataFrame({"体重": _td_vals[:30]}))) == 0)
check("取值种类太少（量表）→ 跳过",
      len(DC.check_terminal_digits(pd.DataFrame(
          {"满意度": list(np.random.default_rng(19).integers(1, 8, 300))}))) == 0)
check("编号类列 → 跳过",
      len(DC.check_terminal_digits(pd.DataFrame(
          {"学号": list(np.random.default_rng(20).integers(1e6, 1e7, 300))}))) == 0)
check("整数且全不重复（疑似编号）→ 跳过",
      len(DC.check_terminal_digits(pd.DataFrame(
          {"序号": list(np.random.default_rng(21).permutation(200) * 7 + 3)}))) == 0)

# 反例 6：只统计显著但偏离小（12% vs 10%）→ 不许报（效应量门槛）
_td_mild = list(np.random.default_rng(22).integers(0, 10, 1000)) + [0] * 30
check("仅轻微偏离（效应量不足 25%）→ 不报（防大样本误报机器）",
      len(DC.check_terminal_digits(pd.DataFrame({"测量值": _td_mild}))) == 0,
      f"got={_titles(DC.check_terminal_digits(pd.DataFrame({'测量值': _td_mild})))}")

# _terminal_digit 单元语义（现行契约：先按报告精度规范化，再取末位有效数字）
check("_terminal_digit(120.5) = 5", DC._terminal_digit(120.5) == 5)
check("_terminal_digit(123) = 3", DC._terminal_digit(123) == 3)
check("_terminal_digit(125) = 5（读数到个位）", DC._terminal_digit(125) == 5)
check("_terminal_digit(120.0) = 0（补位零不算，规范成 120）", DC._terminal_digit(120.0) == 0)
check("_terminal_digit(0.0420) = 2（补位零不算，规范成 0.042）", DC._terminal_digit(0.0420) == 2)
check("_terminal_digit('abc') = None（非数字）", DC._terminal_digit("abc") is None)
check("_terminal_digit(1e-5) = None（科学计数法跳过）", DC._terminal_digit(1e-5) is None)
# 回归护栏：整数列经 float 化后末位不得全部塌成 0（历史上踩过的 bug）
check("整数序列末位不全塌成 0（历史 bug 回归护栏）",
      DC._terminal_digit(1234.0) == 4 and DC._terminal_digit(999.0) == 9,
      f"got={DC._terminal_digit(1234.0)}, {DC._terminal_digit(999.0)}")

# ===========================================================================
section("10c. 取证检测器：绝不误伤内置示例数据（回归护栏）")
# ===========================================================================
# 5 份内置示例数据是"正常研究数据"的基准，取证检测器若在其上报 issue 就是误报。
import os as _os
_examples = [p for p in ("rm_anova_data.csv", "two_way_data.csv", "questionnaire_data.csv",
                         "sample_regression_data.csv", "student_scores.csv")
             if _os.path.exists(_os.path.join("examples", p))]
for _name in _examples:
    _dfx = pd.read_csv(_os.path.join("examples", _name))
    _b = DC.check_benford(_dfx)
    _t = DC.check_terminal_digits(_dfx)
    check(f"示例 {_name}：Benford 不误报", len(_b) == 0, f"got={_titles(_b)}")
    check(f"示例 {_name}：末位偏好不误报", len(_t) == 0, f"got={_titles(_t)}")

# ===========================================================================
section("11. 聚合入口：干净数据必须 0 报告（生命线）")
# ===========================================================================
clean = pd.DataFrame({
    "学号": list(range(1, 31)),
    "性别": (["男", "女"] * 15),
    "前测": _rng(30, 1).tolist(),
    "后测": (_rng(30, 1) + np.random.default_rng(2).normal(2, 3, 30)).round(1).tolist(),
    "学习时长": _rng(30, 3).tolist(),
})
rep = DC.run_datacheck(clean)
check("干净数据 → 0 issue", rep["summary"]["total"] == 0,
      f"got={_titles(rep['issues'])}")
check("verdict 为『未发现明显数据问题』", "未发现明显数据问题" in rep["summary"]["verdict"])
check("summary 结构完整",
      all(k in rep["summary"] for k in ("total", "high", "mid", "low", "rows", "cols", "verdict", "checks_run", "disclaimer")))
check("disclaimer 含『不代表数据造假』", "不代表数据造假" in rep["summary"]["disclaimer"])
check("全部检测器都跑过（注册表口径，不冻结数字）",
      len(rep["summary"]["checks_run"]) == len(DC._CHECKS),
      f"got={rep['summary']['checks_run']}")
check("取证检测器已注册（Benford + 末位偏好）",
      any("本福特" in c for c in rep["summary"]["checks_run"])
      and any("末位" in c for c in rep["summary"]["checks_run"]),
      f"got={rep['summary']['checks_run']}")

# ===========================================================================
section("12. 聚合入口：脏数据排序 + 裁决")
# ===========================================================================
dirty = pd.DataFrame({
    "分项1": [10, 20, 30, 40, 50, 60],
    "分项2": [10, 20, 30, 40, 50, 60],
    "总分": [20, 40, 60, 80, 100, 125],
    "成绩": [80, 90, 105, 70, 85, 88],
    "年级": [3, 3, 3, 3, 3, 3],
})
rep2 = DC.run_datacheck(dirty)
check("脏数据 → 有 high", rep2["summary"]["high"] >= 1, f"got={rep2['summary']}")
check("verdict 提示先核对", "建议先核对修正" in rep2["summary"]["verdict"])
levels = [i["level"] for i in rep2["issues"]]
order = {DC.LEVEL_HIGH: 0, DC.LEVEL_MID: 1, DC.LEVEL_LOW: 2}
check("issue 按 high→mid→low 排序",
      levels == sorted(levels, key=lambda l: order[l]), f"got={levels}")
check("每条 issue 字段齐全",
      all(all(k in i for k in ("level", "category", "title", "evidence", "explain", "suggestion", "rows", "columns"))
          for i in rep2["issues"]))

# ===========================================================================
section("13. 健壮性：绝不崩")
# ===========================================================================
for name, bad_df in [
    ("空表（0 行）", pd.DataFrame({"a": [], "b": []})),
    ("单行", pd.DataFrame({"a": [1], "b": ["x"]})),
    ("全空列", pd.DataFrame({"a": [np.nan] * 5, "b": [np.nan] * 5})),
    ("纯文本列", pd.DataFrame({"a": ["甲", "乙", "丙", "丁", "戊"], "b": ["x", "y", "z", "w", "v"]})),
    ("数值+文本混列", pd.DataFrame({"a": [1, "ab", 3, None, 5], "b": [2, 3, 4, 5, 6]})),
]:
    try:
        r = DC.run_datacheck(bad_df)
        ok = isinstance(r, dict) and "issues" in r and "summary" in r
    except Exception as e:  # noqa: BLE001
        ok = False
        r = e
    check(f"{name} → 不抛异常且返回契约完整", ok, f"err={r}")

# ===========================================================================
section("14. HTTP 接口 /api/datacheck")
# ===========================================================================
try:
    import app as A
    c = A.app.test_client()

    csv = ("学号,分项1,分项2,总分,成绩\n"
           "1,10,10,20,88\n2,20,20,40,92\n3,30,30,60,105\n"
           "4,40,40,80,76\n5,50,50,100,81\n6,60,60,125,90\n").encode("utf-8")
    up = c.post("/api/upload", data={"file": (__import__("io").BytesIO(csv), "t.csv")},
                content_type="multipart/form-data").get_json()
    check("上传返回 file_id", bool(up.get("file_id")), f"got={up}")

    resp = c.post("/api/datacheck", json={"file_id": up.get("file_id")})
    j = resp.get_json()
    check("接口 200 且 ok=True", resp.status_code == 200 and j.get("ok") is True, f"got={j}")
    check("返回 issues + summary", isinstance(j.get("issues"), list) and isinstance(j.get("summary"), dict))
    check("检出总分对不上", any("总分" in i["title"] for i in j.get("issues", [])), f"got={[i['title'] for i in j.get('issues', [])]}")
    check("检出成绩越界", any("成绩" in i["title"] and i["level"] == "high" for i in j.get("issues", [])))

    bad = c.post("/api/datacheck", json={"file_id": "nope"}).get_json()
    check("无效 file_id → 友好报错", bad.get("ok") is False and "过期" in bad.get("error", ""))
except Exception as e:  # noqa: BLE001
    check("HTTP 接口测试整体执行", False, f"err={e}")

# ===========================================================================
section("15. propose_fix —— 清洗副本（入口第二站）")
# ===========================================================================
import io as _io

# ---- 15.1 反向题漏反向计分 → 自动反向计分 ----
rev_df = pd.DataFrame({
    "q1": [1, 2, 3, 4, 5, 6, 7, 2, 3, 4],
    "q2": [2, 3, 4, 5, 6, 7, 1, 3, 4, 5],
    "q3": [3, 4, 5, 6, 7, 1, 2, 4, 5, 6],
    "q4": [7, 6, 5, 4, 3, 2, 1, 6, 5, 4],   # 反向题：与其余题项呈负相关
    "q5": [1, 3, 2, 5, 4, 6, 7, 2, 4, 3],
})
rev_snapshot = rev_df.copy()
fix = DC.propose_fix(rev_df)
check("① 绝不修改入参 df", rev_df.equals(rev_snapshot))
check("① 识别出自动反向计分", any(a["kind"] == "reverse_score" and a["auto"] for a in fix["actions"]),
      f"kinds={[a['kind'] for a in fix['actions']]}")
q4_changes = [c for c in fix["changes"] if c["column"] == "q4"]
check("① changes 逐格记录 q4 的改动", len(q4_changes) == 10, f"n={len(q4_changes)}")
clean1 = pd.read_csv(_io.StringIO(fix["clean_csv"].lstrip("\ufeff")))
check("① 副本里 q4 首行 7 → 1", int(clean1.loc[0, "q4"]) == 1, f"got={clean1.loc[0, 'q4']}")
check("① 副本里 q4 次行 6 → 2", int(clean1.loc[1, "q4"]) == 2, f"got={clean1.loc[1, 'q4']}")
check("① 其它列原样不动", int(clean1.loc[0, "q1"]) == 1 and int(clean1.loc[1, "q2"]) == 3)

# ---- 15.2 完全重复行 → 自动去重（保留首条） ----
dup_df = pd.DataFrame({
    "编号": [1, 2, 3, 4, 5, 6, 7, 8, 9, 3],       # 第 10 行与第 3 行完全相同
    "姓名": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "c"],
    "得分": [10, 20, 30, 40, 50, 60, 70, 80, 90, 30],
})
fix2 = DC.propose_fix(dup_df)
check("② 识别出自动去重", any(a["kind"] == "dedup_exact" and a["auto"] for a in fix2["actions"]),
      f"kinds={[a['kind'] for a in fix2['actions']]}")
check("② 恰好删 1 行", fix2["stats"]["rows_removed"] == 1, f"got={fix2['stats']}")
check("② 副本 10 → 9 行", fix2["rows"] == 9, f"got={fix2['rows']}")

# ---- 15.3 合计 ≠ 分项和 → 只建议，绝不自动改 ----
sum_df = pd.DataFrame({
    "学号": [1, 2, 3, 4, 5, 6],
    "分项1": [10, 20, 30, 40, 50, 60],
    "分项2": [10, 20, 30, 40, 50, 60],
    "总分": [20, 40, 60, 80, 100, 125],           # 最后一行对不上
})
fix3 = DC.propose_fix(sum_df)
sum_acts = [a for a in fix3["actions"] if a["kind"] == "sum_overwrite"]
check("③ 合计问题转成「需人工确认」", bool(sum_acts) and all(not a["auto"] for a in sum_acts))
check("③ 未改动任何单元格 / 未删行",
      fix3["stats"]["cells_changed"] == 0 and fix3["stats"]["rows_removed"] == 0,
      f"got={fix3['stats']}")
check("③ 副本形状与原表一致", fix3["rows"] == 6 and fix3["cols"] == 4)

# ---- 15.4 干净数据 → 无动作、无改动（不许误报式清洗） ----
n = 40
rng_clean = np.random.default_rng(3)
clean_df = pd.DataFrame({
    "学号": np.arange(1, n + 1),
    "q1": rng_clean.integers(1, 8, n),
    "q2": rng_clean.integers(1, 8, n),
    "q3": rng_clean.integers(1, 8, n),
})
fix4 = DC.propose_fix(clean_df)
check("④ 干净数据：无自动动作", fix4["stats"]["auto_actions"] == 0, f"got={fix4['stats']}")
check("④ 干净数据：零改动", fix4["stats"]["cells_changed"] == 0 and fix4["stats"]["rows_removed"] == 0)

# ---- 15.5 健壮性：空表 / 单行 不抛异常且契约完整 ----
for name, frame in (("空表", pd.DataFrame()), ("单行", pd.DataFrame({"a": [1]}))):
    try:
        r = DC.propose_fix(frame)
        ok = (isinstance(r, dict) and "actions" in r and "stats" in r
              and isinstance(r["clean_csv"], str))
    except Exception as e:  # noqa: BLE001
        ok = False
        r = e
    check(f"⑤ {name} → 不抛异常且契约完整", ok, f"err={r}")

# ---- 15.6 HTTP /api/datacheck/fix ----
try:
    import app as A2
    c2 = A2.app.test_client()
    csv2 = ("学号,q1,q2,q3,q4\n"
            "1,1,2,3,7\n2,2,3,4,6\n3,3,4,5,5\n4,4,5,6,4\n"
            "5,5,6,7,3\n6,6,7,1,2\n7,7,1,2,1\n8,2,3,4,6\n").encode("utf-8")
    up2 = c2.post("/api/upload", data={"file": (__import__("io").BytesIO(csv2), "t2.csv")},
                  content_type="multipart/form-data").get_json()
    resp = c2.post("/api/datacheck/fix", json={"file_id": up2.get("file_id")})
    jf = resp.get_json()
    check("⑥ fix 接口 200 且 ok=True", resp.status_code == 200 and jf.get("ok") is True, f"got={jf}")
    check("⑥ 返回 actions + stats + clean_csv",
          isinstance(jf.get("actions"), list) and isinstance(jf.get("stats"), dict)
          and isinstance(jf.get("clean_csv"), str))
    check("⑥ clean_csv 含表头", "学号" in jf.get("clean_csv", ""))
    bad2 = c2.post("/api/datacheck/fix", json={"file_id": "nope"}).get_json()
    check("⑥ 无效 file_id → 友好报错",
          bad2.get("ok") is False and "过期" in bad2.get("error", ""), f"got={bad2}")
except Exception as e:  # noqa: BLE001
    check("⑥ HTTP fix 接口测试整体执行", False, f"err={e}")

# ===========================================================================
print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
raise SystemExit(1 if FAIL else 0)
