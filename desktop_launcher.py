"""
智论助手 - 桌面端启动器
直接 import Flask app 运行，无需子进程。
特性：
- 若 5000 端口已有本应用实例，则直接复用并打开浏览器
- 否则自动寻找空闲端口启动
"""
import sys
import time
import socket
import threading
import webbrowser
import urllib.request
from pathlib import Path

DEFAULT_PORT = 5000
APP_TITLE = "智论助手 v0.9.0"


def is_our_app(port: int, timeout: float = 1.0) -> bool:
    """检测指定端口上是否运行着本应用（通过 /health 返回 ok）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return '"ok"' in body and "true" in body
    except Exception:
        return False


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
        input("按回车键退出...")
        return

    print("\n" + "=" * 44)
    print(f"{APP_TITLE} 已启动！")
    print(f"浏览器地址：{url}")
    print("关闭此窗口即停止服务")
    print("=" * 44 + "\n")

    try:
        app.run(host="127.0.0.1", port=port, debug=False)
    except KeyboardInterrupt:
        print("\n[退出] 已停止")


if __name__ == "__main__":
    main()
