"""
智论助手 - 桌面端启动器
直接 import Flask app 运行，无需子进程。
特性：
- 若 5000 端口已有本应用实例，则直接复用并打开浏览器
- 否则自动寻找空闲端口启动
"""
import json
import sys
import time
import socket
import threading
import webbrowser
import urllib.request
from pathlib import Path

DEFAULT_PORT = 5000
APP_TITLE = "智论助手 v0.9.0"
# 本机 /health 里的身份标记（见 `app.health`）。老版本没有这个字段，所以
# **缺字段仍按自家算，字段不对才算别人** —— 既认得老实例，又不会把别人认成自己。
SERVICE_MARKER = "zhilun-assistant"

# v2.19：回环地址的请求**强制不走代理**。开过代理工具 / 公司网络下，
# `urlopen` 会把 127.0.0.1 也发给代理，于是"已有实例"永远检测不到 ——
# 又起一个新实例（端口还不一样，用户会以为刚才的数据丢了）。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_get(url: str, timeout: float) -> str:
    """GET 一个**本机** URL，绕过一切代理，返回响应体文本。"""
    req = urllib.request.Request(url, headers={"User-Agent": "zhilun-launcher"})
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def is_our_app(port: int, timeout: float = 1.0) -> bool:
    """检测指定端口上是否运行着本应用。

    判定标准：``/health`` 返回**合法 JSON** 且 ``ok`` 为真；若响应里带了
    ``service`` 标记，则必须等于 ``zhilun-assistant``。

    旧实现只做 ``'"ok"' in body and "true" in body`` 子串匹配，会把任何
    返回 ``{"ok": true}`` 的第三方服务误认成自己（5000 是 Flask 默认端口，
    撞车概率不低），然后打开别人的页面。
    """
    try:
        body = _http_get(f"http://127.0.0.1:{port}/health", timeout)
    except Exception:  # noqa: BLE001 —— 连不上 / 超时 / 非 2xx，一律"不是我们"
        return False
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict) or not data.get("ok"):
        return False
    service = data.get("service")
    return service is None or service == SERVICE_MARKER


def port_is_free(port: int) -> bool:
    """端口是否可绑定。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_free_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    """从 start 开始找一个可绑定的空闲端口。"""
    for port in range(start, start + tries):
        if port_is_free(port):
            return port
    raise RuntimeError(f"从 {start} 起找不到空闲端口")


def open_browser_delayed(url: str, delay: float = 2.0) -> None:
    time.sleep(delay)
    webbrowser.open(url)


def _wait_for_enter(prompt: str = "按回车键退出...") -> None:
    """等用户回车。无控制台时（--windowed 打包、CI、被脚本拉起）直接放过。

    旧实现直接 `input(...)`：没有 stdin 的环境会抛 `EOFError`，
    用户还没看到"无法导入 app"这行报错，窗口就先炸了。
    """
    try:
        input(prompt)
    except (EOFError, OSError):
        pass


def main() -> None:
    print("=" * 44)
    print(f"{APP_TITLE} - 桌面端")
    print("=" * 44)

    # 1) 若本应用已在运行，直接复用
    if is_our_app(DEFAULT_PORT):
        url = f"http://127.0.0.1:{DEFAULT_PORT}"
        print(f"[复用] 检测到已有实例运行，直接打开 {url}")
        webbrowser.open(url)
        return

    # 2) 选择端口（默认 5000，被占用则自动顺延）
    if port_is_free(DEFAULT_PORT):
        port = DEFAULT_PORT
    else:
        port = find_free_port(DEFAULT_PORT + 1)
        print(f"[端口] {DEFAULT_PORT} 已被占用，改用 {port}")

    # 3) 把资源目录加入 import 路径（PyInstaller 冻结后）
    if getattr(sys, "frozen", False):
        resource_dir = str(Path(sys._MEIPASS))
        sys.path.insert(0, resource_dir)
        print(f"[资源] {resource_dir}")

    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=open_browser_delayed, args=(url,), daemon=True).start()

    # 4) 启动 Flask
    print(f"[启动] 正在启动 Flask 后端（端口 {port}）...")
    try:
        from app import app
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 无法导入 app：{e}")
        _wait_for_enter()
        return

    print("\n" + "=" * 44)
    print(f"{APP_TITLE} 已启动！")
    print(f"浏览器地址：{url}")
    print("关闭此窗口即停止服务")
    print("=" * 44 + "\n")

    try:
        # use_reloader 必须显式关：reloader 会派生子进程，冻结包里行为不可控
        # （且本项目实测 reloader 子进程会随父进程回收，端口变成时好时坏）。
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[退出] 已停止")
    except OSError as e:
        # 检测端口和实际绑定之间有时间差，被别的程序抢了就会走到这里。
        # 旧实现只捕获 KeyboardInterrupt，这里会甩一屏 traceback，
        # 而浏览器线程已经把用户带到了这个打不开的地址上。
        print(f"\n[错误] 端口 {port} 无法绑定：{e}")
        print("（多半是刚才检测完又被别的程序占用了，重开一次即可）")


if __name__ == "__main__":
    main()
