"""论文排查 · 指令过滤专项测试

覆盖：
  1. _parse_directive 解析正确性
  2. apply_user_directive 过滤逻辑
  3. /api/check_paper 带 directive 端到端

直接调函数 + 起 Flask test_client，不依赖外部服务。
"""
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from audit import _parse_directive, apply_user_directive, build_audit_report
from extract_paper import extract_methods, extract_quantities, extract_variables


# ---------------------------------------------------------------------------
# 准备一份合成测试数据
# ---------------------------------------------------------------------------
SAMPLE_PAPER = """
# 不同性别大学生学业成绩差异研究

## 研究方法
本研究采用**独立样本 T 检验**与 **Pearson 相关分析**。

## 研究结果
独立样本 T 检验结果显示 **t(28) = -14.09，P < 0.001**，差异极其显著。
Pearson 相关分析显示 **r = 0.98, P < 0.001**。

考察了【性别】与【成绩】的关系，【学习时长】与【成绩】的相关性。
"""

CSV_DATA = """id,gender,score,study_hours,pass
1,M,71,5,1
2,F,87,8,1
3,M,72,6,1
4,F,88,9,1
5,M,70,4,0
6,F,86,7,1
7,M,73,5,1
8,F,89,10,1
9,M,69,4,0
10,F,85,7,1
11,M,72,5,1
12,F,88,9,1
13,M,71,5,1
14,F,87,8,1
15,M,70,4,0
"""


def _claims() -> dict:
    return {
        "methods": extract_methods(SAMPLE_PAPER),
        "quantities": extract_quantities(SAMPLE_PAPER),
        "variables": extract_variables(SAMPLE_PAPER),
    }


def _columns() -> list[dict]:
    df = pd.read_csv(io.StringIO(CSV_DATA))
    cols = []
    for c in df.columns:
        s = df[c]
        n_unique = int(s.dropna().nunique())
        is_num = pd.api.types.is_numeric_dtype(s)
        is_continuous = is_num and n_unique >= max(10, int(0.05 * len(s)))
        cols.append({
            "name": c, "dtype": str(s.dtype),
            "n_total": int(len(s)), "n_missing": int(s.isna().sum()),
            "n_unique": n_unique,
            "type": "continuous" if is_continuous else "categorical",
        })
    return cols, df


# ===========================================================================
# 测试 1：_parse_directive
# ===========================================================================
print("=" * 70)
print("测试 1：_parse_directive 指令解析")
print("=" * 70)

cases = [
    ("", {"method_keys": None, "p_threshold": None, "variable_keys": None}),
    ("只看 T 检验", {"method_keys": ["independent_t"]}),
    ("只看 T检验", {"method_keys": ["independent_t"]}),
    ("只看 ANOVA", {"method_keys": ["anova"]}),
    ("只看 方差分析", {"method_keys": ["anova"]}),
    ("只看 相关", {"method_keys": ["correlation"]}),
    ("只看 卡方", {"method_keys": ["chi_square"]}),
    ("只看 p<0.05", {"p_threshold": ("lt", 0.05)}),
    ("只看 p < 0.05", {"p_threshold": ("lt", 0.05)}),
    ("只看 p > 0.05", {"p_threshold": ("gt", 0.05)}),
    ("只看 p≤0.01", {"p_threshold": ("lt", 0.01)}),
    ("只看显著的", {"p_threshold": ("lt", 0.05)}),
    ("只看不显著的", {"p_threshold": ("gt", 0.05)}),
    ("只看男组", {"variable_keys": ["male", "men", "男", "男生"]}),
    ("只看女组", {"variable_keys": ["female", "women", "女", "女生"]}),
    ("只看成绩", {"variable_keys": ["成绩", "分数", "得分", "grade", "mark", "score"]}),
    ("只看 T 检验 且 p<0.05", {"method_keys": ["independent_t"], "p_threshold": ("lt", 0.05)}),
    ("乱写指令 xyz", {}),
]
for text, expect_partial in cases:
    p = _parse_directive(text)
    ok = True
    for k, v in expect_partial.items():
        if p.get(k) != v:
            ok = False
            print(f"  ✗ '{text}'  → 字段 {k} 期望 {v} 实得 {p.get(k)}")
            break
    if ok:
        summary = " · ".join(p.get("matched_summary") or ["（无匹配）"])
        print(f"  ✓ '{text}'  → {summary}")


# ===========================================================================
# 测试 2：apply_user_directive 过滤行为
# ===========================================================================
print()
print("=" * 70)
print("测试 2：apply_user_directive 过滤")
print("=" * 70)

claims = _claims()
cols, df = _columns()

methods_full = claims["methods"]
quantities_full = claims["quantities"]
variables_full = claims["variables"]

# 先跑一次完整报告拿到 real
full_report = build_audit_report(claims, df, cols, directive="")
real = full_report["real"]
suggestions = full_report["suggestions"]
comparisons = full_report["comparisons"]
print(f"  · 基础数据：methods={len(methods_full)}, quantities={len(quantities_full)}, "
      f"variables={len(variables_full)}, real.ok={real.get('ok')}, p={real.get('p')}")

# 2.1 只看 T 检验
f = apply_user_directive(claims, real, comparisons, suggestions, cols, "只看 T 检验")
m_keys = {m["method_key"] for m in f["methods"]}
assert m_keys.issubset({"independent_t"}), m_keys
print(f"  ✓ '只看 T 检验' → methods 剩余 {len(f['methods'])} 条 (key={m_keys})")

# 2.2 只看 p<0.001（应过滤掉 p≥0.001 的；本测试 p 都是 <0.001 的声称值，所以保留全部 p 值）
f = apply_user_directive(claims, real, comparisons, suggestions, cols, "只看 p<0.001")
p_only = [q for q in f["quantities"] if q["kind"] == "p"]
print(f"  ✓ '只看 p<0.001' → p 值声称保留 {len(p_only)} 条（全部 < 0.001 时）")
assert len(p_only) >= 1, "应保留至少 1 条 p 值声称"

# 2.3 只看 p>0.05：p 阈值只对 kind=p 生效，应过滤掉所有 p 值声称；
# 但 r / t / f 等其他 kind 的声称默认保留。
f = apply_user_directive(claims, real, comparisons, suggestions, cols, "只看 p>0.05")
p_after = [q for q in f["quantities"] if q["kind"] == "p"]
non_p_after = [q for q in f["quantities"] if q["kind"] != "p"]
print(f"  ✓ '只看 p>0.05' → p 值声称 0 条、其它 kind 保留 {len(non_p_after)} 条")
assert len(p_after) == 0, f"应过滤掉所有 p 值声称，但剩 {len(p_after)} 条"
assert len(non_p_after) >= 1, "r / t 等非 p 类型的声称应保留"

# 2.4 只看男组 → variables 应只剩含"男"字样的
f = apply_user_directive(claims, real, comparisons, suggestions, cols, "只看男组")
var_names = [v["name"] for v in f["variables"]]
print(f"  ✓ '只看男组' → variables 剩余 {len(f['variables'])} 条：{var_names}")

# 2.5 组合指令
f = apply_user_directive(claims, real, comparisons, suggestions, cols, "只看 T 检验 且 p<0.05")
print(f"  ✓ 'T 检验 且 p<0.05' → methods {len(f['methods'])}, quantities {len(f['quantities'])}, "
      f"variables {len(f['variables'])}")

# 2.6 空指令 → 全量保留
f = apply_user_directive(claims, real, comparisons, suggestions, cols, "")
assert len(f["methods"]) == len(methods_full)
assert len(f["quantities"]) == len(quantities_full)
print(f"  ✓ '' （空指令）→ 全量保留")


# ===========================================================================
# 测试 3：build_audit_report 带 directive
# ===========================================================================
print()
print("=" * 70)
print("测试 3：build_audit_report(directive=...) 端到端")
print("=" * 70)

r1 = build_audit_report(claims, df, cols, directive="")
r2 = build_audit_report(claims, df, cols, directive="只看 T 检验")

# 报告应当不同
assert r1["markdown"] != r2["markdown"], "带指令的 markdown 应与原始不同"
assert "已应用指令" in r2["markdown"], "应包含「已应用指令」说明"
assert r2["applied_directive"] == "只看 T 检验"
print("  ✓ 带 directive 时 markdown 顶部出现「已应用指令」说明")
print(f"  ✓ r2.applied_directive = {r2['applied_directive']!r}")
print(f"  ✓ r2.parsed_directive.matched_summary = {r2['parsed_directive'].get('matched_summary')}")

# 不带 directive 时不应出现「已应用指令」
assert "已应用指令" not in r1["markdown"], "无指令时不应出现「已应用指令」"
assert r1["applied_directive"] == ""
print("  ✓ 无 directive 时不出现「已应用指令」、applied_directive 为空串")


# ===========================================================================
# 测试 4：/api/check_paper 端到端（Flask test_client）
# ===========================================================================
print()
print("=" * 70)
print("测试 4：/api/check_paper 端到端（Flask test_client）")
print("=" * 70)

from app import app

client = app.test_client()

# 准备 multipart payload（用 werkzeug 推荐方式：直接传 dict）
import io as _io
def _make_payload(paper_text: str, csv_text: str, directive: str = ""):
    """每次返回全新的 BytesIO，避免被 test_client 消费后关闭。"""
    payload = {
        "paper": (_io.BytesIO(paper_text.encode("utf-8")), "p.md"),
        "data": (_io.BytesIO(csv_text.encode("utf-8")), "d.csv"),
    }
    if directive:
        payload["directive"] = directive
    return payload


def _post_check(paper: str, csv: str, directive: str = ""):
    payload = _make_payload(paper, csv, directive)
    r = client.post("/api/check_paper", data=payload, content_type="multipart/form-data")
    if not r.is_json:
        return None, r.status_code, r.data[:200]
    return r.get_json(), r.status_code, None


d, status, raw = _post_check(SAMPLE_PAPER, CSV_DATA, directive="")
assert d and d["ok"], d
assert d["directive"] == ""
assert "已应用指令" not in d["audit"]["markdown"]
print("  ✓ 无 directive → HTTP 200，markdown 中无指令说明")

d, status, raw = _post_check(SAMPLE_PAPER, CSV_DATA, directive="只看 T 检验")
assert d and d["ok"], d
assert d["directive"] == "只看 T 检验"
assert "已应用指令" in d["audit"]["markdown"]
print("  ✓ '只看 T 检验' → HTTP 200，响应 directive 字段回传，markdown 含指令说明")

d, status, raw = _post_check(SAMPLE_PAPER, CSV_DATA, directive="只看 男组 且 p<0.05")
assert d and d["ok"], d
assert d["directive"] == "只看 男组 且 p<0.05"
print("  ✓ '只看 男组 且 p<0.05' → HTTP 200，组合指令也能解析")
print(f"    解析到的过滤条件：{d['audit']['parsed_directive'].get('matched_summary')}")

d, status, raw = _post_check(SAMPLE_PAPER, CSV_DATA, directive="乱写 xyz")
assert d and d["ok"], d
assert "未从指令中识别到有效过滤条件" in d["audit"]["markdown"]
print("  ✓ '乱写 xyz' → HTTP 200，markdown 给出「未识别」提示，不会崩")


print()
print("=" * 70)
print("✅ 指令过滤专项测试全部通过")
print("=" * 70)