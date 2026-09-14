"""v2.20 · `plugin_worker` 插件沙箱回归测试。

为什么补它：覆盖扫描显示 `plugin_worker.py` 是**唯一零测试的产品模块**
（65 行，插件沙箱的子进程入口）。它是"外部代码"与"主程序"之间唯一的墙。

读 `plugin_registry._run_subprocess` 时发现的真缺陷：
父进程拿的是 `pickle.loads(proc.stdout)`，插件往 stdout 写的任何东西都会混进
pickle 流。**不 flush 的小 print 会侥幸通过**（文本层缓冲到进程退出才 flush，
落在 pickle 之后，而 `loads` 忽略尾部多余字节），但 `print(..., flush=True)` /
输出超过 8KB 缓冲区 / 容器里常见的 `PYTHONUNBUFFERED=1`，就一定会炸 ——
报的还是"插件沙箱返回了无法解析的结果（UnpicklingError）"，而 stderr 是空的，
用户完全看不出是自己多打了一行调试 print。§6 用一段"旧式实现"把两种情形
都实测了一遍：**别把"侥幸"当成"没问题"**。

修掉三个：

  1. **stdout 保留给协议**：插件执行期间 `sys.stdout` 指向 `sys.stderr`，
     print / logging / `sys.stdout.write` 全部改道。
  2. **结果先落内存缓冲**：不可 pickle 的结果（如含 lambda）不会把
     半截 pickle 写进 stdout —— 否则父进程读到的还是"半个对象"，
     我们准备的那句 `UnpicklableResult` 提示根本传不出去。
  3. **插件同目录可 import**（`sys.path.append`，不是 insert，
     免得插件自带的 numpy.py 把标准库顶掉）。

跑法：python plugin_worker_test.py
退出码：0 全通过 / 1 有真失败 / 2 环境未就绪
"""
from __future__ import annotations

import os
import pickle
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import pandas as pd
except ImportError:  # 环境未就绪
    print("plugin_worker_test: 未安装 pandas，跳过（rc=2）")
    sys.exit(2)

import plugin_worker as pw  # noqa: E402

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
# 调真实子进程（不 mock —— 协议的正确性就在管道上）
# ---------------------------------------------------------------------------
def call_worker(request: bytes, timeout: float = 60.0):
    env = dict(os.environ)
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{ROOT}{os.pathsep}{old}" if old else str(ROOT)
    return subprocess.run([sys.executable, "-m", "plugin_worker"],
                          input=request, capture_output=True,
                          env=env, cwd=str(ROOT), timeout=timeout)


def write_plugin(d: str, src: str, name: str = "plug.py",
                 siblings: dict[str, str] | None = None) -> str:
    for fn, body in (siblings or {}).items():
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            f.write(body)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(src)
    return p


def req(path, df=None, kwargs=None) -> bytes:
    return pickle.dumps((path, df if df is not None else pd.DataFrame({"a": [1, 2, 3]}),
                         kwargs or {}))


def unpack(proc):
    """把子进程 stdout 还原成 (tag, payload)；无法解析时返回 ("<garbage>", raw)。"""
    try:
        return pickle.loads(proc.stdout)
    except Exception as e:  # noqa: BLE001
        return ("<garbage>", f"{type(e).__name__}: {e}")


TMP = tempfile.mkdtemp(prefix="zl_plugin_")


def section1() -> None:
    section("1. 正常路径：结果原样穿过管道")
    df = pd.DataFrame({"x": [1, 2, 3, 4], "g": list("aabb")})
    p = write_plugin(TMP, "def run(df, **kw):\n"
                          "    return {'ok': True, 'n': int(len(df)), "
                          "'cols': list(df.columns), 'kw': kw}\n")
    proc = call_worker(req(p, df, {"alpha": 0.05}))
    tag, payload = unpack(proc)
    check("返回 ok", tag == "ok", f"{tag} / {payload!r}")
    check("子进程退出码 0", proc.returncode == 0, str(proc.returncode))
    check("结果内容正确", payload == {"ok": True, "n": 4, "cols": ["x", "g"],
                                  "kw": {"alpha": 0.05}}, str(payload))


def section2() -> None:
    section("2. v2.20 修复①：插件 print 不再污染协议")
    p = write_plugin(TMP, "def run(df, **kw):\n"
                          "    print('调试：进入插件')\n"
                          "    return {'ok': True}\n", name="p_print.py")
    proc = call_worker(req(p))
    tag, payload = unpack(proc)
    check("带 print 的插件仍返回 ok（旧实现会炸）", tag == "ok",
          f"{tag} / {payload!r} / stderr={proc.stderr[:200]!r}")
    check("print 的内容去了 stderr", "调试：进入插件" in proc.stderr.decode("utf-8", "replace"),
          proc.stderr[:200].decode("utf-8", "replace"))
    check("stdout 没有多余文本（第一个字节就是 pickle）",
          proc.stdout[:1] == b"\x80", repr(proc.stdout[:20]))

    p = write_plugin(TMP, "import sys\n"
                          "def run(df, **kw):\n"
                          "    sys.stdout.write('raw write\\n')\n"
                          "    return {'ok': True}\n", name="p_write.py")
    tag, payload = unpack(call_worker(req(p)))
    check("直接 sys.stdout.write 也不污染", tag == "ok", f"{tag} / {payload!r}")

    p = write_plugin(TMP, "def run(df, **kw):\n"
                          "    for i in range(5000):\n"
                          "        print('noise %d' % i)\n"
                          "    return {'ok': True}\n", name="p_flood.py")
    proc = call_worker(req(p), timeout=90)
    tag, payload = unpack(proc)
    check("大量输出（5000 行）不死锁也不污染", tag == "ok", f"{tag} / {payload!r}")


def section3() -> None:
    section("3. 错误路径：都要变成消息，不能崩")

    p = write_plugin(TMP, "def run(df, **kw):\n    raise ValueError('boom')\n",
                     name="p_raise.py")
    tag, payload = unpack(call_worker(req(p)))
    check("插件抛异常 → err 消息", tag == "err" and "ValueError: boom" in payload,
          f"{tag} / {payload!r}")

    p = write_plugin(TMP, "run = 42\n", name="p_norun.py")
    tag, payload = unpack(call_worker(req(p)))
    check("没有可调用的 run → err 消息", tag == "err" and "AttributeError" in payload,
          f"{tag} / {payload!r}")

    p = write_plugin(TMP, "import sys\n"
                          "def run(df, **kw):\n    sys.exit(3)\n", name="p_exit.py")
    tag, payload = unpack(call_worker(req(p)))
    check("插件 sys.exit → 变成 err 而不是子进程崩掉",
          tag == "err" and "SystemExit" in payload, f"{tag} / {payload!r}")

    p = os.path.join(TMP, "不存在的插件.py")
    tag, payload = unpack(call_worker(req(p)))
    check("插件文件不存在 → err 消息", tag == "err", f"{tag} / {payload!r}")

    proc = call_worker("这不是一个 pickle".encode("utf-8"))
    tag, payload = unpack(proc)
    check("非法请求体 → BadRequest", tag == "err" and "BadRequest" in str(payload),
          f"{tag} / {payload!r}")
    check("非法请求体 → 退出码 1", proc.returncode == 1, str(proc.returncode))

    proc = call_worker(b"")
    tag, payload = unpack(proc)
    check("空 stdin → BadRequest 而不是挂死", tag == "err" and "BadRequest" in str(payload),
          f"{tag} / {payload!r}")


def section4() -> None:
    section("4. v2.20 修复②：不可 pickle 的结果")
    p = write_plugin(TMP, "def run(df, **kw):\n    return {'f': lambda x: x}\n",
                     name="p_lambda.py")
    proc = call_worker(req(p))
    tag, payload = unpack(proc)
    check("含 lambda 的结果 → 可解析的 err（旧实现是半个 pickle）", tag == "err",
          f"{tag} / {payload!r}")
    check("错误信息点明 UnpicklableResult", "UnpicklableResult" in str(payload),
          str(payload))


def section5() -> None:
    section("5. v2.20 修复③ + `_load` 单元行为")
    p = write_plugin(TMP, "import helper\n"
                          "def run(df, **kw):\n    return {'v': helper.VAL}\n",
                     name="p_multi.py", siblings={"helper.py": "VAL = 7\n"})
    tag, payload = unpack(call_worker(req(p)))
    check("多文件插件能 import 同目录模块", tag == "ok" and payload == {"v": 7},
          f"{tag} / {payload!r}")

    mod = pw._load(p)
    check("_load 能加载插件模块", callable(getattr(mod, "run", None)))
    check("_load 把插件目录加进 sys.path（追加而非插入）",
          str(Path(p).resolve().parent) in sys.path and sys.path[0] != str(Path(p).resolve().parent),
          str(sys.path[:2]))
    try:
        pw._load(os.path.join(TMP, "真的不存在.py"))
        check("_load 对不存在的插件抛错", False, "没抛")
    except Exception as e:  # noqa: BLE001
        check("_load 对不存在的插件抛错", True, f"{type(e).__name__}")


OLDISH_SRC = '''
import importlib.util, pickle, sys
path, df, kw = pickle.load(sys.stdin.buffer)
spec = importlib.util.spec_from_file_location("_zl_plugin", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
pickle.dump(("ok", mod.run(df, **kw)), sys.stdout.buffer)
sys.stdout.buffer.flush()
'''


def call_oldish(plugin_path: str):
    """用"旧式实现"（不重定向 stdout）跑一次，返回子进程结果。"""
    runner = os.path.join(TMP, "_oldish_worker.py")
    with open(runner, "w", encoding="utf-8") as f:
        f.write(OLDISH_SRC)
    return subprocess.run([sys.executable, runner], input=req(plugin_path),
                          capture_output=True, timeout=90)


def section6() -> None:
    section("6. 反向护栏：旧式实现在什么条件下会炸（证明修复非多余）")

    p_flush = write_plugin(TMP, "def run(df, **kw):\n"
                                "    print('调试', flush=True)\n"
                                "    return {'ok': True}\n", name="p_flush.py")
    proc = call_oldish(p_flush)
    tag, payload = unpack(proc)
    check("旧式实现 + print(flush=True) → 父进程解析失败", tag == "<garbage>",
          f"tag={tag} payload={payload!r}")
    check("污染确实落在 pickle 前面",
          proc.stdout.startswith("调试".encode("utf-8")), repr(proc.stdout[:30]))

    p_soft = write_plugin(TMP, "def run(df, **kw):\n"
                               "    print('调试')\n"
                               "    return {'ok': True}\n", name="p_soft.py")
    proc = call_oldish(p_soft)
    tag_soft, payload_soft = unpack(proc)
    # 实测：不 flush 的小 print 会**侥幸**通过 —— 文本层缓冲到进程退出才 flush，
    # 于是它落在 pickle 之后，而 pickle.loads 忽略尾部多余字节。
    # 所以这是个定时炸弹（换 flush / 输出超 8KB / PYTHONUNBUFFERED=1 就炸），
    # 不是必炸 —— 把话说准，别为了显得修复很重要而夸大。
    check("小 print（不 flush）侥幸通过 → 说明这是定时炸弹而非必炸",
          tag_soft == "ok", f"tag={tag_soft} payload={payload_soft!r}")
    check("但污染字节确实进了 stdout（只是落在尾部）",
          proc.stdout.endswith(b"\n"), repr(proc.stdout[-20:]))

    p_big = write_plugin(TMP, "def run(df, **kw):\n"
                               "    for i in range(3000):\n"
                               "        print('noise %d' % i)\n"
                               "    return {'ok': True}\n", name="p_big.py")
    tag_big, _ = unpack(call_oldish(p_big))
    check("输出超过缓冲区（3000 行）→ 旧式实现必炸", tag_big == "<garbage>",
          f"tag={tag_big}")

    check("修复后：flush=True 也安全", unpack(call_worker(req(p_flush)))[0] == "ok")
    check("修复后：大量输出也安全", unpack(call_worker(req(p_big)))[0] == "ok")


try:
    section1()
    section2()
    section3()
    section4()
    section5()
    section6()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

out = "\n".join(_LINES)
sys.stdout.buffer.write(out.encode("utf-8"))
sys.stdout.buffer.write(
    f"\n\nplugin_worker_test: {PASS} 通过 / {FAIL} 失败\n".encode("utf-8"))
sys.exit(1 if FAIL else 0)
