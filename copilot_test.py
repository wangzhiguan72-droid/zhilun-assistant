"""论文副驾驶 · 流水线 + 写作引擎测试

覆盖：
  1. Pipeline 顺序门控（未完成前置 → 后续 blocked）
  2. Pipeline 状态扫描 / 恢复（跳过已完成阶段）
  3. 上下文桥接（只传摘要、裁剪到 5 行）
  4. 产出文件校验
  5. Claim 抽取（含基线 / 置信度 / 来源）
  6. 写作闸门（无来源不可写、非 high 不进摘要）
  7. 图表清单（支撑关系 / 就绪判据）
  8. 双模板渲染（thesis / latex）
  9. lint 写作自检（正例 0 报错、负例必报错）
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

import app  # noqa: E402
import paper_writer as pw  # noqa: E402
import paper_polisher as ppol  # noqa: E402
import pipeline as pl  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {msg}")
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------------
section("1. Pipeline 顺序门控")
d = tempfile.mkdtemp()
p = pl.Pipeline(d)
st = p.status_dict()
check(st["total"] == 7, f"阶段总数 = 7（实际 {st['total']}）")
check(st["done"] == 0, "初始 0 完成")
check(st["phases"][0]["key"] == "datacheck", "第 0 关 = datacheck（数据体检）")
check(st["phases"][0]["status"] == "pending", "datacheck = pending（无前置）")
check(all(x["status"] == "blocked" for x in st["phases"][1:]),
      "其余阶段全部 blocked（顺序即契约）")
check(p.next_phase().key == "datacheck", "next_phase = datacheck（必须先体检）")

# 第 0 关门控：干净数据 → 放行
import pandas as pd  # noqa: E402

_clean = pd.DataFrame({
    "学号": range(1, 21),
    "q1": [1, 2, 3, 4, 5] * 4,
    "q2": [2, 3, 4, 5, 1] * 4,
    "q3": [3, 4, 5, 1, 2] * 4,
})
_r = p.run_datacheck_gate(_clean, filename="clean.csv")
check(_r["ok"] is True and _r["passed"] is True, "干净数据：体检通过")
check(_r["high"] == 0, "干净数据：0 处高优先级问题")
check(os.path.exists(os.path.join(d, _r["report_file"])), "体检报告已落盘")
check(p.gate_state().get("passed") is True, "gate_state 记录通过")
check(p.next_phase().key == "collect", "体检通过后 next = collect")

# 第 0 关门控：脏数据 → 止步（报告照样落盘，但不放行）
_dirty = pd.DataFrame({
    "学号": [1, 2, 3, 4, 5, 6],
    "分项1": [10, 20, 30, 40, 50, 60],
    "分项2": [10, 20, 30, 40, 50, 60],
    "总分": [20, 40, 60, 80, 100, 125],     # 最后一行合计对不上 → high
})
_r2 = p.run_datacheck_gate(_dirty, filename="dirty.csv")
check(_r2["ok"] is True and _r2["passed"] is False, "脏数据：体检未通过")
check(_r2["high"] > 0, f"脏数据：检出 {_r2['high']} 处高优先级问题")
check(os.path.exists(os.path.join(d, "datacheck_res.md")), "未通过也落盘报告（要能看问题）")
check(p.next_phase().key == "datacheck", "未通过时 next 仍停在 datacheck（门控生效）")
_st2 = p.status_dict()
check(_st2["gate"]["passed"] is False, "status_dict 透出 gate 未通过")
check(all(x["status"] == "blocked" for x in _st2["phases"][1:]),
      "未通过时后续阶段全部 blocked")

# 回到干净数据，恢复放行，继续原有流程
p.run_datacheck_gate(_clean, filename="clean.csv")

# 完成 collect
os.makedirs(os.path.join(d, "papers"), exist_ok=True)
with open(os.path.join(d, "papers", "_index.md"), "w", encoding="utf-8") as f:
    f.write("# 资料索引\n1. 论文A\n2. 论文B\n3. 论文C\n")
p.mark("collect", "done")
check(p.next_phase().key == "survey", "完成 collect 后 next = survey")
st = p.status_dict()
check(st["done"] == 2 and st["progress"] > 0, f"进度 = {st['done']}/7")
check(st["phases"][2]["status"] == "pending", "survey 解锁为 pending")
check(st["phases"][3]["status"] == "blocked", "plan 仍 blocked")

# ---------------------------------------------------------------------------
section("2. 产出文件校验")
v = p.validate("collect")
check(v["ok"] is True, "collect 校验通过")
v2 = p.validate("survey")
check(v2["ok"] is False and "survey_res.md" in v2["outputs_missing"],
      "survey 校验失败并指出缺失文件")
try:
    p.validate("nonexistent")
    check(False, "未知阶段应抛 KeyError")
except KeyError:
    check(True, "未知阶段抛 KeyError")

# ---------------------------------------------------------------------------
section("3. 上下文桥接")
ctx = p.dispatch_context(pl.PHASE_BY_KEY["survey"], {"collect": "3 篇论文，方向：统计建模"})
check(ctx.startswith("/survey"), "桥接串以 /skill 开头")
check("3 篇论文" in ctx, "含前置摘要")
check("survey_res.md" in ctx, "含预期产出")
check("验收标准" in ctx, "含验收标准")
# 裁剪验证：超长摘要只保留前 5 行
long_bridge = "\n".join(f"line{i}" for i in range(20))
ctx2 = p.dispatch_context(pl.PHASE_BY_KEY["survey"], {"collect": long_bridge})
check("line4" in ctx2 and "line5" not in ctx2, "摘要裁剪到 5 行")

# ---------------------------------------------------------------------------
section("4. Claim 抽取（真实分析结果）")
df = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "examples", "student_scores.csv"))
res = [
    app.run_independent_t(df, "gender", "score"),
    app.run_correlation(df, "score", "study_hours"),
    app.run_cronbach_alpha(df, ["score", "study_hours"]),
    app.run_linear_regression(df, "score", ["study_hours"]),
]
claims = pw.build_claim_inventory(res)
check(len(claims) == 4, f"抽出 4 条 claim（实际 {len(claims)}）")
check(all(c.claim_id.startswith("claim-") for c in claims), "claim_id 规范")
check(all(c.source_files for c in claims), "每条 claim 都有来源文件")
check(all(c.claim_text for c in claims), "每条 claim 都有正文")
check(all(c.claim_type == "result" for c in claims), "claim_type = result")
# 含定量陈述
import re as _re
check(all(_re.search(r"\d", c.claim_text) for c in claims), "每条 claim 都含定量陈述")
# 基线
check(all(c.baseline for c in claims), "每条 claim 都声明基线")
# t 检验 claim 的 t 值正确
t_claim = next(c for c in claims if "独立样本" in c.claim_text)
check("-14.093" in t_claim.claim_text, "T 检验 claim 含真实 t = -14.093")
check("0.001" in t_claim.claim_text, "T 检验 claim 含显著性 p < 0.001")

# ---------------------------------------------------------------------------
section("5. 写作闸门（证据契约）")
c_no_src = pw.Claim(claim_id="c-x", claim_text="某结论", claim_type="result", source_files=[])
check(c_no_src.is_writable() is False, "无来源 → 不可写")
check(c_no_src.can_enter_abstract() is False, "无来源 → 不进摘要")
c_low = pw.Claim(claim_id="c-y", claim_text="某结论", claim_type="result",
                 source_files=["a.md"], confidence="low")
check(c_low.is_writable() is True, "有来源 → 可写")
check(c_low.can_enter_abstract() is False, "confidence=low → 不进摘要")
c_high = pw.Claim(claim_id="c-z", claim_text="某结论", claim_type="result",
                  source_files=["a.md"], confidence="high")
check(c_high.can_enter_abstract() is True, "confidence=high → 可进摘要")

# ---------------------------------------------------------------------------
section("6. 图表清单")
figs = pw.build_figures_manifest(res, claims,
                                 {"independent_t": "figures/t.png",
                                  "correlation": "figures/c.png"})
check(len(figs) == 4, f"生成 4 条图条目（实际 {len(figs)}）")
check(all(f.supports_claim_ids for f in figs), "每条图条目都声明支撑的 claim")
check(all(f.is_ready() for f in figs), "所有图条目就绪（section/placement/callout/supports）")
check(all(f.section == pw.SECTION_RESULTS for f in figs), "图归属 main_results")
# 支撑关系双向一致
claim_ids = {c.claim_id for c in claims}
for f in figs:
    check(all(cid in claim_ids for cid in f.supports_claim_ids),
          f"{f.figure_id} 支撑的 claim 都在台账内")

# ---------------------------------------------------------------------------
section("7. 双模板渲染")
b_thesis = pw.build_paper_bundle(res, title="学生成绩研究", template="thesis")
b_latex = pw.build_paper_bundle(res, title="学生成绩研究", template="latex")
check("paper/claim_inventory.md" in b_thesis, "thesis 含 claim_inventory")
check("paper/figures_manifest.md" in b_thesis, "thesis 含 figures_manifest")
check("paper/draft.md" in b_thesis, "thesis 含 draft.md")
check("paper/thesis.md" in b_thesis, "thesis 含 thesis.md")
check("paper/manuscript.tex" in b_latex, "latex 含 manuscript.tex")
check("paper/sections/main_results.tex" in b_latex, "latex 含 sections/main_results.tex")
check("paper/build_paper.sh" in b_latex, "latex 含 build_paper.sh")
check("\\documentclass" in b_latex["paper/manuscript.tex"], "latex 含 documentclass")
check("## 摘要" in b_thesis["paper/thesis.md"], "thesis 含摘要")
# 摘要只用 high 置信度
check("claim-001" in b_thesis["paper/thesis.md"], "摘要锚定 claim-001")

# ---------------------------------------------------------------------------
section("8. lint 写作自检")
issues_thesis = pw.lint_draft(b_thesis["paper/thesis.md"], claims)
issues_latex = pw.lint_draft(b_latex["paper/manuscript.tex"], claims)
check(len(issues_thesis) == 0, f"thesis 无违规（实际 {len(issues_thesis)}）")
check(len(issues_latex) == 0, f"latex 无违规（实际 {len(issues_latex)}）")
# 负向对照：无锚点的结果句必须被抓
bad = "独立样本 T 检验显示 t = 3.2，p = 0.01。"
check(any(i["level"] == "error" for i in pw.lint_draft(bad, claims)),
      "负例：无锚点结果句被拦下")
good = "（claim-001）独立样本 T 检验显示 t = 3.2，p = 0.01。"
check(len(pw.lint_draft(good, claims)) == 0, "正例：有锚点结果句通过")
# 大词无数字必须报警
praise = "该模型表现出显著优势。"
check(any(i["level"] == "warn" for i in pw.lint_draft(praise, claims)),
      "负例：无数字的大词被警告")

# ---------------------------------------------------------------------------
section("9. 阶段目录 API")
cat = pl.phase_catalog()
check(len(cat) == 7, f"阶段目录 7 项（含第 0 关 数据体检，实际 {len(cat)}）")
check(all("key" in c and "name" in c and "outputs" in c for c in cat),
      "目录项字段齐备")
check(cat[0]["key"] == "datacheck" and cat[-1]["key"] == "write",
      "顺序：datacheck → write")
check(cat[0].get("gate") is True, "第 0 关标记为门控（gate=True）")

# ---------------------------------------------------------------------------
section("10. Copilot API 端到端（Flask test_client）")
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
import io  # noqa: E402
client = app.app.test_client()

# 上传
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "examples", "student_scores.csv"), "rb") as f:
    r = client.post("/api/upload",
                    data={"file": (io.BytesIO(f.read()), "student_scores.csv")},
                    content_type="multipart/form-data")
up = r.get_json()
check(up.get("ok") is True, "上传成功（test_client）")
fid = up.get("file_id")

r = client.post("/api/copilot/phases")
check(r.status_code == 200 and len(r.get_json()["phases"]) == 7,
      "GET /api/copilot/phases 返回 7 阶段（含第 0 关）")

r = client.post("/api/copilot/status", json={})
st = r.get_json()
check(st.get("ok") is True and "next_phase" in st, "POST /api/copilot/status 可用")

r = client.post("/api/copilot/paper", json={
    "file_id": fid, "title": "API 端到端测试", "template": "thesis",
    "analyses": [
        {"method": "independent_t", "group_col": "gender", "value_col": "score"},
        {"method": "correlation", "value_col": "score", "value_col2": "study_hours"},
    ],
})
o = r.get_json()
check(o.get("ok") is True, "论文生成 API 成功")
check(o.get("claim_count") == 2, f"生成 2 条 claim（实际 {o.get('claim_count')}）")
check(len(o.get("lint", [])) == 0, "生成稿写作自检 0 违规")
check("paper/claim_inventory.md" in o.get("written", []), "已写入 claim_inventory.md")
check("paper/thesis.md" in o.get("written", []), "已写入 thesis.md")

r = client.post("/api/copilot/paper", json={
    "file_id": fid, "title": "LaTeX 测试", "template": "latex",
    "analyses": [{"method": "independent_t", "group_col": "gender", "value_col": "score"}],
})
o2 = r.get_json()
check("paper/manuscript.tex" in o2.get("written", []), "latex 模板写入 manuscript.tex")

# 错误路径
r = client.post("/api/copilot/paper", json={"file_id": fid})
check(r.status_code == 400, "缺 analyses → 400")
r = client.post("/api/copilot/validate", json={})
check(r.status_code == 400, "缺 phase → 400")
r = client.post("/api/copilot/mark", json={"phase": "bogus"})
check(r.status_code == 400, "未知阶段 → 400")
r = client.post("/api/copilot/mark", json={"phase": "collect", "status": "done"})
check(r.get_json().get("ok") is True, "合法 mark 成功")

# 首页含副驾驶 Tab
r = client.get("/")
check("tab-copilot" in r.get_data(as_text=True), "首页含论文副驾驶 Tab")

# ---------------------------------------------------------------------------
section("11. LLM 润色层（证据闸门约束）")
# 用真实统计结果造一份底稿，作为润色输入
_res = [
    app.run_independent_t(df, "gender", "score"),
    app.run_correlation(df, "score", "study_hours"),
]
_pclaims = pw.build_claim_inventory(_res)
_base_thesis = pw.build_paper_bundle(_res, title="润色测试", template="thesis")["paper/thesis.md"]
_base_latex = pw.build_paper_bundle(_res, title="润色测试", template="latex")["paper/manuscript.tex"]


def _fake_ok(user_prompt, system, max_tokens=3000):
    """模拟一次「合规」润色：轻度改措辞，保留全部锚点与数字。"""
    out = user_prompt.replace("本研究围绕", "本文围绕", 1)
    out = out.replace("本文关注", "本文聚焦于", 1)
    return out, None, "fake-model"


def _fake_drop_anchor(user_prompt, system, max_tokens=3000):
    """模拟一次「污染」润色：丢掉 claim-001 全部锚点（含图表 callout 中的引用）。"""
    return user_prompt.replace("claim-001", ""), None, "fake-model"


def _fake_change_num(user_prompt, system, max_tokens=3000):
    """模拟一次「污染」润色：篡改了统计量数字。"""
    return user_prompt.replace("-14.093", "-99.999", 1), None, "fake-model"


def _fake_fail(*a, **k):
    """模拟 LLM 不可用 / 无 Key。"""
    return None, "LLM 不可用：未配置 API Key", ""


# 11.1 合规润色：锚点 + 数字保真，且正文确实被改
_orig = ppol._call_llm
ppol._call_llm = _fake_ok
polished, meta = ppol.polish_paper(_base_thesis, _pclaims, template="thesis")
check(meta.get("llm_used") is True, "合规润色：llm_used=True")
check(polished != _base_thesis, "合规润色：正文确实被润色")
check(all(c.claim_id in polished for c in _pclaims), "合规润色：所有 claim 锚点保留")
check(ppol._validate_polish(_base_thesis, polished, _pclaims)[0] is True, "合规润色：通过证据校验")
check(meta.get("lint_after", 1) == 0, "合规润色：润色稿 0 违规")
ppol._call_llm = _orig

# 11.2 污染（丢锚点）→ 丢弃润色稿，保留原底稿，标记 rejected
ppol._call_llm = _fake_drop_anchor
polished2, meta2 = ppol.polish_paper(_base_thesis, _pclaims, template="thesis", force=True)
check(meta2.get("rejected") is True, "丢锚点：判定为污染稿 rejected=True")
check(polished2 == _base_thesis, "丢锚点：静默回退到原底稿")
ppol._call_llm = _orig

# 11.3 污染（篡改数字）→ 丢弃
ppol._call_llm = _fake_change_num
polished3, meta3 = ppol.polish_paper(_base_thesis, _pclaims, template="thesis", force=True)
check(meta3.get("rejected") is True, "改数字：判定为污染稿 rejected=True")
check(polished3 == _base_thesis, "改数字：保留原底稿")
ppol._call_llm = _orig

# 11.4 LLM 不可用 → 不阻断，返回原底稿（llm_used=False）
ppol._call_llm = _fake_fail
polished4, meta4 = ppol.polish_paper(_base_thesis, _pclaims, template="thesis", force=True)
check(meta4.get("llm_used") is False, "无 LLM：llm_used=False（静默降级）")
check(polished4 == _base_thesis, "无 LLM：返回零幻觉底稿")
ppol._call_llm = _orig

# 11.5 缓存命中：相同底稿二次润色不调 LLM（先清空，避免与 11.1 串缓存）
ppol.llm_cache.clear()
_calls = {"n": 0}


def _fake_ok_count(*a, **k):
    _calls["n"] += 1
    return _fake_ok(*a, **k)


ppol._call_llm = _fake_ok_count
ppol.polish_paper(_base_thesis, _pclaims, template="thesis")          # miss → 调用
_, meta_cached = ppol.polish_paper(_base_thesis, _pclaims, template="thesis")  # hit
check(meta_cached.get("cached") is True, "缓存：二次润色命中缓存 cached=True")
check(_calls["n"] == 1, "缓存：命中后未再调用 LLM（实际调用 %d 次）" % _calls["n"])
ppol._call_llm = _orig

# 11.6 polish_bundle 不触碰证据真源文件
_org = ppol._call_llm
ppol._call_llm = _fake_ok
_bundle = pw.build_paper_bundle(_res, title="润色测试", template="thesis")
_new_bundle, _bmeta = ppol.polish_bundle(_bundle, _pclaims, template="thesis")
check(_new_bundle["paper/claim_inventory.md"] == _bundle["paper/claim_inventory.md"],
      "bundle 润色：claim_inventory.md 未被改动（证据真源）")
check(_new_bundle["paper/figures_manifest.md"] == _bundle["paper/figures_manifest.md"],
      "bundle 润色：figures_manifest.md 未被改动")
check(_new_bundle["paper/thesis.md"] != _bundle["paper/thesis.md"],
      "bundle 润色：thesis.md 已被润色")
check(_bmeta.get("llm_used") is True, "bundle 润色：llm_used=True")
ppol._call_llm = _org

# 11.7 latex 底稿同样受数字保真约束（force 绕过缓存，确保真正调用 mock）
ppol.llm_cache.clear()
ppol._call_llm = _fake_change_num
polished_l, metal = ppol.polish_paper(_base_latex, _pclaims, template="latex", force=True)
check(metal.get("rejected") is True, "latex 改数字：rejected=True")
check(polished_l == _base_latex, "latex 改数字：保留原底稿")
ppol._call_llm = _org

print("\n" + "=" * 70)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
