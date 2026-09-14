"""插件沙箱的**子进程入口**（v2.14 · 可插拔方法市场）

为什么是独立进程，而不是 `multiprocessing`：
    `multiprocessing` 在 Windows 上用 **spawn** 启动子进程，而 spawn 会
    **重新 import 一遍 `__main__`**。本项目所有测试脚本都是「模块级直接跑断言」
    的风格（不套 `if __name__ == "__main__"`），一旦子进程重跑主模块，
    就会在子进程里再跑一遍全部断言 —— 输出污染 + 可能递归派生进程。
    用 `python -m plugin_worker` 起独立解释器就完全不碰 `__main__`，干净得多。

协议（全部走内存管道，**不落盘**）：
    stdin  : pickle(plugin_path, df, kwargs)
    stdout : pickle(("ok", result)) 或 pickle(("err", "ExcType: msg"))

用法：
    python -m plugin_worker        # 供 plugin_registry 调用，不要手工跑
"""
from __future__ import annotations

import importlib.util
import io
import pickle
import sys
from pathlib import Path


def _load(path: str):
    """按文件路径加载插件模块（不要求它在 sys.path 上）。"""
    spec = importlib.util.spec_from_file_location("_zl_plugin", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法从 {path} 加载插件模块")
    # v2.20：允许插件 import 同目录的辅助模块（多文件插件）。
    # **追加**到 sys.path 末尾而不是插到开头，免得插件自带的 numpy.py / json.py
    # 之类把标准库或项目模块顶掉。
    parent = str(Path(path).resolve().parent)
    if parent not in sys.path:
        sys.path.append(parent)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 插件的 import 必须无副作用
    return mod


def main() -> int:
    try:
        path, df, kwargs = pickle.load(sys.stdin.buffer)
    except Exception as e:  # noqa: BLE001 - 入口协议层，任何错误都要回给父进程
        pickle.dump(("err", f"BadRequest: {type(e).__name__}: {e}"),
                    sys.stdout.buffer)
        sys.stdout.buffer.flush()
        return 1

    # v2.20 修复①：协议走 stdout，插件的 print / logging 必须让路。
    # 父进程是 `pickle.loads(proc.stdout)`，插件往 stdout 写的任何东西都会混进
    # pickle 流。不 flush 的小 print 会**侥幸**通过（文本层缓冲到进程退出才
    # flush，落在 pickle 之后，而 `loads` 忽略尾部多余字节），但
    # `print(..., flush=True)` / 输出超过缓冲区 / 开了 `PYTHONUNBUFFERED=1`
    # 就必炸 —— 报的还是"插件沙箱返回了无法解析的结果"，而 stderr 是空的，
    # 用户完全看不出是自己多打了一行调试 print（见 `plugin_worker_test` §6）。
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        try:
            mod = _load(path)
            run = getattr(mod, "run", None)
            if not callable(run):
                raise AttributeError("插件必须暴露可调用的 run(df, **kw)")
            result = run(df, **(kwargs or {}))
            payload = ("ok", result)
        except BaseException as e:  # noqa: BLE001 - 插件是外部代码，崩溃也要变成消息
            payload = ("err", f"{type(e).__name__}: {e}")
    finally:
        sys.stdout = real_stdout

    # v2.20 修复②：先落内存缓冲，成功才整体写出。
    # 旧实现直接往 stdout dump，结果不可 pickle（如含 lambda）时会先写半截
    # 再补一个错误对象 → 父进程读到"半个 pickle"，照样解析不了，
    # 我们精心准备的那句 UnpicklableResult 提示根本传不出去。
    buf = io.BytesIO()
    try:
        pickle.dump(payload, buf)
    except Exception as e:  # noqa: BLE001 - 结果不可 pickle（如含 lambda）
        buf = io.BytesIO()
        pickle.dump(("err", f"UnpicklableResult: {type(e).__name__}: {e}"), buf)
    try:
        sys.stdout.buffer.write(buf.getvalue())
        sys.stdout.buffer.flush()
    except Exception:  # noqa: BLE001 - stdout 断了也要有个退出码
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
