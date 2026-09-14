# -*- coding: utf-8 -*-
"""跨端适配层契约测试（cross_platform.py + CORS 钩子 + 小程序接入前提）。

守护三件事：
  ① **默认零行为改变**：不配 CORS_ALLOW_ORIGINS 时，后端不发任何 CORS 头，
     浏览器同源访问与以前完全一致（这是「不为了新端把老端搞坏」的底线）。
  ② **预检不计入限流**：否则用户会被自己的预检耗光额度，
     症状是「跨域调用时好时坏」—— 本项目已实际踩到并修复。
  ③ **小程序接入前提成立**：后端不使用 cookie 会话（全靠 file_id），
     `wx.request` 不走同源策略，故小程序无需 CORS 即可直接调用。

不依赖外部网络；CORS 相关用例通过 reload 切换环境变量。
"""
import importlib
import io
import json
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def _reload_app(cors_origins=None, cors_cred=None):
    """重载 app 模块以应用新的 CORS 环境变量。"""
    if cors_origins is None:
        os.environ.pop("CORS_ALLOW_ORIGINS", None)
    else:
        os.environ["CORS_ALLOW_ORIGINS"] = cors_origins
    if cors_cred is None:
        os.environ.pop("CORS_ALLOW_CREDENTIALS", None)
    else:
        os.environ["CORS_ALLOW_CREDENTIALS"] = cors_cred
    import app as app_mod
    importlib.reload(app_mod)
    return app_mod


# ---------------------------------------------------------------------------
def test_default_off():
    print("\n[1] 默认关闭：不发任何 CORS 头（老端行为不变）")
    m = _reload_app()
    c = m.app.test_client()

    r = c.get("/health", headers={"Origin": "https://evil.example.com"})
    check("默认 /health 200", r.status_code == 200, f"got {r.status_code}")
    check("默认不发 Allow-Origin",
          r.headers.get("Access-Control-Allow-Origin") is None,
          f"got {r.headers.get('Access-Control-Allow-Origin')!r}")

    r2 = c.get("/", headers={"Origin": "https://evil.example.com"})
    check("默认首页仍 200", r2.status_code == 200, f"got {r2.status_code}")
    check("默认首页也不发 Allow-Origin",
          r2.headers.get("Access-Control-Allow-Origin") is None)

    # 未启用 CORS 时，OPTIONS 不该被伪造成 204（要如实走 405）
    ro = c.options("/api/analyze", headers={"Origin": "https://evil.example.com"})
    check("未启用时 OPTIONS 不被伪装成 204",
          ro.status_code != 204, f"got {ro.status_code}")

    check("cors_enabled() 为 False", m.cross_platform.cors_enabled() is False)


def test_whitelist():
    print("\n[2] 白名单：只放行列出的来源")
    m = _reload_app("https://ok.example.com,https://two.example.com")
    c = m.app.test_client()

    check("cors_enabled() 为 True", m.cross_platform.cors_enabled() is True)
    check("白名单解析为 2 项",
          m.cross_platform.allowed_origins() ==
          ["https://ok.example.com", "https://two.example.com"],
          f"got {m.cross_platform.allowed_origins()}")

    r = c.get("/health", headers={"Origin": "https://ok.example.com"})
    check("允许来源 -> 回显来源",
          r.headers.get("Access-Control-Allow-Origin") == "https://ok.example.com",
          f"got {r.headers.get('Access-Control-Allow-Origin')!r}")
    check("带 Vary: Origin（防代理串缓存）",
          "origin" in (r.headers.get("Vary") or "").lower(),
          f"got {r.headers.get('Vary')!r}")

    r2 = c.get("/health", headers={"Origin": "https://evil.example.com"})
    check("未列入来源 -> 不发 Allow-Origin",
          r2.headers.get("Access-Control-Allow-Origin") is None,
          f"got {r2.headers.get('Access-Control-Allow-Origin')!r}")
    check("未列入来源的请求本身仍正常处理（同源不受影响）",
          r2.status_code == 200, f"got {r2.status_code}")

    # 无 Origin 头（同源请求/CLI/小程序）不应受影响
    r3 = c.get("/health")
    check("无 Origin 头 -> 200 且不发 Allow-Origin",
          r3.status_code == 200 and r3.headers.get("Access-Control-Allow-Origin") is None)
    check("无 Origin 头时不加 Vary",
          r3.headers.get("Vary") is None, f"got {r3.headers.get('Vary')!r}")


def test_preflight():
    print("\n[3] 预检：允许 204 / 拒绝 403")
    m = _reload_app("https://ok.example.com")
    c = m.app.test_client()

    ok = c.options("/api/analyze", headers={
        "Origin": "https://ok.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    check("允许来源预检 -> 204", ok.status_code == 204, f"got {ok.status_code}")
    check("预检带 Allow-Origin",
          ok.headers.get("Access-Control-Allow-Origin") == "https://ok.example.com")
    check("预检带 Allow-Methods（含 POST）",
          "POST" in (ok.headers.get("Access-Control-Allow-Methods") or ""))
    check("预检带 Max-Age（可缓存，省往返）",
          bool(ok.headers.get("Access-Control-Max-Age")),
          f"got {ok.headers.get('Access-Control-Max-Age')!r}")

    bad = c.options("/api/analyze", headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "POST",
    })
    check("非白名单预检 -> 403（显式拒绝）", bad.status_code == 403, f"got {bad.status_code}")
    check("被拒预检不发 Allow-Origin",
          bad.headers.get("Access-Control-Allow-Origin") is None)

    # 常见预检头都要在白名单里
    am = ok.headers.get("Access-Control-Allow-Headers") or ""
    for h in ("Content-Type",):
        check(f"Allow-Headers 含 {h}", h.lower() in am.lower(), f"got {am!r}")
    # Retry-After 必须暴露，前端才能知道等几秒
    exh = ok.headers.get("Access-Control-Expose-Headers") or c.get(
        "/health", headers={"Origin": "https://ok.example.com"}
    ).headers.get("Access-Control-Expose-Headers") or ""
    check("Expose-Headers 含 Retry-After",
          "retry-after" in exh.lower(), f"got {exh!r}")


def test_preflight_not_rate_limited():
    print("\n[4] 预检不计入限流（本项目实际踩过的坑）")
    # 打开限流
    old = os.environ.get("RATE_LIMIT_DISABLE")
    os.environ["RATE_LIMIT_DISABLE"] = "0"
    try:
        m = _reload_app("https://ok.example.com")
        c = m.app.test_client()
        codes = [
            c.options("/api/analyze", headers={
                "Origin": "https://ok.example.com",
                "Access-Control-Request-Method": "POST",
            }).status_code
            for _ in range(90)
        ]
        check("90 次预检无 429", 429 not in codes,
              f"出现 {codes.count(429)} 次 429")
        check("预检全部 204", set(codes) == {204}, f"got {sorted(set(codes))}")
    finally:
        if old is None:
            os.environ.pop("RATE_LIMIT_DISABLE", None)
        else:
            os.environ["RATE_LIMIT_DISABLE"] = old

    # 但真实请求（GET）仍要受限流保护
    os.environ["RATE_LIMIT_DISABLE"] = "0"
    try:
        m2 = _reload_app("https://ok.example.com")
        c2 = m2.app.test_client()
        gcodes = [c2.get("/api/methods_graph").status_code for _ in range(70)]
        check("真实 GET 仍受限流（豁免只针对 OPTIONS）",
              429 in gcodes, f"未见 429，状态集合={sorted(set(gcodes))}")
    finally:
        if old is None:
            os.environ.pop("RATE_LIMIT_DISABLE", None)
        else:
            os.environ["RATE_LIMIT_DISABLE"] = old

    check("security_guard.is_preflight 存在",
          hasattr(importlib.import_module("security_guard"), "is_preflight"))
    sg = importlib.import_module("security_guard")
    importlib.reload(sg)
    check("is_preflight('OPTIONS') True", sg.is_preflight("OPTIONS") is True)
    check("is_preflight('options') True（大小写不敏感）",
          sg.is_preflight("options") is True)
    for meth in ("GET", "POST", "PUT", "DELETE", ""):
        check(f"is_preflight({meth!r}) False", sg.is_preflight(meth) is False)


def test_wildcard_and_credentials():
    print("\n[5] 通配与凭据的安全处理")
    m = _reload_app("*", None)
    c = m.app.test_client()
    r = c.get("/health", headers={"Origin": "https://any.example.com"})
    check("通配 + 不带凭据 -> '*'",
          r.headers.get("Access-Control-Allow-Origin") == "*",
          f"got {r.headers.get('Access-Control-Allow-Origin')!r}")

    os.environ.pop("CORS_ALLOW_ORIGINS", None)
    check("清空后 cors_enabled False", m.cross_platform.cors_enabled() is False)

    m2 = _reload_app("*", "1")
    c2 = m2.app.test_client()
    r2 = c2.get("/health", headers={"Origin": "https://any.example.com"})
    got = r2.headers.get("Access-Control-Allow-Origin")
    check("通配 + 带凭据 -> 回显来源而非 '*'（浏览器规范要求）",
          got == "https://any.example.com", f"got {got!r}")
    check("带凭据时下发 Allow-Credentials",
          (r2.headers.get("Access-Control-Allow-Credentials") or "").lower() == "true")
    warns = m2.cross_platform.warn_dangerous_config()
    check("危险组合会产生告警", len(warns) >= 1, f"got {warns}")

    m3 = _reload_app("https://safe.example.com", None)
    check("安全配置无告警",
          m3.cross_platform.warn_dangerous_config() == [],
          f"got {m3.cross_platform.warn_dangerous_config()}")


def test_miniprogram_prerequisites():
    print("\n[6] 小程序接入前提（后端侧）")
    m = _reload_app()

    # ① 不使用 cookie 会话
    src = (BASE / "app.py").read_text(encoding="utf-8")
    check("后端设置 cookie 的调用为 0（无 cookie 会话）",
          src.count("set_cookie") == 0, f"found {src.count('set_cookie')}")
    check("会话靠服务端 file_id 承载", "_SESSION[file_id]" in src)
    check("上传返回 file_id", "file_id" in src)

    # ② 上传字段名是 file（wx.uploadFile 的 name 必须一致）
    check("上传读取 request.files['file']",
          'request.files.get("file")' in src)

    # ③ 错误出口是 JSON（小程序解析 JSON，不是 HTML）
    c = m.app.test_client()
    r404 = c.get("/api/definitely_not_here")
    check("API 404 -> JSON", r404.status_code == 404)
    try:
        body = r404.get_json()
        check("404 body 有 ok 字段", isinstance(body, dict) and "ok" in body,
              f"got {body!r}")
    except Exception as e:
        check("404 body 是 JSON", False, repr(e))

    r405 = c.delete("/api/analyze")
    check("405 -> JSON", r405.status_code == 405, f"got {r405.status_code}")
    try:
        b405 = r405.get_json()
        check("405 body 是 JSON", isinstance(b405, dict), f"got {b405!r}")
    except Exception as e:
        check("405 body 是 JSON", False, repr(e))

    # ④ 无 Origin 头也能正常工作（小程序不发 Origin）
    r = c.get("/api/methods_graph")
    check("无 Origin 头调业务接口 -> 200", r.status_code == 200, f"got {r.status_code}")

    # ⑤ 提示文案存在
    hint = m.cross_platform.miniprogram_request_domains_hint()
    check("提供微信域名配置提示", "request 合法域名" in hint)
    check("提示里说明上传/下载域名", "uploadFile" in hint and "downloadFile" in hint)


def test_file_id_contract():
    print("\n[7] file_id 无鉴权（已知取舍，需明确）")
    m = _reload_app()
    c = m.app.test_client()

    # 伪造 file_id 应被拒（会话不存在）
    r = c.post("/api/analyze", json={"file_id": "forged_xxx",
                                     "method": "independent_t",
                                     "group_col": "g", "value_col": "v"})
    check("伪造 file_id -> 400", r.status_code == 400, f"got {r.status_code}")
    try:
        b = r.get_json()
        check("伪造 file_id 返回 ok:false", b.get("ok") is False, f"got {b!r}")
    except Exception as e:
        check("伪造 file_id 响应是 JSON", False, repr(e))

    # 真实上传后可用
    csv = ("gender,score\n" + "".join(
        f"{'男' if i % 2 else '女'},{60 + (i % 20)}\n" for i in range(30)
    )).encode("utf-8-sig")
    up = c.post("/api/upload", data={"file": (io.BytesIO(csv), "t.csv")},
                content_type="multipart/form-data")
    check("上传 -> 200", up.status_code == 200, f"got {up.status_code}")
    fid = (up.get_json() or {}).get("file_id")
    check("上传返回 file_id", bool(fid), f"got {fid!r}")

    if fid:
        r2 = c.post("/api/analyze", json={"file_id": fid, "method": "independent_t",
                                          "group_col": "gender", "value_col": "score"})
        check("带有效 file_id 分析 -> 200", r2.status_code == 200, f"got {r2.status_code}")
        body = r2.get_json() or {}
        check("分析返回 ok:true", body.get("ok") is True)
        check("分析返回 summary.p", "p" in (body.get("summary") or {}),
              f"summary keys={(body.get('summary') or {}).keys()}")

        # 文档必须讲清「file_id 无鉴权」这一取舍
        doc = (BASE / "docs" / "CROSS_PLATFORM.md").read_text(encoding="utf-8")
        check("文档说明 file_id 没有鉴权",
              "file_id" in doc and ("没有鉴权" in doc or "无鉴权" in doc))
        check("文档说明服务重启后 file_id 失效",
              "重启" in doc and "失效" in doc)


def test_docs_exist():
    print("\n[8] 跨端文档齐备")
    for name, must in [
        ("CROSS_PLATFORM.md", ["AppID", "备案", "uploadFile", "CORS_ALLOW_ORIGINS",
                               "预检", "wx.request"]),
        ("API.md", ["/api/upload", "/api/analyze", "file_id", "429", "wx.uploadFile"]),
    ]:
        p = BASE / "docs" / name
        check(f"docs/{name} 存在", p.is_file())
        if not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8")
        for kw in must:
            check(f"docs/{name} 提到 {kw}", kw in txt)
    # API.md 不能引用不存在的文件
    api = BASE / "docs" / "API.md"
    if api.is_file():
        txt = api.read_text(encoding="utf-8")
        for ref in ("DEPLOY_H5.md", "ARCHITECTURE.md"):
            if ref in txt:
                check(f"API.md 引用的 {ref} 存在", (BASE / "docs" / ref).is_file())


def main():
    print("=" * 64)
    print("跨端适配层契约测试")
    print("=" * 64)
    test_default_off()
    test_whitelist()
    test_preflight()
    test_preflight_not_rate_limited()
    test_wildcard_and_credentials()
    test_miniprogram_prerequisites()
    test_file_id_contract()
    test_docs_exist()

    print("\n" + "=" * 64)
    print(f"总计: {PASS} PASS / {FAIL} FAIL")
    if FAILED:
        print("失败项:")
        for f in FAILED:
            print("  -", f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
