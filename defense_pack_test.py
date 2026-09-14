"""
答辩准备包 + 作答时长检测离线测试（v2.10，全 mock / 本地计算，零 API）
================================================================
覆盖总纲 P4 残项（作答时长）与 P6（答辩准备包）的行为契约：

    [A] 作答时长检测器（datacheck.check_response_duration）
    [B] QA 引擎（defense_pack.build_qa_pack）——数字必须与实算一致
    [C] API 端点（起本地服务后跑；未起自动 SKIP）

用法：.venv/Scripts/python.exe defense_pack_test.py
"""
import os
import sys
import json
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from datacheck import check_response_duration, run_datacheck, _CHECKS
from defense_pack import build_qa_pack
from methods_registry import call_method

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


# ---------------------------------------------------------------------------
print("[A] 作答时长检测器")
# ---------------------------------------------------------------------------
df = pd.DataFrame({
    "id": range(20), "q1": [3] * 20, "q2": [4] * 20,
    "总时长(秒)": [180] * 17 + [12, 9, 15],
})
issues = check_response_duration(df)
check("过快行报出", len(issues) == 1)
if issues:
    it = issues[0]
    check("行号是 Excel 约定（+2）", it["rows"] == [19, 20, 21], f"实际={it['rows']}")
    check("证据含阈值秒数", "18" in it["evidence"], f"实际={it['evidence'][:60]}")
check("无时长列不报", check_response_duration(df.drop(columns=["总时长(秒)"])) == [])
check("reaction 列不报（单题反应时≠总时长）",
      check_response_duration(df.rename(columns={"总时长(秒)": "reaction_time"})) == [])
df3 = df.copy()
df3["总时长(秒)"] = [180] * 8 + [10] * 12
check("过半过快（锚点异常）不报", check_response_duration(df3) == [])
check("已注册进 _CHECKS", any(f.__name__ == "check_response_duration"
                              for _lbl, f in _CHECKS))
for f in ("student_scores.csv", "questionnaire_data.csv",
          "two_way_data.csv", "rm_anova_data.csv"):
    d = pd.read_csv(os.path.join("examples", f))
    check(f"内置示例 {f} 零误报", check_response_duration(d) == [])
rep = run_datacheck(df)
check("run_datacheck 集成（checks_run 含 作答时长）",
      "作答时长" in rep["summary"]["checks_run"])

# ---------------------------------------------------------------------------
print("\n[B] QA 引擎（build_qa_pack）")
# ---------------------------------------------------------------------------
sdf = pd.read_csv(os.path.join("examples", "student_scores.csv"))
res = call_method("independent_t", sdf, {"group_col": "gender", "value_col": "score"})
s = res["summary"]
pack = build_qa_pack([{"method": "independent_t", "label": "独立样本T检验",
                       "summary": s}])
check("独立 T 生成 4 题（结果/方法/效应量/样本量）", len(pack["qa"]) == 4,
      f"实际={len(pack['qa'])}")
qs = " ".join(q["question"] for q in pack["qa"])
check("四类问题都出现",
      all(k in qs for k in ("一句话", "为什么用", "效应量", "样本量")))
result_q = pack["qa"][0]
p_expect = "p < 0.001" if float(s["p"]) < 0.001 else f"p = {float(s['p']):.3f}"
check("结果题引用实算 p", p_expect in result_q["points"][0],
      f"实际={result_q['points'][0]}")
check("结果题引用 t 值", f"t({s['df']})" in result_q["points"][1],
      f"实际={result_q['points'][1]}")
eff_q = next(q for q in pack["qa"] if "效应量" in q["question"])
check("效应题引用实算 d", f"{abs(float(s['d'])):.2f}" in eff_q["points"][0],
      f"实际={eff_q['points'][0]}")
samp_q = next(q for q in pack["qa"] if "样本量" in q["question"])
check("样本题引用实算 n", f"n₁={s['n1']}" in samp_q["points"][0],
      f"实际={samp_q['points'][0]}")
check("used_analyses 记录方法名", pack["used_analyses"] == ["独立样本T检验"])

check("空历史 → qa 空", build_qa_pack([])["qa"] == [])
check("坏 summary 不崩",
      build_qa_pack([{"method": "x", "label": "X", "summary": {}}])["qa"] == [])

dc = run_datacheck(sdf)
pack2 = build_qa_pack([{"method": "independent_t", "label": "独立样本T检验",
                        "summary": s}], datacheck_report=dc)
check("并入体检报告 → 数据质量题出现",
      any("清洗" in q["question"] for q in pack2["qa"]))
check("数据质量题引用体检结论",
      any(dc["summary"]["verdict"][:10] in pt
          for q in pack2["qa"] if "清洗" in q["question"] for pt in q["points"]))

# ---------------------------------------------------------------------------
print("\n[C] API 端点（需本地服务）")
# ---------------------------------------------------------------------------
def _post(path, payload, raw=False):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, dict(r.headers), body if raw else json.loads(body)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"{}")


def _server_up():
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


if _server_up():
    with open(os.path.join("examples", "student_scores.csv"), "rb") as fh:
        boundary = "----zhiluntest"
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; '
                f'filename="student_scores.csv"\r\n'
                f"Content-Type: text/csv\r\n\r\n").encode() + fh.read() + \
            f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            BASE + "/api/upload", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            fid = json.loads(r.read())["file_id"]
    runs = [{"method": "independent_t", "group_col": "gender", "value_col": "score"}]

    code, _h, d = _post("/api/defense_pack",
                        {"file_id": fid, "runs": runs, "include_datacheck": True})
    check("defense_pack 端到端 200", code == 200 and d.get("ok"))
    check("含独立 T 四题 + 数据质量题", len(d.get("qa", [])) >= 5,
          f"实际={len(d.get('qa', []))}")

    code, h, body = _post("/api/defense_charts",
                          {"file_id": fid,
                           "runs": runs + [{"method": "histogram"}]}, raw=True)
    import io
    import zipfile as zf_mod
    ok_zip = code == 200
    names = []
    if ok_zip:
        z = zf_mod.ZipFile(io.BytesIO(body))
        names = z.namelist()
    check("defense_charts 返回 zip", ok_zip)
    check("未知方法被跳过（只 1 张图）", len(names) == 1, f"实际={names}")
    miss_hdr = h.get("X-Missing-Charts", "")
    check("X-Missing-Charts 标注（ASCII）", "histogram" in miss_hdr,
          f"实际={miss_hdr!r}")

    code, _h, d = _post("/api/defense_charts",
                        {"file_id": fid, "runs": [{"method": "bogus"}]})
    check("全失败 → 400 + 中文报错", code == 400 and "失败" in d.get("error", ""),
          f"code={code} err={d.get('error', '')[:40]}")

    code, _h, d = _post("/api/defense_pack", {"file_id": fid, "runs": []})
    check("空 runs → 400", code == 400 and "至少一次分析" in d.get("error", ""))
else:
    print("  [SKIP] 本地服务未启动（python app.py），[C] 端到端部分跳过")

print(f"\n{'=' * 50}")
print(f"答辩准备包测试：{PASS} 通过 / {FAIL} 失败")
print(f"{'=' * 50}")
sys.exit(1 if FAIL else 0)
