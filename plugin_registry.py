"""可插拔方法市场 · 插件层（v2.14）
=============================================
把一个统计方法做成**一个独立的 .py 文件**丢进 `plugins/`，重启即可用 ——
不必改 `app.py`、不必改前端、不必动 `methods_registry.py`。

插件契约（`plugins/run_xxx.py`）：

    SCHEMA = {
        "key": "tost",                    # 方法 key，全局唯一，小写字母数字下划线
        "label": "TOST 等价性检验",
        "needs": ["group_col", "value_col"],   # 必需参数（payload 里的键）
        "params": [{"name": "alpha", "default": 0.05}],  # 可选参数（带默认值）
        "ui": "group_value",              # 前端字段形状，见 _UI_SHAPES
        "picker_label": "...",            # 可选：下拉里显示的名字
        "error_hint": "...",              # 可选：缺参数时的中文提示
    }

    def run(df, group_col, value_col, alpha=0.05, **kw) -> dict:
        ...
        return {"p": 0.03, "n": 60, ...}

三条护栏（缺一不可，按重要性排序）：

1. **沙箱执行**：插件跑在**独立子进程**（`python -m plugin_worker`），
   超时即杀（默认 10s）。插件写死循环只会让这一次调用失败，
   不会冻住整个界面。数据经 stdin/stdout 的 pickle 管道传递，**不落盘**。
2. **结果合理性校验** `validate_result()`：p 必须在 [0,1]、自由度必须 > 0、
   样本量必须 ≥ 2。校验不通过**绝不进报告**——
   宁可报错，也不能把 `p=2` 这种鬼东西显示给用户。
3. **契约校验**：SCHEMA 缺字段 / key 不合法 / 没有可调用的 `run` → 该插件
   加载失败并**记录原因**，其余插件照常可用（单个坏插件不拖垮市场）。

关于**冻结环境**（PyInstaller 打包的 exe）：此时 `sys.executable` 是程序本体
而不是 Python 解释器，起不了子进程 → 自动降级为**进程内执行**并通过
`executor()` 对外暴露，绝不静默——`/api/plugins` 会明确显示 `sandbox: inprocess`。

用法：

    from plugin_registry import scan_plugins, run_plugin
    plugins, errors = scan_plugins()
"""
from __future__ import annotations

import math
import os
import pickle
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 插件目录（项目根下的 plugins/）
PLUGIN_DIR: Path = Path(__file__).resolve().parent / "plugins"

#: 单次插件执行的超时秒数（超时即杀，防死循环冻住界面）
DEFAULT_TIMEOUT_SEC = 10.0

#: 最多加载多少个插件（防目录被塞爆拖慢启动）
MAX_PLUGINS = 30

#: 合法的方法 key
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: 前端字段形状（与 methods_registry 的 alias 对齐）
_UI_SHAPES = ("group_value", "two_cols", "x_cols", "item_cols")

#: 结果里代表 p 值的键
_P_KEYS = ("p", "p_value", "pvalue", "p_val", "sig")
#: 结果里代表自由度的键
_DF_KEYS = ("df", "df1", "df2", "df_between", "df_within",
            "df_error", "df_residual", "df_total", "dof")
#: 结果里代表样本量的键
_N_KEYS = ("n", "n_total", "n1", "n2", "sample_size", "n_valid")


class PluginError(ValueError):
    """插件层的一切错误。

    刻意继承 ValueError —— `app._dispatch_analysis` 已捕获 ValueError
    并把它变成给用户看的中文提示，插件错误因此无需改动分发层。
    """


@dataclass(frozen=True)
class PluginInfo:
    """一个通过校验的插件。"""

    key: str
    label: str
    path: str                                   # 绝对文件路径
    module: str                                 # 文件名（报错时给人看）
    needs: tuple[str, ...] = ()
    params: tuple[tuple[str, Any], ...] = ()    # (name, default)
    ui: str = "group_value"
    picker_label: str = ""
    error_hint: str = ""
    description: str = ""


# ---------------------------------------------------------------------------
# 契约校验
# ---------------------------------------------------------------------------
def _bad_schema(module: str, why: str) -> dict[str, str]:
    return {"module": module, "error": why}


def _validate_schema(mod, path: Path) -> tuple[PluginInfo | None, str]:
    """校验一个插件模块的 SCHEMA + run，返回 (PluginInfo | None, 错误原因)。"""
    name = path.name

    run = getattr(mod, "run", None)
    if not callable(run):
        return None, f"{name}：缺少可调用的 run(df, **kw)"

    schema = getattr(mod, "SCHEMA", None)
    if not isinstance(schema, dict):
        return None, f"{name}：缺少 SCHEMA 字典"

    key = schema.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        return None, (f"{name}：SCHEMA.key 必须是小写字母开头、"
                      f"仅含小写字母/数字/下划线（最多 32 字符），当前为 {key!r}")

    label = schema.get("label")
    if not isinstance(label, str) or not label.strip():
        return None, f"{name}：SCHEMA.label 必须是非空字符串"

    needs = schema.get("needs") or []
    if not isinstance(needs, (list, tuple)) or \
            not all(isinstance(x, str) and x for x in needs):
        return None, f"{name}：SCHEMA.needs 必须是字符串列表"

    params_raw = schema.get("params") or []
    params: list[tuple[str, Any]] = []
    if not isinstance(params_raw, (list, tuple)):
        return None, f"{name}：SCHEMA.params 必须是列表"
    for p in params_raw:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            return None, f"{name}：SCHEMA.params 每项需含字符串 name"
        params.append((p["name"], p.get("default")))

    ui = schema.get("ui") or "group_value"
    if ui not in _UI_SHAPES:
        return None, f"{name}：SCHEMA.ui 必须是 {' / '.join(_UI_SHAPES)} 之一"

    return PluginInfo(
        key=key,
        label=label.strip(),
        path=str(path.resolve()),
        module=name,
        needs=tuple(needs),
        params=tuple(params),
        ui=ui,
        picker_label=str(schema.get("picker_label") or ""),
        error_hint=str(schema.get("error_hint") or ""),
        description=str(schema.get("description") or ""),
    ), ""


def scan_plugins(plugin_dir: str | Path | None = None
                 ) -> tuple[list[PluginInfo], list[dict[str, str]]]:
    """扫描插件目录。

    返回 `(可用插件, 加载失败清单[{"module","error"}])`。
    **单个插件坏掉不影响其它插件** —— 这是市场模型的基本容错。
    """
    d = Path(plugin_dir) if plugin_dir else PLUGIN_DIR
    if not d.is_dir():
        return [], []

    import importlib.util

    found: list[PluginInfo] = []
    errors: list[dict[str, str]] = []

    for path in sorted(d.glob("*.py")):
        if path.name.startswith("_"):
            continue
        if len(found) >= MAX_PLUGINS:
            errors.append(_bad_schema(
                path.name,
                f"插件数量超过上限 {MAX_PLUGINS}，其余未加载"))
            break
        try:
            spec = importlib.util.spec_from_file_location(
                f"_zl_plugin_{path.stem}", path)
            if spec is None or spec.loader is None:
                errors.append(_bad_schema(path.name, "无法构造 module spec"))
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)   # 插件的 import 必须无副作用
        except Exception as e:  # noqa: BLE001 - 坏插件只记不抛
            errors.append(_bad_schema(
                path.name, f"导入失败：{type(e).__name__}: {e}"))
            continue

        info, why = _validate_schema(mod, path)
        if info is None:
            errors.append(_bad_schema(path.name, why))
        else:
            found.append(info)

    return found, errors


# ---------------------------------------------------------------------------
# 结果校验（防"插件算错了还理直气壮地显示出来"）
# ---------------------------------------------------------------------------
def _num(v: Any) -> float | None:
    """把值转成 float（非数字/NaN/inf 一律 None）。"""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(f) else f


def validate_result(result: Any, _depth: int = 0) -> tuple[bool, str]:
    """插件结果是否"像一份统计结果"。

    只查**硬矛盾**（p 越界、自由度为负、样本量 < 2），不查"对不对"——
    算得对不对是方法本身的事，校验管不了，也不该管。

    **会递归进嵌套 dict**：本项目的结果约定是 `{"summary": {"p": ..., "df": ...}}`，
    只查顶层等于什么都没查。递归深度限 3 层，防止插件塞个巨大结构把校验拖死。

    返回 (是否通过, 不通过的原因)。
    """
    if not isinstance(result, dict):
        return False, f"返回值必须是 dict，实际是 {type(result).__name__}"
    if not result:
        return False, "返回了空 dict（等于什么都没算）"

    for k in _P_KEYS:
        if k in result:
            p = _num(result[k])
            if p is None:
                return False, f"{k} 不是有效数字（{result[k]!r}）"
            if not (0.0 <= p <= 1.0):
                return False, f"{k}={p:g} 越界（p 值必须在 0–1 之间）"

    for k in _DF_KEYS:
        if k in result:
            df = _num(result[k])
            if df is None:
                return False, f"{k} 不是有效数字（{result[k]!r}）"
            if df <= 0:
                return False, f"{k}={df:g} 不合理（自由度必须 > 0）"

    for k in _N_KEYS:
        if k in result:
            n = _num(result[k])
            if n is None:
                return False, f"{k} 不是有效数字（{result[k]!r}）"
            if n < 2:
                return False, f"{k}={n:g} 不合理（样本量必须 ≥ 2）"

    if _depth < 3:
        for v in result.values():
            if isinstance(v, dict) and v:
                ok, why = validate_result(v, _depth + 1)
                if not ok:
                    return False, why

    return True, ""


def _sanitize(v: Any, _depth: int = 0) -> Any:
    """把 numpy 标量 / NaN / inf 转成 JSON 安全的值（插件结果要过 jsonify）。"""
    if _depth > 6:
        return str(v)
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return None if not math.isfinite(v) else v
    if isinstance(v, dict):
        return {str(k): _sanitize(x, _depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_sanitize(x, _depth + 1) for x in v]
    # numpy 标量等
    item = getattr(v, "item", None)
    if callable(item):
        try:
            return _sanitize(item(), _depth + 1)
        except Exception:  # noqa: BLE001
            return str(v)
    return str(v)


# ---------------------------------------------------------------------------
# 执行：子进程沙箱（超时可杀）
# ---------------------------------------------------------------------------
def _frozen() -> bool:
    """是否运行在 PyInstaller 等打包环境（此时 sys.executable 不是解释器）。"""
    return bool(getattr(sys, "frozen", False))


def executor() -> str:
    """当前插件执行方式：`"subprocess"`（有超时保护）或 `"inprocess"`（无）。"""
    return "inprocess" if _frozen() else "subprocess"


def _run_subprocess(path: str, df: Any, kwargs: dict,
                    timeout: float) -> Any:
    """在独立子进程里跑插件，超时即杀。"""
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent)
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{old}" if old else root

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "plugin_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
    except Exception as e:  # noqa: BLE001
        raise PluginError(f"无法启动插件沙箱：{type(e).__name__}: {e}") from e

    try:
        out, err = proc.communicate(pickle.dumps((path, df, kwargs or {})),
                                    timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        raise PluginError(
            f"插件执行超时（>{timeout:g}s，已强制终止）。"
            f"请检查插件是否陷入死循环或处理的数据量过大。") from None

    try:
        tag, payload = pickle.loads(out)
    except Exception as e:  # noqa: BLE001
        detail = (err or b"").decode("utf-8", "replace")[:300]
        raise PluginError(
            f"插件沙箱返回了无法解析的结果（{type(e).__name__}）"
            + (f"；stderr：{detail}" if detail else "")) from e
    if tag == "err":
        raise PluginError(f"插件执行出错：{payload}")
    return payload


def _run_inprocess(path: str, df: Any, kwargs: dict) -> Any:
    """进程内执行（冻结环境兜底，无超时保护）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_zl_plugin_inline", path)
    if spec is None or spec.loader is None:
        raise PluginError(f"无法加载插件：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    run = getattr(mod, "run", None)
    if not callable(run):
        raise PluginError("插件必须暴露可调用的 run(df, **kw)")
    return run(df, **(kwargs or {}))


def timeout_sec() -> float:
    """当前生效的超时秒数（可用环境变量 PLUGIN_TIMEOUT_SEC 覆盖）。"""
    raw = os.getenv("PLUGIN_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_SEC


def run_plugin(info: PluginInfo, df: Any, kwargs: dict | None = None,
               *, timeout: float | None = None) -> dict:
    """执行插件 → 校验结果 → 返回可 JSON 序列化的 dict。

    任一环节出错都抛 `PluginError`（= ValueError），由分发层转成中文提示。
    """
    t = timeout_sec() if timeout is None else timeout
    kw = dict(kwargs or {})

    if executor() == "subprocess":
        raw = _run_subprocess(info.path, df, kw, t)
    else:
        raw = _run_inprocess(info.path, df, kw)

    ok, why = validate_result(raw)
    if not ok:
        raise PluginError(f"插件「{info.label}」的结果未通过合理性校验：{why}")

    out = _sanitize(raw)
    if not isinstance(out, dict):
        raise PluginError("插件结果清洗后不再是 dict（插件不应返回此类结构）")
    return out


def build_kwargs(info: PluginInfo, payload: dict) -> dict:
    """按插件声明从 payload 组出 run() 的关键字参数。

    `needs` 缺一个即抛 PluginError（= ValueError，分发层会给中文提示）；
    `params` 取不到就用声明的默认值。
    """
    kw: dict[str, Any] = {}
    for name in info.needs:
        v = payload.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise PluginError(
                info.error_hint
                or f"「{info.label}」缺少必需参数：{name}")
        kw[name] = v
    for name, default in info.params:
        v = payload.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            kw[name] = default
            continue
        kw[name] = v
    return kw
