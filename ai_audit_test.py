"""
AI 痕迹自查测试（v2.15，本地规则 + 可选端到端，零 LLM API）
================================================================
覆盖《BZD 数模论文 AI 痕迹审计》规则化落地（ai_audit.py）的行为契约：

    [A] 区分度：AI 腔文本高分 / 正常学术文本低分
    [B] 单项检测：连接词分档、拔高词有量化不扣、提示词残留 P0、
        数模红名单（中文相邻词边界）、无边界推广、被动句
    [C] 红线：报告只谈"风格风险"，绝不出现判定身份的措辞
    [D] 健壮性：空文本 / 纯公式 / 超短文本不崩
    [E] 端到端 /api/ai_audit（需本地服务；未起自动 SKIP）

用法：.venv/Scripts/python.exe ai_audit_test.py
"""
import os
import sys
import json
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_audit import audit_ai_traces

PASS = 0
FAIL = 0
BASE = "http://127.0.0.1:5000"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


BAD = (
    "综上所述，本文进一步构建了模型。此外，与此同时，值得注意的是，可以看出模型效果很好。\n"
    "该模型显著提高了预测精度。结果表明本方法广泛应用于各个领域。采用LSTM进行预测。\n"
    "好的，以下是为你撰写的论文内容。综上所述，由此可见模型具有重要价值。\n"
    "综上所述，进一步验证了模型。此外，需要指出的是，不难看出结果良好。与此同时，模型稳定。\n"
    "综上所述，模型精度高。此外，结果显著。与此同时，方法有效。由此可见，方案可行。\n"
)

GOOD = (
    "本文以某市 2023 年 120 家商户的流水数据为样本，建立多元线性回归模型考察客流量\n"
    "与营业额的关系。模型保留 3 个显著自变量（p 均小于 0.05），调整后 R² 为 0.62。\n"
    "稳健性检验中，我们将样本按商圈分层重跑，系数方向与显著性保持一致（Beta 变化\n"
    "不超过 0.08），说明结论对样本划分不敏感。研究局限在于样本仅覆盖一类业态，\n"
    "结论外推需谨慎。"
)

# ---------------------------------------------------------------------------
print("[A] 区分度")
# ---------------------------------------------------------------------------
r_bad = audit_ai_traces(BAD)
r_good = audit_ai_traces(GOOD)
check(f"AI 腔文本高风险（score={r_bad['score']}）", r_bad["score"] >= 60)
check(f"正常学术文本低风险（score={r_good['score']}）", r_good["score"] <= 20)
check("区分度足够（差 ≥ 40 分）", r_bad["score"] - r_good["score"] >= 40)
check("AI 腔文本等级为 高/中高", r_bad["level"] in ("高", "中高"))
check("正常文本等级为 低", r_good["level"] == "低")

# ---------------------------------------------------------------------------
print("\n[B] 单项检测")
# ---------------------------------------------------------------------------
check("提示词残留命中（好的，以下是）",
      any("以下是" in h["phrase"] or "好的" in h["phrase"]
          for h in r_bad["metrics"]["residue"]))
check("拔高词有量化支撑不扣分",
      audit_ai_traces("模型显著提高了预测精度，误差从 0.31 降到 0.12。")
      ["breakdown"]["拔高词无量化"] == 0)
check("拔高词无量化扣分",
      audit_ai_traces("模型显著提高了预测精度，效果很好。")
      ["breakdown"]["拔高词无量化"] > 0)

# 中文相邻词边界（\b 在中文语境失效的回归锁定）
r_lstm = audit_ai_traces("采用LSTM进行预测。")
check("红名单命中（LSTM 与中文相邻）",
      any(h["model"] == "LSTM" for h in r_lstm["metrics"]["redlist"]))
r_lstm_ok = audit_ai_traces("选用LSTM，因为它适合时序数据且本题样本量充足。")
check("有适配性说明（因为/适合）不扣分",
      r_lstm_ok["breakdown"]["高风险模型未说明"] == 0)

check("无边界推广命中",
      audit_ai_traces("该方法广泛应用于各个领域。")
      ["breakdown"]["无边界推广"] > 0)
check("被动句高风险（>30%）",
      audit_ai_traces("原始数据被收集并整理。回归模型被建立起来。全部参数被优化完成。"
                      "检验结果被验证无误。异常样本被筛选剔除。所有变量被标准化处理。"
                      "关键特征被提取出来。预测误差被计算完毕。统计图表被绘制成型。"
                      "研究结论被给出说明。")
      ["metrics"]["passive"]["band"] == "高风险")
conn_100 = audit_ai_traces("综上所述，" + "。".join(["进一步分析"] * 30) + "。")
check("连接词全占 → 高风险档", conn_100["metrics"]["connection"]["band"] == "高风险")

# ---------------------------------------------------------------------------
print("\n[C] 红线")
# ---------------------------------------------------------------------------
all_text = r_bad["markdown"] if "markdown" in r_bad else ""
report_str = json.dumps(r_bad, ensure_ascii=False)
forbidden = ["确系AI生成", "确系 AI 生成", "由AI生成", "判定为AI", "证明是AI"]
check("报告不含判定作者身份的措辞",
      not any(w in report_str for w in forbidden), f"命中={[w for w in forbidden if w in report_str]}")
check("报告含免责声明（不替代正式检测）",
      "不判定" in r_bad["disclaimer"] and "不替代" in r_bad["disclaimer"])
check("人工自查清单存在（≥8 条）", len(r_bad["manual_checks"]) >= 8)

# ---------------------------------------------------------------------------
print("\n[D] 健壮性")
# ---------------------------------------------------------------------------
for label, t in [("空文本", ""), ("纯公式", "y = ax + b\n|x| < 1\n2*3=6"),
                 ("超短文本", "很短。"), ("None", None)]:
    try:
        r = audit_ai_traces(t)
        check(f"{label} 不崩", r.get("ok") is True)
    except Exception as e:  # noqa: BLE001
        check(f"{label} 不崩", False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n[E] 端到端 /api/ai_audit（需本地服务）")
# ---------------------------------------------------------------------------


def _server_up():
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


if _server_up():
    req = urllib.request.Request(
        BASE + "/api/ai_audit",
        data=json.dumps({"text": BAD}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    check("端到端返回 ok + 高风险", d.get("ok") and d.get("score", 0) >= 60)
    check("端到端带 text_length", d.get("text_length", 0) > 100)

    empty = urllib.request.Request(
        BASE + "/api/ai_audit", data=json.dumps({"text": ""}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(empty, timeout=15) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    check("空文本 → 400", code == 400)
else:
    print("  [SKIP] 本地服务未启动（python app.py），端到端部分跳过")

print(f"\n{'=' * 50}")
print(f"AI 痕迹自查测试：{PASS} 通过 / {FAIL} 失败")
print(f"{'=' * 50}")
sys.exit(1 if FAIL else 0)
