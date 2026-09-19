"""版本号单一真源（v2.22 起统一管理）
====================================
以前版本号散在三处（页面角标 v1.1 / 启动器横幅 v0.9.0 / CHANGELOG v2.21），
改一处漏两处，用户看到三个"当前版本"。现在统一从这里读：

    - 页面右上角角标（app.py 首页路由注入 templates/index.html）
    - 桌面启动器控制台横幅（desktop_launcher.py）
    - build_desktop.py 的构建产物报告

改版本号 = 只改下面这一行，再在 CHANGELOG.md 加一段。
"""
from __future__ import annotations

APP_NAME = "智论助手"
APP_VERSION = "v2.27"          # 与 CHANGELOG.md 最新条目保持一致
APP_TITLE = f"{APP_NAME} {APP_VERSION}"   # 桌面启动器横幅用


def badge_text() -> str:
    """页面角标文案（内测版 vX.Y.Z）。"""
    return f"内测版 {APP_VERSION}"
