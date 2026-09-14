"""v2.19 · `desktop_launcher` 桌面端启动器回归测试。

为什么补它：覆盖扫描显示这是**唯一一个零测试的产品模块**（105 行）。
它管的是用户双击图标后的第一件事——"复用已有实例，还是新起一个"，
错了的后果是"开了第二个实例 / 打开了别人的页面 / 报错窗口一闪而过"。

实测三个真缺陷（本文件逐条守着）：

  1. **回环请求走代理**：`urlopen` 会把 127.0.0.1 也发给 `HTTP_PROXY`，
     开了代理工具就永远检测不到已有实例 → 又起一个新实例（端口还不一样，
     用户以为刚才的数据丢了）。本项目自己的测试都要设 `NO_PROXY`，
     说明这个坑真实存在。
  2. **身份判定太松**：`'"ok"' in body and "true" in body` 是子串匹配，
     任何返回 `{"ok": true}` 的第三方服务都会被当成"我的另一个实例"
     —— 而 5000 是 Flask 默认端口，撞车概率不低。
  3. **报错路径会二次崩溃**：导入失败时直接 `input()`，无控制台环境
     （`--windowed` 打包 / CI / 被脚本拉起）抛 `EOFError`，
     用户还没看到"无法导入 app"窗口就先炸了。
     另外 `app.run` 只捕获 `KeyboardInterrupt`，端口被抢时甩一屏 traceback。

跑法：python desktop_launcher_test.py
退出码：0 全通过 / 1 有真失败 / 2 环境未就绪
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import desktop_launcher as dl  # noqa: E402

PASS = 0
FAIL = 0
_LINES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        _LINES.append(f"  [PASS] {name}")
    else:
        FAIL += 1
        _LINES.append(f"  [FAIL] {name}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    _LINES.append("")
    _LINES.append("=" * 72)
    _LINES.append(title)
    _LINES.append("=" * 72)


# ---------------------------------------------------------------------------
# 本机 stub 服务
# ---------------------------------------------------------------------------
def make_server(payload: bytes, status: int = 200, sleep: float = 0.0):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if sleep:
                time.sleep(sleep)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def j(body: dict) -> bytes:
    return json.dumps(body).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. is_our_app 身份判定
# ---------------------------------------------------------------------------
section("1. is_our_app：只认自己人")

srv, port = make_server(j({"ok": True, "service": "zhilun-assistant", "ts": 1}))
check("本机 /health 带身份标记 → 是自家", dl.is_our_app(port) is True)
srv.shutdown()

srv, port = make_server(j({"ok": True}))
check("老版本（无 service 字段）→ 仍算自家", dl.is_our_app(port) is True)
srv.shutdown()

srv, port = make_server(j({"ok": True, "service": "someone-else"}))
check("v2.19 修复②：别人的 ok:true 不再被认成自己", dl.is_our_app(port) is False)
srv.shutdown()

srv, port = make_server(j({"ok": False, "service": "zhilun-assistant"}))
check("ok=false → 不是", dl.is_our_app(port) is False)
srv.shutdown()

srv, port = make_server(b"hello, not json")
check("非 JSON 响应 → 不是（旧实现会漏判）", dl.is_our_app(port) is False)
srv.shutdown()

srv, port = make_server(b'"ok": true')
check("裸子串 '\"ok\": true' → 不是（旧实现会误判为是）", dl.is_our_app(port) is False)
srv.shutdown()

srv, port = make_server(j([1, 2, 3]))
check("JSON 但不是对象 → 不是", dl.is_our_app(port) is False)
srv.shutdown()

srv, port = make_server(j({"ok": True}), status=500)
check("5xx → 不是（旧实现不判状态码）", dl.is_our_app(port) is False)
srv.shutdown()

srv, port = make_server(j({"ok": True}), status=404)
check("404 → 不是", dl.is_our_app(port) is False)
srv.shutdown()

check("没人监听的端口 → 不是", dl.is_our_app(9) is False)
srv_slow, port_slow = make_server(j({"ok": True}), sleep=1.5)
check("超时参数生效（慢响应 + 短超时 → 不是）", dl.is_our_app(port_slow, timeout=0.3) is False)
check("超时也不抛异常（不把 urlerror 冒出去）", True)
srv_slow.shutdown()

# ---------------------------------------------------------------------------
# 2. 代理（v2.19 修复①）
# ---------------------------------------------------------------------------
section("2. v2.19 修复①：回环请求强制不走代理")

srv, port = make_server(j({"ok": True, "service": "zhilun-assistant"}))
saved = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")}
try:
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
    os.environ["http_proxy"] = "http://127.0.0.1:9"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    os.environ["https_proxy"] = "http://127.0.0.1:9"
    check("设了不可达代理后，is_our_app 依然为 True", dl.is_our_app(port) is True)
    check("_http_get 仍能取到正文", '"ok"' in dl._http_get(
        f"http://127.0.0.1:{port}/health", 2.0))
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
srv.shutdown()

import urllib.request as _ur

# 只断言"没有任何**生效**的代理"，不冻结具体挂载方式：
# 实测本版本 `build_opener(ProxyHandler({}))` 干脆不挂 ProxyHandler（同样达到目的），
# 而别的版本可能挂一个 proxies={} 的——两种都对，冻死实现会变成假失败。
proxy_handlers = [h for h in dl._NO_PROXY_OPENER.handlers
                  if isinstance(h, _ur.ProxyHandler)]
check("opener 上没有任何生效的代理", all(not getattr(h, "proxies", None)
                                for h in proxy_handlers), str(proxy_handlers))
# 反向护栏：默认 opener **会**读环境代理，所以这个绕过不是多余的。
# 显式设一个假代理再断言，免得"这台机器恰好没配代理"变成假失败。
_saved_proxy = os.environ.get("HTTP_PROXY")
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
try:
    default_opener = _ur.build_opener()
    default_proxied = any(isinstance(h, _ur.ProxyHandler) and getattr(h, "proxies", None)
                          for h in default_opener.handlers)
finally:
    if _saved_proxy is None:
        os.environ.pop("HTTP_PROXY", None)
    else:
        os.environ["HTTP_PROXY"] = _saved_proxy
check("默认 opener 会带环境代理（故本绕过非多余）", default_proxied,
      str(default_opener.handlers))

# ---------------------------------------------------------------------------
# 3. 端口探测
# ---------------------------------------------------------------------------
section("3. 端口探测")

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy = s.getsockname()[1]
    check("被占用的端口 → port_is_free False", dl.port_is_free(busy) is False)
    check("同一个端口不会同时被判成空闲", dl.port_is_free(busy) is False)
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
    s2.bind(("127.0.0.1", 0))
    free_probe = s2.getsockname()[1]
check("刚释放的端口 → port_is_free True", dl.port_is_free(free_probe) is True)

p = dl.find_free_port(30000, tries=50)
check("find_free_port 返回可绑定端口", dl.port_is_free(p) is True, str(p))
check("find_free_port 尊重 start 下界", p >= 30000, str(p))
check("find_free_port 不越过 tries 上界", p < 30000 + 50, str(p))

base = None
for cand in range(31000, 39000):
    if all(dl.port_is_free(cand + i) for i in range(3)):
        base = cand
        break
if base is None:
    check("构造「全忙」区间（用于测 RuntimeError）", False, "找不到连续 3 个空闲端口")
else:
    held = []
    for i in range(3):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", base + i))
        s.listen(1)
        held.append(s)
    try:
        dl.find_free_port(base, tries=3)
        check("区间全忙 → 抛 RuntimeError", False, "没抛")
    except RuntimeError as e:
        check("区间全忙 → 抛 RuntimeError", True)
        check("RuntimeError 带端口信息", str(base) in str(e), str(e))
    finally:
        for s in held:
            s.close()

# ---------------------------------------------------------------------------
# 4. 开浏览器 / 等回车
# ---------------------------------------------------------------------------
section("4. open_browser_delayed 与 _wait_for_enter")

calls: list[tuple] = []


class FakeBrowser:
    opened: list[str] = []

    def open(self, url):  # noqa: A003
        FakeBrowser.opened.append(url)
        return True


old_wb, old_sleep = dl.webbrowser, time.sleep
dl.webbrowser = FakeBrowser()
try:
    dl.time.sleep = lambda s: calls.append(("sleep", s))
    dl.open_browser_delayed("http://127.0.0.1:5000", delay=2.0)
finally:
    dl.time.sleep = old_sleep
    dl.webbrowser = old_wb

check("open_browser_delayed 打开了 url", FakeBrowser.opened == ["http://127.0.0.1:5000"],
      str(FakeBrowser.opened))
check("open_browser_delayed 先等了 2 秒", ("sleep", 2.0) in calls, str(calls))
check("等待发生在打开之前", calls and calls[0][0] == "sleep", str(calls))


def _raise_eof(_prompt=""):
    raise EOFError("no stdin")


old_input = __builtins__.input if hasattr(__builtins__, "input") else input
try:
    __builtins__.input = _raise_eof
    dl._wait_for_enter()
    check("无 stdin（EOFError）不崩", True)
except Exception as exc:  # noqa: BLE001
    check("无 stdin（EOFError）不崩", False, f"{type(exc).__name__}: {exc}")
finally:
    __builtins__.input = old_input

try:
    import builtins

    builtins.input = lambda _p="": "x"
    dl._wait_for_enter()
    check("有 stdin 时正常等待一次", True)
finally:
    builtins.input = old_input

# ---------------------------------------------------------------------------
# 5. main() 的三条路径
# ---------------------------------------------------------------------------
section("5. main() 路径")


class FakeApp:
    def __init__(self):
        self.calls = []

    def run(self, **kw):
        self.calls.append(kw)


class _SyncThread:
    """把"开浏览器"的后台线程变成同步调用，免得断言时它还没跑。"""

    def __init__(self, target=None, args=(), daemon=None):
        self._t, self._a = target, args

    def start(self):
        if self._t:
            self._t(*self._a)


@contextlib.contextmanager
def _empty_stdin():
    """把 stdin 换成空流。

    v2.23：main() 收尾会调 `_wait_for_enter()` → `input()`，而 `input()` 读的是
    **stdin** 不是 stdout（`contextlib` 只有 redirect_stdout/stderr，没有 stdin 的）。
    只重定向 stdout 的话，在 CI / 后台批处理里 stdin 是空管道，`input()` 会
    **永久阻塞** —— 实测把整轮全量回归挂死 12 分钟。
    换成空 StringIO 后 input() 立刻 EOF，顺带也覆盖了 v2.19「无控制台不许炸」的修复。
    """
    old = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        yield
    finally:
        sys.stdin = old


def run_main(*, our_app: bool, port_free, app_mod=None, run_raises=None,
             try_tray: bool = False):
    """在完全隔离的假环境下跑一遍 main()，返回 (FakeApp, FakeBrowser, 输出文本)。

    try_tray=True 时打桩 `_try_tray` 返回 True（模拟托盘可用，接管主线程）。
    """
    FakeBrowser.opened = []
    fake_app = FakeApp()
    if run_raises:
        def _run(**kw):
            fake_app.calls.append(kw)
            raise run_raises
        fake_app.run = _run
    mod = types.ModuleType("app")
    mod.app = fake_app
    if app_mod is not None:
        mod = app_mod
    saved_mods = sys.modules.get("app")
    sys.modules["app"] = mod
    old = {
        "is_our_app": dl.is_our_app,
        "port_is_free": dl.port_is_free,
        "open_browser_delayed": dl.open_browser_delayed,
        "webbrowser": dl.webbrowser,
        "threading": dl.threading,
        "_try_tray": dl._try_tray,
    }
    dl.is_our_app = lambda *a, **k: our_app
    dl.port_is_free = port_free
    dl.open_browser_delayed = lambda url, delay=2.0: FakeBrowser.opened.append(url)
    dl.webbrowser = FakeBrowser()
    dl.threading = types.SimpleNamespace(Thread=_SyncThread)
    # v2.23：必须打桩。`_try_tray` 一旦成功就会用 pystray 的 icon.run() 占住
    # **主线程**（Windows 托盘的要求），测试里真调它会永久阻塞 —— 实测把整轮
    # 全量回归挂死 12 分钟。默认让它接管失败，走控制台分支；
    # 需要测托盘时用 try_tray 参数覆盖。
    dl._try_tray = (lambda *a, **k: True) if try_tray else (lambda *a, **k: False)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), _empty_stdin():
            dl.main()
    finally:
        for k, v in old.items():
            setattr(dl, k, v)
        if saved_mods is None:
            sys.modules.pop("app", None)
        else:
            sys.modules["app"] = saved_mods
    return fake_app, FakeBrowser, buf.getvalue()


app_f, browser_f, out = run_main(our_app=True, port_free=lambda p: True)
check("复用路径：打开了 5000", browser_f.opened == ["http://127.0.0.1:5000"], str(browser_f.opened))
check("复用路径：不再启动 Flask", app_f.calls == [], str(app_f.calls))
check("复用路径：日志说明是复用", "复用" in out, out[:200])

app_f, browser_f, out = run_main(our_app=False, port_free=lambda p: True)
check("新起路径：启动了 Flask", len(app_f.calls) == 1, str(app_f.calls))
check("新起路径：用 5000", app_f.calls and app_f.calls[0]["port"] == 5000, str(app_f.calls))
check("新起路径：host 绑 127.0.0.1", app_f.calls and app_f.calls[0]["host"] == "127.0.0.1")
check("新起路径：debug=False", app_f.calls and app_f.calls[0]["debug"] is False)
check("新起路径：use_reloader=False", app_f.calls and app_f.calls[0]["use_reloader"] is False)
check("新起路径：也会开浏览器", browser_f.opened == ["http://127.0.0.1:5000"], str(browser_f.opened))

app_f, browser_f, out = run_main(our_app=False, port_free=lambda p: p != 5000)
check("5000 被占：自动顺延", app_f.calls and app_f.calls[0]["port"] == 5001, str(app_f.calls))
check("5000 被占：日志告知换端口", "5001" in out, out[:300])
check("5000 被占：浏览器打开的是新端口",
      browser_f.opened == ["http://127.0.0.1:5001"], str(browser_f.opened))

# v2.23：托盘可用时接管主线程，**不再走控制台的 app.run**（否则两套主循环打架）。
# 这条同时是护栏：谁把 `if _try_tray(...): return` 删了，这里立刻红。
app_f, browser_f, out = run_main(our_app=False, port_free=lambda p: True, try_tray=True)
check("托盘接管：不再调 app.run", app_f.calls == [], str(app_f.calls))
check("托盘接管：浏览器照样打开", browser_f.opened == ["http://127.0.0.1:5000"], str(browser_f.opened))

broken = types.ModuleType("app")  # 没有 app 属性 → from app import app 抛 ImportError
app_f, browser_f, out = run_main(our_app=False, port_free=lambda p: True, app_mod=broken)
check("导入失败：打印可读错误", "无法导入 app" in out, out[:300])
check("导入失败：不启动 Flask", app_f.calls == [])

try:
    import builtins

    builtins.input = _raise_eof
    app_f, browser_f, out = run_main(our_app=False, port_free=lambda p: True, app_mod=broken)
    check("v2.19 修复③：无控制台时报错路径不二次崩溃", True)
except Exception as exc:  # noqa: BLE001
    check("v2.19 修复③：无控制台时报错路径不二次崩溃", False, f"{type(exc).__name__}: {exc}")
finally:
    builtins.input = old_input

app_f, browser_f, out = run_main(our_app=False, port_free=lambda p: True,
                                 run_raises=OSError("address already in use"))
check("端口被抢：捕获 OSError 不甩 traceback", True)
check("端口被抢：给出可读提示", "无法绑定" in out, out[-300:])

# ---------------------------------------------------------------------------
out_txt = "\n".join(_LINES)
sys.stdout.buffer.write(out_txt.encode("utf-8"))
sys.stdout.buffer.write(
    f"\n\ndesktop_launcher_test: {PASS} 通过 / {FAIL} 失败\n".encode("utf-8"))
sys.exit(1 if FAIL else 0)
