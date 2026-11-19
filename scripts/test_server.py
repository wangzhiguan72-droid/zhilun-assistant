# -*- coding: utf-8 -*-
"""回归用的共享 Flask 服务夹具。

为什么需要它：9 个套件（methods / paper_check / regression / regression_audit /
smoke / ai_audit / defense_pack / table_cross 的端到端段）走**真实 HTTP**，
不是 test_client。服务没起时它们要么 rc=2、要么静默 [SKIP]，而 runner 把这些
当成「环境未就绪，已忽略」，最后照样打印「全部通过」——测试没跑 ≠ 测试通过。

设计取舍：
  - 用 `wsgi.py` 而不是 `app.py`：wsgi 明确**不加载 .env**，测试服务拿不到本机凭据。
  - 再主动剥掉所有 *_KEY / *_TOKEN / *_SECRET：回归必须离线、确定、不烧钱。
    需要真 Key 的套件由 runner 显式声明排除，而不是让服务偷偷带着凭据去真调。
  - RATE_LIMIT_DISABLE=1：这些套件连打十几个请求，不关限流会吃 429 然后 rc=2。
    只在本脚本构造的子进程环境里设，不污染全局（CONTRIBUTING §三.7）。
  - 端口被占用就**明确报错**，不去复用一个来历不明的服务——那个服务可能开着
    限流、带着真 Key，测试结果不可信。

用法：
    from test_server import TestServer
    with TestServer(port=5000) as srv:
        ...  # srv.base == "http://127.0.0.1:5000"

    .venv/Scripts/python.exe scripts/test_server.py --port 5000   # 手动起，Ctrl-C 停
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRED_SUFFIX = ("_KEY", "_TOKEN", "_SECRET")


def port_busy(port: int, timeout: float = 1.5) -> bool:
    """只认 HTTP 应答：TCP 连得上但不应答 /health 的进程也算占用。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def child_env(port: int) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().endswith(CRED_SUFFIX)}
    env.update({
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "DEBUG": "0",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "RATE_LIMIT_DISABLE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    })
    return env


class TestServer:
    def __init__(self, port: int = 5000, root: Path | None = None,
                 ready_timeout: float = 60.0):
        self.port = port
        self.root = Path(root or ROOT)
        self.ready_timeout = ready_timeout
        self.proc: subprocess.Popen | None = None
        self.log: Path | None = None
        self._fh = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=2) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> "TestServer":
        if port_busy(self.port):
            raise RuntimeError(
                f"端口 {self.port} 已被占用。回归需要一个干净的服务实例——"
                f"请先关掉正在运行的智论助手（应用窗口 / exe / python app.py），"
                f"或用 --port 指定别的端口。不复用来历不明的服务。")
        py = self.root / ".venv" / "Scripts" / "python.exe"
        exe = str(py) if py.exists() else sys.executable
        # 日志名带端口：runner 在 5000 起服务、test_server_test 在 5010 起服务，
        # 共用一个文件名会互相覆盖，出 500 时 traceback 就找不回来了。
        self.log = self.root / f"_test_server_{self.port}.log"
        self._fh = open(self.log, "wb")
        self.proc = subprocess.Popen(
            [exe, "-u", str(self.root / "wsgi.py")],
            cwd=str(self.root), env=child_env(self.port),
            stdout=self._fh, stderr=subprocess.STDOUT)
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"服务进程提前退出（rc={self.proc.returncode}），"
                    f"启动日志见 {self.log}")
            if self._healthy():
                return self
            time.sleep(0.25)
        self.stop()
        raise RuntimeError(f"服务 {self.ready_timeout:.0f}s 内未就绪，日志见 {self.log}")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        # 进程退出与内核释放监听套接字之间有个窗口：不等干净就返回，
        # 紧接着的下一个 TestServer 会误判「端口被占用」。
        deadline = time.time() + 10
        while time.time() < deadline and port_busy(self.port):
            time.sleep(0.2)

    def __enter__(self) -> "TestServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="起一个回归用的干净服务实例")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    try:
        srv = TestServer(port=args.port).start()
    except RuntimeError as e:
        print(f"[FAIL] {e}", flush=True)
        return 2
    print(f"[OK] {srv.base} 就绪（无凭据、限流已关）。Ctrl-C 停止。", flush=True)
    try:
        while srv.proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())