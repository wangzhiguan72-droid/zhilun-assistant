# -*- coding: utf-8 -*-
"""智论助手 · 生产/H5 部署入口（WSGI）。

为什么需要这个文件：
    本地内测用 `python app.py`（Flask 自带开发服务器）足够，
    但**开发服务器不适合对外提供 H5 服务**：
      - 单进程单线程，多人同时用会排队；
      - 没有优雅重启 / 超时管理；
      - 会打印 Werkzeug 警告，生产环境不该出现。

    本文件按运行平台自动挑选 WSGI 服务器：
      - Linux / macOS / 容器：gunicorn（多 worker）
      - Windows：waitress（纯 Python，gunicorn 不支持 Windows）

用法：
    # Linux / 容器 / 云服务器（Render、Railway、阿里云、腾讯云 …）
    gunicorn --config gunicorn.conf.py wsgi:application

    # Windows
    python wsgi.py

    # 也可以直接交给平台自动探测（Procfile 已写好）

环境变量（与 app.py 保持一致，不需要改代码）：
    PORT           监听端口，默认 5000（PaaS 通常自动注入）
    HOST           监听地址，默认 0.0.0.0（对外服务必须，否则映射端口连不上）
    TRUST_PROXY    置于 Nginx / 云负载均衡之后时设为 1，否则限流拿到的是代理 IP
    DEBUG          生产环境务必保持 0（默认即 0）
    LLM_TIER       免费档 free / 付费档 pro
    <各平台>_API_KEY  可选，不配也能完整运行（AI 解读功能降级）

注意：本文件 import 时**不加载 .env**（与 app.py 一致），
      Key 一律通过真实环境变量注入 —— 这是 12-factor 做法，
      也避免把本机凭据带进容器镜像。
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# import 即得到模块级 app 对象（app.py 第 99 行），无需任何副作用
from app import app as application  # noqa: E402

# 兼容多种 WSGI 服务器对入口名的探测习惯
app = application

DEFAULT_PORT = 5000
# 对外服务必须绑 0.0.0.0：绑 127.0.0.1 时容器/PaaS 映射出去的端口永远连不上
DEFAULT_HOST = "0.0.0.0"


def _resolve_host_port():
    port = os.environ.get("PORT", str(DEFAULT_PORT)).strip() or str(DEFAULT_PORT)
    host = os.environ.get("HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(port)
    except ValueError:
        print(f"[warn] PORT={port!r} 不是合法端口号，回退到 {DEFAULT_PORT}")
        port = DEFAULT_PORT
    return host, port


def _warn_if_insecure(host):
    """把「暴露到公网」这件事显式说出来，避免无意中泄露内测数据。"""
    if host in ("0.0.0.0", "::"):
        print("[info] 监听 0.0.0.0：服务将可被同网络/公网访问。")
        print("       请确认已设置访问控制，且数据合规（本项目后端全内存不落盘）。")
    if os.environ.get("DEBUG", "0") == "1":
        print("[warn] DEBUG=1：会暴露堆栈细节，生产环境请去掉该变量。")


def serve_waitress():
    """Windows / 通用：waitress（纯 Python，多线程）。"""
    from waitress import serve

    host, port = _resolve_host_port()
    _warn_if_insecure(host)
    threads = int(os.environ.get("WAITRESS_THREADS", "8"))
    print(f"智论助手（waitress）→ http://{host}:{port}  threads={threads}")
    serve(application, host=host, port=port, threads=threads,
          ident="zhilun", clear_untrusted_proxy_headers=True)


def serve_gunicorn():
    """Linux / 容器：gunicorn。"""
    from gunicorn.app.base import BaseApplication

    class _App(BaseApplication):
        def load_config(self):
            cfg = {
                "bind": "%s:%s" % _resolve_host_port(),
                "workers": int(os.environ.get("WEB_CONCURRENCY", "2")),
                "threads": int(os.environ.get("GUNICORN_THREADS", "4")),
                "timeout": int(os.environ.get("GUNICORN_TIMEOUT", "120")),
                # 统计计算可能耗时（大样本 ANOVA），给足超时；
                # 但绝不开 worker 重启风暴，避免用户请求被反复掐断。
                "graceful_timeout": 30,
                "keepalive": 5,
                # 日志到 stdout/stderr，交给平台收集
                "accesslog": "-",
                "errorlog": "-",
                "loglevel": os.environ.get("GUNICORN_LOGLEVEL", "info"),
                # 不信任任意代理头（安全性同 security_guard 的设计）
                "forwarded_allow_ips": os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
            }
            for k, v in cfg.items():
                self.cfg.set(k, v)

        def load(self):
            return application

    _warn_if_insecure(os.environ.get("HOST", DEFAULT_HOST))
    _App().run()


def main():
    if sys.platform == "win32":
        serve_waitress()
        return
    try:
        serve_gunicorn()
    except ImportError:
        print("[warn] 未安装 gunicorn，回退到 waitress。")
        print("       建议：pip install gunicorn")
        serve_waitress()


if __name__ == "__main__":
    main()
