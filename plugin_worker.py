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
import pickle
import sys


def _load(path: str):
    """按文件路径加载插件模块（不要求它在 sys.path 上）。"""
    spec = importlib.util.spec_from_file_location("_zl_plugin", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法从 {path} 加载插件模块")
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

    try:
        mod = _load(path)
        run = getattr(mod, "run", None)
        if not callable(run):
            raise AttributeError("插件必须暴露可调用的 run(df, **kw)")
        result = run(df, **(kwargs or {}))
        payload = ("ok", result)
    except BaseException as e:  # noqa: BLE001 - 插件是外部代码，崩溃也要变成消息
        payload = ("err", f"{type(e).__name__}: {e}")

    try:
        pickle.dump(payload, sys.stdout.buffer)
        sys.stdout.buffer.flush()
    except Exception as e:  # noqa: BLE001 - 结果不可 pickle（如含 lambda）
        pickle.dump(("err", f"UnpicklableResult: {type(e).__name__}: {e}"),
                    sys.stdout.buffer)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
