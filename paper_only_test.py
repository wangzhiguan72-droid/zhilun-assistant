"""
v2.48 · 「只传论文、不传数据」入口契约测试（零 LLM API）
=========================================================
覆盖 /api/check_paper_only —— 把三把**零数据依赖**的刀（摘要↔正文自洽、
表格取证、AI 风格自查）从 `/api/check_paper` 里放出来。后者 data 是必填，
于是任何手里没有原始 CSV 的用户（投稿论文、别人的论文、答辩前自查）
都够不到这三项能力。

    [A] 正常路径：只给论文 → 200，三把刀都 ran，无 skipped
    [B] 缺文件 / 空文本 / 扫描版空抽取 → 400 且给人话
    [C] 结构契约：三个子报告各自的必备键（前端与 CLI 都依赖）
    [D] **不静默**：跑不到的项必须进 components.skipped，不能悄悄消失
        （v2.46 纪律：测试没跑 ≠ 测试通过；这里同理——刀没跑 ≠ 没问题）
    [E] 红线：报告文案不得出现判定造假 / 判定作者身份的措辞

为什么用 test_client 而不是真 HTTP：本端点无状态、零外部依赖，
test_client 就够，且不会和别的套件抢 5000 端口（paper_check_test 那种
抢端口的写法是它必须真跑 SSE 才付出的代价，这里不需要）。

用法：.venv/Scripts/python.exe -u paper_only_test.py
"""
import io
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
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  << {detail}" if detail else ""))


import app as A

_client = A.app.test_client()


def post(paper_bytes=None, filename="paper.txt", extra=None):
    data = dict(extra or {})
    if paper_bytes is not None:
        data["paper"] = (io.BytesIO(paper_bytes), filename)
    return _client.post("/api/check_paper_only", data=data,
                        content_type="multipart/form-data")


# ---- 合成样本：一份「摘要与正文对不上」的论文，用来确认这把刀真的开火 ----
# 摘要写 p = 0.001，正文只有 p = 0.62 —— 一个都对不上，必须报出来。
# 若哪天端点退化成恒报「无不一致」，本条会红。
PAPER_BAD = (
    "目  录\n1 绪论 ········· 1\n摘  要 ········· I\nAbstract ········· II\n"
    "\n摘  要\n摘  要\n"
    "本研究以 96 名大学生为对象，测量 BMI 与身体活动量。结果显示两组差异显著"
    "（t(94) = 2.41，p = 0.001），相关系数 r = 0.32。\n"
    "关  键  词：超重；身体活动；大学生\n"
    "\n第一章 绪论\n本研究采用问卷调查法。方差分析显示三组差异显著"
    "（F(2, 93) = 3.10，p = 0.62），相关系数 r = 0.32。\n"
)

# 一份无「关键词」锚的论文：摘要定位不到，必须如实转述而不是报「通过」
PAPER_NOABS = (
    "第一章 绪论\n关\n键\n词\n本文研究某问题，采用 96 份样本，p = 0.04。\n"
)

print("=== [A] 正常路径：只给论文 ===")
r = post(PAPER_BAD.encode("utf-8"))
check("只传论文 → HTTP 200（旧端点此处会 400）", r.status_code == 200,
      f"rc={r.status_code} {r.get_data(as_text=True)[:120]}")
j = r.get_json() or {}
check("ok=True", j.get("ok") is True, str(j)[:200])
comp = j.get("components") or {}
check("三把刀全部 ran，无 skipped",
      sorted(comp.get("ran") or []) == ["abstract_check", "ai_traces", "table_forensics"]
      and not comp.get("skipped"), str(comp))
check("三个子报告都在响应里",
      {"abstract_check", "table_forensics", "ai_traces"} <= set(j), str(list(j)))

print()
print("=== [B] 缺文件 / 空内容 → 400 且给人话 ===")
r2 = post(None)
check("不传 paper → 400", r2.status_code == 400, f"rc={r2.status_code}")
check("不传 paper 时有中文错误提示",
      "上传" in ((r2.get_json() or {}).get("error") or ""),
      str(r2.get_json()))
r3 = post(b"   \n  \n", filename="blank.txt")
check("空文本 → 400（不是 200 + 空报告）", r3.status_code == 400, f"rc={r3.status_code}")
check("空文本的提示指向扫描版/OCR",
      "OCR" in ((r3.get_json() or {}).get("error") or "")
      or "空" in ((r3.get_json() or {}).get("error") or ""),
      str(r3.get_json()))

print()
print("=== [C] 结构契约（前端与 CLI 都依赖这些键） ===")
ac = j.get("abstract_check") or {}
check("abstract_check 含 checked/mismatches/missing_in_body",
      {"checked", "mismatches", "missing_in_body"} <= set(ac), str(list(ac)))
check("abstract_check 带 note（定位不到时说明原因，空串=定位成功）",
      "note" in ac, str(list(ac)))
tf = j.get("table_forensics") or {}
check("table_forensics 含 applicable/abstained",
      {"applicable", "abstained"} <= set(tf), str(list(tf)))
at = j.get("ai_traces") or {}
check("ai_traces 含 score/level",
      {"score", "level"} <= set(at), str(list(at)))
check("paper_claims 三个抽取字段齐全",
      {"methods", "quantities", "variables"} <= set(j.get("paper_claims") or {}),
      str(list(j.get("paper_claims") or {})))

print()
print("=== [D] 这把刀真的开火（不是恒报「无问题」） ===")
# 摘要 p=0.001 vs 正文唯一同类值 p=0.62 → 必须报 1 条不一致。
# 若端点把摘要核对接错（或退化成空转），本条会红——这正是 v2.48 修的那类病。
check("摘要 p=0.001 vs 正文 p=0.62 → 报文 1 条不一致",
      len(ac.get("mismatches") or []) == 1, str(ac)[:300])
if ac.get("mismatches"):
    _m = ac["mismatches"][0]
    check("不一致条目带中文量名与原文摘录",
          bool(_m.get("kind_cn")) and bool(_m.get("abstract_raw")), str(_m)[:200])

# 无「关键词」锚 → 定位不到，必须 note 说明 + 不伪造 mismatch
r4 = post(PAPER_NOABS.encode("utf-8"))
j4 = r4.get_json() or {}
ac4 = j4.get("abstract_check") or {}
check("无摘要锚时不崩、仍 200", r4.status_code == 200, f"rc={r4.status_code}")
check("定位不到摘要 → note 如实说明（绝不静默报「通过」）",
      bool(ac4.get("note")), repr(ac4.get("note")))
check("定位不到时不虚构不一致",
      not (ac4.get("mismatches") or []), str(ac4.get("mismatches")))

print()
print("=== [E] 表格取证：没表格 = 弃权，不是「通过」 ===")
# .txt 无结构化表格 → 7 族全部弃权。断言弃权被**照原样透出**，
# 而不是被折叠成一句「表格无问题」——那是 v2.38「弃权不是通过」的纪律。
check("txt 无表格 → tables_parsed=0", j.get("tables_parsed") == 0,
      str(j.get("tables_parsed")))
check("无表格时弃权族数 > 0（弃权而非静默通过）",
      len(tf.get("abstained") or {}) > 0, str(list(tf.get("abstained") or {})))
check("无表格时 applicable 为空",
      not (tf.get("applicable") or {}), str(tf.get("applicable")))

print()
print("=== [F] 红线：不判造假、不判作者身份 ===")
_blob = (r.get_data(as_text=True) or "")
for _bad in ("判定为造假", "存在学术不端", "由 AI 生成，构成", "作者身份为"):
    check(f"报告不含「{_bad}」", _bad not in _blob)
check("免责声明在响应里",
      "不判定" in (j.get("disclaimer") or ""), str(j.get("disclaimer"))[:80])

print()
print("=== 汇总 ===")
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
