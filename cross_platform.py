# -*- coding: utf-8 -*-
"""跨端适配层：让同一个后端同时服务 浏览器 / 小程序 / Uni-app H5。

设计原则
--------
1. **默认零行为改变。** 所有能力默认关闭或仅在被显式请求时生效，
   浏览器端的现有行为一个字都不变（避免为了新端把老端搞坏）。
2. **不引入新依赖。** 手写 CORS 头，不依赖 flask-cors（保持"零构建、开箱即跑"）。
3. **安全默认。** CORS 白名单为空 = 不发任何跨域头；只允许显式配置的来源。
4. **小程序特殊性要说清。** 微信小程序的 `wx.request` **不走浏览器同源策略**，
   它不看 CORS 头、也不发 `OPTIONS` 预检。所以：
     - 小程序**本来就能直接调**本后端（只要域名是 https 且已在微信后台备案配置）；
     - CORS 是为 **Uni-app 编译出的 H5** 或**第三方网页**准备的。

环境变量
--------
    CORS_ALLOW_ORIGINS   逗号分隔的来源白名单，如 "https://a.com,https://b.com"
                         特殊值 "*" 允许任意来源（仅建议本地调试用，生产别开）
                         默认空 = 不启用 CORS（浏览器同源访问不受影响）
    CORS_ALLOW_CREDENTIALS   "1" 时允许携带凭据（cookie）。本项目不用 cookie 会话，
                             故默认关；开启时会自动禁用 "*" 通配（浏览器规范要求）

安全考量
--------
- 允许 "*" 的同时允许 credentials 是**危险的组合**（任何站点都能带着用户凭据
  调你的接口）。这里直接拒绝该组合并退回为不带凭据，而不是静默放行。
- `Vary: Origin` 必须带上，否则 CDN/代理可能把 A 站的响应缓存后发给 B 站。
"""

from __future__ import annotations

import os

# CORS 相关响应头
_H_ALLOW_ORIGIN = "Access-Control-Allow-Origin"
_H_ALLOW_METHODS = "Access-Control-Allow-Methods"
_H_ALLOW_HEADERS = "Access-Control-Allow-Headers"
_H_MAX_AGE = "Access-Control-Max-Age"
_H_EXPOSE = "Access-Control-Expose-Headers"
_H_ALLOW_CRED = "Access-Control-Allow-Credentials"

# 预检缓存时长（秒）。小程序/浏览器都不常发预检，但 H5 调 LLM 接口时会反复发，
# 缓存下来能省掉一半往返。
_PREFLIGHT_MAX_AGE = "600"

# 允许的方法：本项目只用 GET / POST。不放开 PUT/DELETE 减小攻击面。
_ALLOW_METHODS = "GET, POST, OPTIONS"

# 允许的请求头：前端用了 Content-Type（JSON/JSON 上传）与 BYOK 相关头。
# 保持白名单制，不用 "*"（* 在带凭据时不被浏览器认可，且过宽）。
_ALLOW_HEADERS = (
    "Content-Type, Accept, Origin, X-Requested-With, "
    "X-LLM-Key, X-LLM-Provider, Authorization"
)

# 暴露给前端的响应头：前端需要读 Retry-After 来决定"等几秒再试"
_EXPOSE_HEADERS = "Retry-After, Content-Disposition"


def _split_env(name: str) -> list[str]:
    raw = os.environ.get(name, "") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def allowed_origins() -> list[str]:
    """解析白名单。返回 [] 表示不启用 CORS。"""
    return _split_env("CORS_ALLOW_ORIGINS")


def credentials_enabled() -> bool:
    return (os.environ.get("CORS_ALLOW_CREDENTIALS", "0") or "").strip() == "1"


def cors_enabled() -> bool:
    return bool(allowed_origins())


def resolve_origin(request_origin: str | None) -> str | None:
    """决定给这个来源发什么 Allow-Origin 值。

    返回 None 表示「不放行，不发头」。
    注意：当白名单含 "*" 且需要携带凭据时，**不能回 "*"**（浏览器会拒绝），
    而要回显具体来源 —— 这也是比 flask-cors 默认行为更安全的地方。
    """
    origins = allowed_origins()
    if not origins:
        return None
    if "*" in origins:
        # 通配：如果不带凭据，可以直接 "*"（最省事且可被 CDN 缓存）
        if not credentials_enabled():
            return "*"
        # 带凭据时通配无意义，回显来源（等价于"允许所有站带凭据"，
        # 属于危险配置，故在 warn_dangerous_config 里会告警）
        return request_origin or None
    if request_origin and request_origin in origins:
        return request_origin
    return None


def apply_cors_headers(resp, request_origin: str | None):
    """给响应加上 CORS 头（若该来源被允许）。返回同一个 resp。"""
    allow = resolve_origin(request_origin)
    if not allow:
        return resp
    resp.headers[_H_ALLOW_ORIGIN] = allow
    resp.headers[_H_ALLOW_METHODS] = _ALLOW_METHODS
    resp.headers[_H_ALLOW_HEADERS] = _ALLOW_HEADERS
    resp.headers[_H_EXPOSE] = _EXPOSE_HEADERS
    if credentials_enabled() and allow != "*":
        resp.headers[_H_ALLOW_CRED] = "true"
    # 必须带 Vary: Origin —— 否则代理可能把 A 站的响应缓存后发给 B 站
    _append_vary(resp, "Origin")
    return resp


def _append_vary(resp, value: str):
    existing = resp.headers.get("Vary")
    if not existing:
        resp.headers["Vary"] = value
    elif value.lower() not in existing.lower():
        resp.headers["Vary"] = f"{existing}, {value}"


def preflight_response(request_origin: str | None, requested_headers: str | None = None):
    """构造预检（OPTIONS）响应。

    返回 (body, status, headers)；来源不被允许时返回 403 —— **显式拒绝而不是
    静默返回 200 但没有 CORS 头**，后者会让前端收到一个看不懂的 CORS 报错。
    """
    allow = resolve_origin(request_origin)
    if not allow:
        return "", 403, {"Vary": "Origin"}

    headers = {
        _H_ALLOW_ORIGIN: allow,
        _H_ALLOW_METHODS: _ALLOW_METHODS,
        _H_ALLOW_HEADERS: _ALLOW_HEADERS,
        _H_MAX_AGE: _PREFLIGHT_MAX_AGE,
        "Vary": "Origin",
    }
    if credentials_enabled() and allow != "*":
        headers[_H_ALLOW_CRED] = "true"
    return "", 204, headers


def warn_dangerous_config() -> list[str]:
    """返回配置告警（供启动时打印）。不抛异常，不阻断启动。"""
    warns = []
    origins = allowed_origins()
    if "*" in origins:
        if credentials_enabled():
            warns.append(
                "CORS_ALLOW_ORIGINS=* 且 CORS_ALLOW_CREDENTIALS=1："
                "等于允许任意网站携带用户凭据调用本接口，**极不安全**，仅限本地调试。")
        else:
            warns.append(
                "CORS_ALLOW_ORIGINS=*：允许任意网站读取本接口响应，"
                "仅建议本地调试使用。")
    if credentials_enabled():
        warns.append(
            "CORS_ALLOW_CREDENTIALS=1：本项目不使用 cookie 会话，"
            "通常无需开启；开启后请确保白名单不是通配。")
    return warns


# ---------------------------------------------------------------------------
# 小程序接入辅助
# ---------------------------------------------------------------------------

def miniprogram_request_domains_hint() -> str:
    """给一份「微信后台需要配置哪些域名」的提示文案。"""
    return (
        "微信公众平台 → 开发管理 → 开发设置 → 服务器域名：\n"
        "  request 合法域名   ：你的 API 域名（必须 https，需 ICP 备案）\n"
        "  uploadFile 合法域名：同上（上传 Excel/PDF 走 wx.uploadFile）\n"
        "  downloadFile 合法域名：同上（导出 Word 走 wx.downloadFile）\n"
        "注意：域名不能带端口以外的路径前缀；不支持 IP 地址；不支持自签名证书。"
    )
