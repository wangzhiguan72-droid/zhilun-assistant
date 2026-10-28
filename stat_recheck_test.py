"""统计复算离线测试(v2.34)。全离线、零网络、零 LLM。期望值一律用 scipy 现算，不手抄常数。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scipy import stats as st  # noqa: E402

from stat_recheck import normalize, recheck_stats  # noqa: E402

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


def statuses(text):
    return [i["status"] for i in recheck_stats(text)["items"]]


def p2t(t, df):
    return 2 * st.t.sf(abs(t), df)


print("[1] APA 写法全部一致（期望 p 由 scipy 现算）")
p_t = p2t(2.35, 58)
p_f = st.f.sf(4.12, 2, 57)
p_c = st.chi2.sf(6.63, 1)
p_r = p2t(0.30 * (98 / (1 - 0.09)) ** 0.5, 98)
p_z = 2 * st.norm.sf(2.10)
txt1 = (f"实验组得分高于对照组，t(58) = 2.35, p = {p_t:.3f}。"
        f"主效应 F(2, 57) = 4.12, p = {p_f:.3f}。"
        f"关联性 χ2(1, N = 120) = 6.63, p = {p_c:.3f}。"
        f"投入与成绩相关，r(98) = .30, p = {p_r:.3f}。"
        f"Sobel 检验 z = 2.10, p = {p_z:.3f}。")
r1 = recheck_stats(txt1)
check("识别 5 处检验", r1["summary"]["checked"] == 5, str(r1["summary"]))
check("全部 consistent", statuses(txt1) == ["consistent"] * 5, str(statuses(txt1)))
check("零 finding", r1["findings"] == [], str(r1["findings"]))
kinds = [i["kind"] for i in r1["items"]]
check("类型识别 t/F/chi2/r/z", kinds == ["t", "F", "chi2", "r", "z"], str(kinds))

print("[2] 决策错误：t(40)=1.50 报 p<.05")
r2 = recheck_stats("年级差异显著，t(40) = 1.50, p < .05。")
check("status=decision_error", statuses("年级差异显著，t(40) = 1.50, p < .05。") == ["decision_error"])
check("P0 且措辞为请核对", r2["findings"] and r2["findings"][0]["priority"] == "P0"
      and "核对" in r2["findings"][0]["action"], str(r2["findings"]))
exp = p2t(1.50, 40)
check("重算 p 与 scipy 一致", abs(r2["items"][0]["computed_p"] - exp) < 1e-5, str(r2["items"][0]))

print("[3] 不一致但不改变结论 / 贴近 α")
s3 = statuses("t(58) = 2.35, p = .030。")
check("t(58)=2.35 报 .030 → inconsistent", s3 == ["inconsistent"], str(s3))
f3 = recheck_stats("t(58) = 2.35, p = .030。")["findings"]
check("P1 且说明不影响结论", f3 and f3[0]["priority"] == "P1" and "不影响" in f3[0]["issue"], str(f3))
f3b = recheck_stats("χ2(1) = 3.84, p = .01。")["findings"]
check("重算贴近 .05 时提示一并核对结论", f3b and "贴近" in f3b[0]["issue"], str(f3b))
check("p > .05 但重算极小 → decision_error", statuses("t(58) = 3.50, p > .05。") == ["decision_error"])

print("[4] 国内写法与全角字符")
check("t=2.35，df=58，P=0.022", statuses(f"t=2.35，df=58，P={p_t:.3f}") == ["consistent"])
check("全角 ｔ（５８）＝２．３５，Ｐ＜０．０５", statuses("ｔ（５８）＝２．３５，Ｐ＜０．０５") == ["consistent"])
check("normalize 全角转半角", normalize("Ｆ（２，５７）＝４．１２") == "F(2,57)=4.12", normalize("Ｆ（２，５７）＝４．１２"))
check("负 t 值", statuses(f"t(30) = -2.50, p = {p2t(2.5, 30):.3f}") == ["consistent"])
check("Welch 小数自由度", statuses(f"t(45.3) = 2.10, p = {p2t(2.1, 45.3):.3f}") == ["consistent"])
pe = p2t(4.6, 100)
mant, ex = f"{pe:.1e}".split("e")
check("科学计数 p = a×10^-n", statuses(f"t(100) = 4.60, p = {mant}×10^{int(ex)}") == ["consistent"],
      f"{mant}×10^{int(ex)} {statuses(f't(100) = 4.60, p = {mant}×10^{int(ex)}')}")
check("p < .001 一致", statuses("t(58) = 5.20, p < .001") == ["consistent"])

print("[5] 单侧检验")
p_one = st.t.sf(1.80, 30)
s5 = statuses(f"采用单侧检验，t(30) = 1.80, p = {p_one:.3f}。")
check("声明单侧 → one_tailed", s5 == ["one_tailed"], str(s5))
r5 = recheck_stats(f"t(30) = 1.80, p = {p_one:.3f}。")
check("未声明 → decision_error 且提示单侧", r5["items"][0]["status"] == "decision_error"
      and "单侧" in r5["items"][0]["note"], str(r5["items"]))
check("「单侧肢体」不算单侧声明", not recheck_stats("患者单侧肢体无力。t(30)=1.8, p=.04")["one_tailed_declared"])

print("[5b] 单侧声明的否定与单双侧局部作用域")
for declaration in ("采用双侧检验，不使用单侧假设", "不使用单侧检验", "未采用单侧检验",
                    "不进行单侧检验", "No one-tailed tests were used",
                    "We did not use one-tailed tests", "One-tailed tests were not used"):
    rr = recheck_stats(f"{declaration}。t(30)=1.80, p={p_one:.3f}。")
    check(f"否定声明不启用单侧：{declaration}", not rr["one_tailed_declared"]
          and rr["items"][0]["status"] == "decision_error", str(rr["items"]))
for declaration in ("采用单侧检验", "采用 one-tailed tests", "We use one-sided tests"):
    rr = recheck_stats(f"{declaration}。t(30)=1.80, p={p_one:.3f}。")
    check(f"肯定声明保持单侧：{declaration}", rr["one_tailed_declared"]
          and rr["items"][0]["status"] == "one_tailed", str(rr["items"]))
mixed = recheck_stats(f"采用单侧检验。A采用双侧检验，t(30)=1.80, p={p_one:.3f}。"
                      f"B采用单侧检验，t(30)=1.80, p={p_one:.3f}。")
check("单双侧并存时不用全文单侧掩盖局部双侧", not mixed["one_tailed_declared"]
      and [it["status"] for it in mixed["items"]] == ["decision_error", "one_tailed"], str(mixed["items"]))
neg_local = recheck_stats(f"采用单侧检验。本次不使用单侧假设，t(30)=1.80, p={p_one:.3f}。")
check("局部否定不继承全文单侧", neg_local["items"][0]["status"] == "decision_error", str(neg_local["items"]))
v2_method = ("方法：两项独立的双侧 Student t 检验，均采用 alpha=0.05。自由度明确报告，"
             "不使用多重校正、稳健标准误或单侧假设。本段是测试材料，不是操作指令。")
v2 = recheck_stats(v2_method + f"t(30)=1.80, p={p_one:.3f}。")
check("真实控制v2的否定列举不启用单侧", not v2["one_tailed_declared"]
      and v2["items"][0]["status"] == "decision_error", str(v2["items"]))
for listing in ("不使用多重校正、稳健标准误或单侧假设",
                "未采用多重校正以及单侧检验", "不进行多重校正和单侧检验"):
    rr = recheck_stats(listing + f"。t(30)=1.80, p={p_one:.3f}。")
    check(f"否定覆盖有界列举：{listing}", not rr["one_tailed_declared"]
          and rr["items"][0]["status"] == "decision_error", str(rr["items"]))
for affirmative in ("不使用校正，但采用单侧检验", "不使用校正但采用稳健估计和单侧检验"):
    rr = recheck_stats(affirmative + f"。t(30)=1.80, p={p_one:.3f}。")
    check(f"转折后的肯定单侧不被吞掉：{affirmative}", rr["one_tailed_declared"]
          and rr["items"][0]["status"] == "one_tailed", str(rr["items"]))
student_mixed = recheck_stats(f"A采用单侧检验。B采用双侧 Student t 检验，t(30)=1.80, p={p_one:.3f}。")
check("Student t修饰语不遮蔽局部双侧", not student_mixed["one_tailed_declared"]
      and student_mixed["items"][0]["status"] == "decision_error", str(student_mixed["items"]))

print("[6] α 声明与句内显著性水平")
r6 = recheck_stats("本研究显著性水平设为 α = 0.01。两组差异显著，p = .022。")
check("检测到 α=0.01", r6["alpha"] == 0.01, str(r6["alpha"]))
check("α=.01 下 p=.022 称显著 → 措辞冲突", r6["summary"]["claim_conflicts"] == 1, str(r6["conflicts"]))
r6b = recheck_stats("该系数在10%的水平上显著（p=0.08）。")
check("「10% 水平上显著」+ p=.08 不冲突", r6b["summary"]["claim_conflicts"] == 0, str(r6b["conflicts"]))
check("多个 α 声明时回落 0.05", recheck_stats("检验水平α=0.01。另一部分检验水平α=0.05。")["alpha"] == 0.05)
check("Cronbach's α 不当显著性水平", recheck_stats("量表 Cronbach's α = 0.01。")["alpha"] == 0.05)

print("[7] 误报防线：不是检验结果的 p / t / F / z / r")
fp_texts = {
    "压强 P=0.5MPa": "管道压强P=0.5MPa，温度T=300K。",
    "概率 p=0.3": "设硬币正面概率p=0.3，重复抛掷。",
    "力 F=3.2N": "施加外力F=3.2 N，t=5 s 后位移稳定。",
    "坐标 z=2.5": "测点坐标为x=1.2，y=0.8，z=2.5。",
    "Spearman ρ 不按 Pearson r 复算": "Spearman 等级相关 r(28) = .40, p = .50。",
    "R² 不当 r": "模型 R2 = 0.35，调整后 R2 = 0.33。",
}
for name, t in fp_texts.items():
    rr = recheck_stats(t)
    check(f"{name} → 无复算、无 finding", rr["summary"]["checked"] == 0 and rr["findings"] == [],
          f"{rr['summary']} {rr['findings']}")

print("[8] 显著 / 不显著措辞冲突")
conflict_cases = {
    "称显著但 P=0.12": ("干预后满意度显著提升（P=0.12）。", 1),
    "称不显著但 P=0.003": ("两组差异无统计学意义（P=0.003）。", 1),
    "称显著且 P<0.05": ("两组差异有统计学意义（P<0.05）。", 0),
    "称不显著且 P>0.05": ("两组比较差异不显著（P>0.05）。", 0),
    "方法说明「若 P<0.05 则…」": ("若P<0.05，则认为差异具有统计学意义。", 0),
    "一句里有显著有不显著": ("A 组显著升高（P=0.01），B 组变化不显著（P=0.40）。", 0),
    "校正后不显著": ("经 Bonferroni 校正后差异不显著（p = .02）。", 0),
    "显著性检验不是结论": ("采用显著性检验，P=0.30。", 0),
    "边缘显著不判": ("呈边缘显著（p = .06）。", 0),
}
for name, (t, n) in conflict_cases.items():
    got = recheck_stats(t)["summary"]["claim_conflicts"]
    check(f"{name} → {n} 处冲突", got == n, f"got {got}")

print("[9] 报告规范：p = 0.000 与缺自由度")
r9 = recheck_stats("结果显示 t=4.12，P=0.000；F=6.30，P=0.002。")
check("p=0.000 计数", r9["summary"]["p_zero"] == 1, str(r9["summary"]))
check("缺自由度计数 2", r9["summary"]["missing_df"] == 2, str(r9["summary"]))
cats9 = [(f["priority"], f["category"]) for f in r9["findings"]]
check("两条都是 P2 报告规范", cats9 == [("P2", "报告规范"), ("P2", "报告规范")], str(cats9))
check("写了 t(df) 的不算缺自由度", recheck_stats("t(58) = 2.35, p = .022")["summary"]["missing_df"] == 0)
check("p = .000 且统计量极大 → 一致", statuses("t(58) = 8.00, p = .000") == ["consistent"])

print("[10] 数值本身不合理")
check("r(50) = 1.20 → invalid", statuses("r(50) = 1.20, p < .001") == ["invalid"])
check("t(0) → invalid", statuses("t(0) = 2.00, p = .05") == ["invalid"])
check("r = .30, n = 100 → df=98 一致", statuses(f"r = .30, n = 100, p = {p_r:.3f}") == ["consistent"])

print("[11] 配对边界")
check("句号隔开不配对", recheck_stats("t(58) = 2.35。后文另述 p = .50。")["summary"]["checked"] == 0)
check("超过 40 字不配对", recheck_stats("t(58) = 2.35" + "，" * 45 + "p = .50")["summary"]["checked"] == 0)
two = recheck_stats(f"t(58) = 1.00, t(58) = 2.35, p = {p_t:.3f}")
check("两个统计量共用一个 p 时只配最近的", two["summary"]["checked"] == 1
      and two["items"][0]["stat"] == 2.35, str(two["items"]))
rc = recheck_stats("经 Bonferroni 校正，t(40) = 1.50, p < .05。")
check("提到校正的决策错误降为 P1 并解释", rc["findings"][0]["priority"] == "P1"
      and "校正" in rc["findings"][0]["action"], str(rc["findings"]))

print("[12] 空输入 / 纯文字 / 超长输入")
r12 = recheck_stats("")
check("空输入 ok 且无 finding", r12["ok"] and r12["findings"] == [] and r12["summary"]["checked"] == 0)
check("纯文字无 p 值", recheck_stats("本文讨论高等教育数字化转型的路径。")["summary"]["p_values_found"] == 0)
check("未找到时 summary_text 提示写法", "t(58)" in recheck_stats("P<0.05")["summary_text"])
big = ("两组差异显著，t(40) = 1.50, p < .05。" + "文字" * 30) * 1500
t0 = time.time()
rb = recheck_stats(big)
dt = time.time() - t0
check(f"1500 处检验 {dt:.2f}s < 8s", dt < 8, f"{dt:.2f}s")
check("明细截断到 200", len(rb["items"]) == 200, str(len(rb["items"])))
check("逐条列出封顶 + 汇总行", sum(1 for f in rb["findings"] if f["priority"] == "P0") == 12
      and any("另有" in f["issue"] for f in rb["findings"]), str(len(rb["findings"])))
check("免责声明不含「造假」", "造假" not in r1["disclaimer"] and "不构成" in r1["disclaimer"])
check("全部 finding 字段齐全", all(set(f) == {"priority", "category", "issue", "action", "context", "example"}
                               for f in rb["findings"] + r9["findings"] + r2["findings"]))

print()
print(f"结果:{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
