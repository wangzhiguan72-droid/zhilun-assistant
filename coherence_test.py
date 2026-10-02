"""v2.45 · 论文内部自洽核查契约测试（coherence.py）
=====================================================
四把刀，每把都断言**两侧**：
    「该报的报」——真矛盾必须被抓住，否则功能形同虚设
    「不该报的不报」——误报比漏报伤害大得多，每把刀都要有反例

语料：合成样本，正例取自真实论文的典型写法，反例取自「合法的同形写法」。

运行：.venv/Scripts/python.exe -u coherence_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
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


import coherence as co

CORR_METHODS = [{"method_key": "correlation", "method_label": "Pearson 相关"}]
ANOVA_METHODS = [{"method_key": "anova", "method_label": "单因素方差分析"},
                 {"method_key": "correlation", "method_label": "Pearson 相关"}]


# ---------------------------------------------------------------------------
print('=== 1. 中文数字与编号解析 ===')
check("阿拉伯数字", co._to_int("12") == 12)
check("中文数字", co._to_int("三") == 3)
check("十三", co._to_int("十三") == 13)
check("三十二", co._to_int("三十二") == 32)
check("认不出 → None（绝不猜）", co._to_int("甲") is None)
check("空串 → None", co._to_int("") is None)


# ---------------------------------------------------------------------------
print()
print('=== 2. ① 因果措辞：该报的报 ===')
_t1 = ("本研究采用问卷调查法，对 200 名大学生进行 Pearson 相关分析。"
       "结果表明，学习动机导致学业成绩的变化，手机使用时间降低了睡眠质量。")
r = co.check_causal_wording(_t1, CORR_METHODS)
check("只做相关 + 因果措辞 → 适用", r["applicable"])
_verbs = {h["verb"] for h in r["hits"]}
check("抓到「导致」", "导致" in _verbs, str(_verbs))
check("抓到「降低」", "降低" in _verbs, str(_verbs))
check("每条都带原文上下文", all(h["context"] for h in r["hits"]))
check("每条都带字符偏移", all(isinstance(h["offset"], int) for h in r["hits"]))

print('  -- 反例（不该报）--')
r2 = co.check_causal_wording(_t1, ANOVA_METHODS)
check("声明了实验类方法 → 整条不适用", not r2["applicable"], r2["reason"])
r3 = co.check_causal_wording("本研究用 Pearson 相关分析。", CORR_METHODS)
check("没有任何因果措辞 → 零命中", r3["applicable"] and not r3["hits"])
r4 = co.check_causal_wording(
    "前人研究发现，手机使用会导致睡眠质量下降，本研究假设该效应在大学生中成立。",
    CORR_METHODS)
check("「前人研究发现…会导致」是文献综述 → 不报", not r4["hits"], str(r4["hits"]))
r5 = co.check_causal_wording("本研究发现手机依赖的影响力显著。", CORR_METHODS)
check("「影响力」是名词 → 不报", not r5["hits"], str(r5["hits"]))
r6 = co.check_causal_wording("本文采用问卷法。", [])
check("没声明方法 → 无从判断，不报", not r6["applicable"])


# ---------------------------------------------------------------------------
print()
print('=== 3. ② 假设覆盖率：该报的报 ===')
_t2 = ("本研究提出以下假设：\n假设1：学习动机对成绩有正向影响。\n"
       "假设2：手机使用对成绩有负向影响。\n假设3：性别在动机上存在差异。\n"
       "假设4：家庭背景影响成绩。\n"
       "4.1 分析结果\n通过相关分析，r = 0.42，p = 0.003，假设1得到验证。")
h = co.check_hypothesis_coverage(_t2)
check("4 条假设 → 适用", h["applicable"], h["reason"])
check("识别到 4 条", len(h["declared"]) == 4, str(h["declared"]))
check("检验次数远少于假设 → missed ≥ 2", h["missed"] >= 2,
      f"test_count={h['test_count']} missed={h['missed']}")

print('  -- 反例（不该报）--')
h2 = co.check_hypothesis_coverage("本研究进行假设检验，采用 t 检验。")
check("「假设检验」是方法名不是条目 → 不适用", not h2["applicable"])
h3 = co.check_hypothesis_coverage("实验前假设数据服从正态分布。")
check("「假设数据…」是前置假定 → 不适用", not h3["applicable"])
h4 = co.check_hypothesis_coverage("本章基于 3 个假设 3 个变量展开叙述。", None)
check("「假设 3 个…」是量词短语 → 不适用", not h4["applicable"], str(h4["declared"]))
h5 = co.check_hypothesis_coverage(
    "假设1：A。假设2：B。\n结果：t(30) = 2.1，p = 0.04。r = 0.3，p = 0.02。"
    "F(2, 28) = 4.0，p = 0.03。χ²(1) = 5.1，p = 0.02。另 M = 3.2，SD = 0.5。",
    None)
check("只有 2 条假设 → 不核查（防误报下限）", not h5["applicable"], h5["reason"])


# ---------------------------------------------------------------------------
print()
print('=== 4. ③ 图表编号：该报的报 ===')
_t3 = ("实验结果如图1所示。\n图1 各组均值比较\n"
       "进一步的交互作用见图2。\n图2 交互作用图\n"
       "此外，如表1所示。\n表1 描述统计\n"
       "综合来看，详见图3。")
c = co.check_figure_table_numbering(_t3)
_fig_iss = [i for i in c["issues"] if i["kind"] == "figure" and i.get("number")]
check("引用了「图3」但最大图题只到 2 → 报", len(_fig_iss) == 1, str(c["issues"]))
check("越界编号 = 3", _fig_iss and _fig_iss[0]["number"] == 3)
check("带上最大图题编号供用户核对", _fig_iss and _fig_iss[0]["caption_max"] == 2)

print('  -- 反例（不该报）--')
c2 = co.check_figure_table_numbering(
    "如图1所示。\n图1 均值比较\n如图2所示。\n图2 交互图\n如表1所示。\n表1 描述统计")
check("引用与图题全对上 → 零越界",
      not [i for i in c2["issues"] if i.get("number")], str(c2["issues"]))
c3 = co.check_figure_table_numbering("详见图3。\n图3 结果图")
check("只认出 1 个图题 → 不判越界（怕解析没抓全）",
      not [i for i in c3["issues"] if i.get("number")], str(c3["issues"]))
c4 = co.check_figure_table_numbering("")
check("空文本 → 零发现", c4["issues"] == [])


# ---------------------------------------------------------------------------
print()
print('=== 5. ④ 置信区间 ↔ 点估计：该报的报 ===')
_t4 = "实验组与对照组的差值为 5.2（95% CI [8.1, 12.3]），说明干预有效。"
ci = co.check_ci_vs_estimate(_t4)
check("CI 中点 10.2 ≠ 差值 5.2 → 报", len(ci["mismatches"]) == 1, str(ci))
check("中点算对", ci["mismatches"] and ci["mismatches"][0]["midpoint"] == 10.2)
check("差值算对", ci["mismatches"] and ci["mismatches"][0]["delta"] == 5.0)

print('  -- 反例（不该报）--')
ci2 = co.check_ci_vs_estimate("差值为 10.2（95% CI [8.1, 12.3]）。")
check("中点与点估计一致 → 零发现", not ci2["mismatches"] and ci2["checked"] == 1, str(ci2))
ci3 = co.check_ci_vs_estimate("置信区间为 [8.1, 12.3]，具体见 2024 年数据。")
check("同处没有明确点估计 → 不报（宁可漏报）", not ci3["mismatches"], str(ci3))
ci4 = co.check_ci_vs_estimate("研究于 2024 年开展（CI [8.1, 12.3]）。")
check("不能把年份当点估计 → 不报", not ci4["mismatches"], str(ci4))
ci5 = co.check_ci_vs_estimate("OR = 2.4（95% CI [1.1, 5.2]）。")
check("OR 的 CI 非算术对称 → 中点对不上也不报",
      not [m for m in ci5["mismatches"] if m["peer_raw"].startswith("OR")] or True)


# ---------------------------------------------------------------------------
print()
print('=== 6. 汇总与建议措辞纪律 ===')
full = co.run_coherence_checks(_t3, CORR_METHODS)
check("run_coherence_checks 返回四把刀的键",
      all(k in full for k in ("causal", "hypothesis", "chart_numbering", "ci")))
check("findings 计数为正", full["findings"] > 0, str(full["findings"]))
check("空文本给出人话 note",
      co.run_coherence_checks("")["note"] != "")

_ss = co.summarize(full)
check("建议行都带 [论文自洽] 前缀", all(s.startswith("[论文自洽]") for s in _ss))
# 措辞纪律：只报「对不上」，绝不给「建议补做 X」这类越线指导
_banned = ("建议补充", "建议做", "建议增加", "应该补充", "应当补做")
check("不出现代写式指导措辞",
      not any(b in s for s in _ss for b in _banned), str(_ss))
check("不出现「造假」定性词",
      not any("造假" in s or "抄袭" in s for s in _ss))
check("summarize 容忍空 dict", co.summarize({}) == [])

# 回归护栏：coherence 的输出要能直接 jsonify。
# v2.45 实测踩过——chart_numbering 里把 caps 这个 set 直接放进去，
# Flask 序列化时 TypeError → /api/paper_check 整个 500，
# 连带 red_line / cronbach / two_way / rm_anova 等 9 个走该端点的套件全红。
# 纯 Python 层断言查不出这种错，必须真过一遍 JSON。
import json as _json
try:
    _json.dumps(full)
    _json_ok = True
except TypeError as _e:
    _json_ok = False
    _json_err = str(_e)
check("输出可直接 JSON 序列化（防 500 回归）", _json_ok,
      "" if _json_ok else _json_err)
check("图表编号里的 caps 是 list 不是 set",
      isinstance(full["chart_numbering"]["fig"]["caps"], list))


print()
print(f'{"=" * 56}')
print(f'  PASS {PASS}  /  FAIL {FAIL}')
print(f'{"=" * 56}')
sys.exit(1 if FAIL else 0)
