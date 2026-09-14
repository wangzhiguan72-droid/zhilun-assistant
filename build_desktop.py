# -*- coding: utf-8 -*-
"""
智论助手 - 桌面端一键打包脚本（PyInstaller）

用法：
    python build_desktop.py            # 完整构建（图标 + PyInstaller）
    python build_desktop.py --skip-icon  # 跳过图标生成

产物：
    dist/智论助手.exe   —— 单文件、免安装 Python，双击即用

依赖：PyInstaller、Pillow（生成图标用）。建议在项目 .venv 中运行。

退出码：
    0 = 成功
    1 = PyInstaller 失败或产物缺失
    2 = **打包前预检失败**（desktop.spec 资源清单与代码 import 不同步）
"""
import argparse
import ast
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SPEC = ROOT / "desktop.spec"
ICON = ROOT / "assets" / "icon.ico"
DIST = ROOT / "dist"
APP_NAME = "智论助手"

# -----------------------------------------------------------------------------
# 打包前预检：desktop.spec 的 datas 清单是否覆盖了所有「本地模块」
#
# 为什么需要这道检查（真实事故）：
#   2026-09-11 那次打包漏了 datacheck / table_check / grimmer / access_guard /
#   paper_writer / defense_pack / security_guard / cross_platform /
#   methods_registry / tone_guide 等十几个模块，生成出的 93.9MB exe
#   **双击即闪退**（ImportError 发生在 Flask 启动之前，用户只看到窗口一闪）。
#   靠"记得回来改 spec"是不可靠的 —— 人一定会忘。这里用静态分析自动兜住。
#
# 判定方式（故意保守：只在**能确定**是本地模块时才报）：
#   1. 从入口文件（desktop_launcher.py）出发，递归解析全部本地 .py 的 import
#   2. 只保留「在项目根目录下真实存在同名 .py / 同名包目录」的模块名
#   3. 与 desktop.spec 里 datas 已声明的清单求差集
#   4. 差集非空 → 打印清单 + 退出码 2
# -----------------------------------------------------------------------------
ENTRY = ROOT / "desktop_launcher.py"

# 这些即使在项目里同名也不用拷（标准库同名模块、或 PyInstaller 自己认得）
_SKIP_NAMES = {
    "app",  # 入口起手就 import，且已被 datas 显式覆盖；扫到也没关系
    "cli",  # 命令行工具，桌面端不需要
    # v2.21：删掉这里的 "simulate" —— 它早已随包（datas 里明确列了），
    # 留着豁免只会让"哪天它被从清单里删掉"这件事不再报警，正好是这道检查要防的。
}


def _local_module_names() -> set[str]:
    """项目里真实存在的可导入模块名（顶层 .py 与子包）。"""
    names: set[str] = set()
    for p in ROOT.glob("*.py"):
        if not p.name.startswith("_"):
            names.add(p.stem)
    for p in ROOT.iterdir():
        if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith("."):
            if p.name in ("build", "dist", ".venv"):
                continue
            names.add(p.name)
    return names


def _collect_imports(entry: Path) -> tuple[set[str], set[str]]:
    """递归收集 entry 可达的全部模块名。

    返回 (全部模块名, 详细来源 map 用于报错提示)。
    """
    local = _local_module_names()
    seen_files: set[Path] = set()
    all_mods: set[str] = set()
    origin: dict[str, str] = {}

    def scan(path: Path) -> None:
        try:
            rp = path.resolve()
        except OSError:
            return
        if rp in seen_files or not path.exists():
            return
        seen_files.add(rp)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                             filename=str(path))
        except SyntaxError:
            return

        # 当前文件所在包（用于解析 from .x import y 的相对导入）
        pkg_dir = path.parent if path.parent != ROOT else None
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and pkg_dir is not None:
                    base = pkg_dir.name
                    mods = [(base + "." + node.module) if node.module else base]
                elif node.module:
                    mods = [node.module]

            for m in mods:
                top = m.split(".")[0]
                all_mods.add(top)
                origin.setdefault(top, path.name)
                # 若是本地模块 → 继续递归扫它（函数内延迟 import 也能覆盖）
                if top in local:
                    sub = ROOT / (top.replace(".", "/") + ".py")
                    if sub.exists():
                        scan(sub)
                    else:
                        pkg_init = ROOT / top / "__init__.py"
                        if pkg_init.exists():
                            scan(pkg_init)
                            for child in (ROOT / top).glob("*.py"):
                                scan(child)

    scan(entry)
    # 入口自身要跑，必须一起扫
    scan(ROOT / "app.py")
    return all_mods, origin


def _declared_in_spec() -> set[str]:
    """从 desktop.spec 的 datas 清单里解析出已声明的本地模块名。"""
    text = SPEC.read_text(encoding="utf-8", errors="replace")
    declared: set[str] = set()
    # datas 条目形如 ('xxx.py', '.') 或 ('pkg', 'pkg')
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "datas" not in names:
                continue
            for elt in getattr(node.value, "elts", []):
                if not isinstance(elt, ast.Tuple) or not elt.elts:
                    continue
                src = elt.elts[0]
                if isinstance(src, ast.Constant) and isinstance(src.value, str):
                    v = src.value.strip("./")
                    if v.endswith(".py"):
                        v = v[:-3]
                    declared.add(v.split("/")[0])
    return declared


def preflight() -> int:
    """返回 0 表示通过；2 表示清单不同步。"""
    print("[预检] 核对 desktop.spec 资源清单 ...")
    if not SPEC.exists():
        print(f"[失败] 找不到 {SPEC}")
        return 2
    if not ENTRY.exists():
        print(f"[失败] 找不到入口 {ENTRY}")
        return 2

    all_mods, origin = _collect_imports(ENTRY)
    local = _local_module_names()
    declared = _declared_in_spec()

    # 需要打包的本地模块 = import 到 且 项目里真实存在
    needed = {m for m in all_mods if m in local} - _SKIP_NAMES
    missing = sorted(needed - declared)

    # 反向检查：datas 里声明了但项目里已不存在的文件（拼错名字会静默失败）
    stale = sorted(d for d in declared
                   if d not in local and (ROOT / (d + ".py")).exists() is False
                   and not (ROOT / d).exists())

    if missing:
        print()
        print("=" * 60)
        print("  [预检失败] desktop.spec 的 datas 清单缺少以下本地模块：")
        print("=" * 60)
        for m in missing:
            print(f"    - {m:26s}  (被 {origin.get(m, '?')} 引用)")
        print()
        print("  这些模块不会被打进 exe，双击后会在 Flask 启动前 ImportError 闪退。")
        print("  修法：打开 desktop.spec，在 datas 列表里补上对应条目，例如")
        for m in missing[:3]:
            print(f"        ('{m}.py', '.'),")
        print("=" * 60)
        return 2

    if stale:
        print(f"[预检] 提示：datas 里声明了但不存在的条目（不影响构建）：{', '.join(stale)}")

    print(f"[预检] 通过：{len(needed)} 个本地模块均已声明。")
    return 0


def run(cmd: list[str]) -> None:
    print(">>> " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"[失败] 命令退出码 {result.returncode}")
        sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="智论助手桌面端打包")
    parser.add_argument("--skip-icon", action="store_true", help="跳过图标生成")
    parser.add_argument("--clean", action="store_true", default=True, help="清理中间产物")
    parser.add_argument("--no-preflight", action="store_true",
                        help="跳过打包前清单预检（不推荐）")
    args = parser.parse_args()

    print("=" * 52)
    print("智论助手 - 桌面端打包")
    print("=" * 52)

    # 0) 预检：资源清单是否与代码同步（省掉每次 2-3 分钟的无效构建）
    if not args.no_preflight:
        print("\n[0/4] 打包前预检...")
        rc = preflight()
        if rc != 0:
            print("\n[中止] 预检未通过，未开始构建。")
            sys.exit(rc)
    else:
        print("\n[0/4] 跳过预检")

    # 1) 生成图标
    if not args.skip_icon:
        print("\n[1/4] 生成应用图标...")
        run([sys.executable, str(ROOT / "make_icon.py")])
    else:
        print("\n[1/4] 跳过图标生成")

    if not ICON.exists():
        print(f"[警告] 未找到图标 {ICON}，将使用 PyInstaller 默认图标")

    # 2) PyInstaller 构建
    print("\n[2/4] PyInstaller 构建中（首次约 1-3 分钟）...")
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm"]
    if args.clean:
        cmd.append("--clean")
    run(cmd)

    # 3) 校验产物
    print("\n[3/4] 校验产物...")
    exe = DIST / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"[失败] 未生成 {exe}")
        sys.exit(1)

    size_mb = exe.stat().st_size / 1024 / 1024
    print("=" * 52)
    print(f"构建完成！")
    print(f"  产物：{exe}")
    print(f"  体积：{size_mb:.1f} MB")
    print(f"  用法：双击运行，自动打开浏览器（默认 http://127.0.0.1:5000）")
    print()
    print("  建议再跑一次功能验证：")
    print("      .venv/Scripts/python.exe _exe_e2e.py")
    print("=" * 52)


if __name__ == "__main__":
    main()

