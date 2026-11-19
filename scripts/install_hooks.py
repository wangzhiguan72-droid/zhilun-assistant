# -*- coding: utf-8 -*-
"""安装 pre-commit 隐私拦截钩子。

为什么需要它：本轮清理历史时，我两次把正在删除的隐私片段又写回文档，人工复查
两次都没拦住。结论是「靠人看一眼」不可靠，必须在 commit 这一步机器拦。

    .venv/Scripts/python.exe scripts/install_hooks.py          # 安装/更新
    .venv/Scripts/python.exe scripts/install_hooks.py --check   # 只看状态
    .venv/Scripts/python.exe scripts/install_hooks.py --uninstall

设计取舍：
  - **失败即拦**：找不到 Python、脚本缺失、扫描自身报错，一律阻止提交。
    隐私门静默放行比没有门更危险——使用者会以为已经查过了。
  - **不覆盖别人的钩子**：已存在且不是本工具写的，先备份再写，备份路径打印出来。
  - 钩子只扫暂存内容（git show :<path>），不扫工作区未暂存的改动，
    也不扫 diff 的 `-` 行——删除隐私串的那次提交不该被自己的 diff 拦住。
"""
from __future__ import annotations

import argparse
import stat
import subprocess
import sys
import time
from pathlib import Path

MARKER = '# zhilun-privacy-gate v1'

HOOK = f'''#!/bin/sh
{MARKER}
# 由 scripts/install_hooks.py 生成。改动请改生成器，不要直接改本文件。
# 不用 set -e：扫描失败要先打印原因再退出，set -e 会把提示吞掉。
ROOT=$(git rev-parse --show-toplevel)
if [ -z "$ROOT" ]; then
    echo "[隐私门] 取不到仓库根目录，拒绝提交（失败即拦）" >&2
    exit 1
fi
GATE="$ROOT/scripts/privacy_gate.py"

if [ ! -f "$GATE" ]; then
    echo "[隐私门] 找不到 scripts/privacy_gate.py，拒绝提交（失败即拦）" >&2
    exit 1
fi

PY=""
for cand in "$ROOT/.venv/Scripts/python.exe" "$ROOT/.venv/bin/python" python python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    # Windows 的 python3 可能是应用商店占位符：command -v 有、真跑就报错。
    # 必须实跑一次确认，否则钩子会挑到一个跑不起来的解释器然后拦住所有提交。
    if "$cand" -c "import sys" >/dev/null 2>&1; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[隐私门] 找不到 Python 解释器，无法扫描，拒绝提交（失败即拦）" >&2
    exit 1
fi

PYTHONIOENCODING=utf-8 PYTHONUTF8=1 PYTHONUNBUFFERED=1 "$PY" -u "$GATE"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "" >&2
    echo "[隐私门] 已阻止本次提交（退出码 $rc）。请按上面的文件:行号清理后重新 git add。" >&2
    exit "$rc"
fi
exit 0
'''


def repo_root() -> Path:
    out = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                         capture_output=True, text=True, encoding='utf-8')
    if out.returncode != 0:
        raise SystemExit('[FAIL] 不在 git 仓库内')
    return Path(out.stdout.strip())


def hooks_dir(root: Path) -> Path:
    """尊重 core.hooksPath；未设置时用 .git/hooks。"""
    out = subprocess.run(['git', '-C', str(root), 'config', '--get', 'core.hooksPath'],
                         capture_output=True, text=True, encoding='utf-8')
    if out.returncode == 0 and out.stdout.strip():
        p = Path(out.stdout.strip())
        return p if p.is_absolute() else root / p
    return root / '.git' / 'hooks'


def install(root: Path) -> int:
    d = hooks_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    target = d / 'pre-commit'
    if target.exists():
        old = target.read_text(encoding='utf-8', errors='replace')
        if MARKER not in old:
            bak = target.with_name(f'pre-commit.bak-{time.strftime("%Y%m%d-%H%M%S")}')
            bak.write_bytes(target.read_bytes())
            print(f'[备份] 已有他人的 pre-commit，原样备份到 {bak}')
    # 钩子必须是 LF：CRLF 会让 sh 把 "#!/bin/sh\r" 当成解释器路径。
    target.write_bytes(HOOK.replace('\r\n', '\n').encode('utf-8'))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f'[OK] 已安装 {target}')
    print('     每次 git commit 前自动扫描暂存内容；命中隐私串或长 API Key 即阻止提交。')
    return 0


def uninstall(root: Path) -> int:
    target = hooks_dir(root) / 'pre-commit'
    if not target.exists():
        print('[跳过] 没有 pre-commit 钩子')
        return 0
    if MARKER not in target.read_text(encoding='utf-8', errors='replace'):
        print(f'[跳过] {target} 不是本工具写的，不动它')
        return 0
    target.unlink()
    print(f'[OK] 已移除 {target}')
    return 0


def check(root: Path) -> int:
    target = hooks_dir(root) / 'pre-commit'
    gate = root / 'scripts' / 'privacy_gate.py'
    ok = True
    if not gate.exists():
        print(f'[FAIL] 缺 {gate}')
        ok = False
    if not target.exists():
        print(f'[FAIL] 未安装钩子：{target}')
        return 1
    if MARKER not in target.read_text(encoding='utf-8', errors='replace'):
        print(f'[FAIL] {target} 存在但不是本工具的隐私门')
        return 1
    if b'\r\n' in target.read_bytes():
        print(f'[FAIL] {target} 含 CRLF，sh 会执行失败')
        ok = False
    print(f'[{"OK" if ok else "FAIL"}] 隐私门已就位：{target}')
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--uninstall', action='store_true')
    args = ap.parse_args()
    root = repo_root()
    if args.check:
        return check(root)
    if args.uninstall:
        return uninstall(root)
    return install(root)


if __name__ == '__main__':
    sys.exit(main())