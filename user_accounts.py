"""独立账号系统（v2.27 · 每人一个账号，按人限额、按人隔离）
==========================================================
`access_guard` 的口令门禁是「看门人」：一个共享口令，进了门分不清谁是谁。
本模块在门禁**之内**再加一层「个人账号」，由一个环境变量控制：

    ACCOUNTS_ENABLED=1      # 开启；不设 = 完全关闭（本地/桌面端零干扰）

开启后的行为：
    1. 过了口令门禁还要**注册/登录个人账号**才能用 —— 每人有独立身份；
    2. 限流**按用户名**计数（app.py 传 `user:<名>` 给限流器），
       配额不再被同一校园网出口 IP 均摊；
    3. 上传数据的 `file_id` 归属本人，其他账号访问直接 403；
    4. `/login`、`/register`、`/unlock` 共享**按 IP 防爆破锁定**
       （连续失败 5 次 → 锁 15 分钟，实现在 security_guard.LoginLockout）。

设计原则（与 access_guard 同一门风）：
    1. 纯标准库：口令用 `hashlib.pbkdf2_hmac`（带随机盐、12 万轮）存摘要，
       **绝不存明文**；校验用常量时间比较；未知用户名也做一次等价哈希，
       抹平「用户名是否存在」的时序差。
    2. 不配置就不生效：`ACCOUNTS_ENABLED` 未设 → 所有函数直接放行。
    3. cookie 只存「用户名 + 过期时间 + HMAC 签名」，js 读不到（httponly）；
       签名密钥由口令派生 —— **改口令 = 全部账号会话立即失效**。
    4. 账号索引落盘 `<ACCOUNTS_DIR>/accounts.json`（只有摘要，绝无用户数据）。
       这是与「数据不落盘」红线的边界：数据仍然全内存，账号索引必须持久，
       否则每次重启全员重新注册。目录默认 `.accounts/`，已加入 .gitignore。
    5. 注册是**邀请制**：必须同时提交口令（即 ACCESS_CODE）才能创建账号 ——
       光知道网址的人不能注册，口令仍是部署者手里唯一的准入券。

## 安全边界（如实告知）
    - 账号系统防「用我额度的人分不清谁是谁」，不防定向攻击；
    - 找回密码 = 部署者删掉 accounts.json 里对应条目让本人重新注册（刻意从简）；
    - `/s/<token>` 分享链接对账号系统同样豁免（设计如此：给没账号的导师看）。
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from typing import Any

#: 用户会话 cookie 名
COOKIE_NAME = "zhilun_user"

#: 账号系统开启时，这些路径不需要个人登录（口令门禁的豁免名单之外另行生效）
#: - /login /register /logout-user：登录体系自身
#: - /unlock /logout：口令门禁的入口/出口——账号是门禁**之内**的一层，
#:   不能把"输口令"本身重定向走（否则永远到不了登录页）
#: - /health：探活；/s/：协作分享（设计如此，见模块 docstring）
EXEMPT_PATHS = ("/login", "/register", "/logout-user",
                "/unlock", "/logout", "/health", "/favicon.ico")
EXEMPT_PREFIXES = ("/static/", "/s/")

#: 会话有效期（秒）：7 天
SESSION_TTL = 7 * 24 * 3600

#: 签名盐（不是安全边界，只是让签名不等于裸信息哈希）
_SALT = b"zhilun-accounts-v1"

#: PBKDF2 轮数：单次 ~50ms，注册/登录不卡，离线爆破成本够高
_PBKDF2_ITERS = 120_000

#: 用户名规则：2–32 个字符；中文、字母、数字、下划线、连字符、点
_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\-]{2,32}$", re.UNICODE)

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 开关与存储
# ---------------------------------------------------------------------------
def enabled() -> bool:
    """账号系统是否开启。未配置 → 关闭（本地/桌面端/测试默认）。"""
    return (os.environ.get("ACCOUNTS_ENABLED") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _dir() -> str:
    return (os.environ.get("ACCOUNTS_DIR") or ".accounts")


def _store_path() -> str:
    return os.path.join(_dir(), "accounts.json")


def _load() -> dict:
    """读账号索引。文件缺失/损坏 → 返回空结构（服务照常起，只是没人注册过）。"""
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("users"), dict):
            return data
    except Exception:  # noqa: BLE001 — 损坏不挡服务
        pass
    return {"users": {}}


def _save(data: dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    tmp = _store_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _store_path())      # 原子替换，写一半断电也不损旧档


# ---------------------------------------------------------------------------
# 口令摘要（PBKDF2，绝不存明文）
# ---------------------------------------------------------------------------
def _hash(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex),
        _PBKDF2_ITERS).hex()


def validate_username(name: str) -> str | None:
    """合法返回 None，否则返回人话错误。"""
    if not name:
        return "请填写用户名。"
    if not _USERNAME_RE.match(name):
        return "用户名需 2–32 位，只能用中文、字母、数字、下划线、连字符或点。"
    return None


def validate_password(pw: str) -> str | None:
    """合法返回 None，否则返回人话错误。"""
    if not pw or len(pw) < 8:
        return "密码至少 8 位。"
    if len(pw) > 128:
        return "密码最长 128 位。"
    return None


def register(name: str, password: str) -> tuple[bool, str]:
    """创建账号。成功 (True, "")；失败 (False, 人话错误)。"""
    err = validate_username(name) or validate_password(password)
    if err:
        return False, err
    with _lock:
        data = _load()
        users = data["users"]
        if name in users:
            return False, "该用户名已被注册，请换一个。"
        salt = secrets.token_hex(16)
        users[name] = {
            "salt": salt,
            "hash": _hash(password, salt),
            "created": int(time.time()),
        }
        _save(data)
    return True, ""


def verify(name: str, password: str) -> bool:
    """校验用户名+密码。**常量时间比较**；未知用户也做等价哈希抹平时序差。"""
    if not name or not password:
        return False
    with _lock:
        rec = _load()["users"].get(name)
    salt = rec["salt"] if rec else secrets.token_hex(16)
    calc = _hash(password, salt)
    stored = rec["hash"] if rec else calc      # 未知用户：与自己比较，必然 False 但耗时相同
    return hmac.compare_digest(calc.encode("utf-8"), stored.encode("utf-8")) \
        and rec is not None


def delete(name: str) -> bool:
    """删除账号（找回密码的运维手段：删掉让本人重注册）。"""
    with _lock:
        data = _load()
        if name not in data["users"]:
            return False
        del data["users"][name]
        _save(data)
    return True


# ---------------------------------------------------------------------------
# 会话 cookie：用户名|过期时间|HMAC 签名
# ---------------------------------------------------------------------------
def _secret() -> bytes:
    """签名密钥：优先由口令派生（改口令 = 全员会话失效）；
    未配口令时落到本地随机密钥文件（首次自动生成）。"""
    code = (os.environ.get("ACCESS_CODE") or "").strip()
    if code:
        return hmac.new(_SALT, code.encode("utf-8"), hashlib.sha256).digest()
    path = os.path.join(_dir(), "secret.key")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if raw:
            return bytes.fromhex(raw)
    except Exception:  # noqa: BLE001
        pass
    raw = secrets.token_hex(32)
    os.makedirs(_dir(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    return bytes.fromhex(raw)


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256) \
        .hexdigest()[:32]


def make_token(name: str, *, ttl: int = SESSION_TTL, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + ttl)
    payload = f"{name}|{exp}"
    return f"{payload}|{_sign(payload)}"


def current_user(request: Any) -> str | None:
    """从请求 cookie 解出当前用户名；无效/过期/签名不符 → None。"""
    if not enabled():
        return None
    raw = (request.cookies.get(COOKIE_NAME) or "").strip()
    if not raw:
        return None
    parts = raw.split("|")
    if len(parts) != 3:
        return None
    name, exp, sig = parts
    if not name or not exp.isdigit():
        return None
    if not hmac.compare_digest(_sign(f"{name}|{exp}").encode("utf-8"),
                               sig.encode("utf-8")):
        return None
    if int(exp) < time.time():
        return None
    return name


def set_cookie(resp: Any, name: str) -> Any:
    """种下用户会话 cookie（参数与口令 cookie 同一风格）。"""
    try:
        from app import access_guard   # 延迟 import：复用 secure 判断，避免循环依赖
        secure = access_guard.cookie_secure(request=None)
    except Exception:  # noqa: BLE001
        secure = False
    resp.set_cookie(
        COOKIE_NAME, make_token(name),
        max_age=SESSION_TTL, httponly=True, samesite="Lax", secure=secure)
    return resp


def clear_cookie(resp: Any) -> Any:
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# 自包含登录/注册页（与 access_guard 登录页同一风格，零静态依赖）
# ---------------------------------------------------------------------------
def _page(mode: str, reason: str = "", ok: bool = True,
          need_code: bool = True) -> str:
    """mode: login | register。need_code=注册页要不要口令框。"""
    cls = "" if ok else "err"
    note = html.escape(reason) if reason else ""
    is_reg = mode == "register"
    title = "注册账号" if is_reg else "登录"
    code_field = ("""
    <input type="password" name="code" placeholder="访问口令（部署者提供）"
           autocomplete="current-password">""" if (is_reg and need_code) else "")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 智论助手</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #f5f4f0;
    font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color: #2c2c2a; }}
  .card {{ width: min(380px, calc(100vw - 40px)); background: #fff;
    border: 1px solid rgba(0,0,0,.12); border-radius: 12px; padding: 28px; }}
  h1 {{ font-size: 17px; font-weight: 500; margin: 0 0 6px; }}
  p.sub {{ margin: 0 0 20px; font-size: 13px; color: #5f5e5a; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 15px;
    border: 1px solid rgba(0,0,0,.2); border-radius: 8px; margin-bottom: 12px; }}
  input:focus {{ outline: 2px solid #378ADD; outline-offset: -1px;
    border-color: transparent; }}
  button {{ width: 100%; padding: 11px; font-size: 15px; cursor: pointer;
    background: #1e40af; color: #fff; border: 0; border-radius: 8px; }}
  button:hover {{ background: #185FA5; }}
  .msg {{ font-size: 13px; margin: 0 0 14px;
    color: {('#501313' if not ok else '#5f5e5a')}; }}
  .switch {{ margin-top: 16px; font-size: 13px; }}
  .switch a {{ color: #1e40af; }}
  .hint {{ margin-top: 10px; font-size: 12px; color: #888780; }}
</style>
</head>
<body>
  <form class="card" method="post" action="/{mode}">
    <h1>智论助手 · {title}</h1>
    <p class="sub">{'创建你自己的账号，数据与限额按人独立。' if is_reg else '每人使用自己的账号登录。'}</p>
    <p class="msg {cls}">{note}</p>{code_field}
    <input type="text" name="username" placeholder="用户名（中文/字母/数字）"
           autocomplete="username" autofocus>
    <input type="password" name="password" placeholder="密码（至少 8 位）"
           autocomplete="{'new-password' if is_reg else 'current-password'}">
    <button type="submit">{'注册并进入' if is_reg else '登录'}</button>
    <div class="switch">{'已有账号？<a href="/login">去登录</a>' if is_reg else '没有账号？<a href="/register">注册一个</a>'}</div>
    <div class="hint">密码只存摘要（PBKDF2），管理员也看不到原文。</div>
  </form>
</body>
</html>"""


def login_page(reason: str = "", ok: bool = True) -> str:
    return _page("login", reason, ok)


def register_page(reason: str = "", ok: bool = True,
                  need_code: bool = True) -> str:
    return _page("register", reason, ok, need_code)


# ---------------------------------------------------------------------------
# 路由处理器（app.py 挂 /login /register /logout-user 三个路由）
# ---------------------------------------------------------------------------
def _lock_key(request: Any, scope: str) -> str:
    try:
        from security_guard import client_ip
        return f"{scope}:" + (client_ip(request) or "unknown")
    except Exception:  # noqa: BLE001
        return f"{scope}:unknown"


def handle_login(request: Any) -> Any:
    """GET 渲染登录页；POST 校验账号（带防爆破锁定）。"""
    from flask import make_response, redirect

    if not enabled():
        return redirect("/")
    if request.method == "GET":
        if current_user(request):
            return redirect("/")
        return login_page()

    from security_guard import login_lockout
    key = _lock_key(request, "login")
    locked, wait = login_lockout.status(key)
    if locked:
        return login_page(f"尝试次数过多，已临时锁定，请 {wait} 秒后再试。",
                          ok=False), 429

    name = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not verify(name, password):
        login_lockout.fail(key)
        return login_page("用户名或密码不正确。", ok=False), 401
    login_lockout.ok(key)
    return set_cookie(make_response(redirect("/")), name)


def handle_register(request: Any) -> Any:
    """GET 渲染注册页；POST 创建账号（注册也要过口令，邀请制）。"""
    from flask import make_response, redirect

    if not enabled():
        return redirect("/")
    if request.method == "GET":
        if current_user(request):
            return redirect("/")
        # 口令门禁关闭时，注册页不收口令（纯账号模式）
        need_code = _gate_enabled()
        return register_page(need_code=need_code)

    from security_guard import login_lockout
    key = _lock_key(request, "register")
    locked, wait = login_lockout.status(key)
    if locked:
        return register_page(f"尝试次数过多，已临时锁定，请 {wait} 秒后再试。",
                             ok=False), 429

    # ① 邀请口令（与门禁同一把；防爆破共享同一锁定计数）
    if _gate_enabled():
        code = (request.form.get("code") or "").strip()
        try:
            from access_guard import check_code
            code_ok = check_code(code)
        except Exception:  # noqa: BLE001
            code_ok = False
        if not code_ok:
            login_lockout.fail(key)
            return register_page("访问口令不正确。", ok=False), 401

    # ② 账号规则
    name = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    ok, err = register(name, password)
    if not ok:
        login_lockout.fail(key)
        return register_page(err, ok=False), 400
    login_lockout.ok(key)
    return set_cookie(make_response(redirect("/")), name)


def handle_logout(request: Any) -> Any:
    """退出个人账号（口令门禁不受影响）。"""
    from flask import make_response, redirect
    return clear_cookie(make_response(redirect("/login")))


def _gate_enabled() -> bool:
    try:
        from access_guard import enabled as gate_on
        return gate_on()
    except Exception:  # noqa: BLE001
        return False
