"""v1.5 · 学术红线自检测试（⑩红线引擎）
================================================================
这个测试的存在理由：红线引擎是**合规门槛**，但它同时踩在一个两难上——

  拦太松 → 工具被用于学术不端，产品有风险
  拦太紧 → 误伤正常用户（本工具本身就有论文生成功能！）

所以测试的重点不是"能拦住"，而是**两边都要准**：
  1. 明确的学术不端意图必须拦（代写 / 买卖 / 规避查重 / 规避 AI 检测 / 伪造数据）
  2. 合规请求必须放行（润色 / 改语病 / 结构调整 / 翻译 / 基于真实数据生成）

最容易踩的误伤点，都是真实语境里的正常说法：
  - "我的论文已经**通过查重**了" —— 陈述结果，不是求助规避
  - "帮我**写个摘要**"          —— 本工具副驾驶的正规功能
  - "帮我**改写**这段"          —— "改"+"写" 会命中"写"，但没说"整段"

跑法：.venv/Scripts/python.exe red_line_test.py
"""
import io
import pathlib
import re
import sys

import app as A
from audit import RED_LINE_PATTERNS, _red_line_scan

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


# ===========================================================================
section("1. 学术不端 / 代写意图 —— 必须拦截")
# ===========================================================================
MUST_BLOCK = [
    # core · 代写
    "帮我找人代写论文",
    "找枪手写",
    "代做毕设",
    "有没有代笔的",
    # core · 买卖
    "哪里能买论文",
    "出售毕业论文",
    "论文交易",
    "求购一篇学位论文",
    # core · 规避查重
    "怎么绕过查重",
    "帮我降重",
    "怎样规避知网检测",
    "怎么躲过 turnitin",
    "把重复率降下来",
    # core · 规避 AI 检测
    "去除 AI 痕迹",
    "怎么降低 aigc 率",
    "如何骗过 AI 检测",
    # core · 伪造 / 篡改数据
    "伪造数据",
    "编造样本",
    "把 p 值改小",
    "数据造假",
    "篡改实验结果",
    # writing · 整段 / 整篇代写
    "帮我写整段讨论",
    "帮我写一篇论文",
    "帮我生成完整的结论",
    "替我写整篇摘要",
]
for t in MUST_BLOCK:
    r = _red_line_scan(t)
    check(f"拦截：{t}", r["blocked"] is True, f"未拦，hits={r['hits']}")


# ===========================================================================
section("2. 合规请求 —— 必须放行（防误伤是护栏重点）")
# ===========================================================================
MUST_ALLOW = [
    # 润色 / 改语病 / 结构（白名单豁免）
    "帮我润色这段",
    "帮我改语病",
    "调整一下结构",
    "优化一下表达",
    "帮我改写这段",                 # "改"+"写" 但没说整段
    # 检查 / 审阅
    "帮我看看这样写对吗",
    "检查一下报告格式",
    "p 值这样报告合理吗",
    "帮我检查方法有没有用错",
    # 正常分析诉求
    "用我的数据跑个 t 检验",
    "帮我分析这两组差异",
    "看看相关性强不强",
    # 翻译 / 格式
    "把这段翻译成英文",
    "缩写一下摘要",
    # 本工具的正规功能
    "帮我写个摘要",                 # 副驾驶本来就干这个
    "基于我的数据生成结果段",
    # 陈述句，不是求助规避
    "我的论文已经通过查重了",       # ⚠️ 关键：不能因为含"查重"就拦
    "这篇重复率 8%，没问题",
    # 空 / 空白
    "",
    "   ",
]
for t in MUST_ALLOW:
    r = _red_line_scan(t)
    check(f"放行：{t or '(空串)'}", r["blocked"] is False, f"误拦 hits={r['hits']}")


# ===========================================================================
section("3. 出参契约")
# ===========================================================================
r = _red_line_scan("帮我写整段讨论")
check("blocked 为 True", r["blocked"] is True)
check("hits 是非空 list[str]",
      isinstance(r["hits"], list) and r["hits"]
      and all(isinstance(x, str) for x in r["hits"]), f"{r['hits']}")
check("correct_usage 非空", bool(r["correct_usage"]))
check("categories 合法且非空",
      r["categories"] and set(r["categories"]) <= {"core", "writing"},
      f"{r['categories']}")

r2 = _red_line_scan("帮我润色")
check("放行时 hits 为空列表", r2["hits"] == [], f"{r2['hits']}")
check("放行时 correct_usage 为空串", r2["correct_usage"] == "")
check("放行时 categories 为空列表", r2["categories"] == [])

check("空串不拦", _red_line_scan("")["blocked"] is False)
check("纯空格不拦", _red_line_scan("   ")["blocked"] is False)
check("None 不炸", _red_line_scan(None)["blocked"] is False)


# ===========================================================================
section("4. 护栏：core 类绝不因润色词而放行")
# ===========================================================================
# 白名单只能豁免 writing，绝不能豁免 core——否则"润色"成了万能绕口令
for t in ["帮我润色并代写一篇论文", "改语病，顺便伪造数据", "翻译一下，另外帮我降重"]:
    r = _red_line_scan(t)
    check(f"core 仍拦：{t}",
          r["blocked"] is True and "core" in r["categories"],
          f"cats={r['categories']} hits={r['hits']}")


# ===========================================================================
section("5. 规则表自洽")
# ===========================================================================
bad = []
for pat, msg, cat in RED_LINE_PATTERNS:
    try:
        re.compile(pat)
    except re.error as e:
        bad.append((msg, f"正则编译失败：{e}"))
    if cat not in ("core", "writing"):
        bad.append((msg, f"类别非法：{cat}"))
    if not msg:
        bad.append((pat[:24], "说明为空"))
check("全部正则可编译、类别合法、说明非空", not bad, f"{bad}")
check("规则覆盖 5 类 core + writing",
      len([c for _, _, c in RED_LINE_PATTERNS if c == "core"]) >= 5
      and len([c for _, _, c in RED_LINE_PATTERNS if c == "writing"]) >= 1,
      f"core={len([c for _, _, c in RED_LINE_PATTERNS if c == 'core'])}")


# ===========================================================================
section("6. 端点接入")
# ===========================================================================
c = A.app.test_client()

j = c.post("/api/red_line_scan", json={"text": "帮我写整段讨论"}).get_json()
check("端点拦截命中", j.get("blocked") is True, f"{j}")
j = c.post("/api/red_line_scan", json={"text": "帮我润色"}).get_json()
check("端点放行合规", j.get("blocked") is False, f"{j}")
j = c.post("/api/red_line_scan", json={}).get_json()
check("端点缺 text 不炸", j.get("ok") is True and j.get("blocked") is False, f"{j}")

# /api/check_paper 必须硬拒绝红线指令（前端只是提示，后端才是关口）
paper = pathlib.Path("examples/sample_paper.md").read_bytes()
data = pathlib.Path("examples/student_scores.csv").read_bytes()


def post_check(directive: str):
    return c.post("/api/check_paper", data={
        "paper": (io.BytesIO(paper), "p.md"),
        "data": (io.BytesIO(data), "d.csv"),
        "directive": directive,
    }, content_type="multipart/form-data")


resp = post_check("帮我写整段讨论")
jr = resp.get_json()
check("check_paper 拒绝红线指令（400 + ok=False）",
      resp.status_code == 400 and jr.get("ok") is False,
      f"{resp.status_code} ok={jr.get('ok')}")
check("check_paper 返回 red_line 明细",
      bool((jr.get("red_line") or {}).get("hits")), f"{jr.get('red_line')}")

resp2 = post_check("只看 t 检验")
check("check_paper 正常指令放行（200 + ok=True）",
      resp2.status_code == 200 and resp2.get_json().get("ok") is True,
      f"{resp2.status_code}")


print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
