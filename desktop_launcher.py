"""
智论助手 - 桌面端启动器（v2.22）
================================
直接 import Flask app 运行，无需子进程。

v2.22 新增（总纲 §四「桌面端下一步」）：
    1. **托盘常驻**：右下角托盘图标（双击打开页面 / 右键退出）。
       "关掉黑窗口就停服务"的心智负担没了——控制台最小化即可，退出走托盘。
       pystray 未安装 / 无图标 / 初始化失败 → 自动退回原控制台行为
       （优雅降级，不装任何东西也照常用）。
    2. **拖拽数据文件到 exe 图标启动**：把 .csv/.xlsx 拖到智论助手.exe 图标上，
       应用启动后自动加载该文件并直接出「数据体检」报告，省掉
       "打开浏览器 → 点上传 → 选文件"三步。
       安全边界：文件路径**只经本机环境变量**传给后端（不走 HTTP 参数），
       /api/local_open 只在回环访问 + 桌面模式环境变量存在时才生效——
       部署到内网 / 公网（HOST=0.0.0.0）的服务完全不受影响。

原有特性（v2.19 加固，测试覆盖见 desktop_launcher_test.py）：
- 若 5000 端口已有本应用实例，则直接复用并打开浏览器
- 否则自动寻找空闲端口启动
"""
import json
import os
import sys
import time
import socket
import threading
import webbrowser
import urllib.request
from pathlib import Path

DEFAULT_PORT = 5000

# v2.22：版本号单一真源（以前这里写死 "智论助手 v0.9.0"，页面角标写死 v1.1）。
# frozen 环境下 version.py 在 _MEIPASS 资源目录里，先补路径再导入。
if getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(sys._MEIPASS)))
try:
    from version import APP_TITLE
except ImportError:  # 源码直跑且 version.py 缺失时也不炸
    APP_TITLE = "智论助手"

# 本机 /health 里的身份标记（见 `app.health`）。老版本没有这个字段，所以
# **缺字段仍按自家算，字段不对才算别人** —— 既认得老实例，又不会把别人认成自己。
SERVICE_MARKER = "zhilun-assistant"

# v2.22：拖拽到图标上可自动打开的数据文件扩展名（与 /api/upload 白名单一致）
OPENABLE_EXT = {".csv", ".xlsx", ".xls"}

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


def dropped_file() -> str | None:
    """拖拽到 exe 图标上的数据文件路径（sys.argv[1]，合法才返回）。"""
    if len(sys.argv) < 2:
        return None
    p = sys.argv[1].strip().strip('"')
    if not p or not Path(p).is_file():
        return None
    if Path(p).suffix.lower() not in OPENABLE_EXT:
        return None
    return str(Path(p).resolve())


def _run_flask(app, port: int) -> None:
    """托盘模式下在后台线程跑 Flask（use_reloader 必须关，理由见 main）。"""
    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except OSError as e:
        print(f"\n[错误] 端口 {port} 无法绑定：{e}")


def _try_tray(app, port: int, url: str) -> bool:
    """托盘常驻（v2.22，可选能力）。接管主线程返回 True；否则 False。

    结构：Flask 放后台守护线程，pystray 的 run() 占主线程（Windows 托盘要求）。
    任一环节失败（未装 pystray / 无图标 / 初始化异常）都退回控制台模式。
    """
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print("[托盘] pystray 未安装，退回控制台模式。", flush=True)
        return False

    # 图标：优先打包资源里的 favicon.ico，回退 assets/icon.ico
    icon_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "static" / "favicon.ico"
    if not icon_path.is_file():
        icon_path = Path(__file__).resolve().parent / "assets" / "icon.ico"
    if not icon_path.is_file():
        print("[托盘] 未找到图标文件，退回控制台模式。", flush=True)
        return False
    try:
        image = Image.open(icon_path)
    except Exception:  # noqa: BLE001
        return False

    def _open(icon=None, item=None):  # noqa: ARG001 — pystray 回调签名
        webbrowser.open(url)

    def _quit(icon=None, item=None):  # noqa: ARG001
        print("\n[退出] 托盘退出，正在停止服务…")
        icon.stop()
        # Flask 在守护线程里，主线程退出即整体退出；os._exit 保证立即
        os._exit(0)

    try:
        icon = pystray.Icon(
            "zhilun", image, f"{APP_TITLE}（双击图标打开页面）",
            menu=pystray.Menu(
                pystray.MenuItem("打开页面", _open, default=True),
                pystray.MenuItem("退出", _quit),
            ))
        t = threading.Thread(target=_run_flask, args=(app, port), daemon=True)
        t.start()
        time.sleep(1.5)  # 等 Flask 先绑上端口，避免托盘已就绪而页面打不开
        print("[托盘] 已启动：右下角托盘图标可打开页面 / 退出；"
              "本控制台可最小化（不要关闭）。", flush=True)
        icon.run()  # 占主线程直到"退出"
        return True
    except Exception as e:  # noqa: BLE001 — 托盘失败不挡使用
        print(f"[托盘] 不可用（{type(e).__name__}: {e}），退回控制台模式。", flush=True)
        return False


def main() -> None:
    print("=" * 44)
    print(f"{APP_TITLE} - 桌面端")
    print("=" * 44)

    # v2.22：拖拽数据文件到图标 → 经环境变量交给后端（一次性，不走 HTTP 参数）
    open_path = dropped_file()
    if open_path:
        print(f"[拖拽] 检测到数据文件：{Path(open_path).name}（启动后自动加载并体检）")

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
    if open_path:
        os.environ["ZL_DESKTOP_OPEN_FILE"] = open_path
        os.environ["ZL_DESKTOP_LOCAL_OPEN"] = "1"
        url += "/?localfile=1"

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
    print("=" * 44 + "\n")

    # 5) 托盘可用 → 托盘接管主线程；否则控制台阻塞（原行为）
    if _try_tray(app, port, url):
        return

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
