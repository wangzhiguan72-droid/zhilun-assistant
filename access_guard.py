"""访问门禁 · 一句话加密码（`access_guard.py`）
================================================
场景：把本工具部署到公网给**小范围内部**使用（如 30 个同学），
但**不希望网址被转发后陌生人也能用**。整套逻辑由**一个环境变量**控制：

    ACCESS_CODE=你的口令        # 设置后开启门禁；不设 = 完全关闭（本地开发零干扰）

## 设计原则（与项目红线一致）

1. **纯函数 + 零依赖**：不 import app，可被 CLI / 测试独立复用。
2. **不配置就不生效**：`ACCESS_CODE` 为空 → 所有函数直接放行。
   本地双击桌面端、跑测试、开发调试的行为**完全不变**。
3. **常量时间比较**：用 `hmac.compare_digest` 而非 `==`，避免时序侧信道。
4. **门禁是"看门人"，不是"账号系统"**：只有一个共享口令，没有用户名、
   没有角色、没有注册。30 人内部使用足够；要精细权限请另做用户系统。
5. **口令只存内存 + cookie**：cookie 里放的是**口令的 HMAC 摘要**，
   不是口令明文——即便 cookie 被看到，也反推不出口令。

## 为什么用 cookie 而不是 HTTP Basic Auth

`HTTP Basic` 会在每个请求头发明文口令，且**无法自定义退出登录**（浏览器
会把凭据一直带着）。用签名 cookie：
- 口令只在"解锁"那一次提交；
- 可随时改 `ACCESS_CODE` 让所有旧 cookie 立即失效（摘要随口令变）；
- 可实现"退出登录"按钮。

## 安全边界（务必如实告知使用者）

本模块**只防"网址被转发给无关的人"**，它不是防攻击系统：
- 明文口令经 HTTPS 传输才安全 → **必须配 HTTPS**（见部署指南）；
- 没有防爆破（应配合 `security_guard` 的限流使用）；
- 没有"记住设备"、"找回口令"等便利功能 —— 这是刻意的，越简单越可靠。

## 用法（三行接入 app.py）

    import access_guard
    @app.before_request
    def _access_gate():
        return access_guard.gate(request)

    @app.route("/unlock", methods=["GET", "POST"])
    def unlock():
        return access_guard.handle_unlock(request)
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
from typing import Any

#: cookie 名（带前缀避免与 Flask 自身的 session 冲突）
COOKIE_NAME = "zhilun_pass"

#: 摘要算法里掺的固定盐（不是安全边界，只是别让摘要等于裸口令的哈希）
_SALT = b"zhilun-access-v1"

#: 门禁放行的路径（不设门禁也放行）
_EXEMPT_PATHS = ("/unlock", "/health", "/favicon.ico")
_EXEMPT_PREFIXES = ("/static/",)


def access_code() -> str:
    """当前配置的口令（空串 = 门禁关闭）。

    每次调用都读环境变量，**不缓存** —— 便于测试改写环境变量立即生效，
    也便于运维改完重启即可（本工具以容器/进程为单位，读取开销可忽略）。
    """
    return (os.environ.get("ACCESS_CODE") or "").strip()


def enabled() -> bool:
    """门禁是否开启。空值 → 关闭（本地/桌面端/测试的默认状态）。"""
    return bool(access_code())


def token(code: str | None = None) -> str:
    """口令 → cookie 里存的摘要（HMAC-SHA256 前 32 位十六进制）。

    **绝不把口令本身写进 cookie**：即便 cookie 被他人看到，也只能拿到一个
    不可逆摘要，反推不出口令。
    """
    secret = access_code() if code is None else code
    return hmac.new(_SALT, (secret or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def is_exempt(path: str) -> bool:
    """该路径是否必须放行（否则用户连登录页都打不开 → 死锁）。"""
    if path in _EXEMPT_PATHS:
        return True
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def _const_eq(a: str, b: str) -> bool:
    """常量时间字符串比较（**支持中文**）。

    ⚠️ 千万别直接 `hmac.compare_digest("口令", "口令")` —— 对含非 ASCII 字符的
    `str` 会抛 `TypeError: comparing strings with non-ASCII characters is not
    supported`。而本工具的用户**很可能用中文口令**（如「论文核查2026」），
    直接崩掉等于门禁一开就锁死所有人。
    正确做法：先 `.encode("utf-8")` 成 bytes 再比 —— 同样是常量时间。
    """
    return hmac.compare_digest(
        (a or "").encode("utf-8"), (b or "").encode("utf-8"))


def check_code(candidate: str | None) -> bool:
    """用户提交的口令是否正确。**常量时间比较**。"""
    real = access_code()
    if not real:
        return True          # 门禁关闭 → 任何提交都"通过"（实际不会被调用）
    if not candidate:
        return False
    return _const_eq(real, candidate)


def check_cookie(value: str | None) -> bool:
    """cookie 里带的摘要是否有效（等效于"这个浏览器已解锁"）。"""
    if not enabled():
        return True
    if not value:
        return False
    return _const_eq(token(), value)


def gate(request: Any) -> Any | None:
    """把 request 交给门禁判断。

    返回：
        None          → 放行（未开启门禁 / 已解锁 / 豁免路径）
        Flask Response → 拦下（未解锁：API 请求回 401 JSON，页面请求回登录页）

    接入方式：在 app.py 里 `return access_guard.gate(request)`。
    """
    if not enabled():
        return None
    path = request.path or ""
    if is_exempt(path):
        return None
    if check_cookie(request.cookies.get(COOKIE_NAME)):
        return None

    # 未解锁：区分"程序化调用"与"浏览器点进来的"
    if path.startswith("/api/"):
        # 用 Response(json.dumps(...)) 而非 jsonify()：
        # jsonify 需要 Flask 应用上下文（`Working outside of application context`），
        # 用它会让 gate() 无法脱离真 app 被单测/复用。Response 无此约束。
        import json
        from flask import Response
        body = json.dumps({
            "ok": False,
            "error": "需要访问口令。请先在页面右上角「解锁」后重试。",
            "need_unlock": True,
        }, ensure_ascii=False)
        return Response(body, status=401, mimetype="application/json")
    return login_page(reason="请输入访问口令")


def login_page(reason: str = "", ok: bool = True) -> str:
    """极简登录页（自包含 HTML，不依赖任何静态资源/模板）。"""
    cls = "" if ok else "err"
    note = html.escape(reason) if reason else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>需要访问口令 · 智论助手</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #f5f4f0;
    font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color: #2c2c2a;
  }}
  .card {{
    width: min(380px, calc(100vw - 40px)); background: #fff;
    border: 1px solid rgba(0,0,0,.12); border-radius: 12px; padding: 28px;
  }}
  h1 {{ font-size: 17px; font-weight: 500; margin: 0 0 6px; }}
  p.sub {{ margin: 0 0 20px; font-size: 13px; color: #5f5e5a; }}
  input {{
    width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 15px;
    border: 1px solid rgba(0,0,0,.2); border-radius: 8px; margin-bottom: 12px;
  }}
  input:focus {{ outline: 2px solid #378ADD; outline-offset: -1px; border-color: transparent; }}
  button {{
    width: 100%; padding: 11px; font-size: 15px; cursor: pointer;
    background: #1e40af; color: #fff; border: 0; border-radius: 8px;
  }}
  button:hover {{ background: #185FA5; }}
  .msg {{ font-size: 13px; margin: 0 0 14px; color: {('#501313' if not ok else '#5f5e5a')}; }}
  .hint {{ margin-top: 16px; font-size: 12px; color: #888780; }}
</style>
</head>
<body>
  <form class="card" method="post" action="/unlock">
    <h1>智论助手</h1>
    <p class="sub">本工具限内部使用，请输入访问口令。</p>
    <p class="msg {cls}">{note}</p>
    <input type="password" name="code" placeholder="访问口令" autofocus
           autocomplete="current-password">
    <button type="submit">解锁</button>
    <div class="hint">口令由部署者提供。此页面不会保存你输入的内容。</div>
  </form>
</body>
</html>"""


def handle_unlock(request: Any) -> Any:
    """处理登录页表单提交。

    成功 → 种下摘要 cookie 并跳回首页；失败 → 重新渲染登录页 + 提示。
    期望路由为 `GET/POST /unlock`。
    """
    from flask import make_response, redirect, request as _r  # noqa: F401

    if not enabled():
        return redirect("/")

    if request.method == "GET":
        # 已经解锁过就直接进去，别让用户重复输
        if check_cookie(request.cookies.get(COOKIE_NAME)):
            return redirect("/")
        return login_page(reason="")

    code = (request.form.get("code") or "").strip()
    if not check_code(code):
        return login_page(reason="口令不正确，请重新输入。", ok=False), 401

    resp = make_response(redirect("/"))
    resp.set_cookie(
        COOKIE_NAME, token(),
        max_age=60 * 60 * 24 * 30,      # 30 天
        httponly=True,                   # JS 读不到，防 XSS 窃取
        samesite="Lax",                  # 防 CSRF，同时不挡正常跳转
        secure=cookie_secure(request),   # HTTPS 下自动加 Secure；HTTP 下不加（否则登录死循环）
    )
    return resp


def logout_response() -> Any:
    """退出登录：清掉 cookie。可挂到 `GET /logout`。"""
    from flask import make_response, redirect

    resp = make_response(redirect("/unlock"))
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# 关于 cookie 的 `secure` 标志（部署时请读这段）
# ---------------------------------------------------------------------------
# 当前写死 `secure=False`，理由是兼容「本地内测 + HTTP 反代」两种场景：
# 若置 True，则**只在 HTTPS 下**浏览器才回传 cookie，HTTP 部署会表现为
# 「输了口令却一直回到登录页」。
#
# 配好 HTTPS 后（部署指南第 5 步），建议改成：
#     secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
# 或直接设环境变量 `ACCESS_COOKIE_SECURE=1` 走下面的辅助函数。
def cookie_secure(request: Any = None) -> bool:
    """是否给 cookie 打 `Secure` 标记。

    `ACCESS_COOKIE_SECURE=1` 强制开启（配好 HTTPS 后建议设）；
    否则自动判断：请求本身是 HTTPS，或反代声明了 X-Forwarded-Proto=https。
    """
    if (os.environ.get("ACCESS_COOKIE_SECURE") or "").strip() == "1":
        return True
    if request is None:
        return False
    try:
        if request.is_secure:
            return True
        proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
        return proto.startswith("https")
    except Exception:  # noqa: BLE001
        return False
