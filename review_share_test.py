"""协作审阅测试（v2.15 · 纯本地，零 API）
========================================
分享是**报告离开本机**的唯一通道，所以这套测试的重心不是"能生成链接"，
而是四条底线真的守得住：

  1. **报告只读**：批注只追加，**绝不改写 markdown / comparisons**。
     这是本功能最重要的不变量 —— 一旦批注能改报告，"协作审阅"就变成了
     一条新的造假通道（改完再说是导师意见）。
  2. **链接不可猜**：token 高熵、不重复；**非法 token 一律 404 而不是 500**
     （URL 是用户可编辑的，路由层曾因此 500）。
  3. **无 XSS**：标题 / 批注 / 锚点里塞 `<script>`、事件属性 → 必须被转义，
     分享页**不含任何 JS**，CSP 收到 `default-src 'none'`。
  4. **本地优先**：内容只落在 `REVIEW_SHARE_DIR`，设 `off` 即彻底关闭；
     关闭时路由返回 503 而**不是**悄悄往别处写。

外加：过期清理、批注上限、脏输入不崩、门禁放行 `/s/`（分享的初衷就是
给没口令的人看）、以及真实 HTTP 往返（表单提交批注 → 重定向 → 能看到）。

跑法：.venv/Scripts/python.exe review_share_test.py
"""
import json
import os

# 硬性纪律 7（与 registry_test / wizard_test 同款）：本套件会连续调用限流路径，
# 必须整体关闭限流，否则 60 秒滑窗内必吃 429（v2.27 扫描报告 P1-1）。
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review_share as rs  # noqa: E402

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


_TMP = tempfile.mkdtemp(prefix="zhilun_review_")
_OLD_ENV = os.environ.get(rs.SHARE_DIR_ENV)


def use_tmp_dir():
    os.environ[rs.SHARE_DIR_ENV] = _TMP


def cleanup():
    if _OLD_ENV is None:
        os.environ.pop(rs.SHARE_DIR_ENV, None)
    else:
        os.environ[rs.SHARE_DIR_ENV] = _OLD_ENV
    shutil.rmtree(_TMP, ignore_errors=True)


MARKDOWN = "# 核查报告\n\n**结论**：t 值对不上。\n\n- 建议补报效应量\n"
COMPARISONS = [
    {"status": "mismatch", "kind": "t", "paper": "2.310", "real": "2.051",
     "summary": {"p": 0.0312}},
    {"status": "ok", "kind": "F", "paper": "4.200", "real": "4.200"},
    {"status": "minor_diff", "kind": "r", "paper": "0.31", "real": "0.30"},
]


def new_share(**kw):
    kw.setdefault("title", "张三-实验一核查")
    kw.setdefault("markdown", MARKDOWN)
    kw.setdefault("comparisons", COMPARISONS)
    kw.setdefault("suggestions", ["补报效应量", "说明剔除标准"])
    return rs.create_share(**kw)


# ===========================================================================
section("一、目录与开关（本地优先，可彻底关闭）")
# ===========================================================================
use_tmp_dir()
check("默认开启", rs.enabled() is True)
check("目录指向环境变量", str(rs.share_dir()).startswith(_TMP))

for off in ("off", "0", "false", "OFF", "none"):
    os.environ[rs.SHARE_DIR_ENV] = off
    if not rs.enabled():
        PASS += 1
        print(f"  [PASS] {off!r} → 关闭")
    else:
        FAIL += 1
        print(f"  [FAIL] {off!r} → 关闭")
use_tmp_dir()

os.environ[rs.SHARE_DIR_ENV] = "off"
try:
    rs.create_share("x", "y")
    check("关闭时 create_share 抛错", False, "没抛")
except rs.ShareError as e:
    check("关闭时 create_share 抛错", True)
    check("关闭文案可读", "关闭" in str(e), str(e))
use_tmp_dir()

# 相对路径按项目根解析
os.environ[rs.SHARE_DIR_ENV] = ".review_share"
_root = os.path.dirname(os.path.abspath(__file__))
check("相对路径解析到项目根",
      str(rs.share_dir()).startswith(_root), str(rs.share_dir()))
use_tmp_dir()


# ===========================================================================
section("二、token：不可猜 + 非法值不炸")
# ===========================================================================
toks = {rs.new_token() for _ in range(200)}
check("200 个 token 互不重复", len(toks) == 200, f"{len(toks)}")
check("token 长度 ≥ 12", all(len(t) >= 12 for t in toks))
check("token 只含 URL 安全字符",
      all(rs.TOKEN_RE.match(t) for t in toks))

# 目录穿越 / 绝对路径 / 空值 —— 一律拦下
for bad in ("../evil", "../../etc/passwd", "a/b", "/etc/passwd", "", ".",
            "..", "x" * 200, "中文", "ab", "a b", "a\nb", "%2e%2e%2f"):
    try:
        rs._path(bad)
        check(f"拦截非法 token {bad!r}", False, "未拦截")
    except rs.ShareError:
        check(f"拦截非法 token {bad!r}", True)

# 合法形态必须放行
good = rs.new_token()
try:
    p = rs._path(good)
    check("合法 token 放行", p.name == good + ".json", str(p))
except rs.ShareError as e:
    check("合法 token 放行", False, str(e))

check("非法 token 查不到 → None（不是抛异常）", rs.load_share("../evil") is None)
check("空 token 查不到 → None", rs.load_share("") is None)
check("不存在的合法 token → None", rs.load_share(rs.new_token()) is None)


# ===========================================================================
section("三、清洗：控制字符 / 截断 / NaN")
# ===========================================================================
check("剔除 \\x00", rs._clean("a\x00b", 100) == "ab")
check("剔除 ANSI 转义", rs._clean("[31m红[0m", 100) == "[31m红[0m")
check("保留换行", "\n" in rs._clean("a\nb", 100))
check("CRLF 归一", rs._clean("a\r\nb", 100) == "a\nb")
check("超长截断", len(rs._clean("x" * 500, 10)) <= 12)
check("None → 空串", rs._clean(None, 10) == "")
check("去首尾空白", rs._clean("  a  ", 10) == "a")

nan_doc = rs._sanitize({"a": float("nan"), "b": float("inf"),
                        "c": float("-inf"), "d": 1.5})
check("NaN → None", nan_doc["a"] is None)
check("+Inf → None", nan_doc["b"] is None)
check("-Inf → None", nan_doc["c"] is None)
check("有限浮点保住", nan_doc["d"] == 1.5)
txt = json.dumps(nan_doc, ensure_ascii=False)
check("产出是合法 JSON（无 NaN 字面量）", "NaN" not in txt and "Infinity" not in txt)

deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
check("深嵌套被收敛", rs._sanitize(deep)["a"]["b"]["c"]["d"] is None)
check("列表截断到 200", len(rs._sanitize(list(range(500)))) == 200)
check("字典截断到 40 键", len(rs._sanitize({str(i): i for i in range(100)})) <= 40)


# ===========================================================================
section("四、创建 / 读取往返")
# ===========================================================================
info = new_share()
check("返回 token", bool(info.get("token")))
check("返回相对 url", info.get("url") == f"/s/{info['token']}", str(info.get("url")))
check("标题保留", info.get("title") == "张三-实验一核查")

doc = rs.load_share(info["token"])
check("能读回", isinstance(doc, dict))
# 注意：入库会做一次「去控制字符 + CRLF 归一 + 去首尾空白」的清洗，
# 所以这里比的是清洗后的形态 —— 尾随换行被去掉是**有意为之**，不是丢内容。
check("markdown 内容不丢", doc["markdown"] == MARKDOWN.strip(),
      repr(doc["markdown"][-20:]))
check("comparisons 条数一致", len(doc["comparisons"]) == 3)
check("suggestions 保留", len(doc["suggestions"]) == 2)
check("kind 默认 audit", doc["kind"] == "audit")
check("初始无批注", doc["annotations"] == [])
check("有创建时间", isinstance(doc.get("created_at"), float))
check("有过期时间", doc["expires_at"] > doc["created_at"])
check("默认 30 天有效",
      29 * 86400 < doc["expires_at"] - doc["created_at"] <= 31 * 86400,
      str(doc["expires_at"] - doc["created_at"]))

# TTL 边界：过小 / 过大都要夹住
d1 = rs.load_share(new_share(ttl_days=0)["token"])
check("ttl=0 夹到 ≥1 天", d1["expires_at"] - d1["created_at"] >= 86399)
d2 = rs.load_share(new_share(ttl_days=99999)["token"])
check("ttl 超大夹到 ≤365 天",
      d2["expires_at"] - d2["created_at"] <= 366 * 86400)
d3 = rs.load_share(new_share(ttl_days="abc")["token"])
check("ttl 非数字回落到默认",
      29 * 86400 < d3["expires_at"] - d3["created_at"] <= 31 * 86400)

# 空标题 / 空正文
d4 = rs.load_share(rs.create_share("", "")["token"])
check("空标题有兜底", d4["title"] == "未命名核查报告", d4["title"])
check("空正文不炸", d4["markdown"] == "")

# 只有 comparisons 没有 markdown 也能建
d5 = rs.load_share(rs.create_share("x", "", comparisons=COMPARISONS)["token"])
check("只给 comparisons 也能建", len(d5["comparisons"]) == 3)

# 坏 comparisons 类型
d6 = rs.load_share(rs.create_share("x", "m", comparisons="不是列表")["token"])
check("comparisons 传字符串 → 收敛为空列表", d6["comparisons"] == [])
d7 = rs.load_share(rs.create_share("x", "m", suggestions=None)["token"])
check("suggestions=None 不炸", d7["suggestions"] == [])

# 持久化是真的落盘了（换一次读取仍可见）
check("文件确实在目录里",
      os.path.exists(os.path.join(_TMP, info["token"] + ".json")))


# ===========================================================================
section("五、报告只读（本功能最重要的不变量）")
# ===========================================================================
info2 = new_share()
before_md = rs.load_share(info2["token"])["markdown"]
before_cmp = json.dumps(rs.load_share(info2["token"])["comparisons"],
                        ensure_ascii=False, sort_keys=True)
before_sug = list(rs.load_share(info2["token"])["suggestions"])

rs.add_annotation(info2["token"], text="这里建议补 Cohen d", author="李老师")
rs.add_annotation(info2["token"], text="剔除标准要写清楚", author="同门小王",
                  anchor="t")
rs.add_annotation(info2["token"], text="删除测试", author="x")
rs.delete_annotation(info2["token"],
                     rs.load_share(info2["token"])["annotations"][-1]["id"])

after = rs.load_share(info2["token"])
check("批注后 markdown 一字未改", after["markdown"] == before_md)
check("批注后 comparisons 一字未改",
      json.dumps(after["comparisons"], ensure_ascii=False, sort_keys=True)
      == before_cmp)
check("批注后 suggestions 未改", after["suggestions"] == before_sug)
check("批注真的留下了 2 条", len(after["annotations"]) == 2)
check("批注内容没串进报告正文", "Cohen" not in after["markdown"])
check("批注带作者", after["annotations"][0]["author"] == "李老师")
check("批注带锚点", after["annotations"][1]["anchor"] == "t")
check("批注带时间", isinstance(after["annotations"][0]["at"], float))
check("批注有 id", bool(after["annotations"][0]["id"]))
check("作者留空 → 匿名",
      rs.add_annotation(info2["token"], text="无名")["author"] == "匿名")


# ===========================================================================
section("六、批注：上限 / 空值 / 删除")
# ===========================================================================
try:
    rs.add_annotation(info2["token"], text="")
    check("空批注被拒", False, "没抛")
except rs.ShareError as e:
    check("空批注被拒", True)
    check("空批注文案可读", "为空" in str(e), str(e))

try:
    rs.add_annotation(rs.new_token(), text="x")
    check("给不存在的链接批注 → 报错", False, "没抛")
except rs.ShareError as e:
    check("给不存在的链接批注 → 报错", True)
    check("报错文案不泄露路径", "链接" in str(e) and _TMP not in str(e), str(e))

# 上限：直接把 annotations 填满
big = rs.create_share("上限测试", "m")
tok = big["token"]
doc = rs.load_share(tok)
doc["annotations"] = [{"id": f"a{i}", "author": "x", "anchor": "",
                       "text": "填充", "at": time.time()}
                      for i in range(rs.MAX_ANNOTATIONS)]
rs._save(doc)
try:
    rs.add_annotation(tok, text="第 301 条")
    check("批注条数上限生效", False, "没抛")
except rs.ShareError as e:
    check("批注条数上限生效", True)
    check("上限文案含具体数字", "300" in str(e), str(e))

check("删不存在的 id → False", rs.delete_annotation(tok, "no-such-id") is False)
check("删完条数不变",
      len(rs.load_share(tok)["annotations"]) == rs.MAX_ANNOTATIONS)
real_id = rs.load_share(tok)["annotations"][0]["id"]
check("删存在的 id → True", rs.delete_annotation(tok, real_id) is True)
check("删完少一条",
      len(rs.load_share(tok)["annotations"]) == rs.MAX_ANNOTATIONS - 1)

# 超长文本被截断而不是无限增长
long_tok = rs.create_share("长文本", "m")["token"]
ann = rs.add_annotation(long_tok, text="字" * 10000,
                        author="名" * 200, anchor="锚" * 500)
check("超长批注被截断", len(ann["text"]) <= rs.MAX_TEXT + 1, str(len(ann["text"])))
check("超长作者被截断", len(ann["author"]) <= rs.MAX_AUTHOR + 1)
check("超长锚点被截断", len(ann["anchor"]) <= rs.MAX_ANCHOR + 1)


# ===========================================================================
section("七、过期与清理")
# ===========================================================================
exp = rs.create_share("会过期", "m", ttl_days=1)["token"]
p = os.path.join(_TMP, exp + ".json")
d = json.loads(open(p, encoding="utf-8").read())
d["expires_at"] = time.time() - 10      # 手动拨到过去
open(p, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False))
check("过期后读不到", rs.load_share(exp) is None)
check("过期文件被顺手删掉", not os.path.exists(p))

alive = rs.create_share("还活着", "m", ttl_days=10)["token"]
# 上面那条已被 load_share 顺手删掉，这里**重新造一个**过期的再 gc
# （否则 gc 清的是 0 条，测试就变成了假绿灯）
exp2 = rs.create_share("也会过期", "m", ttl_days=1)["token"]
p2 = os.path.join(_TMP, exp2 + ".json")
d2g = json.loads(open(p2, encoding="utf-8").read())
d2g["expires_at"] = time.time() - 10
open(p2, "w", encoding="utf-8").write(json.dumps(d2g, ensure_ascii=False))
n = rs.gc_expired()
check("gc 清掉过期的", n >= 1, str(n))
check("gc 后过期文件真的没了", not os.path.exists(p2))
check("gc 不会误删未过期的", rs.load_share(alive) is not None)

# 损坏文件：不能让整个功能崩掉
broken = os.path.join(_TMP, rs.new_token() + ".json")
open(broken, "w", encoding="utf-8").write("{不是 JSON")
check("损坏文件读不到", rs.load_share(os.path.basename(broken)[:-5]) is None)
check("list_shares 跳过损坏文件",
      all(x["token"] for x in rs.list_shares()))
os.remove(broken)


# ===========================================================================
section("八、渲染：无 XSS、无 JS、两种形态")
# ===========================================================================
xss = rs.create_share(
    "<script>alert(1)</script>标题",
    "正文 <img src=x onerror=alert(2)> & <b>粗</b>",
    comparisons=[{"status": "mismatch", "kind": "<svg onload=alert(3)>",
                  "paper": "<script>x</script>", "real": "2.05"},
                 {"status": "ok", "kind": "F", "paper": "4.2", "real": "4.2"}],
    suggestions=["<iframe src=evil>"])
xtok = xss["token"]
rs.add_annotation(xtok, text="<script>alert(4)</script>批注",
                  author="<b>李</b>", anchor="<img onerror=1>")
xh = rs.render_share_html(rs.load_share(xtok), form_action=f"/s/{xtok}")

# 只禁**裸标签**：转义后文本里出现 "onerror=alert(2)" 这种字样是安全的
# （它只是字，不是属性），所以禁的是 `<` 开头的真标签。
for frag in ("<script>alert", "<svg onload", "<iframe", "<img src=x",
             "<b>李</b>"):
    check(f"未出现未转义标签 {frag}", frag not in xh)

check("script 被转义", "&lt;script&gt;" in xh)
# 正面断言：整个 <img ... onerror=...> 被整体转义（属性一起变文本）
check("img 及事件属性整体转义",
      "&lt;img src=x onerror=alert(2)&gt;" in xh)
check("页面不含任何 <script 标签", "<script" not in xh)
check("不含 javascript: 协议", "javascript:" not in xh.lower())
check("声明 noindex", 'name="robots"' in xh and "noindex" in xh)

check("含标题（转义后）", "&lt;script&gt;alert(1)&lt;/script&gt;标题" in xh)
check("含比对表", "逐条比对" in xh)
check("含建议清单", "改进建议" in xh)
check("含报告正文", "正文" in xh)
check("含批注", "alert(4)" in xh)
check("含作者", "李" in xh)
check("有批注表单", 'name="text"' in xh)
check("有提交按钮", 'type="submit"' in xh)
check("有「不代表造假」红线文案", "不代表造假" in xh)
check("有「不构成学术不端认定」", "不构成任何学术不端认定" in xh)
check("状态图标出现", "🔴" in xh and "✅" in xh)

static = rs.render_share_html(rs.load_share(xtok), interactive=False)
check("静态快照无表单", 'name="text"' not in static)
check("静态快照有离线说明", "离线快照" in static)
check("静态快照仍含批注", "alert(4)" in static)

err = rs.render_error_html("<b>出错</b>")
check("错误页也转义", "<b>出错</b>" not in err and "&lt;b&gt;" in err)
check("错误页不回显路径", _TMP not in err)

# 空报告也能渲染（不白屏）
empty = rs.render_share_html(rs.load_share(rs.create_share("空", "")["token"]))
check("空报告能渲染", "协作审阅" in empty and len(empty) > 300)

# 数值格式化：不要出现浮点噪音
num = rs.render_share_html(rs.load_share(rs.create_share(
    "数值", "m", comparisons=[{"status": "ok", "kind": "r",
                               "paper": 0.30000000000000004,
                               "real": 0.3}])["token"]))
check("浮点噪音被格式化", "0.30000000000000004" not in num)


# ===========================================================================
section("九、路由端到端（test_client，真实 HTTP 往返）")
# ===========================================================================
try:
    import app as flask_app
except Exception as e:  # noqa: BLE001
    print(f"  [SKIP] 无法导入 app：{e}")
    flask_app = None

if flask_app is not None:
    c = flask_app.app.test_client()

    r = c.post("/api/review/share", json={"title": "HTTP 测试",
                                          "markdown": MARKDOWN,
                                          "comparisons": COMPARISONS,
                                          "suggestions": ["建议"],
                                          "kind": "audit"})
    check("POST /api/review/share → 200", r.status_code == 200, r.status)
    body = r.get_json()
    check("返回 ok", body.get("ok") is True, str(body)[:200])
    tok = body.get("token")

    r = c.get(f"/s/{tok}")
    check("GET /s/<token> → 200", r.status_code == 200, r.status)
    html = r.get_data(as_text=True)
    check("页面含报告", "核查报告" in html)
    check("页面带 CSP default-src 'none'", "default-src 'none'" in html
          or "default-src 'none'" in r.headers.get("Content-Security-Policy", ""))
    check("页面带 nosniff",
          r.headers.get("X-Content-Type-Options") == "nosniff")
    check("页面带 noindex", "noindex" in r.headers.get("X-Robots-Tag", ""))

    r = c.post(f"/s/{tok}", data={"text": "路由提交的批注",
                                  "author": "导师", "anchor": "t"})
    check("POST 批注 → 302（PRG）", r.status_code == 302, r.status)
    doc = rs.load_share(tok)
    check("批注真的写进去了", len(doc["annotations"]) == 1)
    check("作者正确", doc["annotations"][0]["author"] == "导师")

    r = c.post(f"/s/{tok}", data={"text": "   "})
    check("空批注 → 400 而不是 500", r.status_code == 400, r.status)

    r = c.get(f"/s/{tok}/export")
    check("GET /s/<token>/export → 200", r.status_code == 200, r.status)
    check("导出带附件头",
          "attachment" in r.headers.get("Content-Disposition", ""))
    check("导出是静态快照", "离线快照" in r.get_data(as_text=True))

    r = c.get("/s/no-such-token")
    check("不存在的链接 → 404", r.status_code == 404, r.status)
    r = c.get("/s/..%2f..%2fetc")
    check("穿越 token → 404 不是 500", r.status_code in (404, 400), r.status)
    r = c.get("/s/")
    check("/s/ 空 token → 404", r.status_code == 404, r.status)
    r = c.get("/s/xx/export")
    check("非法 token 的导出 → 404", r.status_code == 404, r.status)

    r = c.get(f"/api/review/{tok}")
    check("GET /api/review/<token> → 200", r.status_code == 200, r.status)
    check("返回 doc", (r.get_json() or {}).get("doc", {}).get("token") == tok)

    r = c.post(f"/api/review/{tok}/annotate",
               json={"text": "JSON 批注", "author": "A"})
    check("JSON 批注 → 200", r.status_code == 200, r.status)
    aid = (r.get_json() or {}).get("annotation", {}).get("id")
    check("返回批注 id", bool(aid))
    r = c.post(f"/api/review/{tok}/annotate", json={"text": ""})
    check("JSON 空批注 → 400", r.status_code == 400, r.status)
    r = c.post(f"/api/review/{tok}/annotate/delete", json={"id": aid})
    check("删除批注 → 200", (r.get_json() or {}).get("deleted") is True)
    r = c.post("/api/review/" + rs.new_token() + "/annotate",
               json={"text": "x"})
    check("给不存在链接批注 → 400", r.status_code == 400, r.status)

    r = c.post("/api/review/share", json={"title": "空"})
    check("空报告 → 400", r.status_code == 400, r.status)

    # 关闭后：503 而不是 500，更不是偷偷写盘
    os.environ[rs.SHARE_DIR_ENV] = "off"
    r = c.post("/api/review/share", json={"title": "x", "markdown": "y"})
    check("关闭后 → 503", r.status_code == 503, r.status)
    use_tmp_dir()


# ===========================================================================
section("十、门禁放行 /s/（分享就是给没口令的人看）")
# ===========================================================================
try:
    import access_guard
    check("/s/ 在放行前缀里", "/s/" in access_guard._EXEMPT_PREFIXES)
    check("is_exempt('/s/abc') 为真", access_guard.is_exempt("/s/abc") is True)
    check("放行不影响 /api/ 仍受保护",
          access_guard.is_exempt("/api/analyze") is False)
    check("/unlock 仍然放行", access_guard.is_exempt("/unlock") is True)
    check("/static/ 仍然放行", access_guard.is_exempt("/static/a.js") is True)
except Exception as e:  # noqa: BLE001
    check("门禁模块可用", False, str(e))


# ===========================================================================
section("十一、脏输入不崩")
# ===========================================================================
dirty = [
    {"title": None, "markdown": None},
    {"title": 123, "markdown": 456, "comparisons": [1, 2, 3]},
    {"title": ["a"], "markdown": {"a": 1}, "suggestions": [None, 1, {"x": 1}]},
    {"title": "x", "markdown": "y", "comparisons": [None, "s", [], {}]},
    {"title": "x" * 9999, "markdown": "y" * 99999},
    {"title": "emoji 🎉", "markdown": "中文测试\n\t制表"},
    {"title": "x", "markdown": "y", "kind": 12345, "ttl_days": -99},
    {"title": "x", "markdown": "y", "suggestions": "不是列表"},
]
ok = 0
for i, kw in enumerate(dirty):
    try:
        inf = rs.create_share(**kw) if "title" in kw else None
        if inf:
            d = rs.load_share(inf["token"])
            rs.render_share_html(d)
            rs.add_annotation(inf["token"], text="脏输入批注")
        ok += 1
    except Exception as e:  # noqa: BLE001
        check(f"脏输入 {i} 不崩", False, f"{type(e).__name__}: {e}")
check(f"全部 {len(dirty)} 组脏输入不崩", ok == len(dirty), f"{ok}/{len(dirty)}")

check("list_shares 有内容", len(rs.list_shares()) > 0)
check("list_shares 不含正文",
      all("markdown" not in x for x in rs.list_shares()))
check("list_shares 按时间倒序",
      all((rs.list_shares()[i]["created_at"] or 0)
          >= (rs.list_shares()[i + 1]["created_at"] or 0)
          for i in range(len(rs.list_shares()) - 1)))


# ===========================================================================
section("十二、管理接口：列表 / 清理（v2.16 补上调用方）")
# ===========================================================================
if flask_app is not None:
    c = flask_app.app.test_client()

    # 路由优先级：静态 /api/review/list 不能被动态 /api/review/<token> 吃掉
    r = c.get("/api/review/list")
    check("GET /api/review/list → 200", r.status_code == 200, r.status)
    body = r.get_json() or {}
    check("返回 items 是列表", isinstance(body.get("items"), list))
    check("列表项只有元信息（不含正文）",
          all("markdown" not in it for it in body.get("items", [])))
    check("列表项含标题/时间/批注数",
          all({"title", "created_at", "n_annotations"} <= set(it)
              for it in body.get("items", [])),
          str(body.get("items", [])[:1]))

    r = c.get("/api/review/list?limit=abc")
    check("limit 非数字不炸 → 200", r.status_code == 200, r.status)
    r = c.get("/api/review/list?limit=1")
    check("limit 生效", len((r.get_json() or {}).get("items", [])) <= 1)

    # gc：只删过期的
    alive_tok = rs.create_share("gc-存活", "m", ttl_days=10)["token"]
    dead_tok = rs.create_share("gc-过期", "m", ttl_days=1)["token"]
    p = os.path.join(_TMP, dead_tok + ".json")
    d = json.loads(open(p, encoding="utf-8").read())
    d["expires_at"] = time.time() - 1
    open(p, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False))

    r = c.post("/api/review/gc")
    check("POST /api/review/gc → 200", r.status_code == 200, r.status)
    check("返回 removed 数量", (r.get_json() or {}).get("removed", -1) >= 1,
          str(r.get_json()))
    check("过期的被清掉", rs.load_share(dead_tok) is None)
    check("未过期的没被误删", rs.load_share(alive_tok) is not None)

    os.environ[rs.SHARE_DIR_ENV] = "off"
    r = c.get("/api/review/list")
    check("关闭后 list → 503", r.status_code == 503, r.status)
    r = c.post("/api/review/gc")
    check("关闭后 gc → 503", r.status_code == 503, r.status)
    use_tmp_dir()


# ===========================================================================
section("十三、体检报告也能分享（/api/datacheck 返回 markdown）")
# ===========================================================================
if flask_app is not None:
    import io as _io

    c = flask_app.app.test_client()
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "examples", "student_scores.csv")
    if os.path.exists(csv_path):
        with open(csv_path, "rb") as fh:
            raw = fh.read()
        r = c.post("/api/upload", data={
            "file": (_io.BytesIO(raw), "student_scores.csv"),
        }, content_type="multipart/form-data")
        check("上传示例数据 → 200", r.status_code == 200, r.status)
        fid = (r.get_json() or {}).get("file_id")
        check("拿到 file_id", bool(fid), str(r.get_json())[:200])

        r = c.post("/api/datacheck", json={"file_id": fid})
        check("POST /api/datacheck → 200", r.status_code == 200, r.status)
        dc = r.get_json() or {}
        check("响应带 markdown", isinstance(dc.get("markdown"), str)
              and len(dc.get("markdown", "")) > 50,
              str(dc.get("markdown"))[:80])
        check("markdown 是体检报告（不是别的）",
              "数据体检报告" in (dc.get("markdown") or ""),
              str(dc.get("markdown"))[:80])

        # 拿它去开一个分享
        r = c.post("/api/review/share", json={
            "title": "体检分享", "markdown": dc.get("markdown", ""),
            "suggestions": ["示例建议"], "kind": "datacheck",
        })
        check("体检报告可创建分享 → 200", r.status_code == 200, r.status)
        tok = (r.get_json() or {}).get("token")
        if tok:
            r = c.get(f"/s/{tok}")
            check("分享页 → 200", r.status_code == 200, r.status)
            html = r.get_data(as_text=True)
            check("分享页标注为数据体检报告", "数据体检报告" in html)
            check("分享页含体检结论", "数据体检报告" in html and len(html) > 500)
    else:
        check("示例数据存在", False, csv_path)


# ===========================================================================
print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
cleanup()
sys.exit(1 if FAIL else 0)
