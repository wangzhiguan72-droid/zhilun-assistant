# -*- coding: utf-8 -*-
"""
智论助手 - 桌面端一键打包脚本（PyInstaller）

用法：
    python build_desktop.py            # 完整构建（图标 + PyInstaller）
    python build_desktop.py --skip-icon  # 跳过图标生成

产物：
    dist/智论助手.exe   —— 单文件、免安装 Python，双击即用

依赖：PyInstaller、Pillow（生成图标用）。建议在项目 .venv 中运行。
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SPEC = ROOT / "desktop.spec"
ICON = ROOT / "assets" / "icon.ico"
DIST = ROOT / "dist"
APP_NAME = "智论助手"


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
    args = parser.parse_args()

    print("=" * 52)
    print("智论助手 - 桌面端打包")
    print("=" * 52)

    # 1) 生成图标
    if not args.skip_icon:
        print("\n[1/3] 生成应用图标...")
        run([sys.executable, str(ROOT / "make_icon.py")])
    else:
        print("\n[1/3] 跳过图标生成")

    if not ICON.exists():
        print(f"[警告] 未找到图标 {ICON}，将使用 PyInstaller 默认图标")

    # 2) PyInstaller 构建
    print("\n[2/3] PyInstaller 构建中（首次约 1-3 分钟）...")
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm"]
    if args.clean:
        cmd.append("--clean")
    run(cmd)

    # 3) 校验产物
    print("\n[3/3] 校验产物...")
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
    print("=" * 52)


if __name__ == "__main__":
    main()
