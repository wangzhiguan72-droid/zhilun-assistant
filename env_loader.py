"""
极简 .env 加载器（v0.5.3）
==========================
项目本地开发用：把 .env 里的 KEY=VALUE 注入 os.environ。
不引第三方依赖（不装 python-dotenv），20 行搞定。

设计原则：
    - **已存在的环境变量优先**（不覆盖），方便 CI / 生产用真实环境变量
    - 支持 `#` 注释、空行、引号包裹的值
    - 找不到文件就静默跳过（生产环境通常没有 .env）

用法：
    from env_loader import load_dotenv
    load_dotenv()                     # 自动找项目根 .env / .env.local
    load_dotenv("/custom/path/.env")  # 指定文件
"""
from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_FILES = (".env", ".env.local")


def load_dotenv(path: str | os.PathLike | None = None, *, override: bool = False) -> int:
    """加载 .env 到 os.environ，返回成功注入的键数量。

    path=None 时依次尝试项目根的 .env、.env.local（先找到先用）。
    override=True 时覆盖已有环境变量（默认不覆盖）。
    """
    candidates = [Path(path)] if path else [_PROJECT_ROOT / f for f in _DEFAULT_FILES]
    for p in candidates:
        if not p.exists():
            continue
        count = 0
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key:
                    continue
                if override or not os.environ.get(key):
                    os.environ[key] = value
                    count += 1
        except OSError:
            continue
        return count
    return 0
