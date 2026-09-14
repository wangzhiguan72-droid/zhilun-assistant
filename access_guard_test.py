"""访问门禁测试（v2.13 · 纯本地，零 API）
================================================
被测模块：`access_guard.py`（`ACCESS_CODE` 环境变量一句话加密码）

本模块要防的事故：
  1. **死锁** —— 登录页本身被门禁拦住，用户永远进不来。这属于一级事故
     （功能整个不可用，且没有自救入口）。必须有护栏。
  2. **不配置就生效** —— 本地开发/桌面端/测试没设 ACCESS_CODE 却被要求输口令。
     这会毁掉"双击即用"的核心体验。
  3. **口令泄漏进 cookie** —— cookie 里若存明文口令，被看到就等于被拿到。
  4. **假解锁** —— 错口令/伪造 cookie 被放行。
  5. **改口令后旧 cookie 仍有效** —— 这是运维逃生手段，必须真的生效。
  6. **时序侧信道** —— 用 `==` 比较口令会因短路提前返回。
  7. **崩溃** —— 脏输入 / 缺字段不得抛异常。

跑法：.venv/Scripts/python.exe access_guard_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import access_guard as ag  # noqa: E402

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


def set_code(code):
    """设置/清除 ACCESS_CODE。空串与 None 都表示"门禁关闭"。"""
    if code is None:
        os.environ.pop("ACCESS_CODE", None)
    else:
        os.environ["ACCESS_CODE"] = code


# 极简 request 替身（不依赖 Flask，纯逻辑可测）
class FakeReq:
    def __init__(self, path="/", method="GET", cookies=None, form=None,
                 secure=False, xfp=None):
        self.path = path
        self.method = method
        self.cookies = cookies or {}
        self.form = form or {}
        self.is_secure = secure
        self.headers = {}
        if xfp:
            self.headers["X-Forwarded-Proto"] = xfp


# ===========================================================================
section("1. 开关：不配置就绝不生效（本地零干扰 —— 最高优先级）")
# ===========================================================================

set_code(None)
check("1.1 未设 ACCESS_CODE → enabled() 为 False", ag.enabled() is False)
check("1.2 未设时空串也是关闭", ag.access_code() == "")
check("1.3 未设时任意请求被放行（gate 返回 None）",
      ag.gate(FakeReq("/api/analyze", "POST")) is None)
check("1.4 未设时 cookie 检查恒为 True（不拦）",
      ag.check_cookie(None) is True and ag.check_cookie("垃圾") is True)
check("1.5 未设时 check_code 恒为 True",
      ag.check_code("") is True and ag.check_code("随便") is True)

set_code("   ")
check("1.6 纯空白视同未配置（避免误开）", ag.enabled() is False)
set_code("")
check("1.7 空串视同未配置", ag.enabled() is False)

set_code("abc123")
check("1.8 设了口令 → enabled() 为 True", ag.enabled() is True)
check("1.9 口令自动去首尾空格",
      (set_code("  abc123  "), ag.access_code() == "abc123")[1])


# ===========================================================================
section("2. 死锁护栏：必须放行的路径")
# ===========================================================================

set_code("abc123")
for path in ("/unlock", "/health", "/favicon.ico",
             "/static/app.js", "/static/css/main.css"):
    check(f"2.1 {path} 必须放行（否则用户无法自救）",
          ag.gate(FakeReq(path)) is None,
          f"实际被拦：{ag.gate(FakeReq(path))!r}")

check("2.2 首页被拦下（要输口令）",
      ag.gate(FakeReq("/")) is not None)
check("2.3 API 被拦下",
      ag.gate(FakeReq("/api/analyze", "POST")) is not None)
check("2.4 /unlock 的 POST 也放行",
      ag.gate(FakeReq("/unlock", "POST")) is None)


# ===========================================================================
section("3. 该拦的必须拦：未解锁一律挡在门外")
# ===========================================================================

for path, method in [("/", "GET"), ("/api/analyze", "POST"),
                     ("/api/upload", "POST"), ("/api/chat", "POST")]:
    check(f"3.1 未解锁 {method} {path} 被拦",
          ag.gate(FakeReq(path, method)) is not None)

# API 拦下应是 401 JSON（前端能识别），不是 HTML 登录页
_gate_api = ag.gate(FakeReq("/api/analyze", "POST"))
check("3.2 API 未解锁返回 401 响应",
      getattr(_gate_api, "status_code", None) == 401,
      f"实际 {_gate_api!r}")
if getattr(_gate_api, "status_code", None) == 401:
    _body = _gate_api.get_data(as_text=True)
    check("3.3 API 401 体是 JSON 且标志 need_unlock",
          "need_unlock" in _body and "口令" in _body,
          f"实际 {_body[:160]!r}")
    check("3.3b API 401 的 Content-Type 是 JSON",
          "application/json" in (_gate_api.headers.get("Content-Type") or ""),
          f"实际 {_gate_api.headers.get('Content-Type')!r}")

# 页面拦下应是登录页 HTML
_gate_page = ag.gate(FakeReq("/"))
check("3.4 页面未解锁返回登录页（含表单）",
      isinstance(_gate_page, str) and "<form" in _gate_page
      and 'action="/unlock"' in _gate_page)


# ===========================================================================
section("4. 解锁：正确口令放行，错误口令拒绝")
# ===========================================================================

set_code("abc123")
check("4.1 正确口令通过", ag.check_code("abc123") is True)
check("4.2 错误口令拒绝", ag.check_code("wrong") is False)
check("4.3 空口令拒绝", ag.check_code("") is False)
check("4.4 None 口令拒绝", ag.check_code(None) is False)
check("4.5 大小写敏感（abc123 ≠ ABC123）",
      ag.check_code("ABC123") is False)
check("4.6 前后空格视同没输对（服务端已 strip 过表单值，此处应拒绝）",
      ag.check_code(" abc123 ") is False)

# 解锁后带正确 cookie → 放行
_tok = ag.token()
check("4.7 带正确 cookie 的请求放行",
      ag.gate(FakeReq("/", cookies={ag.COOKIE_NAME: _tok})) is None)
check("4.8 带正确 cookie 的 API 也放行",
      ag.gate(FakeReq("/api/analyze", "POST",
                      cookies={ag.COOKIE_NAME: _tok})) is None)
check("4.9 伪造 cookie 拒绝",
      ag.gate(FakeReq("/", cookies={ag.COOKIE_NAME: "0" * 32})) is not None)
check("4.10 空 cookie 拒绝",
      ag.gate(FakeReq("/", cookies={ag.COOKIE_NAME: ""})) is not None)


# ===========================================================================
section("5. 口令绝不进 cookie（摘要必须不可逆且不等于口令）")
# ===========================================================================

set_code("abc123")
_t = ag.token()
check("5.1 cookie 值不等于口令明文", _t != "abc123")
check("5.2 cookie 值里不含口令子串", "abc123" not in _t)
check("5.3 摘要是定长十六进制 32 位",
      len(_t) == 32 and all(c in "0123456789abcdef" for c in _t),
      f"实际 {_t!r}")
check("5.4 同一口令摘要稳定（可跨请求校验）", ag.token() == ag.token())

set_code("xyz789")
check("5.5 换口令 → 摘要变（旧 cookie 立即失效）", ag.token() != _t)
check("5.6 旧摘要在新口令下不再被接受",
      ag.gate(FakeReq("/", cookies={ag.COOKIE_NAME: _t})) is not None)

# 摘要算法确定性（换个进程也该一致；这里只验同进程可复现）
set_code("abc123")
check("5.7 换回原口令 → 摘要复现", ag.token() == _t)


# ===========================================================================
section("6. 登录页与解锁流程")
# ===========================================================================

set_code("secret88")
_page = ag.login_page(reason="")
check("6.1 登录页含密码输入框", 'type="password"' in _page)
check("6.2 登录页表单 POST 到 /unlock", 'action="/unlock"' in _page)
check("6.3 登录页不含任何口令明文", "secret88" not in _page)
check("6.4 带提示语的登录页（失败态）含错误文案",
      "口令不正确" in ag.login_page(reason="口令不正确，请重新输入。", ok=False))

# 登录页 XSS 防御：reason 里塞脚本必须被转义
_xss = ag.login_page(reason='<script>alert(1)</script>')
check("6.5 登录页对提示语做 HTML 转义（XSS 防御）",
      "<script>" not in _xss and "&lt;script&gt;" in _xss,
      f"实际出现未转义 <script>：{'<script>' in _xss}")

check("6.6 脏 reason（None 之外的空串）不崩", isinstance(ag.login_page(""), str))
check("6.7 超长 reason 不崩", isinstance(ag.login_page("啊" * 5000), str))


# ===========================================================================
section("7. cookie Secure 标记（HTTPS 自动识别，防登录死循环）")
# ===========================================================================

os.environ.pop("ACCESS_COOKIE_SECURE", None)
check("7.1 HTTP 请求 → 不加 Secure（否则 HTTP 部署会登录死循环）",
      ag.cookie_secure(FakeReq("/", secure=False)) is False)
check("7.2 HTTPS 请求 → 加 Secure",
      ag.cookie_secure(FakeReq("/", secure=True)) is True)
check("7.3 反代声明 X-Forwarded-Proto=https → 加 Secure",
      ag.cookie_secure(FakeReq("/", xfp="https")) is True)
check("7.4 反代声明 http → 不加",
      ag.cookie_secure(FakeReq("/", xfp="http")) is False)
check("7.5 request 为 None 时安全兜底为 False",
      ag.cookie_secure(None) is False)

os.environ["ACCESS_COOKIE_SECURE"] = "1"
check("7.6 环境变量强制开启时，即使 HTTP 也加 Secure",
      ag.cookie_secure(FakeReq("/", secure=False)) is True)
os.environ.pop("ACCESS_COOKIE_SECURE", None)


# ===========================================================================
section("8. 脏输入 / 边界：永不崩")
# ===========================================================================

set_code("abc123")
for bad in (None, "", " ", "a", "口令" * 500, "\n\t", "\x00", "';DROP--"):
    try:
        ag.check_code(bad)
        ag.token(bad if bad else None)
        _ok = True
    except Exception as e:  # noqa: BLE001
        _ok = False
        print(f"      -> {bad!r} 抛了 {e!r}")
    check(f"8.1 脏口令不崩：{str(bad)[:14]!r}", _ok)

# ⚠️ 中文口令专项：hmac.compare_digest 对含非 ASCII 的 str 会抛 TypeError，
#    必须编码成 bytes 再比。直接用它 = 门禁一开就锁死所有人。
set_code("论文核查2026")
check("8.1b 中文口令能正确通过（compare_digest 非 ASCII 崩溃护栏）",
      ag.check_code("论文核查2026") is True)
check("8.1c 中文口令错误时正确拒绝",
      ag.check_code("论文核查2025") is False)
check("8.1d 中英混合口令也不崩",
      ag.check_code("abc论文123") is False)
check("8.1e 中文口令的 token 生成不崩且可用",
      isinstance(ag.token(), str) and len(ag.token()) == 32)
check("8.1f 中文口令下 cookie 校验可用",
      ag.check_cookie(ag.token()) is True)
set_code("abc123")

for p in ("", "/", "/api/", "//", "/unlock/../secret", "无斜杠"):
    try:
        ag.gate(FakeReq(p))
        _ok = True
    except Exception as e:  # noqa: BLE001
        _ok = False
        print(f"      -> {p!r} 抛了 {e!r}")
    check(f"8.2 脏路径不崩：{p!r}", _ok)

# 未配置时 gate 也必须能处理脏输入
set_code(None)
check("8.3 未配置时脏路径也不崩", ag.gate(FakeReq("/api/")) is None)


# ===========================================================================
section("9. 与 Flask app 的集成（真 app，非替身）")
# ===========================================================================

try:
    import app as flask_app

    client = flask_app.app.test_client()

    # 9.1 未配置 ACCESS_CODE → 首页照常打开（回归：默认零干扰）
    os.environ.pop("ACCESS_CODE", None)
    _r = client.get("/")
    check("9.1 未配置口令时首页照常可用（默认零干扰）",
          _r.status_code == 200, f"实际 {_r.status_code}")

    # 9.2 配置口令 → 首页被重定向到登录页或被拦
    os.environ["ACCESS_CODE"] = "testpass123"
    _r2 = client.get("/")
    _blocked = (_r2.status_code in (401, 302, 303)) or (b"unlock" in _r2.data)
    check("9.2 配置口令后首页被拦下", _blocked, f"实际 {_r2.status_code}")

    # 9.3 /unlock 本身可访问（死锁护栏在真实 app 里也要成立）
    _r3 = client.get("/unlock")
    check("9.3 /unlock 在真实 app 里可访问（死锁护栏）",
          _r3.status_code == 200, f"实际 {_r3.status_code}")
    check("9.3b 登录页确实渲染出来了", b"type=\"password\"" in _r3.data)

    # 9.4 /health 放行（Docker healthcheck 依赖）
    _r4 = client.get("/health")
    check("9.4 /health 放行（Docker healthcheck 依赖它）",
          _r4.status_code == 200, f"实际 {_r4.status_code}")

    # 9.5 未解锁时 API 返回 401 JSON
    _r5 = client.post("/api/analyze", json={})
    check("9.5 未解锁时 API 返回 401 + 可识别标志",
          _r5.status_code == 401 and b"need_unlock" in _r5.data,
          f"实际 {_r5.status_code} / {_r5.data[:120]!r}")

    # 9.6 走完整解锁流程：POST 正确口令 → 拿到 cookie → 首页可用
    _r6 = client.post("/unlock", data={"code": "testpass123"},
                      follow_redirects=False)
    check("9.6 正确口令解锁后重定向（非 401）",
          _r6.status_code in (302, 303), f"实际 {_r6.status_code}")
    _cookie_set = any(ag.COOKIE_NAME in (h or "")
                      for h in _r6.headers.getlist("Set-Cookie"))
    check("9.6b 解锁响应种下了口令 cookie", _cookie_set,
          f"Set-Cookie: {_r6.headers.getlist('Set-Cookie')}")

    # test_client 会保留 cookie jar → 后续请求应已解锁
    _r7 = client.get("/")
    check("9.7 解锁后首页可正常访问（不再被拦）",
          _r7.status_code == 200, f"实际 {_r7.status_code}")

    # 9.8 错误口令 → 401 且不种 cookie
    _fresh = flask_app.app.test_client()
    _r8 = _fresh.post("/unlock", data={"code": "wrongpass"})
    check("9.8 错误口令返回 401", _r8.status_code == 401,
          f"实际 {_r8.status_code}")
    _bad_cookie = any(ag.COOKIE_NAME in (h or "")
                      for h in _r8.headers.getlist("Set-Cookie"))
    check("9.8b 错误口令不种 cookie", not _bad_cookie)

    # 9.9 /logout 清 cookie
    _r9 = client.get("/logout")
    check("9.9 /logout 可用且清掉 cookie",
          _r9.status_code in (302, 303), f"实际 {_r9.status_code}")

    # 9.10 改口令 → 旧 cookie 失效（运维逃生手段）
    client2 = flask_app.app.test_client()
    client2.post("/unlock", data={"code": "testpass123"})
    _ok_before = client2.get("/").status_code == 200
    os.environ["ACCESS_CODE"] = "changed456"
    _after = client2.get("/")
    _reblocked = (_after.status_code in (401, 302, 303)) or (b"unlock" in _after.data)
    check("9.10 改口令后旧 cookie 立即失效（已是逃生手段）",
          _ok_before and _reblocked,
          f"改前可用={_ok_before} 改后状态={_after.status_code}")

    # 9.11 复位：清掉口令，确认没有污染后续测试
    os.environ.pop("ACCESS_CODE", None)
    _final = flask_app.app.test_client().get("/")
    check("9.11 清掉口令后首页恢复可用（集成测试自身无污染）",
          _final.status_code == 200, f"实际 {_final.status_code}")

except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("9.0 Flask 集成测试可运行", False, str(e))


# ===========================================================================
section("10. 文案红线：不夸大安全能力")
# ===========================================================================

set_code("abc123")
_doc = ag.__doc__ or ""
check("10.1 模块文档明确说明只防转发、不防攻击",
      "防" in _doc and ("攻击" in _doc or "爆破" in _doc))
check("10.2 模块文档提醒必须配 HTTPS",
      "HTTPS" in _doc or "https" in _doc)
check("10.3 模块文档说明这不是账号系统",
      "账号" in _doc)

os.environ.pop("ACCESS_CODE", None)

print()
print("=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
