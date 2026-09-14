"""v2.21 · `build_desktop` 打包清单回归测试。

打包脚本本身不是产品，但**打出来的 exe 是**。2026-09-11 出过真事故：
`desktop.spec` 的 datas 漏了十几个模块，生成出 93.9MB 的 exe **双击即闪退**
（ImportError 发生在 Flask 启动之前，用户只看到窗口一闪）。事后加了 `preflight()`
预检，但**没人跑它** —— 实测 v2.16 引入的 `review_share` 就一直没进清单，
也就是说**当时 `desktop.spec` 打出来的 exe 是坏的**：

```
[预检失败] desktop.spec 的 datas 清单缺少以下本地模块：
    - review_share                (被 app.py 引用)
```

本文件的核心价值一句话：**把"记得回来改 spec"变成"CI 会红"**。
`preflight()` 从入口 `desktop_launcher.py` 出发递归解析 import，与 spec 的 datas
清单求差集 —— 只要断言 `preflight() == 0`，以后新增任何被 app 引用的模块
忘了同步 spec，全量回归就会红。

跑法：python build_desktop_test.py
退出码：0 全通过 / 1 有真失败 / 2 环境未就绪
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import build_desktop as bd
except ImportError:  # 环境未就绪
    print("build_desktop_test: 无法导入 build_desktop，跳过（rc=2）")
    sys.exit(2)

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


def run_preflight() -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = bd.preflight()
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
section("1. 核心护栏：spec 清单必须与代码同步（漏了 → exe 闪退）")

rc, out = run_preflight()
check("preflight 通过（rc=0）", rc == 0, f"rc={rc}\n{out[:400]}")
check("输出里明确说通过", "通过" in out, out[:200])
check("输出里没有「缺少」字样", "缺少" not in out, out[:400])

# ---------------------------------------------------------------------------
section("2. _local_module_names：只认真实存在的模块")

local = bd._local_module_names()
for m in ("app", "review_share", "export_docx", "plugin_worker", "simulate",
          "datacheck", "grimmer", "desktop_launcher"):
    check(f"识别到 {m}", m in local, str(sorted(local))[:300])
check("不含下划线开头的私有脚本", not any(n.startswith("_") for n in local),
      str([n for n in local if n.startswith("_")]))
check("不含 build/dist 产物目录", not ({"build", "dist"} & local), str(sorted(local)))

# ---------------------------------------------------------------------------
section("3. _declared_in_spec：spec 里声明了什么")

declared = bd._declared_in_spec()
for m in ("app", "review_share", "simulate", "plugin_worker", "export_docx",
          "datacheck", "tone_guide", "access_guard"):
    check(f"spec 声明了 {m}", m in declared, str(sorted(declared))[:400])
check("spec 没把 cli 打进桌面端", "cli" not in declared, str(sorted(declared))[:300])

# ---------------------------------------------------------------------------
section("4. _collect_imports：从入口递归找得到被依赖的模块")

mods, origin = bd._collect_imports(bd.ENTRY)
for m in ("app", "review_share", "datacheck", "methods_registry", "export_docx",
          "security_guard", "plugin_registry"):
    check(f"入口可达 {m}", m in mods, str(sorted(mods))[:400])
check("review_share 的来源被记成 app.py", origin.get("review_share") == "app.py",
      str(origin.get("review_share")))

res = bd._collect_imports(ROOT / "真的不存在的入口.py")
check("入口文件不存在时不抛异常", isinstance(res, tuple) and len(res) == 2, str(res)[:100])
# 注意：入口不存在也仍会扫 app.py —— 这是有意为之（desktop_launcher 是在 main()
# 函数体里 `from app import app`，静态扫描不该因为入口异常就整段失效）。
check("入口不存在时仍会兜底扫 app.py", "review_share" in res[0], str(sorted(res[0]))[:200])

# ---------------------------------------------------------------------------
section("5. 反向护栏：真漏了模块时，preflight 必须红")

old_local, old_collect = bd._local_module_names, bd._collect_imports
try:
    bd._local_module_names = lambda: local | {"zl_fake_new_module"}
    bd._collect_imports = lambda entry: (
        {"review_share", "zl_fake_new_module"},
        {"zl_fake_new_module": "app.py"})
    rc, out = run_preflight()
    check("漏模块 → rc=2（不是 0）", rc == 2, f"rc={rc}")
    check("报错点名了漏掉的模块", "zl_fake_new_module" in out, out[:400])
    check("报错说明了后果（ImportError 闪退）", "闪退" in out, out[:400])
    check("报错给出了修法示例", "('zl_fake_new_module.py', '.')" in out, out[:400])
finally:
    bd._local_module_names, bd._collect_imports = old_local, old_collect

rc, out = run_preflight()
check("恢复后仍是 rc=0（没污染模块状态）", rc == 0, f"rc={rc}")

# ---------------------------------------------------------------------------
section("6. 环境异常：缺文件要给退出码 2，不能甩 traceback")

old_spec = bd.SPEC
try:
    bd.SPEC = ROOT / "不存在的.spec"
    rc, out = run_preflight()
    check("spec 不存在 → rc=2", rc == 2, f"rc={rc}")
    check("spec 不存在 → 有可读提示", "找不到" in out, out[:200])
finally:
    bd.SPEC = old_spec

old_entry = bd.ENTRY
try:
    bd.ENTRY = ROOT / "不存在的入口.py"
    rc, out = run_preflight()
    check("入口不存在 → rc=2", rc == 2, f"rc={rc}")
    check("入口不存在 → 有可读提示", "找不到" in out, out[:200])
finally:
    bd.ENTRY = old_entry

rc, out = run_preflight()
check("收尾再跑一次仍是 rc=0", rc == 0, f"rc={rc}")

# ---------------------------------------------------------------------------
txt = "\n".join(_LINES)
sys.stdout.buffer.write(txt.encode("utf-8"))
sys.stdout.buffer.write(
    f"\n\nbuild_desktop_test: {PASS} 通过 / {FAIL} 失败\n".encode("utf-8"))
sys.exit(1 if FAIL else 0)
