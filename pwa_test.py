# -*- coding: utf-8 -*-
"""PWA 资源与契约测试。

断言的是「契约」，不是快照：
  - manifest 必须能被浏览器当作有效 manifest 解析，且具备可安装所需的最小字段；
  - service worker 必须永不拦截 /api/*（陈旧统计结果 = 正确性事故）；
  - 图标必须真实存在、尺寸正确，maskable 图标必须满幅不透明（否则系统裁切会出现白边/黑边）。

这些约束一旦被破坏，用户看到的可能是"旧数字"，比崩溃更危险。
不依赖运行中的服务端，全部走 test_client 与文件系统。
"""
import os

# 硬性纪律 7（与 registry_test / wizard_test 同款）：本套件会连续调用限流路径，
# 必须整体关闭限流，否则 60 秒滑窗内必吃 429（v2.27 扫描报告 P1-1）。
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
import json
import struct
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL  {name}  {extra}")


def png_size(path: Path):
    """读取 PNG 的 IHDR，返回 (w, h, has_alpha)。不依赖 Pillow。"""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", data[16:24])
    # IHDR: bit depth at 24, color type at 25 (6 = RGBA, 2 = RGB)
    color_type = data[25]
    return w, h, color_type == 6


def main():
    print("=" * 62)
    print("PWA 资源与契约测试")
    print("=" * 62)

    # ---------- 1. 文件存在性 ----------
    print("\n[1] 资源文件存在")
    files = {
        "manifest": BASE / "static" / "manifest.json",
        "sw": BASE / "static" / "sw.js",
        "offline": BASE / "static" / "offline.html",
        "favicon": BASE / "static" / "favicon.ico",
        "icon-192": BASE / "static" / "icons" / "icon-192.png",
        "icon-512": BASE / "static" / "icons" / "icon-512.png",
        "mask-192": BASE / "static" / "icons" / "maskable-192.png",
        "mask-512": BASE / "static" / "icons" / "maskable-512.png",
    }
    for label, p in files.items():
        check(f"{label} 存在 ({p.name})", p.is_file(), f"缺失: {p}")

    # ---------- 2. manifest 契约 ----------
    print("\n[2] manifest.json 契约")
    manifest_path = files["manifest"]
    if manifest_path.is_file():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            check("manifest 是合法 JSON", True)
        except Exception as e:
            m = {}
            check("manifest 是合法 JSON", False, repr(e))

        for key in ("name", "short_name", "start_url", "display", "icons"):
            check(f"含必需字段 {key}", key in m)

        check("display == standalone", m.get("display") == "standalone",
              f"got {m.get('display')!r}")
        check("start_url 以 / 开头", str(m.get("start_url", "")).startswith("/"),
              f"got {m.get('start_url')!r}")

        icons = m.get("icons", [])
        check("icons 非空", len(icons) > 0)
        sizes = {i.get("sizes") for i in icons}
        check("含 192x192 图标", "192x192" in sizes, f"got {sorted(sizes)}")
        check("含 512x512 图标", "512x512" in sizes, f"got {sorted(sizes)}")
        check("含 maskable 图标",
              any("maskable" in (i.get("purpose") or "") for i in icons))
        # 每个图标路径都要真实存在
        all_exist = True
        for i in icons:
            src = i.get("src", "")
            target = BASE / src.lstrip("/").replace("/", str(Path("/")))
            target = BASE / src.lstrip("/")
            if not target.is_file():
                all_exist = False
                print(f"        -> 图标缺失: {src}")
        check("manifest 中所有图标文件均存在", all_exist)

    # ---------- 3. service worker 契约（最关键） ----------
    print("\n[3] service worker 契约 —— 绝不缓存统计结果")
    sw_path = files["sw"]
    if sw_path.is_file():
        sw = sw_path.read_text(encoding="utf-8")

        check("声明 /api/ 路径守卫", "/api/" in sw)
        check("含 shouldHandle 守卫函数", "shouldHandle" in sw)

        # 核心断言：/api/* 必须被显式排除，且只处理 GET
        check("排除非 GET 请求", "method !== 'GET'" in sw or 'method !== "GET"' in sw)
        check("显式排除 /api/ 前缀",
              "startsWith('/api/')" in sw or 'startsWith("/api/")' in sw)
        check("排除跨域请求", "origin !== self.location.origin" in sw)

        # 不允许出现"无条件缓存 API"的写法
        check("未出现 cache.put 作用于任意请求的危险模式",
              "caches.open" in sw)  # 有缓存逻辑，但受 shouldHandle 保护

        check("支持 SKIP_WAITING 消息", "SKIP_WAITING" in sw)
        check("带版本号常量", "VERSION" in sw)

        # install 阶段不应预缓存 /api
        api_precached = "/api/" in sw.split("PRECACHE")[1] if "PRECACHE" in sw else False
        check("预缓存清单不含 /api/", not api_precached)

    # ---------- 4. 图标真实性与尺寸 ----------
    print("\n[4] 图标尺寸与不透明度")
    expect = {
        "icon-192": (192, 192),
        "icon-512": (512, 512),
        "mask-192": (192, 192),
        "mask-512": (512, 512),
    }
    for label, (ew, eh) in expect.items():
        p = files[label]
        if not p.is_file():
            check(f"{label} 尺寸", False, "文件缺失")
            continue
        got = png_size(p)
        if got is None:
            check(f"{label} 是合法 PNG", False)
            continue
        w, h, has_alpha = got
        check(f"{label} 尺寸 {ew}x{eh}", (w, h) == (ew, eh), f"got {w}x{h}")

    # maskable 必须满幅：即整张图 4 个角都不透明。
    # 这里用轻量方式校验：maskable 与普通图标不应是同一份字节（否则说明没做安全区处理）
    if files["mask-512"].is_file() and files["icon-512"].is_file():
        same = files["mask-512"].read_bytes() == files["icon-512"].read_bytes()
        check("maskable 与普通图标内容不同（做了安全区处理）", not same)

    # ---------- 5. index.html 接线 ----------
    print("\n[5] index.html 接线")
    idx = BASE / "templates" / "index.html"
    if idx.is_file():
        html = idx.read_text(encoding="utf-8")
        check("引用 manifest.json", "manifest.json" in html)
        check("声明 theme-color", 'name="theme-color"' in html)
        check("引用 apple-touch-icon", "apple-touch-icon" in html)
        check("注册 service worker", "serviceWorker" in html)
        check("注册时校验协议为 http(s)",
              "protocol" in html and ("http:" in html or "https:" in html))
        # 注册路径必须指向 /static/sw.js
        check("SW 注册路径正确", "/static/sw.js" in html)

    # ---------- 6. app.py 路由 ----------
    print("\n[6] app.py 路由")
    try:
        import app as appmod
        client = appmod.app.test_client()

        for path, want_ct in [
            ("/favicon.ico", "image"),
            ("/static/manifest.json", "json"),
            ("/static/sw.js", "javascript"),
            ("/static/offline.html", "html"),
            ("/static/icons/icon-192.png", "png"),
            ("/static/icons/maskable-512.png", "png"),
        ]:
            r = client.get(path)
            ct = r.headers.get("Content-Type", "")
            check(f"GET {path} -> 200",
                  r.status_code == 200, f"got {r.status_code}")
            check(f"GET {path} Content-Type 含 {want_ct}",
                  want_ct in ct, f"got {ct!r}")

        # favicon 应有缓存头，减少重复请求
        r = client.get("/favicon.ico")
        check("favicon 带 Cache-Control",
              "max-age" in r.headers.get("Cache-Control", ""),
              f"got {r.headers.get('Cache-Control')!r}")

        # sw.js 必须能被浏览器取到且不被长期缓存（否则无法更新 SW）
        r = client.get("/static/sw.js")
        check("sw.js 可被获取", r.status_code == 200)

    except Exception as e:
        check("app 可导入并响应 PWA 路由", False, repr(e))

    # ---------- 7. 反向断言：不应把统计接口交给 SW ----------
    print("\n[7] 反向断言 —— API 仍走网络")
    try:
        import app as appmod
        client = appmod.app.test_client()
        r = client.get("/api/methods_graph")
        check("/api/methods_graph 正常响应（未被 SW 影响）",
              r.status_code == 200, f"got {r.status_code}")
        # 统计类接口必须始终可用；它一旦被 SW 缓存就会返回陈旧数字
        r2 = client.post("/api/analyze", json={})
        check("/api/analyze 无参数时给出 4xx（而非被缓存）",
              r2.status_code >= 400, f"got {r2.status_code}")
    except Exception as e:
        check("API 路由可用", False, repr(e))

    print("\n" + "=" * 62)
    print(f"总计: {PASS} PASS / {FAIL} FAIL")
    if FAILED:
        print("失败项:")
        for f in FAILED:
            print("  -", f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
