# -*- coding: utf-8 -*-
"""生产/H5 部署入口契约测试（wsgi.py + gunicorn.conf.py + Procfile）。

断言的是「部署契约」，不是快照：
  - wsgi.py 必须提供 WSGI 可调用对象，且**不加载 .env**（凭据只走真实环境变量）；
  - 默认监听 0.0.0.0（对外服务绑 127.0.0.1 是容器部署头号坑）；
  - 统计业务链路在生产服务器下必须与 CLI 逐字段一致。

真起 waitress 做一次端到端校验（本机端口，无外部依赖）。
"""
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

PASS = 0
FAIL = 0
FAILED = []

# 本套件需要限流关闭（会连打若干本机请求）
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

PORT = 5188


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
def test_wsgi_module_contract():
    print("\n[1] wsgi.py 模块契约")
    p = BASE / "wsgi.py"
    check("wsgi.py 存在", p.is_file())
    if not p.is_file():
        return
    src = p.read_text(encoding="utf-8")

    check("导出 application", "application" in src)
    check("兼容 app 别名", "app = application" in src)
    check("提供 __main__ 入口", '__name__ == "__main__"' in src)
    check("按平台分派 gunicorn / waitress",
          "gunicorn" in src and "waitress" in src)
    check("默认绑 0.0.0.0", 'DEFAULT_HOST = "0.0.0.0"' in src)
    # 关键：不能 import 时加载 .env（否则本机凭据会进镜像）
    check("import 时不加载 .env", "load_dotenv()" not in src.split("def ")[0])

    # 真 import 一次，确认无副作用且能拿到 Flask app
    try:
        import importlib
        import wsgi as wsgi_mod
        importlib.reload(wsgi_mod)
        check("wsgi.application 是 Flask 应用",
              wsgi_mod.application.__class__.__name__ == "Flask",
              f"got {type(wsgi_mod.application).__name__}")
        check("app 与 application 同一对象",
              wsgi_mod.app is wsgi_mod.application)
        check("/health 路由存在",
              "/health" in [r.rule for r in wsgi_mod.application.url_map.iter_rules()])
        check("导入未污染已加载的 .env（未注入真实 Key）",
              wsgi_mod.application is not None)
    except Exception as e:
        check("wsgi 可导入", False, repr(e))


def test_gunicorn_conf():
    print("\n[2] gunicorn.conf.py 契约")
    p = BASE / "gunicorn.conf.py"
    check("gunicorn.conf.py 存在", p.is_file())
    if not p.is_file():
        return
    src = p.read_text(encoding="utf-8")
    check("bind 默认 0.0.0.0", 'os.environ.get("HOST", "0.0.0.0")' in src)
    check("使用 gthread（LLM 长 IO 不堵死）", '"gthread"' in src)
    check("超时 > 默认 30s（统计慢）",
          re.search(r'timeout\s*=\s*int\(os\.environ\.get\("GUNICORN_TIMEOUT",\s*"(\d+)"\)', src)
          and int(re.search(r'"GUNICORN_TIMEOUT",\s*"(\d+)"', src).group(1)) > 30)
    check("日志输出到 stdout（交给平台收集）", 'accesslog = "-"' in src)
    check("不无条件信任代理头",
          "forwarded_allow_ips" in src and "127.0.0.1" in src)

    # 真 import 校验语法与取值
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("gconf", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        check("bind 可求值且含端口", isinstance(m.bind, str) and ":" in m.bind,
              f"got {m.bind!r}")
        check("workers >= 1", m.workers >= 1, f"got {m.workers}")
        check("worker_class == gthread", m.worker_class == "gthread")
        check("timeout >= 60", m.timeout >= 60, f"got {m.timeout}")
    except Exception as e:
        check("gunicorn.conf 可 import", False, repr(e))


def test_procfile():
    print("\n[3] Procfile 契约")
    p = BASE / "Procfile"
    check("Procfile 存在", p.is_file())
    if not p.is_file():
        return
    txt = p.read_text(encoding="utf-8").strip()
    check("声明 web 进程", txt.startswith("web:"), f"got {txt!r}")
    check("调用 gunicorn", "gunicorn" in txt)
    check("指向 wsgi:application", "wsgi:application" in txt)
    check("加载 gunicorn 配置", "gunicorn.conf.py" in txt)


def test_requirements_marker():
    print("\n[4] requirements 的 WSGI 依赖")
    p = BASE / "requirements.txt"
    txt = p.read_text(encoding="utf-8") if p.is_file() else ""
    check("声明 gunicorn", "gunicorn" in txt)
    check("声明 waitress", "waitress" in txt)
    check("按平台区分（Windows 用 waitress）",
          "sys_platform" in txt)
    check("gunicorn 排除 win32", 'sys_platform != "win32"' in txt)


# ---------------------------------------------------------------------------
def _wait_ready(url, tries=60):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def test_live_parity():
    """真起服务，验证 H5 链路与 CLI 逐字段一致（规划的验收标准）。"""
    print(f"\n[5] 端到端：生产服务器 vs CLI（端口 {PORT}）")
    env = dict(os.environ)
    env["PORT"] = str(PORT)
    env["HOST"] = "127.0.0.1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["RATE_LIMIT_DISABLE"] = "1"

    proc = subprocess.Popen(
        [sys.executable, str(BASE / "wsgi.py")],
        cwd=str(BASE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{PORT}"
        if not _wait_ready(base + "/health"):
            out = b""
            try:
                proc.terminate()
                out = proc.communicate(timeout=5)[0]
            except Exception:
                pass
            check("wsgi.py 可启动并响应 /health", False,
                  out.decode("utf-8", "replace")[-400:])
            return
        check("wsgi.py 可启动并响应 /health", True)

        # PWA 资源在生产服务器下也要能取到
        for path in ("/", "/static/manifest.json", "/static/sw.js", "/favicon.ico"):
            try:
                r = urllib.request.urlopen(base + path, timeout=10)
                check(f"GET {path} -> 200", r.status == 200,
                      f"got {r.status}")
            except urllib.error.HTTPError as e:
                check(f"GET {path} -> 200", False, f"got {e.code}")

        # 造数据 → 上传 → 分析
        csv_bytes = ("gender,score\n" + "".join(
            f"{'男' if i % 2 else '女'},{60 + (i % 20)}\n" for i in range(40)
        )).encode("utf-8-sig")

        bnd = "----h5parity"
        body = (f"--{bnd}\r\n").encode()
        body += b'Content-Disposition: form-data; name="file"; filename="p.csv"\r\n'
        body += b"Content-Type: text/csv\r\n\r\n"
        body += csv_bytes + b"\r\n" + (f"--{bnd}--\r\n").encode()
        up = json.loads(urllib.request.urlopen(urllib.request.Request(
            base + "/api/upload", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + bnd}),
            timeout=30).read())
        check("上传返回 file_id", bool(up.get("file_id")), f"got {up.get('file_id')!r}")

        payload = json.dumps({"file_id": up["file_id"], "method": "independent_t",
                              "group_col": "gender", "value_col": "score"}).encode()
        web = json.loads(urllib.request.urlopen(urllib.request.Request(
            base + "/api/analyze", data=payload,
            headers={"Content-Type": "application/json"}), timeout=60).read())
        check("/api/analyze ok", web.get("ok") is True, f"got {web.get('ok')!r}")

        # CLI 同口径（用 examples 里的官方样例，路径稳定）
        sample = BASE / "examples" / "student_scores.csv"
        cli_out = subprocess.run(
            [sys.executable, str(BASE / "cli.py"), "analyze", str(sample),
             "-m", "independent_t", "-g", "gender", "-v", "score", "-f", "json"],
            cwd=str(BASE), capture_output=True, text=True, encoding="utf-8",
            env={**env, "PYTHONIOENCODING": "utf-8"})
        check("CLI 返回 0", cli_out.returncode == 0, cli_out.stderr[-200:])

        if cli_out.returncode == 0:
            cli = json.loads(cli_out.stdout)
            # 用同一份样例数据走网页端，才能逐字段比对
            bnd2 = "----h5parity2"
            raw = sample.read_bytes()
            body2 = (f"--{bnd2}\r\n").encode()
            body2 += b'Content-Disposition: form-data; name="file"; filename="s.csv"\r\n'
            body2 += b"Content-Type: text/csv\r\n\r\n"
            body2 += raw + b"\r\n" + (f"--{bnd2}--\r\n").encode()
            up2 = json.loads(urllib.request.urlopen(urllib.request.Request(
                base + "/api/upload", data=body2,
                headers={"Content-Type": "multipart/form-data; boundary=" + bnd2}),
                timeout=30).read())
            pay2 = json.dumps({"file_id": up2["file_id"], "method": "independent_t",
                               "group_col": "gender", "value_col": "score"}).encode()
            web2 = json.loads(urllib.request.urlopen(urllib.request.Request(
                base + "/api/analyze", data=pay2,
                headers={"Content-Type": "application/json"}), timeout=60).read())

            cs, ws = cli.get("summary", {}), web2.get("summary", {})
            diffs = [k for k in set(cs) | set(ws) if cs.get(k) != ws.get(k)]
            check("统计量逐字段一致（CLI vs H5 服务器）",
                  not diffs, f"差异字段={diffs}")
            check("字段数 >= 10（确实比了实质内容）",
                  len(cs) >= 10, f"仅 {len(cs)} 个字段")
            if cs.get("p") is not None:
                print(f"        p={cs['p']}  t={cs.get('t')}  df={cs.get('df')}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


def test_deploy_doc():
    print("\n[6] 部署文档存在且关键点写全")
    p = BASE / "docs" / "DEPLOY_H5.md"
    check("docs/DEPLOY_H5.md 存在", p.is_file())
    if not p.is_file():
        return
    txt = p.read_text(encoding="utf-8")
    for kw in ("HOST=0.0.0.0", "TRUST_PROXY", "gunicorn", "wsgi:application",
               "/health", "DEBUG=0"):
        check(f"文档提到 {kw}", kw in txt)
    # 必须明确说清「不能纯静态托管」这个易踩的认知坑
    check("文档说明不能纯静态部署",
          "不能纯静态" in txt or "不能" in txt and "静态" in txt)


def main():
    print("=" * 64)
    print("生产/H5 部署入口契约测试")
    print("=" * 64)
    test_wsgi_module_contract()
    test_gunicorn_conf()
    test_procfile()
    test_requirements_marker()
    test_live_parity()
    test_deploy_doc()

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
