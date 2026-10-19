"""extract_quantities 统计量抽取测试（v2.49 · 纯本地，零 API）
================================================================
背景：v2.49 修复 r2/β/OR 支路的 500 崩溃——分类正则收得了 APA 无前导零
小数（"R2= .95"），抽取正则却要求等号后以数字开头，re.search 返回 None
后未加守卫直接 .group(1)。W34/W69 两篇真实论文复现。

测试重点：
  1. **该修的必须修**：两篇真实论文的逐字触发片段必须抽出数值、绝不抛异常
  2. **既有口径不回退**：前导零写法（"R² = 0.86"）照常工作
  3. **守卫路径**：抽不到数值时跳过该条，而不是整篇失败
  4. **永不崩**：脏输入 / 空输入不抛异常
  5. **真实 PDF 回放**：本地有 W34/W69 原文则整篇回放（文件不入库，缺了就 SKIP）

跑法：.venv/Scripts/python.exe extract_paper_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_paper import extract_quantities  # noqa: E402

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


def find_kind(quants, kind):
    return [q for q in quants if q.get("kind") == kind]


# ===========================================================================
section("1. W34/W69 逐字触发片段（v2.49 修复的崩溃场景）")
# ===========================================================================
try:
    qs = extract_quantities("本研究发现模型拟合较好，R2= .95，见表 3。")
    r2 = find_kind(qs, "r2")
    check("W34 片段 'R2= .95' 不崩溃且抽出 r2", len(r2) >= 1)
    check("W34 片段数值 = 0.95", any(abs(q["value"] - 0.95) < 1e-9 for q in r2),
          f"got={[q['value'] for q in r2]}")
except Exception as e:  # noqa: BLE001
    check("W34 片段 'R2= .95' 不崩溃且抽出 r2", False, repr(e))

try:
    qs = extract_quantities("回归结果显示 R2=.037，解释力有限。")
    r2 = find_kind(qs, "r2")
    check("W69 片段 'R2=.037' 不崩溃且抽出 r2", len(r2) >= 1)
    check("W69 片段数值 = 0.037", any(abs(q["value"] - 0.037) < 1e-9 for q in r2),
          f"got={[q['value'] for q in r2]}")
except Exception as e:  # noqa: BLE001
    check("W69 片段 'R2=.037' 不崩溃且抽出 r2", False, repr(e))

# ===========================================================================
section("2. 既有口径不回退")
# ===========================================================================
qs = extract_quantities("模型解释了大部分变异，R² = 0.86。")
check("前导零 'R² = 0.86' 照常抽出 0.86",
      any(abs(q["value"] - 0.86) < 1e-9 for q in find_kind(qs, "r2")),
      f"got={[q['value'] for q in find_kind(qs, 'r2')]}")

qs = extract_quantities("/logistic 回归显示 OR = 2.1。")
check("'OR = 2.1' 照常抽出 2.1",
      any(abs(q["value"] - 2.1) < 1e-9 for q in find_kind(qs, "or")),
      f"got={[q['value'] for q in find_kind(qs, 'or')]}")

qs = extract_quantities("相关显著，r = 0.45，Cohen's d = 0.80。")
check("'r = 0.45' 守卫加固后照常抽出",
      any(abs(q["value"] - 0.45) < 1e-9 for q in find_kind(qs, "r")),
      f"got={[q['value'] for q in find_kind(qs, 'r')]}")
check("'d = 0.80' 守卫加固后照常抽出",
      any(abs(q["value"] - 0.80) < 1e-9 for q in find_kind(qs, "d")),
      f"got={[q['value'] for q in find_kind(qs, 'd')]}")

# ===========================================================================
section("3. 守卫路径与脏输入（永不崩）")
# ===========================================================================
for dirty in ["", "   ", "这段话没有任何统计量。", "R2 = ", "R2=", "β = −0.31（分类层暂不识别，只求不崩）",
              "p = .05（p 支路既有口径）", None if False else "表格脚注 *** p<0.01, ** p<0.05"]:
    try:
        extract_quantities(dirty or "")
        check(f"脏输入不崩：{dirty[:18]!r}", True)
    except Exception as e:  # noqa: BLE001
        check(f"脏输入不崩：{dirty[:18]!r}", False, repr(e))

# ===========================================================================
section("4. 真实 PDF 回放（本地有 W34/W69 原文才跑，否则 SKIP）")
# ===========================================================================
_CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "开源赛道", "用来检测的论文-联网扩样-20261019")
import glob as _glob  # noqa: E402
_REAL = {
    "W34": "硕士_MIT_2006_detection*",
    "W69": "硕士_MIT_2017_aging*",
}
_real_present = {}
for _c, _pat in _REAL.items():
    _hits = sorted(_glob.glob(os.path.join(_CORPUS, _pat + ".pdf")))
    if _hits:
        _real_present[_c] = _hits[0]
if not _real_present:
    check("真实 PDF 回放（本地无原文，SKIP）", True, "W34/W69 原文仅本地留存")
else:
    # PDF 文本抽取依赖 werkzeug FileStorage 入口（与产品一致）
    from werkzeug.datastructures import FileStorage  # noqa: E402
    from extract_paper import read_paper_text  # noqa: E402
    for case, path in _real_present.items():
        try:
            with open(path, "rb") as f:
                text = read_paper_text(FileStorage(stream=f, filename=f"{case}.pdf"))
            extract_quantities(text)
            check(f"{case} 整篇回放不崩溃（{len(text)} 字符）", True)
        except Exception as e:  # noqa: BLE001
            check(f"{case} 整篇回放不崩溃", False, repr(e))

# ===========================================================================
print()
print("=" * 72)
print(f"extract_quantities 测试：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
raise SystemExit(1 if FAIL else 0)
